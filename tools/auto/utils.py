"""tools/auto/utils.py — Shared micro-utilities for the autonomous mode."""
from __future__ import annotations

import configparser
from datetime import datetime, timezone
from typing import Any


def _ts() -> str:
    """Return the current UTC time as an ISO-8601 string (YYYY-MM-DDTHH:MM:SSZ)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cfg_mode(
    config: configparser.ConfigParser,
    section: str,
    key: str,
    task_mode: str,
    fallback: Any = None,
) -> Any:
    """Read a mode-specific config key with graceful fallback (AUTO-CR-3).

    Priority (highest -> lowest):

    1. ``{key}_{task_mode}`` in *section*  -- e.g. ``max_tokens_creative``
    2. ``{key}``            in *section*  -- e.g. ``max_tokens``
    3. *fallback*

    Works for any ``task_mode`` value; when ``task_mode`` is ``"code"`` (the
    default) step 1 is effectively skipped because ``max_tokens_code`` is
    unlikely to be set, so it behaves identically to a plain ``config.get``.

    Parameters
    ----------
    config:
        Parsed ``agents.ini``.
    section:
        INI section name (e.g. ``"coder"``).
    key:
        Base key name (e.g. ``"max_tokens"``).
    task_mode:
        Current task mode (e.g. ``"creative"``).
    fallback:
        Value returned when neither key variant is present.

    Returns
    -------
    str | None
        The raw string value, or *fallback* when not found.  Callers are
        responsible for casting to ``int`` / ``float`` as required.
    """
    mode_key = f"{key}_{task_mode}"
    val = config.get(section, mode_key, fallback=None)
    if val is not None and str(val).strip():
        return val
    val = config.get(section, key, fallback=None)
    if val is not None and str(val).strip():
        return val
    return fallback
