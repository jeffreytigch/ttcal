from __future__ import annotations

import time

import pytest

from ttcal.store import (
    STATE_ACTIVE,
    STATE_TOMBSTONE_EVENT,
    STATE_TOMBSTONE_RECORD,
    Pair,
    Store,
)


def make_pair(
    record_key="rec1",
    uid="uid1",
    href="http://cal/1.ics",
    record_hash="rh",
    event_hash="eh",
    etag="etag1",
    state=STATE_ACTIVE,
    meta=None,
) -> Pair:
    return Pair(
        record_key=record_key,
        uid=uid,
        href=href,
        record_hash=record_hash,
        event_hash=event_hash,
        etag=etag,
        state=state,
        meta=dict(meta or {}),
        updated_at=int(time.time()),
    )


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "state.db")
    yield store
    store.close()


def test_save_and_find_by_record_uid_and_href(store):
    store.save_pair(make_pair())
    by_record = store.pair_for_record("rec1")
    assert by_record is not None
    assert by_record.uid == "uid1"
    assert store.pair_for_uid("uid1").record_key == "rec1"
    assert store.pair_for_href("http://cal/1.ics").record_key == "rec1"


def test_save_pair_upserts_by_record_key(store):
    store.save_pair(make_pair())
    store.save_pair(make_pair(record_hash="rh2", event_hash="eh2", etag="e2"))
    assert len(store.all_pairs()) == 1
    pair = store.pair_for_record("rec1")
    assert pair.record_hash == "rh2"
    assert pair.event_hash == "eh2"
    assert pair.etag == "e2"


def test_uid_is_unique(store):
    import sqlite3

    store.save_pair(make_pair())
    with pytest.raises(sqlite3.IntegrityError):
        store.save_pair(make_pair(record_key="rec2", href=None))


def test_states_and_counts(store):
    store.save_pair(make_pair(record_key="a", uid="u1", href="h1"))
    store.save_pair(
        make_pair(record_key="b", uid="u2", href=None, state=STATE_TOMBSTONE_RECORD)
    )
    store.save_pair(
        make_pair(record_key="c", uid="u3", href=None, state=STATE_TOMBSTONE_EVENT)
    )
    assert store.counts() == {
        STATE_ACTIVE: 1,
        STATE_TOMBSTONE_RECORD: 1,
        STATE_TOMBSTONE_EVENT: 1,
    }
    assert [p.record_key for p in store.active_pairs()] == ["a"]


def test_meta_round_trip(store):
    meta = {"t1": 1, "t2": 2, "ds": "x #y"}
    store.save_pair(make_pair(meta=meta))
    assert store.pair_for_record("rec1").meta == meta


def test_delete_pair_and_forget_href(store):
    store.save_pair(make_pair())
    store.forget_href("http://cal/1.ics")
    assert store.pair_for_record("rec1").href is None
    store.delete_pair("rec1")
    assert store.all_pairs() == []


def test_cursors(store):
    assert store.get_cursor("x") is None
    store.set_cursor("x", "1.5")
    assert store.get_cursor("x") == "1.5"
    store.set_cursor("x", "2.5")
    assert store.get_cursor("x") == "2.5"


def test_conflicts_are_ordered_newest_first(store):
    store.add_conflict("rec", "uid", "record", {"ds": "a"}, {"ds": "b"})
    time.sleep(0.01)
    store.add_conflict("rec", "uid", "event", {"ds": "c"}, {"ds": "d"})
    recent = store.recent_conflicts()
    assert [c["winner"] for c in recent] == ["event", "record"]
    assert store.conflict_count() == 2


def test_skips_dedupe_and_prune(store):
    store.add_skip("h1", "u1", "all-day", "no time")
    store.add_skip("h2", "u2", "recurring", "rrule")
    assert {s["href"] for s in store.list_skips()} == {"h1", "h2"}

    store.remove_skip("h1")
    assert {s["href"] for s in store.list_skips()} == {"h2"}

    store.prune_skips(["h2"])
    assert {s["href"] for s in store.list_skips()} == {"h2"}

    store.prune_skips([])
    assert store.list_skips() == []
