"""CalDAV client wrapper around ``caldav``.

Only the operations ttcal needs: find/create the calendar, list changes with
ETags, create/update/delete a single event by UID.  UIDs are always minted by
ttcal, so a broken client-side duplicate can never poison the collection.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import caldav
from caldav.lib import error as caldav_error
from caldav.lib.url import URL

from .model import EventFields, UnsupportedEvent, event_hash_of, parse_event

log = logging.getLogger(__name__)

_UID_RE = re.compile(r"^UID:(.*)$", re.MULTILINE)


class CalendarError(Exception):
    """Raised when the calendar server cannot be used."""


@dataclass(frozen=True)
class SkipItem:
    href: str
    uid: str | None
    reason: str
    detail: str


@dataclass(frozen=True)
class CalEvent:
    href: str
    uid: str
    etag: str | None
    start: int
    end: int
    summary: str
    modified: int

    @property
    def fields(self) -> EventFields:
        return EventFields(
            uid=self.uid,
            start=self.start,
            end=self.end,
            summary=self.summary,
            modified=self.modified,
        )

    @property
    def hash(self) -> str:
        return event_hash_of(self.fields)


@dataclass(frozen=True)
class CalChanges:
    events: list[CalEvent]
    deleted_hrefs: set[str]
    sync_token: str | None
    full: bool
    skips: list[SkipItem] = field(default_factory=list)


def _href(url: URL | str) -> str:
    try:
        return str(url.canonical())  # type: ignore[union-attr]
    except Exception:
        return str(url)


def _etag(value: object) -> str | None:
    text = getattr(value, "value", value)
    return None if text is None else str(text)


def _guess_uid(raw: str) -> str | None:
    unfolded = re.sub(r"\r?\n[ \t]", "", raw)
    match = _UID_RE.search(unfolded)
    return match.group(1).strip() if match else None


def _to_cal_event(raw: str, url: URL | str, etag: object) -> CalEvent:
    parsed = parse_event(raw)
    fields = parsed.fields
    return CalEvent(
        href=_href(url),
        uid=fields.uid,
        etag=_etag(etag),
        start=fields.start,
        end=fields.end,
        summary=fields.summary,
        modified=fields.modified,
    )


class CalendarClient:
    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        calendar_name: str,
        timeout: int = 30,
    ) -> None:
        self.url = url
        self.username = username
        self.password = password
        self.calendar_name = calendar_name
        self.timeout = timeout
        self.client: caldav.DAVClient | None = None
        self.calendar: caldav.Calendar | None = None

    def connect(self) -> None:
        try:
            self.client = caldav.DAVClient(
                url=self.url,
                username=self.username,
                password=self.password,
                timeout=self.timeout,
                require_tls=self.url.startswith("https://"),
                enable_rfc6764=False,
            )
            self.client.principal()
        except Exception as err:
            raise CalendarError(
                f"cannot connect to CalDAV at {self.url}: {err}"
            ) from None

    def calendars(self) -> list[caldav.Calendar]:
        if self.client is None:
            self.connect()
        assert self.client is not None
        try:
            principal = self.client.principal()
            return list(principal.calendars())
        except Exception as err:
            raise CalendarError(f"cannot list calendars at {self.url}: {err}") from None

    def open_calendar(self, create: bool = False) -> caldav.Calendar:
        """Find the calendar by display name; optionally create it."""
        if self.calendar is not None:
            return self.calendar
        if self.client is None:
            self.connect()
        assert self.client is not None
        try:
            principal = self.client.principal()
            for calendar in principal.calendars():
                if calendar.get_display_name() == self.calendar_name:
                    self.calendar = calendar
                    return calendar
            if not create:
                raise CalendarError(
                    f"calendar '{self.calendar_name}' not found at {self.url}"
                )
            log.info("creating calendar %r", self.calendar_name)
            self.calendar = principal.make_calendar(
                name=self.calendar_name,
                supported_calendar_component_set=["VEVENT"],
            )
            return self.calendar
        except CalendarError:
            raise
        except Exception as err:
            raise CalendarError(
                f"cannot open calendar '{self.calendar_name}': {err}"
            ) from None

    # -- reads -----------------------------------------------------------
    def changes(self, sync_token: str | None) -> CalChanges:
        calendar = self.open_calendar()
        try:
            collection = calendar.get_objects_by_sync_token(
                sync_token=sync_token, load_objects=True
            )
        except Exception as err:
            raise CalendarError(f"cannot read calendar changes: {err}") from None

        events: list[CalEvent] = []
        skips: list[SkipItem] = []
        for obj in collection.objects:
            raw = getattr(obj, "data", None)
            if not raw:
                continue
            href = _href(obj.url)
            try:
                events.append(_to_cal_event(raw, obj.url, getattr(obj, "etag", None)))
            except UnsupportedEvent as err:
                skips.append(
                    SkipItem(
                        href=href,
                        uid=_guess_uid(raw),
                        reason=err.reason,
                        detail=err.detail,
                    )
                )

        deleted = {_href(url) for url in (collection.deleted_urls or set())}
        token = getattr(collection, "sync_token", None)
        token = None if token is None else str(token)
        return CalChanges(
            events=events,
            deleted_hrefs=deleted,
            sync_token=token,
            full=sync_token is None or not token,
            skips=skips,
        )

    def get_event(self, uid: str) -> CalEvent | None:
        calendar = self.open_calendar()
        try:
            obj = calendar.get_event_by_uid(uid)
        except caldav_error.NotFoundError:
            return None
        except Exception as err:
            raise CalendarError(f"cannot fetch event {uid}: {err}") from None
        if obj is None:
            return None
        try:
            return _to_cal_event(obj.data, obj.url, obj.etag)
        except UnsupportedEvent as err:
            raise CalendarError(
                f"event {uid} is no longer representable: {err.reason}"
            ) from None

    # -- writes ----------------------------------------------------------
    def create_event(self, uid: str, start: int, end: int, summary: str) -> CalEvent:
        from .model import build_event

        calendar = self.open_calendar(create=True)
        try:
            obj = calendar.save_event(build_event(uid, start, end, summary))
        except Exception as err:
            raise CalendarError(f"cannot create event {uid}: {err}") from None
        return _to_cal_event(obj.data, obj.url, obj.etag)

    def update_event(self, uid: str, start: int, end: int, summary: str) -> CalEvent:
        from .model import update_event_ical

        calendar = self.open_calendar()
        try:
            obj = calendar.get_event_by_uid(uid)
            if obj is None:
                raise caldav_error.NotFoundError(uid)
            obj.data = update_event_ical(obj.data, start, end, summary)
            obj.save()
            refreshed = calendar.get_event_by_uid(uid)
        except caldav_error.NotFoundError:
            raise CalendarError(
                f"event {uid} disappeared while updating (deleted concurrently?)"
            ) from None
        except Exception as err:
            raise CalendarError(f"cannot update event {uid}: {err}") from None
        try:
            return _to_cal_event(refreshed.data, refreshed.url, refreshed.etag)
        except UnsupportedEvent as err:
            raise CalendarError(
                f"event {uid} became unrepresentable: {err.reason}"
            ) from None

    def delete_event(self, uid: str) -> bool:
        calendar = self.open_calendar()
        try:
            obj = calendar.get_event_by_uid(uid)
        except caldav_error.NotFoundError:
            return False
        except Exception as err:
            raise CalendarError(
                f"cannot fetch event {uid} for deletion: {err}"
            ) from None
        if obj is None:
            return False
        try:
            obj.delete()
        except caldav_error.NotFoundError:
            return False
        except Exception as err:
            raise CalendarError(f"cannot delete event {uid}: {err}") from None
        return True
