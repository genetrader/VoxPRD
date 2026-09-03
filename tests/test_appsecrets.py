"""Tests for the secrets loader.

The loader is intentionally tiny. We test:
- Env var wins over .env file.
- secrets/.env is consulted before ./.env.
- Comment lines and quoted values are handled.
- Missing files don't raise.
- get_required raises with a clear message.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import appsecrets as sec
from appsecrets import get, get_required, load_env


@pytest.fixture
def tmp_app_dir(tmp_path, monkeypatch):
    """Set up a fake project root with secrets/.env and ./.env."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    secrets_env = secrets_dir / ".env"
    legacy_env = tmp_path / ".env"
    return tmp_path, secrets_env, legacy_env


def test_load_env_secrets_dir_wins(tmp_app_dir, monkeypatch):
    app_dir, secrets_env, legacy_env = tmp_app_dir
    secrets_env.write_text('FOO=from_secrets\n', encoding="utf-8")
    legacy_env.write_text('FOO=from_legacy\nBAR=legacy_only\n', encoding="utf-8")
    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.delenv("BAR", raising=False)

    env = load_env(app_dir)
    assert env["FOO"] == "from_secrets"
    assert env["BAR"] == "legacy_only"


def test_process_env_wins_over_file(tmp_app_dir, monkeypatch):
    app_dir, secrets_env, legacy_env = tmp_app_dir
    secrets_env.write_text('FOO=from_secrets\n', encoding="utf-8")
    monkeypatch.setenv("FOO", "from_env")

    env = load_env(app_dir)
    assert env["FOO"] == "from_env"


def test_comments_and_blank_lines_ignored(tmp_app_dir, monkeypatch):
    app_dir, secrets_env, _ = tmp_app_dir
    secrets_env.write_text(
        "# This is a comment\n"
        "\n"
        "FOO=bar\n"
        "  # indented comment\n"
        "BAZ=qux\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.delenv("BAZ", raising=False)

    env = load_env(app_dir)
    assert env["FOO"] == "bar"
    assert env["BAZ"] == "qux"


def test_quoted_values_strip_quotes(tmp_app_dir, monkeypatch):
    app_dir, secrets_env, _ = tmp_app_dir
    secrets_env.write_text(
        'DOUBLE="hello world"\n'
        "SINGLE='single quoted'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DOUBLE", raising=False)
    monkeypatch.delenv("SINGLE", raising=False)
    env = load_env(app_dir)
    assert env["DOUBLE"] == "hello world"
    assert env["SINGLE"] == "single quoted"


def test_missing_files_no_raise(tmp_app_dir, monkeypatch):
    app_dir, _, _ = tmp_app_dir
    monkeypatch.delenv("FOO", raising=False)
    env = load_env(app_dir)
    assert "FOO" not in env


def test_get_returns_value(tmp_app_dir, monkeypatch):
    app_dir, secrets_env, _ = tmp_app_dir
    secrets_env.write_text("MY_KEY=hello\n", encoding="utf-8")
    monkeypatch.delenv("MY_KEY", raising=False)
    assert get("MY_KEY", app_dir=app_dir) == "hello"


def test_get_returns_default_when_missing(tmp_app_dir, monkeypatch):
    app_dir, _, _ = tmp_app_dir
    monkeypatch.delenv("MISSING", raising=False)
    assert get("MISSING", default="x", app_dir=app_dir) == "x"


def test_get_required_raises_with_clear_message(tmp_app_dir, monkeypatch):
    app_dir, _, _ = tmp_app_dir
    monkeypatch.delenv("MISSING", raising=False)
    with pytest.raises(FileNotFoundError) as exc:
        get_required("MISSING", app_dir=app_dir)
    assert "MISSING" in str(exc.value)
    assert "secrets/.env" in str(exc.value)
