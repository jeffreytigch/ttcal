# ttcal — implementation plan

Two-way mirror between a self-hosted CalDAV calendar (Radicale) and TimeTagger.
Every calendar Event *is* a TimeTagger Record; edits, deletions and restores work from either side.
Open source, lean, single Python service.

Status: agreed with the owner on 2026-10-30. Durable record of the design session; keep it current as phases complete.

---

## 1. Definition of done

1. **Mirror works end to end**: backfill, create, edit, delete, restore, conflict resolution and skipped-event reporting all verified against a real Radicale + real TimeTagger API, in both directions.
2. **Automated tests**: unit tests for mapping/hashing/validation, plus an integration suite that runs against Docker-hosted Radicale and TimeTagger (skipped when Docker is absent). Green in CI.
3. **Open-source docs**: `README.md`, `docs/install.md`, `docs/configuration.md`, `docs/usage.md`, `docs/troubleshooting.md` — vendor-neutral, with Uberspace, systemd and Docker as examples. No secrets, no Uberspace-only assumptions.
4. **Deployed**: running as a supervised service, able to survive restart, catch up after downtime, and report its own state via `ttcal status`.
5. **Model intact**: `CONTEXT.md` and `docs/adr/` describe what the code actually does.

---

## 2. Decisions already made

| # | Decision | Why (short) | Record |
|---|----------|-------------|--------|
| D1 | An Event *is* a Record, mirrored 1:1. No plan/fact split. | One artifact; delete/edit round-trips; lean. | ADR 0001 |
| D2 | No recurrence. Duplicate Events instead; recurring Events are skipped. | Series-edit semantics and materialisation windows are not worth it; real times differ anyway. | ADR 0002 |
| D3 | Radicale is the calendar store, deployed outside this repo. This repo ships only the bridge. | Own data, no OAuth, works with Apple/Thunderbird/DAVx5. | ADR 0003 |
| D4 | Deletion = `HIDDEN` on the Record side; Event removal + Tombstone on the calendar side. Restorable both ways. | TimeTagger has no hard delete; protects tracked time. | ADR 0004 |
| D5 | Backfill horizon (default 90 days) on first run, then mirror every change regardless of age. | Avoid dumping years of history into a phone calendar; keep the invariant for anything touched. | `CONTEXT.md` |
| D6 | Conflicts: latest edit wins (Record `mt` vs Event `DTSTAMP`/`LAST-MODIFIED`), tie → calendar; both versions logged. | Same-host clocks are sane; nothing is lost silently. | this file |
| D7 | Running Records (start == stop) are invisible until stopped. | Zero churn, no lying blocks. | this file |
| D8 | Title → Record description verbatim (tags stay inline as `#tag`). Notes/location/URL stay calendar-only. | Matches CLI muscle memory; TimeTagger gives one 255-char line. | this file |
| D9 | One calendar, named `TimeTagger`. Never move items between calendars. | Cross-calendar moves are the least reliable CalDAV operation. | this file |
| D10 | Bridge talks to the TimeTagger **HTTP API** directly; `timetagger_cli` is not a dependency. | Fewer dependencies, full field control. | this file |
| D11 | Poll every 60s by default. | Cheap; Apple still refreshes on its own ~15 min cycle. | this file |

Accepted consequences (documented in the README, not bugs):
- A future-dated Event is a future-dated Record and counts in TimeTagger totals.
- Editing a past Event rewrites tracked history.
- Calendar notes do not survive into TimeTagger.
- No mass "every weekday" creation; you duplicate.

---

## 3. Architecture

```
Apple Calendar / iOS / Thunderbird / DAVx5
              │ CalDAV (TLS terminated by host/reverse proxy)
              ▼
        Radicale  (separate deployment, this repo only talks to it)
              │ CalDAV, sync-token + ETags
              ▼
        ttcal bridge (one process, SQLite sidecar, 60s loop)
              │ TimeTagger HTTP API: GET /updates, GET /records, PUT /records
              ▼
        TimeTagger server
```

Failure model: the bridge is stateless apart from SQLite. All cursors are persisted **after** an item is successfully applied, so a crash re-processes at worst one poll. Both sides are idempotent by content hash, so replays are harmless.

### 3.1 Repository layout

```
ttcal/
├── CONTEXT.md                    glossary (no implementation detail)
├── README.md                     what/why/quickstart/architecture
├── LICENSE                       open item: owner to pick (MIT proposed)
├── pyproject.toml                package + deps + entry point `ttcal`
├── docs/
│   ├── plan.md                   this file
│   ├── adr/                      design decisions
│   ├── install.md                pipx/uv/systemd/docker, Uberspace as example
│   ├── configuration.md          every option, defaults, examples
│   ├── usage.md                  connect Apple/iOS/Android, editing semantics
│   └── troubleshooting.md        conflicts, duplicate UID, resync, hidden records
├── dev/
│   ├── docker-compose.yml        timetagger + radicale for local work
│   ├── radicale/{config,users}   dev-only credentials
│   └── seed.py                   creates a few records/events for manual testing
├── src/ttcal/
│   ├── config.py                 TOML load, validation, 0600 check
│   ├── ttapi.py                  TimeTagger client (updates/records/PUT)
│   ├── calapi.py                 CalDAV wrapper (sync, put, delete, ensure calendar)
│   ├── model.py                  Event↔Record mapping, canonical hashes, validation
│   ├── store.py                  SQLite: pairs, cursors, conflicts, skips
│   ├── sync.py                   reconcile loop, conflict resolution, tombstones
│   └── cli.py                    run | sync | status | repair | doctor
└── tests/
    ├── fakes/timetagger.py       in-process stub of /updates and /records
    ├── test_model.py
    ├── test_store.py
    ├── test_sync.py
    └── test_integration.py       marker `docker`, real Radicale container
```

### 3.2 Stack

- Python ≥ 3.11, `pyproject.toml` with `uv` for development.
- Dependencies: `caldav` (CalDAV client, sync-token support), `vobject` (iCalendar), `requests` (HTTP, already transitive). Nothing else; config with stdlib `tomllib`, CLI with `argparse`, state with stdlib `sqlite3`.
- No web framework, no ORM, no queue. One loop, one process.
- If `caldav`'s sync-token support proves unreliable, fall back to ETag diffing via `calendar-query` REPORT; the store already keys on `href` + `etag`, so the swap is contained in `calapi.py`.

### 3.3 State schema (SQLite)

```sql
CREATE TABLE pairs (
  record_key TEXT PRIMARY KEY,
  uid        TEXT UNIQUE NOT NULL,
  href       TEXT,
  record_hash TEXT NOT NULL,   -- canonical hash of (t1,t2,ds) as last synced
  event_hash  TEXT NOT NULL,   -- canonical hash of (dtstart,dtend,summary) as last synced
  etag       TEXT,
  state      TEXT NOT NULL,    -- active | tombstone_record | tombstone_event
  meta       TEXT,             -- JSON: last known t1,t2,ds (survives tombstones)
  updated_at INTEGER NOT NULL
);

CREATE TABLE cursors (name TEXT PRIMARY KEY, value TEXT NOT NULL);
-- 'timetagger_since' = server_time from /updates
-- 'caldav_sync_token'

CREATE TABLE conflicts (
  id INTEGER PRIMARY KEY, record_key TEXT, uid TEXT, winner TEXT,
  applied TEXT, discarded TEXT, created_at INTEGER
);

CREATE TABLE skips (
  href TEXT PRIMARY KEY, uid TEXT, reason TEXT, detail TEXT, seen_at INTEGER
);
```

Canonical hashes are the change detector; they must exclude fields the bridge itself does not own (notes, location, ETag churn, `st`).

### 3.4 Sync pass

1. `GET /updates?since=<timetagger_since>` → changed Records (including `HIDDEN` ones).
2. CalDAV change feed: full listing on first run, `sync-token` afterwards → changed/deleted `href`s.
3. For each touched item, load its pair, recompute hashes, classify:
   - only Record changed → push to calendar;
   - only Event changed → push to TimeTagger;
   - both changed → conflict (D6);
   - neither → drop stale pair reference.
4. Apply the winning version, then store the new baseline hash and cursor **in one transaction per item**.
5. Unknown Event (no pair) → create Record with a freshly generated key; unknown Record → create Event. Unrepresentable Event → `skips` + log, Event left untouched.

Echo suppression: after any write, record the hash of what the server accepted (re-read the item when the API does not return it), so the next poll sees "unchanged".

---

## 4. Mapping and validation rules

| Input | Rule |
|-------|------|
| Event summary | Becomes `ds` verbatim, tags included. Empty summary → empty `ds`. |
| Summary > 255 chars | Truncate to 255 with `…`, keep the full title in the calendar, warn in `status`. |
| Summary starting with `HIDDEN` | Refuse (skip + warn): TimeTagger would read it as deleted. |
| Notes / location / URL | Calendar-only. Never written to TimeTagger, never overwritten when pushing a Record change. |
| All-day Event (`VALUE=DATE`) | Skipped (`reason=all-day`). |
| Recursive Event (`RRULE`/`RDATE`/`EXDATE`) | Skipped (`reason=recurring`). |
| Multi-day Event | Allowed; TimeTagger only flags >48h in `diagnose`. |
| Zero/negative duration | Clamp to 60s block on the calendar; a Record never gets `t2 < t1`. |
| Running Record (`t1 == t2`) | No Event until stopped (D7). |
| Hidden Record | Delete Event, keep Tombstone `tombstone_record`. |
| Record resurrected | Recreate Event with the stored `uid`. |
| Event deleted | Hide Record (`HIDDEN ` prefix), keep Tombstone `tombstone_event`. |
| Record un-hidden | Recreate Event with stored `uid`. |
| Calendar deleted from server | Refuse: log error, no mass-hide. |
| Duplicate `UID` in one calendar | Bridge mints its own `uid` on upload (fallback from Phase 0). |
| Time representation | Store/transfer UTC (`Z`). Apple renders local; matches TimeTagger's absolute timestamps. |
| `DTEND` missing | Use `DURATION`; if both missing, 60s. |

Identity: the bridge generates the Record `key` (UUID4) and the Event `uid` (`ttcal-<uuid4>`); the pairing lives only in SQLite. No data is smuggled through the UID or the description.

Recovery: `ttcal repair --rebuild` reconstructs `pairs` by matching exact `(t1,t2,ds)`; unmatched items are reported, never deleted.

---

## 5. Development environment

`dev/docker-compose.yml`:

```yaml
services:
  timetagger:
    image: ghcr.io/almarklein/timetagger
    ports: ["127.0.0.1:1081:80"]
    volumes: ["./timetagger:/root/_timetagger"]
    environment:
      - TIMETAGGER_BIND=0.0.0.0:80
      - TIMETAGGER_DATADIR=/root/_timetagger
      - TIMETAGGER_LOG_LEVEL=info
      - TIMETAGGER_CREDENTIALS=test:$$2a$$08$$... # test:test
  radicale:
    image: tomsquest/docker-radicale
    ports: ["127.0.0.1:5232:5232"]
    volumes: ["./radicale/data:/data", "./radicale/config:/config:ro"]
```

- API base for dev: `http://localhost:1081/timetagger/api/v2/` (the image serves the app under `/timetagger/`); CalDAV base: `http://localhost:5232/`; dev user `test:test`.
- `make dev-up`, `make dev-down`, `make dev-reset` (wipes volumes), `make seed` (runs `dev/seed.py`).
- Docker daemon must be running locally (Docker Desktop or colima); CLI + Compose are already installed.
- Tests: `pytest` (unit, no Docker) and `pytest -m docker` (integration, skipped if the daemon is down).
- Never point tests at the live instance. The owner runs Phase 5 acceptance against their own deployment.

---

## 6. Phases

Each phase ends with recorded evidence in this file (or the goal record) and a green test run.

### Phase 0 — Spikes and dev environment
Objective: remove the unknowns that could invalidate the design, and have a reproducible Docker environment.
Tasks:
1. Start Docker; bring up `dev/docker-compose.yml`; confirm both services answer (TimeTagger UI on 1081, Radicale on 5232).
2. Connect **real Apple Calendar** to the dev Radicale; create, edit, duplicate and delete a timed event.
3. Answer and record: does *Duplicate* arrive with a fresh `UID`? What does Radicale do when it does not? What does Apple send for `DTSTART`/`DTEND` (TZID vs Z)?
4. Verify the TimeTagger API contract with a scratch script: `/updates?since=`, `PUT /records` create + update, hiding and un-hiding, running record semantics.
5. Decide the UID strategy based on 3 (likely: bridge-minted UIDs).
Verification: a written note in this file with the four answers, plus a screenshot-free "works" check of each API call.

### Phase 1 — Read path (TimeTagger → calendar)
Objective: Records in the backfill horizon become Events, safely and repeatably.
Tasks: config loader + validation; `ttapi.py`; `calapi.py` (discovery, ensure calendar, put, delete); `store.py`; backfill; `ttcal sync --dry-run` and `ttcal doctor`.
Verification: unit tests for mapping/hashing; integration test creating N records in the fake/docker TimeTagger and asserting N events; dry run against docker env produces no writes.

### Phase 2 — Write path (calendar → TimeTagger)
Objective: create and edit Records from Events, with all validation rules of §4.
Tasks: Event→Record mapping; create/update; truncation + `HIDDEN` guard; skipped-event reporting; `ttcal status`.
Verification: integration test — create/move/retitle an Event via CalDAV and assert the Record; all-day/recurring events appear in `skips` and produce no Record.

### Phase 3 — Deletion, restore, conflicts, running timers
Objective: the destructive and racy paths behave as decided.
Tasks: hide/unhide; tombstones; calendar-deletion refusal; conflict detection + latest-wins + conflict log; running-record materialisation on stop; `ttcal repair --rebuild`.
Verification: round-trip tests for delete→restore on each side; a forced conflict test asserting the winner and both logged versions; running record invisible, then present with exact bounds after stop.

### Phase 4 — Packaging and open-source documentation
Objective: a stranger can install, configure and run this.
Tasks: `pyproject.toml`, entry point, versioning; `README.md` (what it is, why, architecture diagram, quickstart); `docs/install.md` (pipx/uv, Docker, systemd unit, **Uberspace example** as one option among others); `docs/configuration.md` (every key, default, secret handling, file permissions); `docs/usage.md` (connect Apple Calendar/iOS/Thunderbird/DAVx5, editing semantics, what is skipped and why, future events caveat); `docs/troubleshooting.md`; `LICENSE`; CI running unit tests + docker integration; `.gitignore` for dev data.
Verification: fresh clone → documented quickstart → working mirror on a clean machine/container; docs contain no host-specific paths except clearly marked examples.

### Phase 5 — Deployment and acceptance on Uberspace
Objective: it runs unattended on the real instance.

> **Handover (2026-10-30):** the owner deploys and runs acceptance personally;
> no live system was changed from this workspace. The repository provides the
> runbook ([deploy-uberspace.md](deploy-uberspace.md)) and the checklist
> ([acceptance.md](acceptance.md)).
Tasks: Radicale reachable on its own host (subdomain root, so Apple's `.well-known` discovery works); bridge as a supervisord service with restart; secrets in `0600` file; logs; add the calendar to macOS + iOS; run the acceptance checklist below.
Verification (acceptance checklist):
1. Record created via CLI yesterday → Event exists.
2. Event created for yesterday 09:00–10:00 → Record with matching bounds.
3. Event retitled → Record description updated (tags intact).
4. Event deleted → Record hidden; Record un-hidden → Event returns with same UID.
5. Record hidden via web UI → Event disappears; un-hidden → Event returns.
6. Running timer → no Event; after stop → exact Event.
7. All-day + recurring Events → no Records, both listed in `status`.
8. Bridge stopped for an hour, then started → no duplicates, no lost edits.
9. Conflict: edit both sides within one poll → later edit wins, both versions in conflict log.

---

## 7. Open items

- **License**: owner to choose; MIT proposed.
- **Repo name/package name**: `ttcal` assumed.
- **Apple duplicate UID**: resolved in Phase 0; fallback already specified.
- **Whether Radicale's sync-token is exposed through `caldav`**: verify in Phase 1; ETag fallback specified.
- **Timezone travel**: UTC storage means historical Events shift when you change timezone, exactly as TimeTagger does. Documented, not fixed.

## 8. Non-goals

No web UI (status is CLI/log), no recurrence expansion, no per-tag calendars, no multi-user, no attendee/reminder/invite handling, no natural-language entry, no reporting, no mobile app, no hosting of Radicale or TimeTagger in this repo.

---

## Phase 0 evidence (2026-10-30)

Dev stack: `make dev-up`; TimeTagger on `http://localhost:1081/timetagger/` (the image mounts the app under `/timetagger/`, so the API base is `http://localhost:1081/timetagger/api/v2/`); Radicale on `http://localhost:5232/`, user `test:test`. Host port 1080 was already taken by an unrelated container, hence 1081.

1. **Both services answer**: `GET /timetagger/` → 200, `GET /` on Radicale → 302 to `/.web/` → 200, authenticated `PROPFIND /` → 207.
2. **Radicale and duplicate UIDs**: creating a second object with an existing `UID` in the same collection returns **409** with `<C:no-uid-conflict/>`. So the bridge must never reuse a UID, and a client that duplicates without minting a new UID will fail to upload. Bridge mints `ttcal-<uuid4>` UIDs (fallback from §4 adopted as the rule).
3. **Apple duplicate behaviour**: not verifiable headlessly — Calendar's AppleScript `duplicate` refuses events ("Events can not be copied", -1717) and UI automation needs Accessibility permission. Deductive evidence: Apple's Duplicate command works against iCloud, which also enforces per-calendar UID uniqueness, so it must mint a new UID. **Still to confirm manually on a real device in Phase 5**; the bridge is unaffected either way because it mints its own UIDs.
4. **TimeTagger API contract** (verified with a scratch script against the dev instance, v26.1.3):
   - `POST /bootstrap_authentication` with a base64-encoded JSON body `{"method":"usernamepassword","username":...,"password":...}` returns `{"token": <jwt>}`; the JWT goes in the `authtoken` header. This removes the need for the CLI.
   - `GET /updates?since=<float>` returns `{server_time, reset, records, settings}`; `reset` is truthy when `since` is older than the last reset and the full dump is returned; an early exit returns `reset: 0` with empty lists.
   - `GET /records` **requires** `timerange=<t1>-<t2>`; `hidden`/`running` are optional tri-state flags; hidden detection is `ds LIKE 'HIDDEN%'`.
   - `PUT /records` takes a list, returns `{accepted, failed, errors}`. The server casts `t1`, `t2` and `mt` to **integer seconds** and sets `st` itself (monotonic). An incoming item is ignored only when the stored `mt` is *strictly* greater; equal `mt` overwrites, so same-second edits are last-write-wins at one-second resolution.
   - Hiding is purely the `HIDDEN` prefix on `ds`; the record stays in `/records` and appears in `/updates`.
   - A running record has `t1 == t2` and is returned with `running=true`; it is otherwise a normal record.

---

## Phase 1–3 evidence (2026-10-30)

Implementation: `src/ttcal/{config,model,store,ttapi,calapi,sync,cli}.py`, tests in `tests/`. State database schema matches §3.3 plus the `meta` column (last known `t1`, `t2`, `ds`, needed to recreate an Event after a tombstone). Radicale exposes a working sync-token (`http://radicale.org/ns/sync/...`), so `calapi.changes()` uses `get_objects_by_sync_token()`; the ETag fallback remains in place when the server returns none.

Verified manually against the Docker stack and covered by the test suite:

1. **Read path (Phase 1)**: first sync backfills the horizon; every visible, stopped Record becomes exactly one Event. A second sync is a no-op (no writes, cursor advanced). `sync --dry-run` opens/creates nothing and writes nothing. Hidden Records and running Records produce no Event.
2. **Write path (Phase 2)**: an Event created through CalDAV becomes a Record (`t1`/`t2` rounded to integer seconds); moving and retitling an Event updates the Record; tags stay inline in `ds`. All-day, recurring and `HIDDEN`-prefixed Events are skipped with a reason, leave no Record, and appear in `ttcal status`. A summary over 255 characters is truncated to 255 (ending in `…`) with a logged warning.
3. **Deletion and restore (Phase 3)**: Event delete → Record hidden with the `HIDDEN ` prefix (`state=tombstone_event`); un-hiding the Record recreates the Event with the same UID. Record hide → Event deleted (`state=tombstone_record`); un-hiding recreates it with the same UID. A mass deletion (more than `max(3, active//2)` of at least four paired Events in one pass) is refused, and a missing calendar with existing pairs is refused instead of re-created.
4. **Conflicts**: when both sides changed, the later edit wins (`mt` vs `LAST-MODIFIED`/`DTSTAMP`, tie → calendar); both versions are stored in `conflicts` and summarised by `ttcal status`. A deleted Event beats a Record edit and the discarded Record version is logged.
5. **Running timers**: invisible while `t1 == t2`; the Event appears with the exact bounds after the timer stops.
6. **Timezone serialisation**: `vobject` only recognises its own UTC tzinfo, so `model._utc()` uses `vobject.icalendar.utc`; Events serialise as `DTSTART:...Z` and parse back to the same instants.
7. **TimeTagger merge rule**: a `PUT /records` whose `mt` is older than the stored one is silently ignored. The bridge always re-reads after writing and stores the accepted values as its baseline, so this cannot cause a silent divergence.

Test suite: `pytest -q` → **67 passed** (51 unit tests for model, store and config; 16 Docker integration tests). Integration tests use a unique `ttcal-test-<hex>` calendar, a temporary state database and a temporary 0600 config; they are skipped with an actionable message when the dev stack is down and refuse non-local targets unless `TTCAL_TEST_ALLOW_REMOTE=1`. Both directions of every path above are exercised, including dry-run, idempotency, hidden records, running records, both conflict winners, truncation, skipped Events, mass-deletion refusal, the missing-calendar refusal and backfill widening.

---

## Phase 4 evidence (2026-10-30)

Packaging and documentation are in place; every artefact below is in the repository and was exercised.

1. **Packaging**: `pyproject.toml` (hatchling, `ttcal` console script, `docker` pytest marker, dev extra), `uv.lock`, `LICENSE` (MIT), `.gitignore`, `.dockerignore`. `uv build` produces `dist/ttcal-0.1.0.tar.gz` and a wheel; a clean venv with the wheel installed answers `ttcal --version` and `ttcal --help`.
2. **Container image**: `Dockerfile` (Python 3.13-slim, non-root uid 10001, `/config` read-only, `/data` for state). `docker build` succeeds, and the image ran `doctor` successfully against the Docker dev stack (`ok timetagger: version 26.1.3`, `ok caldav`), proving the image has all runtime dependencies and respects the config/data layout.
3. **Configuration example**: `ttcal.example.toml` documents every key, both secret forms and the permission rule; it matches `config.py` one-for-one.
4. **Documentation**, vendor-neutral with Uberspace only as one example:
   - `README.md` — purpose, architecture diagram, requirements, quickstart, command reference, mapping table, accepted trade-offs, development and licence.
   - `docs/install.md` — prerequisites, pipx/uv/venv/Docker install, Radicale quickstart, reverse proxy (Caddy/nginx), Apple discovery constraint, Uberspace example, systemd unit and supervisord example, verification, upgrade.
   - `docs/configuration.md` — every key, defaults, secret handling, state database contents, no environment overrides.
   - `docs/usage.md` — Apple/iOS/Thunderbird/DAVx5 setup, edit mapping, delete/restore, running timers, skips, recurrence, conflicts, status output, backfill widening, repair, operations.
   - `docs/troubleshooting.md` — real error strings from the code, CalDAV/Apple discovery, safety stops, state database, logs.
5. **CI**: `.github/workflows/ci.yml` — unit tests on Python 3.11/3.12/3.13 plus a package build; a Docker job with TimeTagger and Radicale service containers, a readiness wait and `TTCAL_TEST_REQUIRE_STACK=1` so a dead stack fails the job instead of skipping silently.
6. **Test guard**: integration tests now fail instead of skipping when `TTCAL_TEST_REQUIRE_STACK` is set; locally they still skip with `run: make dev-up`.

Local verification: `uv run pytest -q` → **67 passed**; `uv run pytest -q -m "not docker"` → **51 passed**; `uv run ruff check src tests` and `ruff format --check` clean; `docker build` and the container `doctor` run both succeed.

---

## Phase 5 readiness (2026-10-30)

Deployment was handed to the owner by explicit instruction ("Don't connect to
uberspace to test it out. I will follow the guide myself to implement this into
production."). No live service, calendar or TimeTagger record was changed from
this workspace.

Prepared for that deployment:

- `docs/deploy-uberspace.md` — ordered runbook: Radicale virtualenv and config,
  bcrypt password file, supervisord unit, subdomain at the web-backend root
  (CalDAV discovery requirement), ttcal virtualenv, private token/password
  files, dry run before the first real pass, supervisord unit, upgrade and undo.
  It calls out that the account's system Python may be older than 3.11 and that
  a suitable interpreter must be used for both virtualenvs.
- `docs/acceptance.md` — the nine-point checklist from §6 plus an optional Apple
  Duplicate/UID check, each with setup, pass criteria and a results table.
- `docs/troubleshooting.md` — the failure modes the checklist is likely to hit
  (discovery, 401, missing calendar refusal, mass-deletion guard, lock files).

Still to be done by the owner, and therefore not claimed as verified here:

1. Deploy Radicale and ttcal on the account; confirm `ttcal doctor` and
   `ttcal status` on the server.
2. Run the acceptance checklist and record the results (the table in
   `docs/acceptance.md` is the intended place).
3. Confirm the Apple Duplicate UID behaviour (open item from Phase 0).
4. Publish the repository and choose the final licence (MIT is in place;
   replace `LICENSE` and the `pyproject.toml` licence field if another licence
   is preferred).
