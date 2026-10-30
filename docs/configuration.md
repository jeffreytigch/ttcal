# Configuration

ttcal reads one TOML file. The default path is `~/.config/ttcal/ttcal.toml`;
every command accepts `-c/--config PATH`.

Start from [`ttcal.example.toml`](../ttcal.example.toml):

```bash
mkdir -p ~/.config/ttcal
cp ttcal.example.toml ~/.config/ttcal/ttcal.toml
chmod 600 ~/.config/ttcal/ttcal.toml
```

## Secret handling

Secrets can be written inline or read from a separate file:

| Inline | File | Rule |
|--------|------|------|
| `token` | `token_file` | Exactly one of the two. |
| `password` | `password_file` | Exactly one of the two. |

- If the config contains **any** inline secret, the config file itself must not
  be readable by group or others. `ttcal` refuses to start otherwise and tells
  you to run `chmod 600`.
- A `*_file` must exist, must not be empty, and must also be readable only by
  its owner.
- Inline secrets in a file that is checked into version control are a mistake.
  Use `*_file` with a secret store, or keep the config at mode `0600`.

Paths in `token_file`, `password_file` and `state_db` may be absolute, start with
`~`, or be relative to the directory containing the config file.

Unknown keys are ignored, so a config can carry comments and future keys without
breaking older versions. Invalid values stop startup with exit code `2`.

## `[timetagger]`

| Key | Required | Default | Meaning |
|-----|----------|---------|---------|
| `api_url` | yes | — | API base URL, ending in `/api/v2`. A trailing slash is stripped. Self-hosted instances often use `https://host/timetagger/api/v2`. |
| `token` | one of | — | Personal API token, sent in the `authtoken` header. Recommended. |
| `token_file` | one of | — | File containing the token. |
| `username` | alternative | — | TimeTagger username; used with `password` when no token is given. |
| `password` | alternative | — | Password for `username`. |
| `password_file` | alternative | — | File containing the password. |

Give either a token, or username plus password. The password login exchanges
credentials through `POST /bootstrap_authentication`; the resulting JWT is kept
in memory only.

## `[caldav]`

| Key | Required | Default | Meaning |
|-----|----------|---------|---------|
| `url` | yes | — | Base URL of the CalDAV server. A trailing slash is added. For Radicale this is the server root, e.g. `https://cal.example.com/`. |
| `username` | yes | — | CalDAV user. |
| `password` | one of | — | CalDAV password or app password. |
| `password_file` | one of | — | File containing the password. |
| `calendar` | no | `TimeTagger` | Display name of the calendar to mirror. Created on the first sync if missing. |

ttcal discovers the calendar collection under the given URL and matches it by
display name. It never renames the calendar and never moves events between
calendars. Use one dedicated calendar per ttcal instance.

If the named calendar is missing but the state database already contains
pairings, ttcal stops with an error instead of creating a new calendar. That
prevents an accidental full re-import when a calendar is renamed or a server is
misconfigured.

## `[ttcal]`

| Key | Default | Meaning |
|-----|---------|---------|
| `state_db` | `ttcal.db` | SQLite file for pairs, cursors, conflicts and skips. Relative to the config directory. Parent directories are created. Back this up before upgrades. |
| `backfill_days` | `90` | How far back the **first** sync looks. `0` means "start from now". Raising it later is supported: the next pass loads the wider window and keeps existing pairs. After the first pass every change is mirrored regardless of record age. |
| `poll_interval` | `60` | Seconds between passes in `ttcal run`. Minimum `5`. |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. `-v` forces `DEBUG`. |

## Complete example

```toml
[timetagger]
api_url = "https://time.example.com/timetagger/api/v2"
token_file = "~/.config/ttcal/timetagger-token"

[caldav]
url = "https://cal.example.com/"
username = "ttcal"
password_file = "~/.config/ttcal/caldav-password"
calendar = "TimeTagger"

[ttcal]
state_db = "/var/lib/ttcal/ttcal.db"
backfill_days = 90
poll_interval = 60
log_level = "INFO"
```

## What lives in the state database

The database is required for incremental syncing; it is not a cache you can
delete casually.

- `pairs` — record key ↔ event UID, event href, last synced canonical hashes and
  state (`active`, `tombstone_record`, `tombstone_event`), plus the last known
  record fields. Tombstones are what allow a restore to reuse the same UID.
- `cursors` — TimeTagger `server_time` cursor and the CalDAV sync token, plus the
  last successful sync time.
- `conflicts` — both versions of every conflict, newest first.
- `skips` — events that cannot be represented (all-day, recurring, no UID,
  `HIDDEN`-prefixed title), with the reason.

Everything can be inspected with `ttcal status`; there is no need to open the
file. If it is lost, `ttcal repair --rebuild` re-pairs existing records and
events by exact `(start, stop, description)` match but cannot restore conflict
history or tombstones.

## Environment variables

ttcal has no environment-variable overrides; the config file is the single
source of truth. This keeps a supervised service reproducible and makes
permission mistakes visible. For container deployments, mount the config file
read-only and point `state_db` at a writable volume.
