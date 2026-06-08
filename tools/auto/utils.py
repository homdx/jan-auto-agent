"""tools/auto/utils.py — Shared micro-utilities for the autonomous mode."""
from __future__ import annotations

from datetime import datetime, timezone


def _ts() -> str:
    """Return the current UTC time as an ISO-8601 string (YYYY-MM-DDTHH:MM:SSZ)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
