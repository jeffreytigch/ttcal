from __future__ import annotations

import json
import unittest

from ttcal.ttapi import TimeTaggerClient, TimeTaggerError


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.content = b"{}" if payload is not None else b""
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.headers: dict[str, str] = {}

    def request(self, method, url, **kwargs):
        self.calls.append(url)
        return self.responses.pop(0)


class TokenRefreshTests(unittest.TestCase):
    def make_client(self, responses: list[FakeResponse], with_credentials: bool):
        client = TimeTaggerClient("http://tt/api/v2", token="stale")
        if with_credentials:
            client._username = "user"  # avoid a real login in the constructor
            client._password = "pass"
        client.session = FakeSession(responses)  # type: ignore[assignment]
        client._bootstrap = lambda username, password: "fresh"  # type: ignore[method-assign]
        return client

    def test_expired_token_is_refreshed_and_the_request_retried(self):
        client = self.make_client(
            [FakeResponse(401), FakeResponse(200, {"version": "26.1.3"})],
            with_credentials=True,
        )

        self.assertEqual(client.version(), "26.1.3")
        self.assertEqual(len(client.session.calls), 2)  # type: ignore[attr-defined]
        self.assertEqual(
            client.session.headers["authtoken"],  # type: ignore[attr-defined]
            "fresh",
        )

    def test_static_token_is_not_retried(self):
        client = self.make_client([FakeResponse(401)], with_credentials=False)

        with self.assertRaises(TimeTaggerError):
            client.version()
        self.assertEqual(len(client.session.calls), 1)  # type: ignore[attr-defined]

    def test_refresh_happens_only_once(self):
        client = self.make_client(
            [FakeResponse(401), FakeResponse(401)], with_credentials=True
        )

        with self.assertRaises(TimeTaggerError):
            client.version()
        self.assertEqual(len(client.session.calls), 2)  # type: ignore[attr-defined]
