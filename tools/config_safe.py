"""Fail-open ConfigParser reads (FIX-2 M3).

``config.getint("auto", "max_rounds", fallback=3)`` only falls back when the
key is *missing*. A key that is present but unparseable — ``max_rounds =
three``, or a stray value left behind while editing agents.ini — raises
``ValueError`` out of the call. Every such bare read is therefore a place a
single typo can abort a run, and this codebase had 31 of them left after the
individually-reported ones (FIX-2 C2 and the gate1_filter block before it)
were guarded by hand.

Hand-guarding the rest would mean 31 more copies of the same six lines. These
helpers hold that shape once::

    from tools.config_safe import safe_getint

    rounds = safe_getint(config, "auto", "max_rounds", fallback=3)

On a malformed value they log a warning naming the section and key — an
operator has to be able to find what they typed wrong — and return
*fallback*, which is what the caller asked for when the key was absent and is
no less right when it is unreadable.

Only ``getint``/``getfloat``/``getboolean`` are wrapped. Plain ``config.get``
returns the raw string and cannot raise ``ValueError``, so it needs nothing.

The argument order deliberately mirrors ``tools.auto.utils._cfg_mode`` —
``(config, section, key, ...)`` — because ``extract_config_reads``
(tools/collect/ast_facts.py) attributes config reads by *call shape*: it
recognises these helpers by name and reads section/key from that fixed
positional layout. A helper with a different signature, or one called under
an alias, is invisible to that scanner, and the config map it produces would
silently lose every read routed through it. If you add a fourth helper here,
add its name to ``_SAFE_CONFIG_HELPERS`` there in the same change.
"""

from __future__ import annotations

import logging
from configparser import ConfigParser

logger = logging.getLogger(__name__)

__all__ = ["safe_getint", "safe_getfloat", "safe_getboolean"]


def safe_getint(
    config: ConfigParser, section: str, key: str, *, fallback: int
) -> int:
    """``config.getint`` that degrades to *fallback* on a malformed value."""
    try:
        return config.getint(section, key, fallback=fallback)
    except ValueError as exc:
        logger.warning(
            "config [%s] %s is malformed (%s) — using %r", section, key, exc, fallback,
        )
        return fallback


def safe_getfloat(
    config: ConfigParser, section: str, key: str, *, fallback: float
) -> float:
    """``config.getfloat`` that degrades to *fallback* on a malformed value."""
    try:
        return config.getfloat(section, key, fallback=fallback)
    except ValueError as exc:
        logger.warning(
            "config [%s] %s is malformed (%s) — using %r", section, key, exc, fallback,
        )
        return fallback


def safe_getboolean(
    config: ConfigParser, section: str, key: str, *, fallback: bool
) -> bool:
    """``config.getboolean`` that degrades to *fallback* on a malformed value."""
    try:
        return config.getboolean(section, key, fallback=fallback)
    except ValueError as exc:
        logger.warning(
            "config [%s] %s is malformed (%s) — using %r", section, key, exc, fallback,
        )
        return fallback
