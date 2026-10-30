"""Configuration loading and validation.

The configuration is a TOML file. Secrets may be given inline (``token``,
``password``) or via a separate file (``token_file``, ``password_file``).  A
configuration that contains inline secrets must not be readable by group or
others; secret files must be private as well.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised when the configuration is missing, malformed or insecure."""


DEFAULT_CALENDAR = "TimeTagger"
DEFAULT_STATE_DB = "ttcal.db"
DEFAULT_BACKFILL_DAYS = 90
DEFAULT_POLL_INTERVAL = 60
DEFAULT_LOG_LEVEL = "INFO"


@dataclass(frozen=True)
class Config:
    timetagger_api_url: str
    timetagger_token: str | None
    timetagger_username: str | None
    timetagger_password: str | None
    caldav_url: str
    caldav_username: str
    caldav_password: str
    calendar_name: str = DEFAULT_CALENDAR
    state_db: Path = Path(DEFAULT_STATE_DB)
    backfill_days: int = DEFAULT_BACKFILL_DAYS
    poll_interval: int = DEFAULT_POLL_INTERVAL
    log_level: str = DEFAULT_LOG_LEVEL
    dry_run: bool = False

    def with_dry_run(self, dry_run: bool) -> Config:
        return replace(self, dry_run=dry_run)


def _as_str(section: dict[str, Any], key: str, where: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: '{key}' must be a non-empty string")
    return value.strip()


def _as_int(section: dict[str, Any], key: str, default: int, where: str) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: '{key}' must be an integer")
    return value


def _read_secret_file(path: Path, what: str) -> str:
    try:
        st = path.stat()
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise ConfigError(f"{what}: file not found: {path}") from None
    except OSError as err:
        raise ConfigError(f"{what}: cannot read {path}: {err}") from None
    if not text:
        raise ConfigError(f"{what}: file is empty: {path}")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(
            f"{what}: {path} is accessible by group or others; run: chmod 600 {path}"
        )
    return text


def _inline_secret_mode_ok(path: Path) -> bool:
    return not (path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def _secret(
    section: dict[str, Any],
    inline_key: str,
    file_key: str,
    where: str,
    base_dir: Path,
    config_path: Path,
    has_inline_secrets: bool,
) -> str | None:
    inline = section.get(inline_key)
    file_value = section.get(file_key)
    if inline is not None and file_value is not None:
        raise ConfigError(
            f"{where}: give either '{inline_key}' or '{file_key}', not both"
        )
    if inline is not None:
        if not isinstance(inline, str) or not inline.strip():
            raise ConfigError(f"{where}: '{inline_key}' must be a non-empty string")
        if has_inline_secrets and not _inline_secret_mode_ok(config_path):
            raise ConfigError(
                f"{config_path} contains secrets but is accessible by group or "
                f"others; run: chmod 600 {config_path}"
            )
        return inline.strip()
    if file_value is not None:
        if not isinstance(file_value, str) or not file_value.strip():
            raise ConfigError(f"{where}: '{file_key}' must be a path")
        return _read_secret_file(_expand(file_value, base_dir), where)
    return None


def _expand(value: str, base_dir: Path) -> Path:
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        path = base_dir / path
    return path


def load_config(path: str | os.PathLike[str]) -> Config:
    config_path = Path(os.path.expanduser(str(path))).resolve()
    try:
        raw = config_path.read_bytes()
    except FileNotFoundError:
        hint = ""
        if not Path(os.path.expanduser("~/.config/ttcal/ttcal.toml")).exists():
            hint = " (copy ttcal.example.toml and edit it)"
        raise ConfigError(f"config file not found: {config_path}{hint}") from None
    except OSError as err:
        raise ConfigError(f"cannot read {config_path}: {err}") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as err:
        raise ConfigError(f"{config_path}: invalid TOML: {err}") from None

    base_dir = config_path.parent
    tt = data.get("timetagger") or {}
    cal = data.get("caldav") or {}
    gen = data.get("ttcal") or {}
    for name, section in (("timetagger", tt), ("caldav", cal), ("ttcal", gen)):
        if not isinstance(section, dict):
            raise ConfigError(f"'{name}' must be a table")

    def inline_present() -> bool:
        return any(
            isinstance(section.get(key), str)
            for section, key in (
                (tt, "token"),
                (tt, "password"),
                (cal, "password"),
            )
        )

    has_inline = inline_present()

    api_url = _as_str(tt, "api_url", "[timetagger]").rstrip("/")
    token = _secret(
        tt, "token", "token_file", "[timetagger]", base_dir, config_path, has_inline
    )
    username = tt.get("username")
    if username is not None and not isinstance(username, str):
        raise ConfigError("[timetagger]: 'username' must be a string")
    password = _secret(
        tt,
        "password",
        "password_file",
        "[timetagger]",
        base_dir,
        config_path,
        has_inline,
    )
    if not token and not (username and password):
        raise ConfigError(
            "[timetagger]: give 'token'/'token_file' (recommended) or "
            "'username' + 'password'"
        )

    caldav_url = _as_str(cal, "url", "[caldav]").rstrip("/") + "/"
    cal_username = _as_str(cal, "username", "[caldav]")
    cal_password = _secret(
        cal,
        "password",
        "password_file",
        "[caldav]",
        base_dir,
        config_path,
        has_inline,
    )
    if not cal_password:
        raise ConfigError("[caldav]: 'password' or 'password_file' is required")

    calendar_name = cal.get("calendar", DEFAULT_CALENDAR)
    if not isinstance(calendar_name, str) or not calendar_name.strip():
        raise ConfigError("[caldav]: 'calendar' must be a non-empty string")
    calendar_name = calendar_name.strip()

    state_db = gen.get("state_db", DEFAULT_STATE_DB)
    if not isinstance(state_db, str) or not state_db.strip():
        raise ConfigError("[ttcal]: 'state_db' must be a path")
    db_path = _expand(state_db, base_dir)

    backfill_days = _as_int(gen, "backfill_days", DEFAULT_BACKFILL_DAYS, "[ttcal]")
    if backfill_days < 0:
        raise ConfigError("[ttcal]: 'backfill_days' must be >= 0")
    poll_interval = _as_int(gen, "poll_interval", DEFAULT_POLL_INTERVAL, "[ttcal]")
    if poll_interval < 5:
        raise ConfigError("[ttcal]: 'poll_interval' must be >= 5 seconds")
    log_level = gen.get("log_level", DEFAULT_LOG_LEVEL)
    if not isinstance(log_level, str):
        raise ConfigError("[ttcal]: 'log_level' must be a string")
    log_level = log_level.upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError(
            "[ttcal]: 'log_level' must be one of DEBUG, INFO, WARNING, ERROR"
        )

    return Config(
        timetagger_api_url=api_url,
        timetagger_token=token,
        timetagger_username=username.strip() if username else None,
        timetagger_password=password,
        caldav_url=caldav_url,
        caldav_username=cal_username,
        caldav_password=cal_password,
        calendar_name=calendar_name,
        state_db=db_path,
        backfill_days=backfill_days,
        poll_interval=poll_interval,
        log_level=log_level,
    )
