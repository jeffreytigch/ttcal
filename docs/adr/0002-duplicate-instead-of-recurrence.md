# No recurrence support; duplicate events instead

Recurring work is scaled by duplicating an Event in the calendar, not by recurrence rules. The bridge refuses recurring Events outright and reports them as skipped.

Expanding `RRULE` would force per-occurrence exceptions, a materialisation window, and a rule for what "edit this and future events" does to Records that already exist — all of it to model series whose actual times differ anyway. Duplicating produces independent Records, which is exactly the desired shape.

Risk: duplicate must arrive with a fresh `UID`. Radicale rejects two items with the same UID in one collection, so this is verified against real Apple Calendar before the write path is built.
