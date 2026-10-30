"""Minimal TimeTagger HTTP API client (no CLI dependency)."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class TimeTaggerError(Exception):
    """Raised for transport, authentication or API-level failures."""


@dataclass(frozen=True)
class Updates:
    server_time: float
    reset: bool
    records: list[dict[str, Any]]
    settings: list[dict[str, Any]]


class TimeTaggerClient:
    def __init__(
        self,
        api_url: str,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: int = 30,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._username = username
        self._password = password
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "ttcal"
        # uvicorn closes idle keep-alive connections; without a retry a single
        # stale socket would abort a whole pass. PUT /records is idempotent by
        # record key, so retrying it is safe.
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=0,
            backoff_factor=0.5,
            allowed_methods=None,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        if token:
            self.session.headers["authtoken"] = token
        else:
            assert username and password
            self.session.headers["authtoken"] = self._bootstrap(username, password)

    # -- plumbing --------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.api_url}/{path.lstrip('/')}"

    def _request(
        self, method: str, path: str, _retry: bool = True, **kwargs: Any
    ) -> Any:
        try:
            response = self.session.request(
                method, self._url(path), timeout=self.timeout, **kwargs
            )
        except requests.RequestException as err:
            raise TimeTaggerError(
                f"cannot reach TimeTagger at {self.api_url}: {err}"
            ) from None
        if response.status_code == 401 and _retry and self._username and self._password:
            # A long-running service may outlive its token. Log in again and
            # retry once, so credentials keep working without a restart.
            self.session.headers["authtoken"] = self._bootstrap(
                self._username, self._password
            )
            return self._request(method, path, _retry=False, **kwargs)
        if response.status_code == 401:
            raise TimeTaggerError(
                "TimeTagger rejected the token (401); "
                "check [timetagger] token/credentials"
            )
        if response.status_code >= 400:
            body = response.text.strip()[:300]
            raise TimeTaggerError(
                f"TimeTagger {method} {path} failed: {response.status_code} {body}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise TimeTaggerError(
                f"TimeTagger {method} {path} returned non-JSON: {response.text[:200]!r}"
            ) from None

    def _bootstrap(self, username: str, password: str) -> str:
        payload = json.dumps(
            {"method": "usernamepassword", "username": username, "password": password}
        ).encode("utf-8")
        try:
            response = self.session.post(
                self._url("bootstrap_authentication"),
                data=base64.b64encode(payload),
                timeout=self.timeout,
            )
        except requests.RequestException as err:
            raise TimeTaggerError(
                f"cannot reach TimeTagger at {self.api_url}: {err}"
            ) from None
        if response.status_code != 200:
            raise TimeTaggerError(
                f"TimeTagger login failed for '{username}': "
                f"{response.status_code} {response.text.strip()[:200]}"
            )
        try:
            token = response.json()["token"]
        except (ValueError, KeyError):
            raise TimeTaggerError(
                "TimeTagger login returned an unexpected response"
            ) from None
        return str(token)

    # -- API -------------------------------------------------------------
    def version(self) -> str:
        data = self._request("GET", "version")
        return str((data or {}).get("version", "unknown"))

    def updates(self, since: float) -> Updates:
        data = self._request("GET", "updates", params={"since": f"{float(since):.6f}"})
        return Updates(
            server_time=float(data["server_time"]),
            reset=bool(data.get("reset")),
            records=list(data.get("records") or []),
            settings=list(data.get("settings") or []),
        )

    def records(
        self,
        t1: int,
        t2: int,
        hidden: bool | None = None,
        running: bool | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"timerange": f"{int(t1)}-{int(t2)}"}
        if hidden is not None:
            params["hidden"] = "true" if hidden else "false"
        if running is not None:
            params["running"] = "true" if running else "false"
        data = self._request("GET", "records", params=params)
        return list((data or {}).get("records") or [])

    def put_records(self, records: Iterable[dict[str, Any]]) -> None:
        items = list(records)
        if not items:
            return
        data = self._request("PUT", "records", json=items) or {}
        failed = data.get("failed") or []
        if failed:
            errors = "; ".join(str(e) for e in data.get("errors") or [])
            raise TimeTaggerError(
                f"TimeTagger rejected {len(failed)} record(s): {errors}"
            )
