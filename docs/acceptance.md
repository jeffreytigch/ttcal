# Acceptance checklist

Run this after deploying, before trusting the mirror with real time tracking.
It exercises both directions, the destructive paths, and the failure modes that
matter. Record the date and result of each item in the table at the end.

Use a **non-critical period** for the first pass — for example, duplicate one
event and edit that, rather than rewriting a week of tracked history.

## Before you start

```bash
ttcal doctor
ttcal status
```

- `doctor: OK`.
- `status` shows the expected number of pairs and no skipped events you did not
  expect.
- The `TimeTagger` calendar is visible in at least one calendar client.

Keep `supervisorctl tail -f ttcal stderr` (or the equivalent) open in a second
terminal while you work.

## 1. Record → Event

Create a record dated yesterday in the TimeTagger web app or CLI, with a title
that includes a tag, e.g. `acceptance test #ttcal`.

**Pass:** within one poll interval (60 s) an event with the same title, start and
end appears in the calendar. The tag is visible in the title.

## 2. Event → Record

In the calendar, create a timed event for yesterday 09:00–10:00 titled
`calendar acceptance #ttcal`.

**Pass:** within one poll interval a TimeTagger record exists with those exact
bounds and that description.

## 3. Retitle

Rename the event from step 2 to `calendar acceptance renamed #ttcal`.

**Pass:** the record description changes, the `#ttcal` tag is still there, and
the times are unchanged.

## 4. Delete and restore from the calendar

Delete the event from step 2/3.

**Pass:** the record disappears from normal TimeTagger views (it is hidden), and
`ttcal status` shows it as a tombstone pair.

Then un-hide the record (remove the `HIDDEN ` prefix from its description, via
the API or a client that exposes hidden records).

**Pass:** the event returns with the **same UID** — check the event's UID in the
calendar's inspector or with `curl`; no duplicate event appears.

## 5. Hide and restore from TimeTagger

Hide a different mirrored record in TimeTagger (prefix its description with
`HIDDEN `).

**Pass:** the event disappears from the calendar.

Un-hide it.

**Pass:** the event returns with the same UID.

## 6. Running timer

Start a timer in TimeTagger and leave it running for two poll intervals.

**Pass:** no event exists for it while it runs.

Stop the timer.

**Pass:** an event appears with exactly the tracked start and stop.

## 7. Skipped events

Create one all-day event and one recurring event in the calendar.

**Pass:** neither produces a record, both appear in `ttcal status` under
`skipped` with reasons `all-day` and `recurring`, and neither event is modified.

Delete them when done; the skip entries disappear on the next full listing.

## 8. Downtime

Stop ttcal for about an hour. During that time:

- edit one mirrored event in the calendar,
- create one record in TimeTagger,
- delete one mirrored event.

Start ttcal again.

**Pass:** all three changes are applied (the calendar edit reached the record,
the new record has an event, the deleted event's record is hidden), with no
duplicates and no extra conflict entries beyond the ones caused by editing the
same pair on both sides.

## 9. Conflict

Pick one mirrored pair and edit both sides within one poll interval: change the
title in the calendar and the description in TimeTagger, then force a pass.

**Pass:** the later edit wins on both sides; `ttcal status` shows one conflict
and the two versions are recorded. The losing value is not silently lost.

## 10. Duplicate behaviour (optional, Apple Calendar)

Duplicate a mirrored event in Apple Calendar (Edit → Duplicate or ⌥-drag).

**Pass:** the duplicate arrives in Radicale with a **fresh UID**, and the next
pass creates a new record for it. If instead the duplicate fails to upload or
does not appear as a new record, note it: the bridge still mints its own UIDs for
records it creates, but duplicated events would need to be recreated rather than
duplicated.

## Results

| # | Check | Date | Result | Notes |
|---|-------|------|--------|-------|
| 1 | Record → Event | | | |
| 2 | Event → Record | | | |
| 3 | Retitle | | | |
| 4 | Calendar delete/restore, same UID | | | |
| 5 | TimeTagger hide/restore, same UID | | | |
| 6 | Running timer | | | |
| 7 | All-day + recurring skipped | | | |
| 8 | One hour of downtime, catch-up | | | |
| 9 | Conflict, later edit wins, both logged | | | |
| 10 | Apple duplicate UID (optional) | | | |

## After acceptance

- Leave the service running and check `ttcal status` again a day later: `last
  sync` should be recent and `conflicts`/`skipped` should make sense.
- Keep the state database backed up (it contains the only mapping between
  records and events).
- If something fails, collect `ttcal status`, the service log at `DEBUG` level
  for one pass, and the failing item's UID/href before changing anything.
