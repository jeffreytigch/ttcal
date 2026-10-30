# ttcal

A calendar interface for TimeTagger: the calendar and TimeTagger show the same tracked time, editable from either side.

## Language

**Record**:
The unit of tracked time in TimeTagger: a start, a stop, and a description that may contain `#tags`.
_Avoid_: entry, item, log, timesheet row

**Event**:
The calendar-shaped form of a Record. An Event and its Record are one thing, not two; creating either creates the other.
_Avoid_: appointment, calendar item, booking

**Mirroring**:
The invariant that an Event and a Record are the same thing. It holds for every Record inside the backfill horizon, and for any older Record from the moment that Record changes.
_Avoid_: sync, replication, two-way sync

**Backfill horizon**:
How far into the past existing Records are given Events the first time the bridge runs (default: 90 days). Records older than the horizon stay TimeTagger-only until they are touched.
_Avoid_: import window, history range, cutover

**Running record**:
A Record whose stop time has not been set yet (start equals stop). It has no Event until it is stopped.
_Avoid_: active timer, open entry

**Hidden**:
The only form of deletion TimeTagger offers: the description is prefixed with `HIDDEN`, so the Record disappears from the UI but survives on the server and can be restored.
_Avoid_: deleted, removed, archived

**Tombstone**:
The remembered pairing between a Record key and an Event UID after one of the two was deleted, so restoring the survivor recreates the other with the same identity.
_Avoid_: mapping, cache, link

**Skipped event**:
An Event the bridge refuses to mirror because no Record can represent it: all-day events, recurring events and single occurrences of a series, cancelled events, events without a UID, invalid events, and events whose title TimeTagger cannot store. Skipped events are reported, never silently converted.
_Avoid_: ignored event, invalid event

**Conflict**:
Both sides of a pair changed since the previous poll. The later edit wins and both versions are kept in the conflict log.
