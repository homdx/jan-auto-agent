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
    "fresh": "--fresh",
    "extra": None,
    "branch": None,
    "trailer": None,
}

#: Flags `arena` sets per round; a profile may never carry them, nor may the
#: passthrough (`rounds._OWNED_FLAGS` is this same tuple). `--out` and `--target`
#: move the round's folder and repository away from where `run list`, `run view`
#: and `run rerun` look.
OWNED_FLAGS = ("--ticket", "--base", "--target", "--out")

#: AR-62: the runner flags that take a value. Given twice (profile key, `extra`,
#: passthrough after `--`), only the last one reaches the runner.
VALUE_FLAGS = ("--max-parallel", "--variant", "--legs", "--backend", "--provider", "--models")
#: Switches (no value) the runner takes at most once.
SWITCH_FLAGS = ("--fresh",)

# `fresh = …`: a switch spelt as a key. Empty is "no" too.
_TRUE_WORDS = ("yes", "true", "1")
_FALSE_WORDS = ("no", "false", "0", "")


def owned_flag(word: str, owned) -> Optional[str]:
    """The flag of *owned* that *word* reaches the runner as, or None.

    Bug 172: `tools.contest run` keeps argparse's `allow_abbrev`, so `--tick 9`
    *is* `--ticket 9`. A word counts when its flag part is an owned flag or an
    abbreviation of one — and not, itself, an exact runner option (an exact
    match wins in argparse, so `--backend` never stands for anything else).
    """
    flag = word.split("=", 1)[0]
    if not flag.startswith("--") or len(flag) <= 2:
        return None
    if flag in owned:
        return flag
    if flag in _runner_options():
        return None
    return next((o for o in owned if o.startswith(flag)), None)


_RUNNER_OPTIONS: Optional[frozenset] = None


def _runner_options() -> frozenset:
    """Every option string `tools.contest run` declares (read once, lazily)."""
    global _RUNNER_OPTIONS
    if _RUNNER_OPTIONS is None:
        from tools.contest import cli as contest_cli  # lazy: cli is heavy
        import argparse
        names: set = set()
        for action in contest_cli._parser()._actions:
            if isinstance(action, argparse._SubParsersAction):
                run = action.choices.get("run")
                if run is not None:
                    for sub in run._actions:
                        names.update(sub.option_strings)
        _RUNNER_OPTIONS = frozenset(names)
    return _RUNNER_OPTIONS


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
        if "fresh" in values:
            try:
                fresh_on(values["fresh"])
            except ProfileError as err:
                raise ProfileError(f"[{section}] {err}") from err
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
    in their own order. An empty value produces nothing; `fresh` is a switch.
    A flag both a key and `extra` give comes out once, the `extra` one (AR-62).
    """
    merged = {**profile, **(overrides or {})}
    flags: list[str] = []
    for key, flag in KNOWN_KEYS.items():
        value = (merged.get(key) or "").strip()
        if flag is None or not value:
            continue
        if flag in SWITCH_FLAGS:
            if fresh_on(value):
                flags.append(flag)
            continue
        flags += [flag, value]

    try:
        extra = shlex.split(merged.get("extra") or "")
    except ValueError as err:  # an unbalanced quote
        raise ProfileError(f"extra: {err}") from err
    for word in extra:
        # `--base X`, `--base=X` and an abbreviation argparse accepts (`--bas`)
        # all count, for every flag arena owns
        flag = owned_flag(word, OWNED_FLAGS)
        if flag in ("--base", "--ticket"):
            raise ProfileError("--base/--ticket are set by arena, not by a profile")
        if flag:
            raise ProfileError(f"{flag} is set by arena, not by a profile")
    return dedupe_flags(flags + extra)


def fresh_on(value: str) -> bool:
    """`yes|true|1` → True, `no|false|0|` → False; anything else is a refusal."""
    word = value.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ProfileError(f"fresh must be yes or no, not {value.strip()!r}")


def dedupe_flags(words: list[str]) -> list[str]:
    """AR-62: each known runner flag once — the last one given wins.

    *words* is split into items: `--flag V` and `--flag=V` (a `VALUE_FLAGS`
    flag) are one item, a `SWITCH_FLAGS` word is one item, every other word is
    an item of its own and passes untouched. A known flag's item survives only
    at its last occurrence, so the order of the rest is kept and the
    passthrough (which comes last) beats the profile. A value flag with no word
    after it is left as it is — the runner says what is wrong with it.
    """
    items: list[tuple[Optional[str], list[str]]] = []
    i = 0
    while i < len(words):
        word = words[i]
        name = word.split("=", 1)[0]
        if name in VALUE_FLAGS and "=" not in word and i + 1 < len(words):
            items.append((name, [word, words[i + 1]]))
            i += 2
            continue
        known = name in VALUE_FLAGS or (word in SWITCH_FLAGS)
        items.append((name if known else None, [word]))
        i += 1
    last = {name: n for n, (name, _) in enumerate(items) if name}
    out: list[str] = []
    for n, (name, item) in enumerate(items):
        if name is None or last[name] == n:
            out += item
    return out


def _one_line(err: Exception) -> str:
    """configparser's messages span lines (file, line, text); keep the first."""
    return str(err).splitlines()[0]
