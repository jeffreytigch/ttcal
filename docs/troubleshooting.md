# Troubleshooting

Start with:

```bash
ttcal doctor -v
ttcal status
```

`doctor` checks the config file, the state database, TimeTagger (version and a
one-day record count) and the CalDAV calendar. `status` shows what the last pass
did and what it skipped. Exit codes: `0` success, `1` runtime error, `2` config
error.

## Startup and configuration

### `configuration error: config file not found: …`

The default path is `~/.config/ttcal/ttcal.toml`. Copy
`ttcal.example.toml` there, or pass `-c /path/to/ttcal.toml`.

### `… contains secrets but is accessible by group or others; run: chmod 600 …`

Inline secrets require a private file. Fix the mode. The same applies to
`token_file` and `password_file`: they must be `0600` and non-empty.

### `give either 'token' or 'token_file', not both`

Pick one. Same for `password` / `password_file`. Prefer `*_file` when a secret
store or container secret mount provides the value.

### `[timetagger]: give 'token'/'token_file' (recommended) or 'username' + 'password'`

ttcal needs credentials. A token is the stable option; a password login is
exchanged for a JWT at startup and refreshed only by restarting the process.

### `[ttcal]: 'poll_interval' must be >= 5 seconds`

Sub-five-second polling adds load without making clients refresh faster; Apple
Calendar refreshes on its own schedule anyway.

## TimeTagger

### `cannot reach TimeTagger at …: …`

Check `api_url` from the machine that runs ttcal (`curl -i <api_url>/version`).
Common causes:

- the URL points at the web UI root instead of the API (`/api/v2` missing);
- a self-hosted instance mounts the app under `/timetagger/`, so the API base is
  `https://host/timetagger/api/v2`;
- a proxy, firewall, DNS or certificate problem. `-v` shows the underlying
  requests error.

### `TimeTagger rejected the token (401)`

The token was revoked, copied with whitespace, or belongs to another instance.
Create a new token in the TimeTagger web app and update the config (or the file
referenced by `token_file`). With username/password, the error appears as
`TimeTagger login failed`.

### `TimeTagger reported a reset; reloading the backfill horizon`

Informational. The server's update counter was reset (database restored or
recreated). ttcal reloads the configured window instead of trusting the cursor;
existing pairs are recognised by record key.

### A record edit did not reach the calendar

Check `ttcal status` for the last sync time and errors. TimeTagger ignores a
`PUT` whose `mt` is older than the stored one, so a client that writes an old
`mt` can silently lose. ttcal always writes the current time and re-reads after
writing, so a normal edit is not affected.

## CalDAV and the calendar

### `cannot connect to CalDAV at …: …`

- Verify the URL is the **server root** (`https://cal.example.com/`), not a
  calendar path.
- Check credentials with a CalDAV client. Radicale's default authentication is
  `denyall`; if every request returns 401, its `[auth]` section is missing or
  wrong.
- TLS errors on a self-signed certificate need a trusted certificate, not a
  bypass: Apple clients reject self-signed CalDAV servers.

### `calendar 'TimeTagger' not found at …`

The first `ttcal sync` creates it, but the account must be allowed to create
collections. If the calendar exists under a different display name, set
`[caldav] calendar` to that name. If you intended a new calendar, remove the
state database first — but read the next item.

### `… Refusing to continue: re-creating the calendar would hide or duplicate every record.`

The state database knows about pairs, but the calendar is gone or renamed. This
is a safety stop, not a bug. Restore the calendar (or rename it back), or, if
you really want a fresh import, move the state database aside and run
`ttcal sync --dry-run` first to see what would happen. Do not delete the state
database casually: tombstones live there.

### `N of M mirrored events vanished at once; refusing to hide records`

More than `max(3, M/2)` paired events disappeared in one pass. This protects
against a broad accidental deletion (or a calendar that was replaced). Nothing
was written: the records are still visible in TimeTagger.

Check the calendar first. If the calendar was recreated or temporarily empty,
fix that and let the next pass run; the guard clears by itself once the events
are back. If the deletion was intentional, re-run the pass with the override:

```sh
ttcal sync --allow-mass-deletion
```

For the service, stop it, run the command above once, then start it again:

```sh
supervisorctl stop ttcal
ttcal sync --allow-mass-deletion
supervisorctl start ttcal
```

`ttcal run` accepts the same flag, so a supervised process can carry it
permanently if mass deletion is part of normal operation. Use it deliberately:
it hides every affected record. Always inspect `ttcal status` afterwards.

### `cannot create event …: 409 … no-uid-conflict`

Radicale rejects two events with the same UID in one collection. ttcal mints a
fresh `ttcal-<uuid4>` UID for every event it creates, so this normally means a
client duplicated an event while keeping the UID. Delete the duplicate in the
calendar (it has no record pairing yet) and create it again through the client's
normal duplicate command.

### The calendar is empty, but `status` shows pairs

The CalDAV URL or the calendar display name changed, so the bridge is looking at
a different collection. The safety stop above usually fires first. Point the
config back at the original calendar; `ttcal repair --rebuild` can re-pair if
the database was lost.

### Apple Calendar does not show the calendar

- Use a dedicated subdomain root. CalDAV discovery does not work reliably under
  a path prefix such as `https://host/caldav/`.
- Remove and re-add the account; Apple caches discovery results.
- Check that `/.well-known/caldav` on the host redirects to the CalDAV root.
- Radicale behind a reverse proxy: forward `Host`, `X-Forwarded-Proto` and
  `X-Forwarded-For`, and allow `PROPFIND`, `REPORT`, `MKCALENDAR`, `PUT` and
  `DELETE` methods.

### Events do not refresh immediately

CalDAV clients poll on their own schedule (Apple often every 15 minutes). Force
a refresh (⌘R in Calendar.app) or use `ttcal sync` to push the TimeTagger side
immediately. ttcal's own `poll_interval` only controls how often it looks for
changes.

## Mirroring behaviour that looks wrong

### An old record is missing from the calendar

Only records inside `backfill_days` are imported on the first pass; older ones
appear when they change. Raise `backfill_days` and wait one pass to import more
history.

### An event did not become a record

Run `ttcal status` and check the `skipped` list. Typical reasons: all-day event,
recurring event, missing UID, or a title starting with `HIDDEN`. Skipped events
are left untouched. `ttcal -v sync` logs the same reasons.

### The title in TimeTagger is cut off

TimeTagger descriptions are 255 characters; ttcal truncates longer summaries and
logs a warning. Keep the important part (including `#tags`) at the front.

### A deleted event still exists in TimeTagger

It is hidden, not gone — that is what makes restore possible. TimeTagger hides
`HIDDEN`-prefixed records from totals and the normal view. Hard-delete it in
TimeTagger only if you will not restore it.

### Restoring an event created a duplicate

The event was restored by creating a *new* event instead of un-hiding the record,
so it got a new UID. Un-hide the record instead (remove the `HIDDEN ` prefix);
ttcal then recreates the event with the original UID. If duplicates already
exist, keep one pair and delete the other event, which hides its record.

### Times shift after a timezone change

Times are stored in UTC, like TimeTagger itself. An event created in
Europe/Amsterdam keeps its absolute instant and appears at a different local
time after you travel. This is inherited behaviour, not a ttcal bug.

### An event is scheduled in the future and already counts in TimeTagger

A future event is a future record by design (1:1 mirroring). If you plan in the
calendar ahead of time, either accept that, or keep planning events in a
different calendar than `TimeTagger`.

## Conflicts

`ttcal status` shows the conflict count and the five most recent entries; the
`conflicts` table keeps the rest. Each entry records the winner, the applied
value and the discarded value. If an important edit lost, re-apply it on the
winning side.

One cause of surprising conflicts is a client that rewrites `DTSTAMP` on every
sync. ttcal prefers `LAST-MODIFIED` when present and falls back to `DTSTAMP`
only for events that never had one.

## State database

### `unable to open database file`

The directory containing `state_db` is not writable by the service user, or the
volume is not mounted. The parent directory is created automatically, but the
service account needs write permission on it. For Docker, set
`state_db = "/data/ttcal.db"` and mount a writable volume at `/data`.

### `database is locked`

Two ttcal processes are using the same database (or a backup tool holds a lock).
Run one instance per state database and calendar. WAL mode is enabled, so
readers do not block the sync.

### Can I delete the database and start over?

It works, but you lose pairings, tombstones and conflict history. On the next
pass every visible record inside the horizon and every event become new pairs;
an event that already corresponds to a record will create a **second record**.
Prefer `ttcal repair --rebuild`. If you must start over, take the calendar out
of service first, or move the state file aside and use `sync --dry-run` to
inspect the result.

## Logs

`ttcal run` logs to stdout/stderr, so your service manager collects it:

- systemd: `journalctl -u ttcal -f`
- supervisord: `supervisorctl tail -f ttcal stderr`
- Docker: `docker logs -f ttcal`

One line per pass with changes, and one line per error, is normal. `-v` (or
`log_level = "DEBUG"`) is for diagnosis only; it logs every pair decision.

If a pass has errors, ttcal deliberately **does not advance its cursors**, so
the same work is retried next pass. Fix the cause; the next pass catches up.
