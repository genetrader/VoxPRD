"""Tiny secrets loader.

Resolves a key from (in order):
  1. process env  (os.environ)
  2. secrets/.env  (preferred location for production / shared installs)
  3. ./.env        (legacy / single-user install)

Returns the value or None. Does NOT raise. Callers decide what to do when
a required secret is missing.

This module is intentionally dependency-free so it can be imported from
tests and from the live app without dragging tkinter / sounddevice in.

Note: we deliberately do NOT cache. The .env is a small file; re-reading
it on every get is cheap and avoids the cross-test contamination that a
module-level cache would introduce.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Ignores comments and blanks.

    Supports quoted values: KEY="value with spaces" -> strips outer quotes.
    Does NOT support escape sequences or variable interpolation; this is
    a flat key-value file, not a shell.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        out[key] = value
    return out


def _candidate_paths(app_dir: Path) -> Iterable[Path]:
    """Return the .env file paths to consult, in priority order.

    secrets/.env is preferred because it can be excluded from a portable
    archive of the project without losing source. ./.env is kept for
    backward compat with the single-user install.
    """
    return (
        app_dir / "secrets" / ".env",
        app_dir / ".env",
    )


def load_env(app_dir: Path | None = None) -> dict[str, str]:
    """Return a merged dict of {KEY: value} from env vars + .env files.

    Precedence (high -> low):
      1. process env
      2. secrets/.env  (preferred location)
      3. ./.env        (legacy)
    """
    app_dir = app_dir or Path(__file__).resolve().parent
    merged: dict[str, str] = {}
    # Iterate LOWEST priority first so higher-priority files overwrite.
    for path in reversed(list(_candidate_paths(app_dir))):
        merged.update(_read_env_file(path))
    # Process env wins over everything.
    for key, value in os.environ.items():
        merged[key] = value
    return merged


def get(key: str, default: str | None = None, app_dir: Path | None = None) -> str | None:
    """Get a single secret value. Returns default if missing."""
    return load_env(app_dir).get(key, default)


def get_required(key: str, app_dir: Path | None = None) -> str:
    """Get a required secret. Raises if missing."""
    val = get(key, app_dir=app_dir)
    if not val:
        raise FileNotFoundError(
            f"Required secret {key!r} not found. "
            f"Set the {key} env var, or add it to secrets/.env or ./.env "
            f"in the project root."
        )
    return val


def reset_cache() -> None:
    """No-op retained for backward compat. Caching was removed."""
    return None
