"""SQLite sidecar state.

``pairs`` is the only place where the Record key and the Event UID are tied
together.  Nothing is smuggled through the UID or the description, so the
state database is required for an incremental sync (``ttcal repair`` can
rebuild it by matching content).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS pairs (
  record_key  TEXT PRIMARY KEY,
  uid         TEXT UNIQUE NOT NULL,
  href        TEXT,
  record_hash TEXT NOT NULL,
  event_hash  TEXT NOT NULL,
  etag        TEXT,
  state       TEXT NOT NULL,
  meta        TEXT,
  updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS pairs_href ON pairs (href);
CREATE INDEX IF NOT EXISTS pairs_uid ON pairs (uid);

CREATE TABLE IF NOT EXISTS cursors (
  name  TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conflicts (
  id         INTEGER PRIMARY KEY,
  record_key TEXT,
  uid        TEXT,
  winner     TEXT NOT NULL,
  applied    TEXT,
  discarded  TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS skips (
  href    TEXT PRIMARY KEY,
  uid     TEXT,
  reason  TEXT NOT NULL,
  detail  TEXT,
  seen_at INTEGER NOT NULL
);
"""

STATE_ACTIVE = "active"
STATE_TOMBSTONE_RECORD = "tombstone_record"
STATE_TOMBSTONE_EVENT = "tombstone_event"


@dataclass
class Pair:
    record_key: str
    uid: str
    href: str | None
    record_hash: str
    event_hash: str
    etag: str | None
    state: str
    meta: dict[str, Any]
    updated_at: int


def _pair_from_row(row: sqlite3.Row) -> Pair:
    meta: dict[str, Any] = {}
    if row["meta"]:
        try:
            meta = json.loads(row["meta"])
        except json.JSONDecodeError:
            meta = {}
    return Pair(
        record_key=row["record_key"],
        uid=row["uid"],
        href=row["href"],
        record_hash=row["record_hash"],
        event_hash=row["event_hash"],
        etag=row["etag"],
        state=row["state"],
        meta=meta,
        updated_at=row["updated_at"],
    )


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- cursors ---------------------------------------------------------
    def get_cursor(self, name: str) -> str | None:
        row = self.db.execute(
            "SELECT value FROM cursors WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def set_cursor(self, name: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO cursors (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (name, value),
        )
        self.db.commit()

    # -- pairs -----------------------------------------------------------
    def pair_for_record(self, record_key: str) -> Pair | None:
        row = self.db.execute(
            "SELECT * FROM pairs WHERE record_key = ?", (record_key,)
        ).fetchone()
        return None if row is None else _pair_from_row(row)

    def pair_for_uid(self, uid: str) -> Pair | None:
        row = self.db.execute("SELECT * FROM pairs WHERE uid = ?", (uid,)).fetchone()
        return None if row is None else _pair_from_row(row)

    def pair_for_href(self, href: str) -> Pair | None:
        row = self.db.execute("SELECT * FROM pairs WHERE href = ?", (href,)).fetchone()
        return None if row is None else _pair_from_row(row)

    def all_pairs(self) -> list[Pair]:
        rows = self.db.execute("SELECT * FROM pairs").fetchall()
        return [_pair_from_row(row) for row in rows]

    def active_pairs(self) -> list[Pair]:
        rows = self.db.execute(
            "SELECT * FROM pairs WHERE state = ?", (STATE_ACTIVE,)
        ).fetchall()
        return [_pair_from_row(row) for row in rows]

    def save_pair(self, pair: Pair) -> None:
        self.db.execute(
            """
            INSERT INTO pairs (record_key, uid, href, record_hash, event_hash,
                               etag, state, meta, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_key) DO UPDATE SET
              uid = excluded.uid,
              href = excluded.href,
              record_hash = excluded.record_hash,
              event_hash = excluded.event_hash,
              etag = excluded.etag,
              state = excluded.state,
              meta = excluded.meta,
              updated_at = excluded.updated_at
            """,
            (
                pair.record_key,
                pair.uid,
                pair.href,
                pair.record_hash,
                pair.event_hash,
                pair.etag,
                pair.state,
                json.dumps(pair.meta, sort_keys=True) if pair.meta else None,
                pair.updated_at or int(time.time()),
            ),
        )
        self.db.commit()

    def delete_pair(self, record_key: str) -> None:
        self.db.execute("DELETE FROM pairs WHERE record_key = ?", (record_key,))
        self.db.commit()

    def forget_href(self, href: str) -> None:
        self.db.execute("UPDATE pairs SET href = NULL WHERE href = ?", (href,))
        self.db.commit()

    # -- conflicts -------------------------------------------------------
    def add_conflict(
        self,
        record_key: str | None,
        uid: str | None,
        winner: str,
        applied: dict[str, Any] | None,
        discarded: dict[str, Any] | None,
    ) -> None:
        self.db.execute(
            "INSERT INTO conflicts (record_key, uid, winner, applied, discarded, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record_key,
                uid,
                winner,
                json.dumps(applied, sort_keys=True) if applied else None,
                json.dumps(discarded, sort_keys=True) if discarded else None,
                int(time.time()),
            ),
        )
        self.db.commit()

    def recent_conflicts(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM conflicts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def conflict_count(self) -> int:
        row = self.db.execute("SELECT COUNT(*) AS n FROM conflicts").fetchone()
        return int(row["n"])

    # -- skips -----------------------------------------------------------
    def add_skip(self, href: str, uid: str | None, reason: str, detail: str) -> None:
        self.db.execute(
            "INSERT INTO skips (href, uid, reason, detail, seen_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(href) DO UPDATE SET uid = excluded.uid, "
            "reason = excluded.reason, detail = excluded.detail, "
            "seen_at = excluded.seen_at",
            (href, uid, reason, detail, int(time.time())),
        )
        self.db.commit()

    def remove_skip(self, href: str) -> None:
        self.db.execute("DELETE FROM skips WHERE href = ?", (href,))
        self.db.commit()

    def prune_skips(self, keep_hrefs: Iterable[str]) -> None:
        keep = list(keep_hrefs)
        if not keep:
            self.db.execute("DELETE FROM skips")
        else:
            placeholders = ",".join("?" for _ in keep)
            self.db.execute(
                f"DELETE FROM skips WHERE href NOT IN ({placeholders})", keep
            )
        self.db.commit()

    def list_skips(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM skips ORDER BY seen_at DESC").fetchall()

    # -- reporting -------------------------------------------------------
    def counts(self) -> dict[str, int]:
        result = {}
        for state in (STATE_ACTIVE, STATE_TOMBSTONE_RECORD, STATE_TOMBSTONE_EVENT):
            row = self.db.execute(
                "SELECT COUNT(*) AS n FROM pairs WHERE state = ?", (state,)
            ).fetchone()
            result[state] = int(row["n"])
        return result
