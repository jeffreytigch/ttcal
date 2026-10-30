"""Shared fixtures.

Unit tests need nothing. Integration tests (marked ``docker``) expect the
throw-away stack from ``make dev-up``:

    TimeTagger  http://localhost:1081/timetagger/api/v2/  test:test
    Radicale    http://localhost:5232/                    test:test

They refuse to run against anything that is not on localhost unless
``TTCAL_TEST_ALLOW_REMOTE=1`` is set, so a typo cannot touch a real account.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests

from ttcal.calapi import CalendarClient
from ttcal.config import load_config
from ttcal.model import hide_ds, is_hidden
from ttcal.store import Store
from ttcal.sync import Syncer
from ttcal.ttapi import TimeTaggerClient

TT_URL = os.environ.get(
    "TTCAL_TEST_TT_URL", "http://localhost:1081/timetagger/api/v2"
).rstrip("/")
CAL_URL = os.environ.get("TTCAL_TEST_CALDAV_URL", "http://localhost:5232/")
USERNAME = os.environ.get("TTCAL_TEST_USERNAME", "test")
PASSWORD = os.environ.get("TTCAL_TEST_PASSWORD", "test")


def _is_local(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host in {"localhost", "127.0.0.1", "::1"}


def _stack_is_up() -> bool:
    for url in (TT_URL + "/", CAL_URL):
        try:
            requests.get(url, timeout=2, auth=(USERNAME, PASSWORD))
        except requests.RequestException:
            return False
    return True


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip docker tests with a clear reason when the stack is not running."""
    marked = [item for item in items if item.get_closest_marker("docker")]
    if not marked:
        return
    if not (_is_local(TT_URL) and _is_local(CAL_URL)) and not os.environ.get(
        "TTCAL_TEST_ALLOW_REMOTE"
    ):
        pytest.exit(
            "refusing to run docker tests against a non-local host; "
            "set TTCAL_TEST_ALLOW_REMOTE=1 if this is a throw-away instance",
            returncode=1,
        )
    if not _stack_is_up():
        if os.environ.get("TTCAL_TEST_REQUIRE_STACK"):
            pytest.exit("dev stack is not reachable", returncode=1)
        skip = pytest.mark.skip(reason="dev stack not running (run: make dev-up)")
        for item in marked:
            item.add_marker(skip)


class Mirror:
    """A configured bridge bound to a fresh calendar and state database."""

    def __init__(
        self, config, store: Store, tt: TimeTaggerClient, cal: CalendarClient
    ) -> None:
        self.config = config
        self.store = store
        self.tt = tt
        self.cal = cal

    def sync(self, dry_run: bool = False):
        config = self.config.with_dry_run(dry_run)
        syncer = Syncer(config, self.store, self.tt, self.cal)
        return syncer.sync_once()

    # -- TimeTagger helpers ---------------------------------------------
    def records(self, days: int = 3650) -> list[dict]:
        now = int(time.time())
        return list(self.tt.records(now - days * 86400, now + 86400))

    def visible_records(self) -> list[dict]:
        return [r for r in self.records() if not is_hidden(str(r["ds"]))]

    def record(self, key: str) -> dict | None:
        for record in self.records():
            if record["key"] == key:
                return record
        return None

    def put(self, key: str, **changes) -> dict:
        record = self.record(key)
        assert record is not None, f"record {key} not found"
        payload = {
            "key": record["key"],
            "t1": int(record["t1"]),
            "t2": int(record["t2"]),
            "ds": record["ds"],
            "mt": int(time.time()),
        }
        payload.update(changes)
        self.tt.put_records([payload])
        return payload

    def add_record(
        self, t1: int, t2: int, ds: str, key: str | None = None, mt: int | None = None
    ) -> str:
        key = key or uuid.uuid4().hex
        self.tt.put_records(
            [
                {
                    "key": key,
                    "t1": t1,
                    "t2": t2,
                    "ds": ds,
                    "mt": int(time.time()) if mt is None else int(mt),
                }
            ]
        )
        return key

    def hide(self, key: str) -> None:
        record = self.record(key)
        assert record is not None
        self.put(key, ds=hide_ds(str(record["ds"])))

    def unhide(self, key: str) -> None:
        record = self.record(key)
        assert record is not None
        self.put(key, ds=str(record["ds"])[len("HIDDEN ") :])


@dataclass
class Env:
    mirror: Mirror
    calendar_name: str


@pytest.fixture
def env(tmp_path: Path) -> Env:
    """Fresh calendar, fresh database, no visible pre-existing records."""
    calendar_name = f"ttcal-test-{uuid.uuid4().hex[:8]}"

    config_file = tmp_path / "ttcal.toml"
    config_file.write_text(
        "\n".join(
            [
                "[timetagger]",
                f'api_url = "{TT_URL}"',
                f'username = "{USERNAME}"',
                f'password = "{PASSWORD}"',
                "",
                "[caldav]",
                f'url = "{CAL_URL}"',
                f'username = "{USERNAME}"',
                f'password = "{PASSWORD}"',
                f'calendar = "{calendar_name}"',
                "",
                "[ttcal]",
                'state_db = "state.db"',
                "backfill_days = 3650",
                "poll_interval = 60",
                'log_level = "DEBUG"',
            ]
        ),
        encoding="utf-8",
    )
    config_file.chmod(0o600)
    config = load_config(config_file)

    tt = TimeTaggerClient(
        config.timetagger_api_url,
        token=config.timetagger_token,
        username=config.timetagger_username,
        password=config.timetagger_password,
    )
    cal = CalendarClient(
        config.caldav_url,
        config.caldav_username,
        config.caldav_password,
        config.calendar_name,
    )
    store = Store(config.state_db)
    mirror = Mirror(config, store, tt, cal)

    # Start every test from a clean slate: hide anything still visible from
    # earlier runs. Hidden records are ignored by the bridge.
    now = int(time.time())
    stale = [
        record
        for record in tt.records(now - 3650 * 86400, now + 86400)
        if not is_hidden(str(record["ds"]))
    ]
    if stale:
        # A stored mt in the future would make TimeTagger ignore the update,
        # so always move mt forward by at least one second.
        tt.put_records(
            [
                {
                    "key": r["key"],
                    "t1": int(r["t1"]),
                    "t2": int(r["t2"]),
                    "ds": hide_ds(str(r["ds"])),
                    "mt": int(max(now, r["mt"]) + 1),
                }
                for r in stale
            ]
        )

    yield Env(mirror=mirror, calendar_name=calendar_name)

    store.close()
    try:
        calendar = cal.open_calendar()
        for event in calendar.events():
            event.delete()
        calendar.delete()
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass
