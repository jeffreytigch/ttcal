"""The reconcile loop.

One pass reads the TimeTagger change feed and the CalDAV change feed, compares
both against the baselines in SQLite, and applies the winner per pair.  Every
write is followed by a read-back so the stored baseline is what the servers
actually hold; that is what suppresses echoes on the next poll.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from . import model
from .calapi import CalChanges, CalendarClient, CalendarError, CalEvent
from .config import Config
from .model import (
    RecordFields,
    UnsupportedEvent,
    record_hash_of,
)
from .store import (
    STATE_ACTIVE,
    STATE_TOMBSTONE_EVENT,
    STATE_TOMBSTONE_RECORD,
    Pair,
    Store,
)
from .ttapi import TimeTaggerClient, TimeTaggerError

log = logging.getLogger(__name__)

CURSOR_TT = "timetagger_since"
CURSOR_CAL = "caldav_sync_token"
CURSOR_BACKFILL = "backfill_days"


class SyncError(Exception):
    """A condition that makes continuing unsafe (e.g. the calendar vanished)."""


@dataclass
class SyncReport:
    created_events: int = 0
    updated_events: int = 0
    deleted_events: int = 0
    created_records: int = 0
    updated_records: int = 0
    hidden_records: int = 0
    restored_records: int = 0
    conflicts: int = 0
    skipped: int = 0
    unchanged: int = 0
    dry_run: bool = False
    actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped_hrefs: set[str] = field(default_factory=set)

    def note(self, action: str) -> None:
        self.actions.append(action)
        log.info(action)

    def summary(self) -> str:
        parts = [
            f"{self.created_events} new event(s)",
            f"{self.updated_events} event update(s)",
            f"{self.deleted_events} event delete(s)",
            f"{self.created_records} new record(s)",
            f"{self.updated_records} record update(s)",
            f"{self.hidden_records} hidden",
            f"{self.restored_records} restored",
            f"{self.conflicts} conflict(s)",
            f"{self.skipped} skipped",
        ]
        if self.unchanged:
            parts.append(f"{self.unchanged} unchanged")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        prefix = "dry run: " if self.dry_run else ""
        return prefix + ", ".join(parts)


class Syncer:
    def __init__(
        self,
        config: Config,
        store: Store,
        timetagger: TimeTaggerClient,
        calendar: CalendarClient,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.store = store
        self.tt = timetagger
        self.cal = calendar
        self.clock = clock

    # -- helpers ---------------------------------------------------------
    def _put_record(self, fields: RecordFields) -> RecordFields:
        self.tt.put_records([model.record_payload(fields)])
        actual = self._read_back_record(fields)
        return actual

    def _read_back_record(self, sent: RecordFields) -> RecordFields:
        try:
            records = self.tt.records(sent.t1, sent.t2)
        except TimeTaggerError:
            return sent
        for payload in records:
            if payload.get("key") == sent.key:
                return model.record_from_payload(payload)
        return sent

    def _save_active(
        self,
        record: RecordFields,
        event: CalEvent,
    ) -> None:
        self.store.save_pair(
            Pair(
                record_key=record.key,
                uid=event.uid,
                href=event.href,
                record_hash=record_hash_of(record),
                event_hash=event.hash,
                etag=event.etag,
                state=STATE_ACTIVE,
                meta={"t1": record.t1, "t2": record.t2, "ds": record.ds},
                updated_at=int(self.clock()),
            )
        )

    def _guard(self, report: SyncReport, fn: Callable[..., None], *args: Any) -> None:
        try:
            fn(*args)
        except (TimeTaggerError, CalendarError) as err:
            report.errors.append(str(err))
            log.error("%s", err)

    def _meta_record(self, pair: Pair) -> tuple[int, int, str]:
        meta = pair.meta or {}
        return (
            int(meta.get("t1", 0)),
            int(meta.get("t2", 0)),
            str(meta.get("ds", "")),
        )

    # -- entry point -----------------------------------------------------
    def sync_once(self) -> SyncReport:
        report = SyncReport(dry_run=self.config.dry_run)
        now = self.clock()

        pairs = self.store.all_pairs()
        pairs_by_key = {p.record_key: p for p in pairs}
        pairs_by_uid = {p.uid: p for p in pairs}
        pairs_by_href = {p.href: p for p in pairs if p.href}

        # Calendar must exist unless this is the very first run.
        try:
            self.cal.open_calendar(create=not pairs and not self.config.dry_run)
        except CalendarError as err:
            if "not found" in str(err) and self.config.dry_run:
                report.note("calendar does not exist yet; a real run would create it")
                changes = CalChanges([], set(), None, True)
            else:
                raise SyncError(
                    f"{err}. Refusing to continue: re-creating the calendar would "
                    f"hide or duplicate every record."
                ) from None
        else:
            changes = self.cal.changes(self.store.get_cursor(CURSOR_CAL))

        tt_records, server_time = self._read_timetagger(now)
        deleted = self._deleted_hrefs(changes, pairs)
        self._guard_mass_deletion(deleted, pairs)

        tt_by_key = {r["key"]: model.record_from_payload(r) for r in tt_records}
        cal_by_uid = {e.uid: e for e in changes.events}

        # -- new records --------------------------------------------------
        for key, record in tt_by_key.items():
            if key in pairs_by_key:
                continue
            self._guard(report, self._import_record, record, report)

        # -- new events ---------------------------------------------------
        for uid, event in cal_by_uid.items():
            if uid in pairs_by_uid:
                continue
            self._guard(report, self._import_event, event, report)

        # -- updates on known pairs --------------------------------------
        for key, record in tt_by_key.items():
            pair = pairs_by_key.get(key)
            if pair is None:
                continue
            self._guard(
                report,
                self._reconcile_pair,
                pair,
                record,
                cal_by_uid.get(pair.uid),
                bool(pair.href and pair.href in deleted),
                report,
            )

        for uid, event in cal_by_uid.items():
            pair = pairs_by_uid.get(uid)
            if pair is None or pair.record_key in tt_by_key:
                continue
            self._guard(report, self._reconcile_pair, pair, None, event, False, report)

        for href in deleted:
            pair = pairs_by_href.get(href)
            if pair is None or pair.record_key in tt_by_key:
                continue
            self._guard(report, self._reconcile_pair, pair, None, None, True, report)

        # -- skips --------------------------------------------------------
        previous_skips = {row["href"]: row for row in self.store.list_skips()}
        current_skip_hrefs = set()
        for item in changes.skips:
            current_skip_hrefs.add(item.href)
            prev = previous_skips.get(item.href)
            if prev is None or prev["reason"] != item.reason:
                report.skipped += 1
                report.note(f"skip {item.href}: {item.reason} ({item.detail})")
            if not self.config.dry_run:
                self.store.add_skip(item.href, item.uid, item.reason, item.detail)
        if changes.full and not self.config.dry_run:
            self.store.prune_skips(current_skip_hrefs | report.skipped_hrefs)

        # -- cursors ------------------------------------------------------
        if not report.errors:
            if not self.config.dry_run:
                self.store.set_cursor(CURSOR_TT, f"{server_time:.6f}")
                self.store.set_cursor(CURSOR_BACKFILL, str(self.config.backfill_days))
                if changes.sync_token:
                    self.store.set_cursor(CURSOR_CAL, changes.sync_token)
        else:
            log.warning(
                "not advancing cursors because %d item(s) failed", len(report.errors)
            )

        return report

    # -- feeds -----------------------------------------------------------
    def _read_timetagger(self, now: float) -> tuple[list[dict[str, Any]], float]:
        since = self.store.get_cursor(CURSOR_TT)
        if since is None:
            # Read the cursor *before* the snapshot: a record written between
            # the two calls then has st >= server_time and is picked up by the
            # next incremental read instead of being missed forever.
            updates = self.tt.updates(now)
            records = self._initial_records(now)
            return records, updates.server_time
        try:
            updates = self.tt.updates(float(since))
        except TimeTaggerError as err:
            raise SyncError(str(err)) from None
        if updates.reset:
            log.warning("TimeTagger reported a reset; reloading the backfill horizon")
            return self._initial_records(now), updates.server_time
        applied = self.store.get_cursor(CURSOR_BACKFILL)
        if applied is None or int(applied) < self.config.backfill_days:
            # The configured horizon was widened after the first run. Load the
            # new window and merge the incremental changes on top of it; pairs
            # that already exist are skipped by key, so this never duplicates.
            log.info(
                "backfill horizon widened to %s day(s); loading older records",
                self.config.backfill_days,
            )
            merged = {r["key"]: r for r in self._initial_records(now)}
            for record in updates.records:
                merged[record["key"]] = record
            return list(merged.values()), updates.server_time
        return updates.records, updates.server_time

    def _initial_records(self, now: float) -> list[dict[str, Any]]:
        start = int(now - self.config.backfill_days * 86400)
        # Records may be dated in the future; one day would hide anything
        # further ahead from the only full read this bridge ever does.
        end = int(now + 3650 * 86400)
        try:
            return self.tt.records(start, end)
        except TimeTaggerError as err:
            raise SyncError(str(err)) from None

    def _deleted_hrefs(self, changes: CalChanges, pairs: list[Pair]) -> set[str]:
        deleted = set(changes.deleted_hrefs)
        if changes.full:
            present = {e.href for e in changes.events}
            for pair in pairs:
                if (
                    pair.state == STATE_ACTIVE
                    and pair.href
                    and pair.href not in present
                ):
                    deleted.add(pair.href)
        return deleted

    def _guard_mass_deletion(self, deleted: set[str], pairs: list[Pair]) -> None:
        active = [p for p in pairs if p.state == STATE_ACTIVE and p.href]
        if len(active) >= 4 and len(deleted) > max(3, len(active) // 2):
            if self.config.allow_mass_deletion:
                log.warning(
                    "%d of %d mirrored events vanished; hiding the records because "
                    "--allow-mass-deletion was given",
                    len(deleted),
                    len(active),
                )
                return
            raise SyncError(
                f"{len(deleted)} of {len(active)} mirrored events vanished at once; "
                f"refusing to hide records. Check the calendar, then re-run with "
                f"--allow-mass-deletion if this is intentional."
            )

    # -- actions ---------------------------------------------------------
    def _import_record(self, record: RecordFields, report: SyncReport) -> None:
        if record.hidden or record.running:
            report.unchanged += 1
            return
        uid = model.new_uid()
        start, end, summary = model.record_to_event(record, uid)
        if self.config.dry_run:
            report.created_events += 1
            report.note(f"would create event for record {record.key} ({summary!r})")
            return
        event = self.cal.create_event(uid, start, end, summary)
        self._save_active(record, event)
        report.created_events += 1
        report.note(f"created event {event.href} for record {record.key}")

    def _summary_warning(self, summary: str, href: str) -> None:
        normalised = " ".join(summary.split())
        if len(normalised) > model.DS_MAX:
            log.warning(
                "summary of %s is %d characters; truncating to %d",
                href,
                len(normalised),
                model.DS_MAX,
            )

    def _import_event(self, event: CalEvent, report: SyncReport) -> None:
        self._summary_warning(event.summary, event.href)
        try:
            ds = model.record_ds_from_summary(event.summary)
        except UnsupportedEvent as err:
            report.skipped += 1
            report.skipped_hrefs.add(event.href)
            report.note(f"skip {event.href}: {err.reason} ({err.detail})")
            if not self.config.dry_run:
                self.store.add_skip(event.href, event.uid, err.reason, err.detail)
            return

        start, end = event.start, event.end
        if end <= start:
            end = start + model.MIN_DURATION
        record = RecordFields(
            key=model.new_record_key(),
            t1=start,
            t2=end,
            ds=ds,
            mt=int(self.clock()),
        )
        if self.config.dry_run:
            report.created_records += 1
            report.note(f"would create record for event {event.href} ({ds!r})")
            return
        actual = self._put_record(record)
        self._save_active(actual, event)
        self.store.remove_skip(event.href)
        report.created_records += 1
        report.note(f"created record {actual.key} for event {event.href}")

    def _reconcile_pair(
        self,
        pair: Pair,
        record: RecordFields | None,
        event: CalEvent | None,
        event_deleted: bool,
        report: SyncReport,
    ) -> None:
        record_changed = (
            record is not None and record_hash_of(record) != pair.record_hash
        )
        event_changed = event is not None and event.hash != pair.event_hash
        state = pair.state

        if state == STATE_ACTIVE:
            if record_changed and (event_changed or event_deleted):
                self._resolve_conflict(pair, record, event, event_deleted, report)
            elif record_changed:
                assert record is not None
                self._push_record(pair, record, event, report)
            elif event_deleted:
                self._event_deleted(pair, report)
            elif event_changed:
                assert event is not None
                self._push_event(pair, event, report)
            else:
                report.unchanged += 1
            return

        if state == STATE_TOMBSTONE_RECORD:
            # The record is hidden; the event was removed. Restore if visible.
            if record is not None and not record.hidden:
                self._restore_from_record(pair, record, report)
            elif record_changed and record is not None and record.hidden:
                self.store.save_pair(
                    replace(
                        pair,
                        record_hash=record_hash_of(record),
                        updated_at=int(self.clock()),
                    )
                )
            else:
                report.unchanged += 1
            return

        if state == STATE_TOMBSTONE_EVENT:
            # The event is gone; the record was hidden. Restore from either side.
            if event_changed and event is not None:
                self._push_event(pair, event, report)
            elif record is not None and not record.hidden:
                self._restore_from_record(pair, record, report)
            else:
                report.unchanged += 1
            return

        log.warning("pair %s has unknown state %r; ignoring", pair.record_key, state)

    def _push_record(
        self,
        pair: Pair,
        record: RecordFields,
        event: CalEvent | None,
        report: SyncReport,
    ) -> None:
        if record.hidden:
            self._hide_from_record(pair, record, report)
            return
        if record.running:
            # The record was re-opened as a timer. It has no end yet, so keep
            # the existing event untouched; the next stop changes the hash and
            # pushes the final times. Never hide the record for this.
            report.unchanged += 1
            if not self.config.dry_run:
                self.store.save_pair(
                    replace(
                        pair,
                        record_hash=model.record_hash_of(record),
                        updated_at=int(self.clock()),
                    )
                )
            return

        start, end, summary = model.record_to_event(record, pair.uid)
        if self.config.dry_run:
            report.updated_events += 1
            report.note(
                f"would update event {pair.href} from record {record.key} ({summary!r})"
            )
            return
        try:
            updated = self.cal.update_event(pair.uid, start, end, summary)
        except CalendarError as err:
            report.errors.append(str(err))
            log.error("%s", err)
            return
        self._save_active(record, updated)
        report.updated_events += 1
        report.note(f"updated event {updated.href} from record {record.key}")

    def _push_event(self, pair: Pair, event: CalEvent, report: SyncReport) -> None:
        self._summary_warning(event.summary, event.href)
        try:
            ds = model.record_ds_from_summary(event.summary)
        except UnsupportedEvent as err:
            report.skipped_hrefs.add(event.href)
            report.errors.append(f"{event.href}: {err.reason}")
            log.warning("cannot mirror event %s: %s", event.href, err.detail)
            return

        start, end = event.start, event.end
        if end <= start:
            end = start + model.MIN_DURATION
        record = RecordFields(
            key=pair.record_key,
            t1=start,
            t2=end,
            ds=ds,
            mt=int(self.clock()),
        )
        if self.config.dry_run:
            report.updated_records += 1
            report.note(
                f"would update record {pair.record_key} from {event.href} ({ds!r})"
            )
            return
        try:
            actual = self._put_record(record)
        except TimeTaggerError as err:
            report.errors.append(str(err))
            log.error("%s", err)
            return
        if model.record_hash_of(actual) != model.record_hash_of(record):
            # TimeTagger kept a newer edit (its mt was greater). Leave both
            # baselines untouched so the next pass resolves the real conflict.
            message = (
                f"record {record.key} changed in TimeTagger while the event was "
                f"being applied; deferring to the next pass"
            )
            report.errors.append(message)
            log.warning("%s", message)
            return
        self._save_active(actual, event)
        self.store.remove_skip(event.href)
        report.updated_records += 1
        report.note(f"updated record {actual.key} from event {event.href}")

    def _hide_from_record(
        self, pair: Pair, record: RecordFields, report: SyncReport
    ) -> None:
        if self.config.dry_run:
            report.deleted_events += 1
            report.note(f"would delete event {pair.href} (record {record.key} hidden)")
            return
        try:
            self.cal.delete_event(pair.uid)
        except CalendarError as err:
            report.errors.append(str(err))
            log.error("%s", err)
            return
        _, _, original = self._meta_record(pair)
        ds = original or model.unhide_ds(record.ds)
        self.store.save_pair(
            Pair(
                record_key=pair.record_key,
                uid=pair.uid,
                href=None,
                record_hash=record_hash_of(record),
                event_hash="",
                etag=None,
                state=STATE_TOMBSTONE_RECORD,
                meta={
                    "t1": record.t1,
                    "t2": record.t2,
                    "ds": ds or model.unhide_ds(record.ds),
                },
                updated_at=int(self.clock()),
            )
        )
        report.deleted_events += 1
        report.note(f"deleted event for hidden record {record.key}")

    def _event_deleted(self, pair: Pair, report: SyncReport) -> None:
        t1, t2, ds = self._meta_record(pair)
        if not ds:
            message = (
                f"pair {pair.record_key} has no stored baseline; run "
                f"'ttcal repair --rebuild' before deleting its event"
            )
            report.errors.append(message)
            log.error("%s", message)
            return
        hidden = RecordFields(
            key=pair.record_key,
            t1=t1,
            t2=t2,
            ds=model.hide_ds(ds),
            mt=int(self.clock()),
        )
        if self.config.dry_run:
            report.hidden_records += 1
            report.note(f"would hide record {pair.record_key} (event deleted)")
            return
        try:
            actual = self._put_record(hidden)
        except TimeTaggerError as err:
            report.errors.append(str(err))
            log.error("%s", err)
            return
        self.store.save_pair(
            Pair(
                record_key=pair.record_key,
                uid=pair.uid,
                href=None,
                record_hash=record_hash_of(actual),
                event_hash="",
                etag=None,
                state=STATE_TOMBSTONE_EVENT,
                meta={"t1": t1, "t2": t2, "ds": ds},
                updated_at=int(self.clock()),
            )
        )
        report.hidden_records += 1
        report.note(f"hid record {pair.record_key} because the event was deleted")

    def _restore_from_record(
        self, pair: Pair, record: RecordFields, report: SyncReport
    ) -> None:
        start, end, summary = model.record_to_event(record, pair.uid)
        if self.config.dry_run:
            report.restored_records += 1
            report.note(f"would recreate event {pair.uid} from record {record.key}")
            return
        try:
            existing = self.cal.get_event(pair.uid)
            if existing is None:
                event = self.cal.create_event(pair.uid, start, end, summary)
            else:
                # A previous restore may have written the event but failed
                # before saving the pair. Adopt it instead of hitting the
                # server's duplicate-UID rejection forever.
                log.info("adopting existing event %s during restore", existing.href)
                event = self.cal.update_event(pair.uid, start, end, summary)
        except CalendarError as err:
            report.errors.append(str(err))
            log.error("%s", err)
            return
        self._save_active(record, event)
        report.restored_records += 1
        report.note(f"restored event {event.href} from record {record.key}")

    def _resolve_conflict(
        self,
        pair: Pair,
        record: RecordFields,
        event: CalEvent | None,
        event_deleted: bool,
        report: SyncReport,
    ) -> None:
        discarded = {
            "ds": record.ds,
            "t1": record.t1,
            "t2": record.t2,
            "mt": record.mt,
        }
        if event_deleted:
            # A deletion carries no timestamp; treat the deletion as the edit
            # that happened "now" and let it win, but keep the record version.
            self._log_conflict(
                pair,
                "calendar",
                {"deleted": True},
                discarded,
                report,
                f"event deleted, record mt={record.mt}",
            )
            self._event_deleted(pair, report)
            return

        assert event is not None
        calendar_wins = record.mt <= event.modified
        winner = "calendar" if calendar_wins else "record"
        self._log_conflict(
            pair,
            winner,
            {"summary": event.summary, "start": event.start, "end": event.end},
            discarded,
            report,
            f"record mt={record.mt}, event modified={event.modified}",
        )
        if calendar_wins:
            self._push_event(pair, event, report)
        else:
            self._push_record(pair, record, event, report)

    def _log_conflict(
        self,
        pair: Pair,
        winner: str,
        applied: dict[str, Any],
        discarded: dict[str, Any],
        report: SyncReport,
        detail: str,
    ) -> None:
        report.conflicts += 1
        report.note(f"conflict on {pair.record_key}: {winner} wins ({detail})")
        if not self.config.dry_run:
            self.store.add_conflict(
                pair.record_key, pair.uid, winner, applied, discarded
            )
