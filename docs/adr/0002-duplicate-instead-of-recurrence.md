# No recurrence support; duplicate events instead

Recurring work is scaled by duplicating an Event in the calendar, not by recurrence rules. The bridge refuses recurring Events outright and reports them as skipped.

Expanding `RRULE` would force per-occurrence exceptions, a materialisation window, and a rule for what "edit this and future events" does to Records that already exist — all of it to model series whose actual times differ anyway. Duplicating produces independent Records, which is exactly the desired shape.

Risk: a duplicate must arrive with a fresh `UID`, because Radicale rejects two items with the same `UID` in one collection. The bridge does not depend on the client for this: it mints `ttcal-<uuid4>` for every Event it creates. A client that reuses a `UID` gets a `409 no-uid-conflict` from Radicale and the bridge reports the error instead of corrupting a pair. The one remaining client question — whether Apple's *Duplicate* mints a fresh `UID` — is an optional acceptance check, not a correctness requirement.
