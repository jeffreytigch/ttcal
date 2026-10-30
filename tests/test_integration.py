"""End-to-end tests against the Docker dev stack (``make dev-up``)."""

from __future__ import annotations

import time

import pytest

from ttcal.calapi import CalendarError
from ttcal.model import DS_MAX, hide_ds
from ttcal.sync import SyncError

pytestmark = pytest.mark.docker


def now() -> int:
    return int(time.time())


def events(env) -> dict[str, object]:
    cal = env.mirror.cal
    cal.calendar = None  # always look at the current remote state
    changes = cal.changes(None)
    return {event.uid: event for event in changes.events}


def one_event(env) -> object:
    found = events(env)
    assert len(found) == 1, f"expected exactly one event, got {found}"
    return next(iter(found.values()))


def fsync(env, dry_run: bool = False):
    return env.mirror.sync(dry_run=dry_run)


# --------------------------------------------------------------------------
# Record -> Event
# --------------------------------------------------------------------------
def test_backfill_creates_one_event_per_record(env):
    start = now() - 7200
    key = env.mirror.add_record(start, start + 1800, "write report #work")

    report = fsync(env, dry_run=True)
    assert report.created_events == 1
    with pytest.raises(CalendarError):  # dry run must not create anything
        env.mirror.cal.open_calendar()

    report = fsync(env)
    assert report.created_events == 1
    assert report.errors == []

    event = one_event(env)
    assert event.uid.startswith("ttcal-")
    assert event.start == start
    assert event.end == start + 1800
    assert event.summary == "write report #work"
    assert env.mirror.store.pair_for_record(key).uid == event.uid


def test_second_sync_is_a_noop(env):
    start = now() - 3600
    env.mirror.add_record(start, start + 600, "one #a")
    env.mirror.add_record(start + 900, start + 1500, "two #b")
    fsync(env)

    report = fsync(env)
    assert (
        report.created_events
        + report.updated_events
        + report.deleted_events
        + report.created_records
        + report.updated_records
        + report.hidden_records
        + report.restored_records
        + report.conflicts
    ) == 0
    assert report.errors == []
    assert len(events(env)) == 2


def test_hidden_and_running_records_are_not_mirrored(env):
    start = now() - 600
    env.mirror.add_record(start, start + 600, hide_ds("already hidden #a"))
    env.mirror.add_record(start, start, "running now #a")
    report = fsync(env)
    assert report.created_events == 0
    assert events(env) == {}


def test_record_edit_updates_event(env):
    start = now() - 3600
    key = env.mirror.add_record(start, start + 600, "original #a")
    fsync(env)
    uid = env.mirror.store.pair_for_record(key).uid

    env.mirror.put(key, t1=start + 120, t2=start + 900, ds="renamed #b")
    report = fsync(env)

    assert report.updated_events == 1
    event = events(env)[uid]
    assert event.summary == "renamed #b"
    assert event.start == start + 120
    assert event.end == start + 900


def test_record_hide_deletes_event_and_unhide_restores_same_uid(env):
    start = now() - 3600
    key = env.mirror.add_record(start, start + 600, "doomed #a")
    fsync(env)
    uid = env.mirror.store.pair_for_record(key).uid

    env.mirror.hide(key)
    report = fsync(env)
    assert report.deleted_events == 1
    assert events(env) == {}
    assert env.mirror.store.pair_for_record(key).state == "tombstone_record"

    env.mirror.unhide(key)
    report = fsync(env)
    assert report.restored_records == 1
    event = events(env)[uid]
    assert event.summary == "doomed #a"
    assert env.mirror.store.pair_for_record(key).state == "active"


# --------------------------------------------------------------------------
# Event -> Record
# --------------------------------------------------------------------------
def test_event_creates_record(env):
    start = now() - 5400
    event = env.mirror.cal.create_event(
        "ttcal-created-by-calendar", start, start + 1800, "from the calendar #a"
    )
    report = fsync(env)

    assert report.created_records == 1
    records = env.mirror.visible_records()
    assert len(records) == 1
    record = records[0]
    assert record["ds"] == "from the calendar #a"
    assert record["t1"] == start
    assert record["t2"] == start + 1800
    assert env.mirror.store.pair_for_uid(event.uid).record_key == record["key"]


def test_event_edit_updates_record(env):
    start = now() - 5400
    uid = "ttcal-edited-by-calendar"
    env.mirror.cal.create_event(uid, start, start + 900, "first #a")
    fsync(env)

    env.mirror.cal.update_event(uid, start + 600, start + 1800, "second #b")
    report = fsync(env)

    assert report.updated_records == 1
    record = env.mirror.visible_records()[0]
    assert record["ds"] == "second #b"
    assert record["t1"] == start + 600
    assert record["t2"] == start + 1800


def test_event_delete_hides_record_and_unhide_restores_event(env):
    start = now() - 5400
    uid = "ttcal-deleted-in-calendar"
    env.mirror.cal.create_event(uid, start, start + 600, "gone soon #a")
    fsync(env)
    record = env.mirror.visible_records()[0]
    assert env.mirror.cal.delete_event(uid)

    report = fsync(env)
    assert report.hidden_records == 1
    record = env.mirror.record(record["key"])
    assert hide_ds("x").startswith("HIDDEN ")
    assert record["ds"].startswith("HIDDEN ")
    assert env.mirror.visible_records() == []

    env.mirror.unhide(record["key"])
    report = fsync(env)
    assert report.restored_records == 1
    event = events(env)[uid]
    assert event.summary == "gone soon #a"


def test_bad_events_are_skipped_and_reported(env):
    base = now() - 7200
    cal = env.mirror.cal
    calendar = cal.open_calendar(create=True)
    all_day = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nBEGIN:VEVENT\r\n"
        "UID:ttcal-all-day\r\nDTSTAMP:20231114T220000Z\r\n"
        "DTSTART;VALUE=DATE:20231114\r\nDTEND;VALUE=DATE:20231115\r\n"
        "SUMMARY:all day\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    recurring = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nBEGIN:VEVENT\r\n"
        "UID:ttcal-recurring\r\nDTSTAMP:20231114T220000Z\r\n"
        "DTSTART:20231114T220000Z\r\nDTEND:20231114T230000Z\r\n"
        "RRULE:FREQ=WEEKLY\r\nSUMMARY:weekly\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    calendar.save_event(all_day)
    calendar.save_event(recurring)
    cal.create_event("ttcal-hidden-title", base, base + 600, "HIDDEN oops")

    report = fsync(env)
    assert report.created_records == 0
    assert report.skipped == 3
    reasons = {row["reason"] for row in env.mirror.store.list_skips()}
    assert reasons == {"all-day", "recurring", "hidden-prefix"}


def test_long_summary_is_truncated(env):
    start = now() - 5400
    env.mirror.cal.create_event(
        "ttcal-long-summary", start, start + 600, "z" * (DS_MAX + 100)
    )
    fsync(env)
    record = env.mirror.visible_records()[0]
    assert len(record["ds"]) == DS_MAX
    assert record["ds"].endswith("…")


# --------------------------------------------------------------------------
# Running timers
# --------------------------------------------------------------------------
def test_running_record_appears_after_stop(env):
    start = now() - 900
    key = env.mirror.add_record(start, start, "timer #a")
    fsync(env)
    assert events(env) == {}

    env.mirror.put(key, t2=start + 600)
    report = fsync(env)
    assert report.created_events == 1
    event = one_event(env)
    assert event.start == start
    assert event.end == start + 600


# --------------------------------------------------------------------------
# Conflicts
# --------------------------------------------------------------------------
def test_record_wins_conflict(env):
    start = now() - 3600
    key = env.mirror.add_record(start, start + 600, "base #a", mt=now() - 7200)
    fsync(env)
    uid = env.mirror.store.pair_for_record(key).uid

    env.mirror.cal.update_event(uid, start, start + 600, "event version #a")
    env.mirror.put(key, ds="record version #b", t1=start + 60, mt=now() + 120)
    report = fsync(env)

    assert report.conflicts == 1
    event = events(env)[uid]
    assert event.summary == "record version #b"
    assert event.start == start + 60
    conflict = env.mirror.store.recent_conflicts()[0]
    assert conflict["winner"] == "record"
    assert env.mirror.visible_records()[0]["ds"] == "record version #b"


def test_event_wins_conflict(env):
    start = now() - 3600
    key = env.mirror.add_record(start, start + 600, "base #a", mt=now() - 7200)
    fsync(env)
    uid = env.mirror.store.pair_for_record(key).uid

    env.mirror.cal.update_event(uid, start + 120, start + 900, "event version #a")
    env.mirror.put(key, ds="record version #b", mt=now() - 3600)
    report = fsync(env)

    assert report.conflicts == 1
    assert env.mirror.record(key)["ds"] == "event version #a"
    conflict = env.mirror.store.recent_conflicts()[0]
    assert conflict["winner"] == "calendar"


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------
def test_mass_event_deletion_is_refused(env):
    start = now() - 7200
    keys = [
        env.mirror.add_record(start + i * 600, start + i * 600 + 300, f"item {i} #a")
        for i in range(4)
    ]
    fsync(env)
    assert len(events(env)) == 4

    for uid in list(events(env)):
        env.mirror.cal.delete_event(uid)

    with pytest.raises(SyncError, match="refusing to hide records"):
        fsync(env)

    # Nothing was hidden behind our back.
    visible = {record["key"] for record in env.mirror.visible_records()}
    assert set(keys) <= visible


def test_missing_calendar_with_existing_pairs_is_refused(env):
    start = now() - 3600
    env.mirror.add_record(start, start + 600, "keep me #a")
    fsync(env)

    cal = env.mirror.cal
    cal.open_calendar().delete()
    cal.calendar = None

    with pytest.raises(SyncError, match="Refusing to continue"):
        fsync(env)


# --------------------------------------------------------------------------
# Backfill
# --------------------------------------------------------------------------
def sync_with_days(env, days: int):
    from dataclasses import replace

    from ttcal.sync import Syncer

    config = replace(env.mirror.config, backfill_days=days)
    return Syncer(config, env.mirror.store, env.mirror.tt, env.mirror.cal).sync_once()


def test_widening_the_backfill_horizon_imports_older_records(env):
    start = now() - 400 * 86400
    key = env.mirror.add_record(start, start + 3600, "old work #archive")

    # The first pass only looks one day back, so this record stays out.
    report = sync_with_days(env, 1)
    assert report.errors == []
    assert env.mirror.store.pair_for_record(key) is None
    assert events(env) == {}

    # Raising backfill_days loads the wider window without duplicating what is
    # already paired.
    report = sync_with_days(env, 400)
    assert report.errors == []
    assert report.created_events == 1
    pair = env.mirror.store.pair_for_record(key)
    assert pair is not None
    assert [event.uid for event in events(env).values()] == [pair.uid]
