# ttcal

Two-way mirror between [TimeTagger](https://timetagger.app) records and a CalDAV
calendar. Every calendar event *is* a TimeTagger record.

Create, edit, delete and restore events in Apple Calendar, iOS, Thunderbird,
DAVx5 or any other CalDAV client. Changes made in the TimeTagger web app or CLI
appear in the calendar on the next pass. TimeTagger stays the source of truth for
tracked time; the calendar becomes a second, comfortable interface to it.

```
┌────────────────────┐        ┌──────────────────────┐        ┌────────────────────┐
│  TimeTagger        │  HTTP  │        ttcal         │ CalDAV │  Radicale (or any  │
│  web app / CLI     │◄──────►│  one poll loop       │◄──────►│  CalDAV server)    │
│  records + /updates│  API   │  + SQLite sidecar    │        │  calendar "TimeTagger"
└────────────────────┘        └──────────────────────┘        └────────────────────┘
                                                                      ▲
                                                           Apple Calendar, iOS,
                                                           Thunderbird, DAVx5 …
```

## Why

TimeTagger is excellent at tracking time but limited as a planner: recurring
blocks have to be created one by one, and creating entries through a CLI is
awkward when you already live in a calendar. ttcal makes the calendar the
editing surface without giving up TimeTagger's reports and totals.

## What it does

- **Backfills** the last 90 days (configurable) of TimeTagger records as events.
- **Mirrors both ways**: a new or edited event becomes a new or edited record,
  and the other way round, within one poll interval (60 s by default).
- **Deletes and restores in both directions**, without destroying tracked time:
  deleting an event hides the record (`HIDDEN` prefix, TimeTagger's own
  convention); un-hiding the record recreates the event.
- **Resolves conflicts** by latest edit (record `mt` vs event `LAST-MODIFIED` /
  `DTSTAMP`), keeps both versions in a conflict log, and prefers the calendar on
  a tie.
- **Skips what it cannot represent** — all-day events, recurring events, events
  without a UID — and lists them in `ttcal status`.
- **Refuses dangerous operations**: a missing calendar with existing pairs, or a
  mass deletion of events, stops the pass instead of wiping history.
- **Keeps its own state** in a small SQLite file so restarts and downtime cause
  no duplicates and no lost edits.

## Requirements

- Python 3.11 or newer (3.12/3.13 recommended), or Docker.
- A TimeTagger instance and an API token (or username and password).
- A CalDAV server. [Radicale](https://radicale.org/) is the tested choice and is
  what the documentation assumes; any standards-compliant CalDAV server with
  calendar-level sync-token or ETag support should work.
- No `timetagger_cli` dependency: ttcal talks to the TimeTagger HTTP API.

## Quickstart

```bash
git clone https://github.com/jeffreytigch/ttcal.git
cd ttcal

# install the command (choose one)
uv tool install .
# pipx install .

# configure
mkdir -p ~/.config/ttcal
cp ttcal.example.toml ~/.config/ttcal/ttcal.toml
chmod 600 ~/.config/ttcal/ttcal.toml
$EDITOR ~/.config/ttcal/ttcal.toml
```

Check both connections, look before writing, then start mirroring:

```bash
ttcal doctor
ttcal sync --dry-run
ttcal sync            # first real pass, creates the calendar and backfills
ttcal run             # poll loop; use a service manager in production
```

`ttcal sync` is safe to run repeatedly; a second pass normally reports no
changes.

## Commands

| Command | What it does |
|---------|--------------|
| `ttcal run [--once]` | Poll forever (default 60 s), or run one pass and exit. |
| `ttcal sync [--dry-run]` | One pass. `--dry-run` reads both sides and writes nothing. |
| `--allow-mass-deletion` | Safety override for `run`/`sync`: allow hiding records when most mirrored events vanished at once. |
| `ttcal status` | Pair counts, last sync, cursors, recent conflicts, skipped events. |
| `ttcal doctor` | Validate config, state database, TimeTagger and CalDAV connectivity. |
| `ttcal repair --rebuild` | Rebuild pairing state by matching `(start, stop, description)` on both sides. |

All commands accept `-c/--config PATH` (default `~/.config/ttcal/ttcal.toml`)
and `-v` for debug logging.

## How events map to records

| Calendar event | TimeTagger record |
|----------------|-------------------|
| Summary/title | Description (`ds`) verbatim, including inline `#tags` |
| Start / end | `t1` / `t2` (UTC, second precision) |
| Notes, location, URL | Kept in the calendar only; never sent to TimeTagger |
| Delete | Record hidden with `HIDDEN ` prefix (reversible) |
| Restore (un-hide) | Event recreated with the same UID |

Titles are limited to 255 characters by TimeTagger; longer titles are truncated
with a warning. Titles starting with `HIDDEN` are refused because they would look
like a deleted record. Events shorter than one minute (or with an end before the
start) become one-minute records.

One calendar, named `TimeTagger` by default, is mirrored. ttcal never moves
events between calendars.

## Behaviour worth knowing

These follow from the 1:1 design and are documented rather than fixed:

- A future-dated event is a future-dated record and therefore already counts in
  TimeTagger totals.
- Editing an old event rewrites tracked history — that is the point, but it is
  not an audit log.
- Calendar notes do not reach TimeTagger; TimeTagger has one 255-character line.
- Recurring events are **not** expanded. Duplicate an event instead: each copy
  is a separate record with its own UID.
- While a timer is running (`start == stop`) no event exists. The event appears
  with exact bounds when the timer stops.
- Times are stored in UTC, like TimeTagger. Historical events shift if you change
  timezone, exactly as TimeTagger's own records do.
- Deleting a whole calendar is refused: that would look like deleting every
  event at once. Delete events individually, or stop ttcal before removing the
  calendar.
- Deleted events leave hidden records behind, which is what makes restore
  possible. Hide them permanently in TimeTagger if they clutter the UI.

## Documentation

- [Install](docs/install.md) — pipx/uv, Docker, systemd, Uberspace example.
- [Configuration](docs/configuration.md) — every key, defaults, secret handling.
- [Usage](docs/usage.md) — client setup, editing semantics, conflicts, skips.
- [Troubleshooting](docs/troubleshooting.md) — symptoms and fixes.
- [Uberspace deployment example](docs/deploy-uberspace.md) — one complete,
  optional walkthrough.
- [Acceptance checklist](docs/acceptance.md) — nine required checks plus one
  optional Apple Calendar check, to run after deploying.
- [Design and decisions](docs/plan.md), [glossary](CONTEXT.md),
  [architecture decisions](docs/adr/).

## Development

```bash
make dev-up      # TimeTagger on :1081, Radicale on :5232 (test:test)
uv sync --extra dev
uv run pytest -q             # 57 unit tests + 21 Docker integration tests
uv run pytest -q -m "not docker"
make dev-down
```

The Docker integration tests create a uniquely named calendar, use a temporary
config and database, and clean up after themselves. They refuse non-local
targets unless `TTCAL_TEST_ALLOW_REMOTE=1` is set.

## License

MIT — see [LICENSE](LICENSE).
