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

The typed readers handle conversion and interpolation errors.
``safe_get_api_key`` handles a separate case: a bare ``%`` in a credential
should be preserved while valid INI references still expand. It shares the
interactive reader's behavior with the autonomous stages (rounds 365 and 367).

The argument order deliberately mirrors ``tools.auto.utils._cfg_mode`` —
``(config, section, key, ...)`` — because ``extract_config_reads``
(tools/collect/ast_facts.py) attributes config reads by *call shape*: it
recognises these helpers by name and reads section/key from that fixed
positional layout. A helper with a different signature, or one called under
an alias, is invisible to that scanner, and the config map it produces would
silently lose every read routed through it. If you add another helper here,
add its name to ``_SAFE_CONFIG_HELPERS`` there in the same change.
"""

from __future__ import annotations

import configparser
import logging
import re
from configparser import ConfigParser, InterpolationError

logger = logging.getLogger(__name__)

#: A credential may legitimately contain a bare ``%`` (``fake%value``). The
#: alternatives are, in order: an already-escaped ``%%`` (keep it), a working
#: ``%(name)s`` reference (keep it), and any other ``%`` (escape it). The
#: capture group is what tells the replacement which case matched.
_API_KEY_PERCENT = re.compile(r"%%|%\([^)]+\)s|(%)")

#: What a present-but-unreadable value raises. CONFIG-PCT: `main.py`'s parser keeps configparser's
#: `%` interpolation, so `canon_check_every = 50%` raised InterpolationSyntaxError and
#: `%(missing)s` InterpolationMissingOptionError — neither a ValueError, so both escaped the helper
#: and aborted the run these helpers exist to keep going. A missing section or key is still the
#: fallback's own case (configparser returns it), never this one.
_MALFORMED = (ValueError, InterpolationError)

__all__ = [
    "ApiKeyInterpolationError",
    "safe_getint",
    "safe_getfloat",
    "safe_getboolean",
    "safe_get_api_key",
]


class ApiKeyInterpolationError(InterpolationError, ValueError):
    """A selected credential holds a missing or cyclic INI reference.

    ``configparser``'s own ``InterpolationError`` text embeds the raw value,
    which for ``api_key`` is the credential itself. This subclass carries a
    message naming only the section and the option, so a traceback, a warning
    or a CLI diagnostic can print it without leaking the key or a fragment of
    it. It is also a ``ValueError`` so the existing "a misconfigured profile
    is an operator error" handlers keep treating it as fatal.
    """

    def __init__(self, section: str, key: str = "api_key") -> None:
        super().__init__(
            key, section, f"config [{section}] {key} has invalid interpolation",
        )


def safe_getint(
    config: ConfigParser, section: str, key: str, *, fallback: int
) -> int:
    """``config.getint`` that degrades to *fallback* on a malformed value."""
    try:
        return config.getint(section, key, fallback=fallback)
    except _MALFORMED as exc:
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
    except _MALFORMED as exc:
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
    except _MALFORMED as exc:
        logger.warning(
            "config [%s] %s is malformed (%s) — using %r", section, key, exc, fallback,
        )
        return fallback


def safe_get_api_key(
    config: ConfigParser, section: str, key: str = "api_key", *, fallback: str = "jan"
) -> str:
    """Read a credential, tolerating a literal ``%`` but not a broken reference.

    Round 365's ``main._read_api_key`` behaviour, shared with every autonomous
    consumer. A bare ``%`` in a key (``fake%value``) is a literal, not an
    interpolation error; ``%%`` still resolves to one ``%``; a working
    ``%(name)s`` reference still expands, including when it is mixed with a
    literal percent in the same value. The caller's parser is never mutated,
    so interpolation of every unrelated option keeps working.

    A missing or cyclic reference is a real configuration error, not a
    literal: it raises :class:`ApiKeyInterpolationError`, whose message names
    only the section and the option. The credential and its fragments never
    reach the exception text, a warning, or a traceback.

    An absent section or key returns *fallback* — each caller keeps its own
    historical default — and a present-but-blank key returns ``""`` (a local
    server needs no credential).
    """
    try:
        return config.get(section, key, fallback=fallback)
    except configparser.InterpolationSyntaxError:
        # A bare `%` is the only recoverable case: re-read the raw value and
        # escape just the literal percents, leaving `%%` and `%(name)s` alone.
        pass
    except configparser.InterpolationError:
        # Missing/cyclic reference: configparser's message embeds the value.
        raise ApiKeyInterpolationError(section, key) from None

    try:
        raw = config.get(section, key, raw=True)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return fallback

    escaped = _API_KEY_PERCENT.sub(
        lambda match: "%%" if match[1] else match[0], raw,
    )
    try:
        # `vars` overrides the option value for this one read, so the escaped
        # copy is what gets interpolated; the parser itself is untouched.
        return config.get(section, key, vars={key: escaped})
    except configparser.InterpolationError:
        raise ApiKeyInterpolationError(section, key) from None
