from __future__ import annotations

import pytest

from ttcal.config import ConfigError, load_config

BASE = """
[timetagger]
api_url = "https://tt.example.com/api/v2/"
token = "secret-token"

[caldav]
url = "https://cal.example.com/"
username = "alice"
password = "secret"
"""


def write(tmp_path, text: str, mode: int = 0o600):
    path = tmp_path / "ttcal.toml"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def test_minimal_config_and_defaults(tmp_path):
    config = load_config(write(tmp_path, BASE))
    assert config.timetagger_api_url == "https://tt.example.com/api/v2"
    assert config.timetagger_token == "secret-token"
    assert config.caldav_url == "https://cal.example.com/"
    assert config.calendar_name == "TimeTagger"
    assert config.backfill_days == 90
    assert config.poll_interval == 60
    assert config.state_db == tmp_path / "ttcal.db"
    assert config.dry_run is False


def test_inline_secrets_require_private_file(tmp_path):
    path = write(tmp_path, BASE, mode=0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        load_config(path)


def test_password_credentials_are_accepted(tmp_path):
    text = BASE.replace('token = "secret-token"', 'username = "bob"\npassword = "pw"')
    config = load_config(write(tmp_path, text))
    assert config.timetagger_username == "bob"
    assert config.timetagger_password == "pw"


def test_token_file_is_read_and_must_be_private(tmp_path):
    token = tmp_path / "token.txt"
    token.write_text("from-file\n", encoding="utf-8")
    token.chmod(0o600)
    text = BASE.replace('token = "secret-token"', 'token_file = "token.txt"')
    assert load_config(write(tmp_path, text)).timetagger_token == "from-file"

    token.chmod(0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        load_config(write(tmp_path, text))


def test_token_file_must_not_be_empty(tmp_path):
    token = tmp_path / "token.txt"
    token.write_text("\n", encoding="utf-8")
    token.chmod(0o600)
    text = BASE.replace('token = "secret-token"', 'token_file = "token.txt"')
    with pytest.raises(ConfigError, match="empty"):
        load_config(write(tmp_path, text))


def test_token_and_token_file_are_mutually_exclusive(tmp_path):
    token = tmp_path / "token.txt"
    token.write_text("x", encoding="utf-8")
    token.chmod(0o600)
    text = BASE.replace(
        'token = "secret-token"', 'token = "secret-token"\ntoken_file = "token.txt"'
    )
    with pytest.raises(ConfigError, match="not both"):
        load_config(write(tmp_path, text))


def test_missing_credentials_are_rejected(tmp_path):
    text = BASE.replace('token = "secret-token"', "")
    with pytest.raises(ConfigError, match="token"):
        load_config(write(tmp_path, text))


def test_api_url_is_required(tmp_path):
    text = BASE.replace('api_url = "https://tt.example.com/api/v2/"', "")
    with pytest.raises(ConfigError, match="api_url"):
        load_config(write(tmp_path, text))


def test_caldav_password_is_required(tmp_path):
    text = BASE.replace('password = "secret"', "")
    with pytest.raises(ConfigError, match="password"):
        load_config(write(tmp_path, text))


def test_state_db_is_resolved_against_the_config_directory(tmp_path):
    text = BASE + '\n[ttcal]\nstate_db = "state/mirror.db"\n'
    config = load_config(write(tmp_path, text))
    assert config.state_db == tmp_path / "state/mirror.db"


def test_overrides_are_validated(tmp_path):
    for body, match in (
        ("backfill_days = -1", "backfill_days"),
        ("poll_interval = 1", "poll_interval"),
        ('log_level = "LOUD"', "log_level"),
        ('backfill_days = "many"', "integer"),
    ):
        text = BASE + f"\n[ttcal]\n{body}\n"
        with pytest.raises(ConfigError, match=match):
            load_config(write(tmp_path, text))


def test_unknown_keys_are_ignored(tmp_path):
    text = BASE + "\n[ttcal]\nfuture_option = true\n"
    assert load_config(write(tmp_path, text)).calendar_name == "TimeTagger"


def test_bad_toml_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(write(tmp_path, BASE + "\nthis is not toml\n"))


def test_missing_file_mentions_example(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with pytest.raises(ConfigError, match="ttcal.example.toml"):
        load_config(tmp_path / "nope.toml")
