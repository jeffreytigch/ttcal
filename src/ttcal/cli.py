"""Command line interface: run, sync, status, doctor, repair."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from . import __version__, model
from .calapi import CalendarClient, CalendarError
from .config import Config, ConfigError, load_config
from .store import STATE_ACTIVE, Pair, Store
from .sync import CURSOR_CAL, CURSOR_TT, Syncer, SyncError
from .ttapi import TimeTaggerClient, TimeTaggerError

log = logging.getLogger("ttcal")

LAST_SYNC = "last_sync"
DEFAULT_CONFIG = "~/.config/ttcal/ttcal.toml"


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _open(config: Config) -> tuple[Store, TimeTaggerClient, CalendarClient]:
    store = Store(config.state_db)
    tt = TimeTaggerClient(
        config.timetagger_api_url,
        token=config.timetagger_token,
        username=config.timetagger_username,
        password=config.timetagger_password,
    )
    cal = CalendarClient(
        config.caldav_url,
        config.caldav_username,
        config.caldav_password,
        config.calendar_name,
    )
    return store, tt, cal


def _config(args: argparse.Namespace) -> Config:
    return load_config(args.config)


# -- commands ------------------------------------------------------------
def cmd_sync(args: argparse.Namespace, once: bool = True) -> int:
    config = _config(args).with_dry_run(bool(getattr(args, "dry_run", False)))
    if getattr(args, "allow_mass_deletion", False):
        config = replace(config, allow_mass_deletion=True)
    _setup_logging("DEBUG" if args.verbose else config.log_level)
    store, tt, cal = _open(config)
    try:
        report = Syncer(config, store, tt, cal).sync_once()
        print(report.summary())
        for action in report.actions:
            print(f"  {action}")
        for error in report.errors:
            print(f"  ERROR {error}", file=sys.stderr)
        if not report.errors and not config.dry_run:
            store.set_cursor(LAST_SYNC, str(int(time.time())))
        return 1 if report.errors else 0
    finally:
        store.close()


def cmd_run(args: argparse.Namespace) -> int:
    config = _config(args)
    if getattr(args, "allow_mass_deletion", False):
        config = replace(config, allow_mass_deletion=True)
    _setup_logging("DEBUG" if args.verbose else config.log_level)
    store, tt, cal = _open(config)
    syncer = Syncer(config, store, tt, cal)
    log.info(
        "ttcal %s: mirroring %s <-> %s [%s] every %ss",
        __version__,
        config.timetagger_api_url,
        config.caldav_url,
        config.calendar_name,
        config.poll_interval,
    )
    stop = threading.Event()

    def _stop(signum: int, frame: object) -> None:
        log.info("received signal %s, stopping after this pass", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        while not stop.is_set():
            started = time.time()
            failed = False
            try:
                report = syncer.sync_once()
                if report.actions or report.errors:
                    log.info("%s", report.summary())
                for error in report.errors:
                    log.error("%s", error)
                failed = bool(report.errors)
                if not failed:
                    store.set_cursor(LAST_SYNC, str(int(time.time())))
            except SyncError as err:
                failed = True
                log.error("%s", err)
            except (TimeTaggerError, CalendarError) as err:
                failed = True
                log.error("%s", err)
            except Exception:  # keep the daemon alive
                failed = True
                log.exception("unexpected error during sync")
            if args.once:
                return 1 if failed else 0
            elapsed = time.time() - started
            stop.wait(max(1.0, config.poll_interval - elapsed))
    except KeyboardInterrupt:  # pragma: no cover - signal handler normally wins
        log.info("stopped")
    finally:
        store.close()
    log.info("stopped")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = _config(args)
    store = Store(config.state_db)
    try:
        counts = store.counts()
        print(f"ttcal {__version__}")
        print(f"config:        {Path(args.config).expanduser()}")
        print(f"state db:      {config.state_db}")
        print(f"timetagger:    {config.timetagger_api_url}")
        print(f"calendar:      {config.calendar_name!r} at {config.caldav_url}")
        print(
            "pairs:         "
            + ", ".join(f"{value} {name}" for name, value in counts.items())
        )
        last = store.get_cursor(LAST_SYNC)
        if last:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(last)))
            age = int(time.time()) - int(last)
            print(f"last sync:     {stamp} ({age}s ago)")
        else:
            print("last sync:     never")
        since_tt = store.get_cursor(CURSOR_TT)
        if since_tt:
            print(f"tt cursor:     {since_tt}")
        print(f"caldav cursor: {store.get_cursor(CURSOR_CAL) or 'none (ETag scan)'}")
        conflicts = store.conflict_count()
        print(f"conflicts:     {conflicts}")
        for row in store.recent_conflicts(5):
            created = time.localtime(row["created_at"])
            when = time.strftime("%Y-%m-%d %H:%M", created)
            print(f"  {when}  {row['winner']} won  record={row['record_key']}")
        skips = store.list_skips()
        print(f"skipped:       {len(skips)}")
        for row in skips[:10]:
            print(f"  {row['href']}  {row['reason']}  {row['detail'] or ''}")
        return 0
    finally:
        store.close()


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _config(args)
    _setup_logging(config.log_level)
    failures = 0

    def check(label: str, fn) -> None:
        nonlocal failures
        try:
            detail = fn()
        except Exception as err:
            failures += 1
            print(f"FAIL  {label}: {err}")
        else:
            print(f"ok    {label}{': ' + detail if detail else ''}")

    print(f"config: {Path(args.config).expanduser()}")
    store = Store(config.state_db)
    try:
        check("state database", lambda: str(config.state_db))

        def tt_check() -> str:
            tt = TimeTaggerClient(
                config.timetagger_api_url,
                token=config.timetagger_token,
                username=config.timetagger_username,
                password=config.timetagger_password,
            )
            version = tt.version()
            records = tt.records(int(time.time()) - 86400, int(time.time()) + 86400)
            return f"version {version}, {len(records)} record(s) in the last day"

        check("timetagger", tt_check)

        def cal_check() -> str:
            cal = CalendarClient(
                config.caldav_url,
                config.caldav_username,
                config.caldav_password,
                config.calendar_name,
            )
            names = [c.get_display_name() for c in cal.calendars()]
            if config.calendar_name in names:
                return f"calendar {config.calendar_name!r} present"
            return f"calendar {config.calendar_name!r} missing (created on first sync)"

        check("caldav", cal_check)
    finally:
        store.close()
    print("doctor: OK" if not failures else f"doctor: {failures} check(s) failed")
    return 1 if failures else 0


def cmd_repair(args: argparse.Namespace) -> int:
    config = _config(args)
    _setup_logging("DEBUG" if args.verbose else config.log_level)
    store, tt, cal = _open(config)
    try:
        if not args.rebuild:
            print("nothing to do; use --rebuild to reconstruct pairs from content")
            return 0
        now = time.time()
        records = tt.records(int(now - config.backfill_days * 86400), int(now + 86400))
        by_content: dict[tuple[int, int, str], dict] = {}
        for payload in records:
            record = model.record_from_payload(payload)
            if record.hidden or record.running:
                continue
            by_content[(record.t1, record.t2, record.ds)] = payload

        changes = cal.changes(None)
        matched = 0
        unmatched = 0
        for event in changes.events:
            try:
                ds = model.record_ds_from_summary(event.summary)
            except model.UnsupportedEvent:
                continue
            start, end = event.start, event.end
            if end <= start:
                end = start + model.MIN_DURATION
            payload = by_content.get((start, end, ds))
            if payload is None:
                unmatched += 1
                continue
            record = model.record_from_payload(payload)
            store.save_pair(
                Pair(
                    record_key=record.key,
                    uid=event.uid,
                    href=event.href,
                    record_hash=model.record_hash_of(record),
                    event_hash=event.hash,
                    etag=event.etag,
                    state=STATE_ACTIVE,
                    meta={"t1": record.t1, "t2": record.t2, "ds": record.ds},
                    updated_at=int(now),
                )
            )
            matched += 1
        print(f"rebuilt {matched} pair(s); {unmatched} event(s) had no record match")
        return 0
    finally:
        store.close()


# -- argument parsing ----------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ttcal",
        description="Mirror TimeTagger records to a CalDAV calendar, both ways.",
    )
    parser.add_argument("--version", action="version", version=f"ttcal {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG,
        help=f"path to the TOML config (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the mirror loop")
    run.add_argument("--once", action="store_true", help="run a single pass and exit")
    run.add_argument(
        "--allow-mass-deletion",
        action="store_true",
        help="hide records when most mirrored events vanished at once",
    )
    run.set_defaults(func=cmd_run)

    sync = sub.add_parser("sync", help="run one sync pass")
    sync.add_argument(
        "--dry-run", action="store_true", help="read both sides, write nothing"
    )
    sync.add_argument(
        "--allow-mass-deletion",
        action="store_true",
        help="hide records when most mirrored events vanished at once",
    )
    sync.set_defaults(func=cmd_sync)

    status = sub.add_parser("status", help="show state, conflicts and skips")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="check config, TimeTagger and CalDAV")
    doctor.set_defaults(func=cmd_doctor)

    repair = sub.add_parser("repair", help="rebuild pairing state from content")
    repair.add_argument(
        "--rebuild", action="store_true", help="match events to records by (t1,t2,ds)"
    )
    repair.set_defaults(func=cmd_repair)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as err:
        print(f"configuration error: {err}", file=sys.stderr)
        return 2
    except (SyncError, TimeTaggerError, CalendarError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
