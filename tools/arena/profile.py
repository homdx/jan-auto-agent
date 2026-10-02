"""tools/arena/profile.py — AR-2: named run profiles → the `tools.contest run` flags.

A profile is a `[arena.profile.NAME]` section in `contest.ini` and/or the
git-ignored `contest.local.ini`. Both files go into one parser, committed file
first, so the local file wins key by key and a key only in the committed file
survives. The parser is roster's own (`roster._new_parser`: no interpolation,
inline `;`/`#` comments), so `legs = 2 ; two legs` reads as `2` and a `%` in a
value is just a character. Roster ignores sections it does not know, so these
sections cost the old `tools.contest` command nothing.

`KNOWN_KEYS` is the one registry of accepted keys. A key not in it is refused
rather than ignored: a typo (`max_paralel`) silently dropped would run a round
with the wrong flags. `base` is deliberately absent — the base and the ticket
are what `arena` itself decides per round (AR-3), never a stored setting — and
`profile_flags` refuses `--base`/`--ticket` smuggled in through `extra`.
"""

from __future__ import annotations

import configparser
import shlex
from pathlib import Path
from typing import Optional

from tools.contest import roster

#: `[arena.profile.<name>]` — the profile section.
SECTION_PREFIX = "arena.profile."

#: The profile name used when `[arena] profile =` is not set.
DEFAULT_PROFILE = "default"

#: Every accepted profile key → its `tools.contest run` flag. `None`: the key is
#: arena-owned data (AR-3/AR-8 read it), not a flag. `extra` is special: its
#: value is `shlex.split` and appended verbatim after the mapped flags. The
#: dict order is the flag order `profile_flags` produces.
KNOWN_KEYS: dict[str, Optional[str]] = {
    "models": "--models",
    "legs": "--legs",
    "max_parallel": "--max-parallel",
    "backend": "--backend",
    "provider": "--provider",
    "variant": "--variant",
    "extra": None,
    "branch": None,
    "trailer": None,
}

# Flags `arena` sets per round; a profile may never carry them.
_FORBIDDEN_FLAGS = ("--base", "--ticket")


class ProfileError(Exception):
    """A profile refusal; the message is the one line the CLI prints."""


def load_profiles(repo: Path) -> tuple[dict[str, dict[str, str]], str]:
    """Return (profiles by name, active profile name) for the checkout *repo*.

    Reads only `contest.ini` then `contest.local.ini`; a missing file is skipped.
    """
    parser = roster._new_parser()
    for name in ("contest.ini", roster.LOCAL_FILENAME):
        path = repo / name
        if path.is_file():
            try:
                parser.read(path, encoding="utf-8")
            except configparser.Error as err:
                # A broken header or a key outside any section: one refusal
                # line naming the file, never a traceback.
                raise ProfileError(f"{name}: {_one_line(err)}") from err

    profiles: dict[str, dict[str, str]] = {}
    for section in parser.sections():
        if not section.startswith(SECTION_PREFIX):
            continue
        values = dict(parser.items(section))
        for key in values:
            if key not in KNOWN_KEYS:
                raise ProfileError(f"[{section}] unknown key {key!r}")
        profiles[section[len(SECTION_PREFIX) :]] = values

    active = DEFAULT_PROFILE
    if parser.has_option("arena", "profile"):
        active = parser.get("arena", "profile").strip() or DEFAULT_PROFILE
    return profiles, active


def profile_flags(
    profile: dict[str, str], overrides: Optional[dict[str, str]] = None
) -> list[str]:
    """Turn *profile* (with *overrides* winning key by key) into run flags.

    Fixed order: the mapped keys in `KNOWN_KEYS` order, then the `extra` words
    in their own order. An empty value produces nothing.
    """
    merged = {**profile, **(overrides or {})}
    flags: list[str] = []
    for key, flag in KNOWN_KEYS.items():
        value = (merged.get(key) or "").strip()
        if flag is None or not value:
            continue
        flags += [flag, value]

    try:
        extra = shlex.split(merged.get("extra") or "")
    except ValueError as err:  # an unbalanced quote
        raise ProfileError(f"extra: {err}") from err
    for word in extra:
        # `--base X` and `--base=X` both count; so would an abbreviation
        # argparse accepts, but a profile has no reason to spell one.
        if word.split("=", 1)[0] in _FORBIDDEN_FLAGS:
            raise ProfileError("--base/--ticket are set by arena, not by a profile")
    return flags + extra


def _one_line(err: Exception) -> str:
    """configparser's messages span lines (file, line, text); keep the first."""
    return str(err).splitlines()[0]
