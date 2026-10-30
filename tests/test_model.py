from __future__ import annotations

import unittest

import vobject

from ttcal import model


def ical(*lines: str) -> str:
    body = "\r\n".join(lines)
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//ttcal test//EN\r\n"
        f"{body}\r\nEND:VCALENDAR\r\n"
    )


class HiddenTests(unittest.TestCase):
    def test_detects_case_insensitively_and_ignores_leading_space(self):
        for ds in ("HIDDEN x", "hidden x", "  Hidden x", "HIDDEN"):
            self.assertTrue(model.is_hidden(ds), ds)
        for ds in ("not HIDDEN", "hid den", ""):
            self.assertFalse(model.is_hidden(ds), ds)

    def test_hide_and_unhide_round_trip(self):
        self.assertEqual(
            model.unhide_ds(model.hide_ds("write report #work")), "write report #work"
        )

    def test_hide_is_idempotent(self):
        once = model.hide_ds("write report")
        self.assertEqual(model.hide_ds(once), once)

    def test_hide_respects_length_limit(self):
        hidden = model.hide_ds("x" * 400)
        self.assertEqual(len(hidden), model.DS_MAX)
        self.assertTrue(hidden.startswith("HIDDEN "))

    def test_unhide_without_prefix_is_a_noop(self):
        self.assertEqual(model.unhide_ds("plain"), "plain")


class SummaryTests(unittest.TestCase):
    def test_newlines_collapse_to_spaces(self):
        self.assertEqual(model.normalize_summary("a\r\nb\nc\td"), "a b c d")

    def test_hidden_prefix_is_refused(self):
        with self.assertRaises(model.UnsupportedEvent) as ctx:
            model.record_ds_from_summary("HIDDEN oops")
        self.assertEqual(ctx.exception.reason, "hidden-prefix")

    def test_long_summary_is_truncated_to_the_limit(self):
        ds = model.record_ds_from_summary("y" * 400)
        self.assertEqual(len(ds), model.DS_MAX)
        self.assertTrue(ds.endswith("…"))

    def test_short_summary_is_unchanged(self):
        self.assertEqual(model.record_ds_from_summary("Short  one"), "Short one")


class HashTests(unittest.TestCase):
    def test_record_hash_is_stable(self):
        self.assertEqual(
            model.record_hash(10, 20, "a #b"),
            model.record_hash(10, 20, "a #b"),
        )

    def test_record_hash_covers_every_field(self):
        base = model.record_hash(10, 20, "a")
        self.assertNotEqual(base, model.record_hash(11, 20, "a"))
        self.assertNotEqual(base, model.record_hash(10, 21, "a"))
        self.assertNotEqual(base, model.record_hash(10, 20, "b"))

    def test_event_hash_matches_record_hash_for_identical_content(self):
        self.assertEqual(model.record_hash(10, 20, "a"), model.event_hash(10, 20, "a"))


class RecordMappingTests(unittest.TestCase):
    def test_record_to_event_clamps_zero_and_negative_durations(self):
        self.assertEqual(
            model.record_to_event(model.RecordFields("k", 100, 100, "x", 0), "u")[:2],
            (100, 100 + model.MIN_DURATION),
        )
        self.assertEqual(
            model.record_to_event(model.RecordFields("k", 100, 40, "x", 0), "u")[:2],
            (100, 100 + model.MIN_DURATION),
        )

    def test_payload_round_trip(self):
        fields = model.RecordFields("k", 1, 2, "ds #tag", 3)
        self.assertEqual(
            model.record_from_payload(model.record_payload(fields)), fields
        )


class BuildEventTests(unittest.TestCase):
    def test_build_event_is_valid_and_parses_back(self):
        raw = model.build_event("ttcal-1", 1_700_000_000, 1_700_003_600, "worked #job")
        parsed = model.parse_event(raw)
        self.assertEqual(parsed.fields.uid, "ttcal-1")
        self.assertEqual(parsed.fields.start, 1_700_000_000)
        self.assertEqual(parsed.fields.end, 1_700_003_600)
        self.assertEqual(parsed.fields.summary, "worked #job")

    def test_build_event_serialises_utc(self):
        raw = model.build_event("ttcal-1", 1_700_000_000, 1_700_003_600, "x")
        self.assertIn("DTSTART:20231114T221320Z", raw)

    def test_update_event_preserves_calendar_only_fields(self):
        raw = ical(
            "BEGIN:VEVENT",
            "UID:ttcal-1",
            "DTSTAMP:20231114T220000Z",
            "DTSTART;TZID=Europe/Amsterdam:20231114T230000",
            "DTEND;TZID=Europe/Amsterdam:20231115T000000",
            "SUMMARY:old",
            "DESCRIPTION:keep me",
            "LOCATION:here",
            "URL:https://example.com",
            "END:VEVENT",
        )
        updated = model.update_event_ical(raw, 1_700_000_000, 1_700_003_600, "new #tag")
        parsed = model.parse_event(updated)
        self.assertEqual(parsed.fields.summary, "new #tag")
        self.assertEqual(parsed.fields.start, 1_700_000_000)
        self.assertEqual(parsed.fields.end, 1_700_003_600)

        document = vobject.readOne(updated)
        vevent = document.vevent
        self.assertEqual(str(vevent.description.value), "keep me")
        self.assertEqual(str(vevent.location.value), "here")
        self.assertEqual(str(vevent.url.value), "https://example.com")
        # Absolute UTC values must not keep a stale TZID parameter.
        self.assertNotIn("TZID", vevent.dtstart.params)
        self.assertNotIn("TZID", vevent.dtend.params)

    def test_update_event_replaces_duration_with_dtend(self):
        raw = ical(
            "BEGIN:VEVENT",
            "UID:ttcal-1",
            "DTSTAMP:20231114T220000Z",
            "DTSTART:20231114T220000Z",
            "DURATION:PT30M",
            "SUMMARY:x",
            "END:VEVENT",
        )
        updated = model.update_event_ical(raw, 1_700_000_000, 1_700_000_600, "x")
        vevent = vobject.readOne(updated).vevent
        self.assertIsNone(getattr(vevent, "duration", None))
        self.assertIsNotNone(getattr(vevent, "dtend", None))
        parsed = model.parse_event(updated)
        self.assertEqual(parsed.fields.end - parsed.fields.start, 600)

    def test_update_event_sets_missing_summary(self):
        raw = ical(
            "BEGIN:VEVENT",
            "UID:ttcal-1",
            "DTSTAMP:20231114T220000Z",
            "DTSTART:20231114T220000Z",
            "DTEND:20231114T230000Z",
            "END:VEVENT",
        )
        updated = model.update_event_ical(raw, 1_700_000_000, 1_700_003_600, "added")
        self.assertEqual(model.parse_event(updated).fields.summary, "added")


class ParseEventTests(unittest.TestCase):
    def test_all_day_event_is_unsupported(self):
        raw = ical(
            "BEGIN:VEVENT",
            "UID:a",
            "DTSTART;VALUE=DATE:20231114",
            "DTEND;VALUE=DATE:20231115",
            "SUMMARY:x",
            "END:VEVENT",
        )
        with self.assertRaises(model.UnsupportedEvent) as ctx:
            model.parse_event(raw)
        self.assertEqual(ctx.exception.reason, "all-day")

    def test_recurring_event_is_unsupported(self):
        raw = ical(
            "BEGIN:VEVENT",
            "UID:a",
            "DTSTAMP:20231114T220000Z",
            "DTSTART:20231114T220000Z",
            "DTEND:20231114T230000Z",
            "RRULE:FREQ=WEEKLY",
            "SUMMARY:x",
            "END:VEVENT",
        )
        with self.assertRaises(model.UnsupportedEvent) as ctx:
            model.parse_event(raw)
        self.assertEqual(ctx.exception.reason, "recurring")

    def test_event_without_uid_is_unsupported(self):
        raw = ical(
            "BEGIN:VEVENT",
            "DTSTART:20231114T220000Z",
            "SUMMARY:x",
            "END:VEVENT",
        )
        with self.assertRaises(model.UnsupportedEvent) as ctx:
            model.parse_event(raw)
        self.assertEqual(ctx.exception.reason, "no-uid")

    def test_multiple_events_are_unsupported(self):
        raw = ical(
            "BEGIN:VEVENT",
            "UID:a",
            "DTSTART:20231114T220000Z",
            "SUMMARY:x",
            "END:VEVENT",
            "BEGIN:VEVENT",
            "UID:b",
            "DTSTART:20231114T220000Z",
            "SUMMARY:y",
            "END:VEVENT",
        )
        with self.assertRaises(model.UnsupportedEvent) as ctx:
            model.parse_event(raw)
        self.assertEqual(ctx.exception.reason, "multi-event")

    def test_duration_is_used_when_dtend_is_missing(self):
        raw = ical(
            "BEGIN:VEVENT",
            "UID:a",
            "DTSTART:20231114T220000Z",
            "DURATION:PT45M",
            "SUMMARY:x",
            "END:VEVENT",
        )
        parsed = model.parse_event(raw)
        self.assertEqual(parsed.fields.end - parsed.fields.start, 2700)

    def test_missing_end_defaults_to_one_minute(self):
        raw = ical(
            "BEGIN:VEVENT",
            "UID:a",
            "DTSTART:20231114T220000Z",
            "SUMMARY:x",
            "END:VEVENT",
        )
        parsed = model.parse_event(raw)
        self.assertEqual(parsed.fields.end - parsed.fields.start, model.MIN_DURATION)

    def test_last_modified_wins_over_dtstamp(self):
        raw = ical(
            "BEGIN:VEVENT",
            "UID:a",
            "DTSTAMP:20231114T220000Z",
            "LAST-MODIFIED:20231115T010000Z",
            "DTSTART:20231114T230000Z",
            "DTEND:20231115T000000Z",
            "SUMMARY:x",
            "END:VEVENT",
        )
        parsed = model.parse_event(raw)
        self.assertEqual(parsed.fields.modified, 1_700_010_000)

    def test_missing_modified_falls_back_to_start(self):
        raw = ical(
            "BEGIN:VEVENT",
            "UID:a",
            "DTSTART:20231114T220000Z",
            "SUMMARY:x",
            "END:VEVENT",
        )
        parsed = model.parse_event(raw)
        self.assertEqual(parsed.fields.modified, parsed.fields.start)

    def test_garbage_is_unsupported(self):
        with self.assertRaises(model.UnsupportedEvent):
            model.parse_event("not a calendar")


if __name__ == "__main__":
    unittest.main()
