# Using ttcal

ttcal runs as a service and keeps a TimeTagger account and one CalDAV calendar
in step. You keep using both ends normally: the calendar for planning and
editing, TimeTagger for tracking and reports.

## Connect a calendar client

The calendar is created by ttcal on the first sync (display name `TimeTagger`
by default). Point a CalDAV client at the **server root**, not at the calendar
URL, and let the client discover collections.

### Apple Calendar (macOS)

1. System Settings → Internet Accounts → **Add Account** → **Other** →
   **CalDAV Account**.
2. Account type **Manual**; server address is the CalDAV host
   (`cal.example.com`), plus username and password.
3. Select the `TimeTagger` calendar in Calendar.app.

If discovery fails, use **Advanced** and enter the collection URL directly
(`https://cal.example.com/USERNAME/`). Apple caches CalDAV configuration
aggressively; if the calendar does not appear, remove and re-add the account.

### iPhone / iPad

Settings → Apps → Calendar → Accounts → **Add Account** → **Other** → **Add
CalDAV Account**, then the same host, user and password.

### Thunderbird

Calendar pane → **New Calendar** → **On the Network** → CalDAV, location
`https://cal.example.com/`, then pick the `TimeTagger` calendar.

### Android (DAVx5)

Add an account with the CalDAV base URL and credentials, select the `TimeTagger`
calendar, and choose which local calendar app should display it.

### TimeTagger itself

No change. Use the web app or the CLI as before. Records you create or edit
there appear in the calendar on the next pass.

## What edits do

| You do this in the calendar | ttcal does this in TimeTagger |
|-----------------------------|-------------------------------|
| Create a timed event | Creates a record with the same start and end |
| Move or resize an event | Updates `t1`/`t2` |
| Retitle an event | Replaces the record description |
| Add `#tags` to the title | Tags are part of the description, as usual in TimeTagger |
| Add notes, location or URL | Nothing — those fields stay calendar-only |
| Delete an event | Hides the record (`HIDDEN ` prefix) |
| Create an all-day or recurring event | Nothing; it is listed as skipped |

And in the other direction, a record that is created, edited, hidden or
un-hidden in TimeTagger/CLI changes the event on the next pass.

The event title is the record description, verbatim. TimeTagger descriptions are
single-line and at most 255 characters; longer titles are truncated with a
warning. A title beginning with `HIDDEN` is refused, because that prefix is how
TimeTagger marks deleted records.

### Deletion and restore

TimeTagger has no hard delete; it hides records with a `HIDDEN` prefix. ttcal
uses the same convention in both directions:

- **Delete an event** → the record is hidden. Totals stop counting it; the
  record still exists.
- **Un-hide the record** (remove the `HIDDEN ` prefix, via the API or a client
  that shows hidden records) → the event returns with the **same UID**, so
  clients treat it as the same event.
- **Hide a record** in TimeTagger → the event is deleted.
- **Un-hide it** → the event returns with the same UID.

Because the pair (record key ↔ UID) is kept in the state database, a restore
does not create a duplicate. Deleting a whole calendar is refused: it would
look like a mass deletion, and ttcal cannot tell that apart from an accident.
If you really do want to delete many events at once, stop the service and run
`ttcal sync --allow-mass-deletion` once, then start it again.

If hidden records clutter TimeTagger, filter them out or remove them
permanently in TimeTagger after you are sure you will not restore them. Once a
record is hard-deleted, its event cannot be restored by ttcal.

### Running timers

While a timer runs (`start == stop`), no event exists: a block that grows on
every poll would be noise, and a zero-length event is not valid CalDAV. When the
timer stops, the event appears with the exact start and end.

### Events that are skipped

Some events cannot be represented as a record:

| Reason | Why |
|--------|-----|
| `all-day` | A record has instants, not a date. |
| `recurring` | Recurrence is not expanded (see below), including single occurrences of a series. |
| `no-uid` | Without a UID there is no stable identity to pair. |
| `empty-summary` | The event has no title, so there is nothing to record. |
| `hidden-prefix` | The title starts with `HIDDEN` and would collide with TimeTagger's deletion marker. |
| `cancelled` | The event is marked `STATUS:CANCELLED`; clients normally delete instead. |
| `invalid-ical`, `multi-event`, `no-dtstart` | The payload is not exactly one timed event with a start. |

Skipped events are never modified and are listed by `ttcal status`, with the
href and reason. Fix the event (give it a time, remove the recurrence, give it a
title, remove `STATUS:CANCELLED`) and the next pass picks it up.

### Recurring events

Recurrence is deliberately not supported: editing a series would require
rewriting an unbounded number of records and deciding what happens to
exceptions. Duplicate a normal event instead.

In Apple Calendar, **Duplicate** (⌥-drag or Edit → Duplicate) creates a new
event with its own UID in a CalDAV calendar, so each copy becomes its own record.
(The duplicate UID behaviour is scheduled for a final manual check on a real
device; the bridge always mints its own UIDs for records it creates, so the
reverse direction is safe either way.)

### Conflicts

If the same pair is edited on **both** sides between two passes, ttcal compares
the record's `mt` (last modification) with the event's `LAST-MODIFIED`
(falling back to `DTSTAMP`):

- the later edit wins and is applied to the other side;
- a tie goes to the calendar;
- both versions are written to the conflict log, nothing is silently dropped.

`ttcal status` shows the number of conflicts and the five most recent ones with
the winner. A deleted event beats a record edit; the record edit is logged as
discarded.

Most of the time this only happens if you edit on both sides within one poll
interval (60 s by default).

## Commands in daily use

```bash
ttcal status          # pairs, last sync, conflicts, skipped events
ttcal sync            # force a pass now instead of waiting for the poll
ttcal sync --dry-run  # show what the next pass would do; writes nothing
ttcal sync --allow-mass-deletion  # confirm a deliberate mass deletion
ttcal doctor          # connectivity and config check after a change
```

`ttcal run` is the service: it polls every `poll_interval` seconds and logs only
when something changed or failed.

### Status output

```
ttcal 0.1.0
config:        /home/user/.config/ttcal/ttcal.toml
state db:      /var/lib/ttcal/ttcal.db
timetagger:    https://time.example.com/timetagger/api/v2
calendar:      'TimeTagger' at https://cal.example.com/
pairs:         412 active, 3 tombstone_record, 1 tombstone_event
last sync:     2026-10-30 14:02:11 (23s ago)
tt cursor:     1793352131.412
caldav cursor: http://radicale.org/ns/sync/…
conflicts:     1
  2026-10-30 13:40  calendar won  record=3f2a…
skipped:       2
  /user/calendars/…/event.ics  recurring  RRULE:FREQ=WEEKLY
```

- `active` pairs are mirrored.
- `tombstone_record` / `tombstone_event` pairs are deleted on one side and
  can still be restored.
- `last sync` is the last pass that completed without errors.
- A missing `caldav cursor` means the server offered no sync token and ttcal
  falls back to comparing ETags.

## Backfill

The first pass imports records from the last `backfill_days` days (90 by
default). Older records are not imported until they change on the TimeTagger
side, after which they are mirrored like any other record.

To bring in more history, raise `backfill_days` and let the next pass run; the
wider window is loaded and existing pairs are kept. It is safe to do this while
the service is running.

## Repairing state

If the state database is lost or restored from an old backup, pairings can be
rebuilt from content:

```bash
ttcal repair --rebuild
```

This matches each event to a record with exactly the same start, end and
description inside the backfill window, and writes the pairs. It deletes
nothing, does not touch timestamps and leaves conflict history alone. Events
without an exact match stay unmatched and are reported; the next normal sync
treats them as new events, which can create duplicate records. Run
`ttcal sync --dry-run` first and check the counts if you are unsure.

Repair is not needed for normal restarts or upgrades: all cursors and pairs live
in SQLite and survive them.

## Operational notes

- **Restarts and downtime are safe.** The TimeTagger cursor (`server_time`) and
  the CalDAV sync token make the next pass catch up on everything that changed.
  If the TimeTagger server reports a reset, ttcal reloads the backfill window
  instead of trusting the cursor.
- **Polling** is cheap: one `/updates` call, one CalDAV sync-token request.
  Apple clients refresh CalDAV on their own schedule (often every 15 minutes);
  press ⌘R in Calendar.app to force it.
- **One instance per calendar.** Two bridges on the same calendar and account
  would fight over the same pairs.
- **Time zones.** Times are stored in UTC, as TimeTagger does. Historical events
  move when your local timezone changes, exactly like TimeTagger records.
- **Future events** are allowed and become future records, which already count
  in TimeTagger totals.
- **Editing history** rewrites tracked time. That is intended, but TimeTagger is
  not an audit log.
