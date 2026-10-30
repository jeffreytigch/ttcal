"""Mapping between TimeTagger records and iCalendar events.

The rules are the ones agreed in docs/plan.md section 4.  This module has no
network or database code, which keeps the mapping unit-testable.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import vobject
from vobject.icalendar import utc as _VOBJ_UTC

DS_MAX = 255
HIDDEN_PREFIX = "HIDDEN"
MIN_DURATION = 60
UID_PREFIX = "ttcal-"


class UnsupportedEvent(Exception):
    """An event that cannot be mirrored into TimeTagger."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class RecordFields:
    key: str
    t1: int
    t2: int
    ds: str
    mt: int

    @property
    def hidden(self) -> bool:
        return is_hidden(self.ds)

    @property
    def running(self) -> bool:
        return self.t1 == self.t2


@dataclass(frozen=True)
class EventFields:
    uid: str
    start: int
    end: int
    summary: str
    modified: int


@dataclass(frozen=True)
class ParsedEvent:
    fields: EventFields
    raw: str


def new_record_key() -> str:
    return uuid.uuid4().hex


def new_uid() -> str:
    return UID_PREFIX + str(uuid.uuid4())


def is_hidden(ds: str) -> bool:
    return ds.lstrip().upper().startswith(HIDDEN_PREFIX)


def hide_ds(ds: str) -> str:
    if is_hidden(ds):
        return ds
    return truncate_ds(f"{HIDDEN_PREFIX} {ds}")


def unhide_ds(ds: str) -> str:
    text = ds.lstrip()
    if not text.upper().startswith(HIDDEN_PREFIX):
        return ds
    rest = text[len(HIDDEN_PREFIX) :]
    rest = rest.removeprefix(" ")
    return rest if rest else "restored"


def normalize_summary(summary: str) -> str:
    """Calendar summaries may contain line breaks; TimeTagger descriptions cannot."""
    text = summary.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return " ".join(text.split())


def truncate_ds(ds: str, limit: int = DS_MAX) -> str:
    if len(ds) <= limit:
        return ds
    return ds[: limit - 1] + "…"


def record_ds_from_summary(summary: str) -> str:
    """Validate and normalize an event summary for use as a record description."""
    ds = normalize_summary(summary)
    if not ds:
        raise UnsupportedEvent("empty-summary", "event has no title")
    if is_hidden(ds):
        raise UnsupportedEvent(
            "hidden-prefix",
            "summary starts with HIDDEN, which TimeTagger reads as a deleted record",
        )
    return truncate_ds(ds)


def record_hash(t1: int, t2: int, ds: str) -> str:
    payload = json.dumps(
        [int(t1), int(t2), ds], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def event_hash(start: int, end: int, summary: str) -> str:
    payload = json.dumps(
        [int(start), int(end), summary], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_hash_of(fields: RecordFields) -> str:
    return record_hash(fields.t1, fields.t2, fields.ds)


def event_hash_of(fields: EventFields) -> str:
    return event_hash(fields.start, fields.end, fields.summary)


def record_from_payload(payload: dict[str, Any]) -> RecordFields:
    return RecordFields(
        key=str(payload["key"]),
        t1=int(payload["t1"]),
        t2=int(payload["t2"]),
        ds=str(payload.get("ds") or ""),
        mt=int(payload.get("mt") or 0),
    )


def record_payload(fields: RecordFields) -> dict[str, Any]:
    return {
        "key": fields.key,
        "t1": int(fields.t1),
        "t2": int(fields.t2),
        "ds": fields.ds,
        "mt": int(fields.mt),
    }


def record_to_event(record: RecordFields, uid: str) -> tuple[int, int, str]:
    """Map a record to event start/end/summary, clamping the duration."""
    start = int(record.t1)
    end = int(record.t2)
    if end <= start:
        end = start + MIN_DURATION
    return start, end, record.ds


def _to_epoch(value: Any) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return int(value.timestamp())
    if isinstance(value, date):
        raise UnsupportedEvent("all-day", "event date has no time component")
    raise UnsupportedEvent("invalid-date", f"unexpected date value: {value!r}")


def _prop_value(vevent: Any, name: str) -> Any:
    prop = getattr(vevent, name, None)
    return None if prop is None else prop.value


def parse_event(raw: str) -> ParsedEvent:
    """Parse an iCalendar payload into flat fields, or raise UnsupportedEvent."""
    try:
        calendar = vobject.readOne(raw)
    except Exception as err:  # vobject raises a variety of errors
        raise UnsupportedEvent("invalid-ical", str(err)) from None

    vevents = getattr(calendar, "vevent_list", None) or []
    if not vevents:
        raise UnsupportedEvent("no-vevent", "calendar has no VEVENT component")
    if len(vevents) > 1:
        raise UnsupportedEvent(
            "multi-event", f"calendar has {len(vevents)} VEVENT components"
        )
    vevent = vevents[0]

    uid = _prop_value(vevent, "uid")
    if not uid or not str(uid).strip():
        raise UnsupportedEvent("no-uid", "event has no UID")

    for name in ("rrule", "rdate", "exdate", "recurrence-id"):
        if getattr(vevent, name, None) is not None:
            raise UnsupportedEvent("recurring", f"event has {name.upper()}")

    status = _prop_value(vevent, "status")
    if status is not None and str(status).strip().upper() == "CANCELLED":
        raise UnsupportedEvent("cancelled", "event is marked CANCELLED")

    dtstart = _prop_value(vevent, "dtstart")
    if dtstart is None:
        raise UnsupportedEvent("no-dtstart", "event has no DTSTART")
    start = _to_epoch(dtstart)

    dtend = _prop_value(vevent, "dtend")
    if dtend is not None:
        end = _to_epoch(dtend)
    else:
        duration = _prop_value(vevent, "duration")
        if duration is not None:
            end = start + int(duration.total_seconds())
        else:
            end = start + MIN_DURATION

    summary = _prop_value(vevent, "summary")
    summary = "" if summary is None else str(summary)

    modified = _prop_value(vevent, "last-modified")
    if modified is None:
        modified = _prop_value(vevent, "dtstamp")
    try:
        modified_ts = _to_epoch(modified) if modified is not None else start
    except UnsupportedEvent:
        modified_ts = start

    return ParsedEvent(
        fields=EventFields(
            uid=str(uid).strip(),
            start=start,
            end=end,
            summary=summary,
            modified=modified_ts,
        ),
        raw=raw,
    )


def _set_datetime(prop: Any, value: datetime) -> None:
    prop.value = value
    # Drop parameters that conflict with an absolute UTC value.
    prop.params.pop("TZID", None)
    prop.params.pop("VALUE", None)


def _utc(timestamp: int) -> datetime:
    # vobject only recognises its own UTC tzinfo when serialising; a plain
    # datetime.timezone.utc makes it raise "Unable to guess TZID".
    return datetime.fromtimestamp(int(timestamp), tz=_VOBJ_UTC)


def build_event(uid: str, start: int, end: int, summary: str) -> str:
    calendar = vobject.iCalendar()
    vevent = calendar.add("vevent")
    vevent.add("uid").value = uid
    vevent.add("dtstamp").value = datetime.now(_VOBJ_UTC).replace(microsecond=0)
    vevent.add("dtstart").value = _utc(start)
    vevent.add("dtend").value = _utc(end)
    vevent.add("summary").value = summary
    return calendar.serialize()


def update_event_ical(raw: str, start: int, end: int, summary: str) -> str:
    """Rewrite only the mirrored fields, preserving notes, location and URL."""
    calendar = vobject.readOne(raw)
    vevents = getattr(calendar, "vevent_list", None) or []
    if not vevents:
        raise UnsupportedEvent("no-vevent", "calendar has no VEVENT component")
    vevent = vevents[0]

    _set_datetime(vevent.dtstart, _utc(start))
    if getattr(vevent, "dtend", None) is None:
        vevent.add("dtend")
    _set_datetime(vevent.dtend, _utc(end))
    if getattr(vevent, "duration", None) is not None:
        del vevent.duration

    if getattr(vevent, "summary", None) is None:
        vevent.add("summary")
    vevent.summary.value = summary

    if getattr(vevent, "dtstamp", None) is None:
        vevent.add("dtstamp")
    vevent.dtstamp.value = datetime.now(_VOBJ_UTC).replace(microsecond=0)

    return calendar.serialize()
