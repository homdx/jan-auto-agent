"""tools/arena/models.py — AR-59: `arena model available | use | drop`.

The model list of a round used to be typed by hand into `--models` or a
profile, copied from `kilo models` output, and a typo was found only when the
round's intake failed. This module reads the list from Kilo, shows which models
are free, and writes the chosen names into a profile *by name* — after checking
every name against what Kilo lists. No model is tested here (that is AR-60): a
model already checked is used as it is.

Where the list comes from
  Kilo only. `KILO_LIST` is the one seam: it is called as
  `KILO_LIST(repo, providers)` and answers `{provider: {model: metadata}}`
  (`providers` empty = every provider Kilo knows). The default implementation
  runs `kilo models PROVIDER --verbose` through `scripts/py_model_test.py`'s
  `kilo_models` — imported, not copied — and raises `KiloError` for a missing
  binary or an error exit. A test replaces the seam; no test needs a network
  or a kilo binary.

Free
  `yes` when the name ends in `:free` / `-free`; `maybe` when Kilo's metadata
  says price 0 (`free_from_kilo`'s rule) without that suffix — Kilo's 0 can mean
  "unknown"; `no` otherwise.

No ini file at all
  Nothing here needs `contest.ini` or `contest.local.ini`. The cache lives in
  `.arena/` (made on the first write), `model_cache_days` falls back to 7, a
  missing roster means the judge check has nothing to read and refuses nothing,
  and `use -p NAME` creates `contest.local.ini` holding only that profile's
  `models =` line.

The cache — the same pattern as `tools/contest/context_memory.py` (KC-67)
  `.arena/models-cache.json`: a JSON list of `{provider, model, free, ctx, at}`.
  Records older than `[arena] model_cache_days` (default 7; 0 or junk = no
  cache) are dropped on every read and every write, the file is written
  atomically (`tempfile.mkstemp` next to it + `os.replace`), and it is
  fail-open: a missing, unreadable or non-list file is an empty cache, never an
  error. `available` refreshes the providers it listed; `use` / `drop` call Kilo
  only when the cache has no record for a name.

`use` and `drop` write `contest.local.ini` only — never `contest.ini`, never
`agents_128k.ini` — by editing the file's text in place, so a `[contest_gate_llm]`
`api_key` and every comment in it come back byte for byte.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import difflib
import glob
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from tools.contest import roster
from tools.contest.kilo_client import _version_key

from . import output, profile

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_NOTHING = 0, 1, 2, 3

#: The default age a cache record is kept, in days (`[arena] model_cache_days`).
DEFAULT_CACHE_DAYS = 7.0
#: The cache file, under the checkout's `.arena/` folder.
CACHE_RELPATH = Path(".arena") / "models-cache.json"
_DAY = 86400.0

FREE_YES, FREE_MAYBE, FREE_NO = "yes", "maybe", "no"
_FREE_VALUES = (FREE_YES, FREE_MAYBE, FREE_NO)
# `x:free`, `x-free` — the name says so itself.
_FREE_SUFFIX_RE = re.compile(r"[:-]free$", re.IGNORECASE)

#: The `[contest]` keys that name a judge's LLM profile, and what the judge is
#: called in a refusal. A judge never competes in the round it judges.
JUDGE_KEYS = (
    ("gate_llm_profile", "the gate model"),
    ("draft_llm_profile", "the ticket writer"),
    ("draft_review_llm_profile", "the ticket reviewer"),
)

#: The columns of `arena model available`. LAST-TEST stays empty until AR-60.
AVAILABLE_COLUMNS = ["NAME", "PROVIDER", "FREE", "CTX", "IN-PROFILE", "LAST-TEST"]

_PROFILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_VARIANT_RE = re.compile(r"[A-Za-z0-9_.-]+")
_PROVIDER_LINE_RE = re.compile(r"^([A-Za-z0-9_.-]+)/\S+$")


class ModelError(Exception):
    """A refusal; the message is the one line the CLI prints."""


class KiloError(ModelError):
    """Kilo could not give the list: no binary, an error exit, a timeout."""


# ── the checkout's ini files (read-only here) ───────────────────────────────
def _read_ini(repo: Path) -> configparser.ConfigParser:
    """`contest.ini` then `contest.local.ini`, as roster reads them.

    A missing file is skipped and a broken one is an empty read: every caller
    here has a default, and the verbs that must refuse a broken file get that
    refusal from `profile.load_profiles`.
    """
    parser = roster._new_parser()
    for name in ("contest.ini", roster.LOCAL_FILENAME):
        path = repo / name
        if not path.is_file():
            continue
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            continue
    return parser


def cache_days(repo: Path) -> float:
    """`[arena] model_cache_days`: 7 when unset, 0 when it is 0, negative or junk."""
    raw = _read_ini(repo).get("arena", "model_cache_days", fallback=None)
    if raw is None or not raw.strip():
        return DEFAULT_CACHE_DAYS
    try:
        days = float(raw)
    except ValueError:
        return 0.0
    return days if math.isfinite(days) and days > 0 else 0.0


# ── the cache ────────────────────────────────────────────────────────────────
class ModelCache:
    """`.arena/models-cache.json`: what Kilo listed, for `model_cache_days`."""

    def __init__(self, path: Path, days: float) -> None:
        self.path = Path(path)
        self.days = days

    @property
    def enabled(self) -> bool:
        return self.days > 0

    def load(self, now: Optional[float] = None) -> list[dict]:
        """The records still inside the age cut; `[]` for anything unreadable."""
        if not self.enabled:
            return []
        stamp = time.time() if now is None else now
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        keep = []
        for entry in data:
            record = _clean_record(entry)
            if record is not None and stamp - record["at"] <= self.days * _DAY:
                keep.append(record)
        return keep

    def merge(self, fresh: list[dict], providers: Iterable[str],
              now: Optional[float] = None) -> list[dict]:
        """Replace *providers*' records with *fresh*, age the rest, write atomically.

        Returns the merged records whether or not the file could be written: a
        cache that cannot be written is a Kilo call next time, not an error.
        """
        if not self.enabled:
            return list(fresh)
        replaced = set(providers) | {r["provider"] for r in fresh}
        merged = [r for r in self.load(now) if r["provider"] not in replaced] + list(fresh)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp",
                                       dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(merged, fh, ensure_ascii=False, indent=2)
                    fh.write("\n")
                os.replace(tmp, str(self.path))
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        except OSError:
            pass
        return merged


def _clean_record(entry: object) -> Optional[dict]:
    """One cache entry as a record, `None` when it is not one."""
    if not isinstance(entry, dict):
        return None
    provider, model, at = entry.get("provider"), entry.get("model"), entry.get("at")
    if not (isinstance(provider, str) and provider and isinstance(model, str) and model):
        return None
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return None
    free = entry.get("free")
    ctx = entry.get("ctx")
    return {
        "provider": provider,
        "model": model,
        "free": free if free in _FREE_VALUES else FREE_NO,
        "ctx": ctx if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0 else 0,
        "at": at,
    }


def model_cache(repo: Path) -> ModelCache:
    """The checkout's model cache, with its age cut read from the ini."""
    return ModelCache(repo / CACHE_RELPATH, cache_days(repo))


# ── Kilo ─────────────────────────────────────────────────────────────────────
def find_kilo_bin(repo: Path) -> str:
    """`[contest] kilo_bin` when an ini sets it, else `kilo` on PATH, else the
    newest `~/.vscode/extensions/kilocode.kilo-code-*/bin/kilo`.

    `auto` (the shipped default) and an empty value are "not set". Raises
    `KiloError` naming where it looked.
    """
    looked = []
    raw = os.path.expandvars(_read_ini(repo).get("contest", "kilo_bin", fallback="")).strip()
    if raw and raw.lower() != "auto":
        if os.path.isfile(raw):
            return raw
        looked.append(raw)
    on_path = shutil.which("kilo")
    if on_path:
        return on_path
    looked.append("kilo on PATH")
    ext_glob = "~/.vscode/extensions/kilocode.kilo-code-*/bin/kilo"
    for path in sorted(glob.glob(os.path.expanduser(ext_glob)), key=_version_key, reverse=True):
        if os.path.isfile(path):
            return path
    looked.append(ext_glob)
    raise KiloError("no kilo binary found; looked at: " + "; ".join(looked))


def _model_check():
    """`scripts/py_model_test.py`, imported on first use."""
    import scripts.py_model_test as model_check

    return model_check


def _kilo_list(repo: Path, providers: list[str]) -> dict[str, dict[str, dict]]:
    """The default `KILO_LIST`: `{provider: {model: metadata}}` from the kilo binary."""
    kilo = find_kilo_bin(repo)
    check = _model_check()
    try:
        names = list(providers) or _kilo_providers(kilo, check)
        found: dict[str, dict[str, dict]] = {}
        for provider in names:
            # `kilo_models` reports a failure on stderr and answers `{}`; that
            # text is the reason, so it is caught rather than printed.
            noise = io.StringIO()
            with contextlib.redirect_stderr(noise):
                models = check.kilo_models(kilo, provider, True)
            if not models and noise.getvalue().strip():
                raise KiloError(" ".join(noise.getvalue().split()))
            if models:
                found[provider] = models
    except OSError as err:
        raise KiloError(f"{kilo}: {err}") from err
    return found


def _kilo_providers(kilo: str, check) -> list[str]:
    """Every provider Kilo knows: the part before the first `/` of `kilo models`."""
    out, err, code = check.run([kilo, "models", "--pure"], os.getcwd(), 120)
    if code != 0:
        raise KiloError(f"kilo models: exit {code} {check.tail(err)}".strip())
    found = set()
    for line in check.ANSI_RE.sub("", out).splitlines():
        match = _PROVIDER_LINE_RE.match(line.strip())
        if match:
            found.add(match.group(1))
    return sorted(found)


#: The seam. Called `KILO_LIST(repo, providers)`; a test replaces it.
KILO_LIST: Callable[[Path, list[str]], dict[str, dict[str, dict]]] = _kilo_list


def _free_of(model: str, priced_zero: dict[str, str]) -> str:
    if _FREE_SUFFIX_RE.search(model):
        return FREE_YES
    return FREE_MAYBE if model in priced_zero else FREE_NO


def _records_of(listing: dict[str, dict[str, dict]], now: float) -> list[dict]:
    """Kilo's `{provider: {model: metadata}}` as cache records, sorted."""
    check = _model_check()
    records = []
    for provider, meta in listing.items():
        # `free_from_kilo`: price 0 on a text tool-calling model; its own FREE /
        # MAYBE split is by name, which `_free_of` redoes with the suffix rule.
        zero = {row[0]: row[1] for row in check.free_from_kilo(meta)}
        for model, data in meta.items():
            ctx = ((data or {}).get("limit") or {}).get("context")
            records.append({
                "provider": provider,
                "model": model,
                "free": _free_of(model, zero),
                "ctx": ctx if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0 else 0,
                "at": now,
            })
    return sorted(records, key=lambda r: (r["provider"], r["model"]))


def _ask_kilo(repo: Path, providers: list[str]) -> dict[str, dict[str, dict]]:
    """`KILO_LIST`, with every way it can fail turned into a `KiloError`."""
    try:
        return KILO_LIST(repo, providers)
    except KiloError:
        raise
    except (OSError, ValueError) as err:
        raise KiloError(str(err) or type(err).__name__) from err


def _refresh(repo: Path, providers: list[str], now: float) -> tuple[list[dict], list[dict]]:
    """(what Kilo lists now, the cache after merging it in). Raises `KiloError`."""
    try:
        listing = _ask_kilo(repo, providers)
    except KiloError as err:
        raise KiloError(f"kilo models failed: {err}") from err
    fresh = _records_of(listing, now)
    return fresh, model_cache(repo).merge(fresh, providers, now)


def list_models(repo: Path, providers: Iterable[str] = (),
                now: Optional[float] = None) -> list[dict]:
    """What Kilo lists now for *providers* (none: every provider), merged into the cache.

    Raises `KiloError`. Records come back sorted by provider, model.
    """
    stamp = time.time() if now is None else now
    return _refresh(repo, [p for p in providers if p], stamp)[0]


# ── names ────────────────────────────────────────────────────────────────────
def split_names(raw: str) -> list[str]:
    """`a,a,b` → `['a', 'a', 'b']`: the `--models` shape, order and repeats kept."""
    return [item.strip() for item in raw.split(",") if item.strip()]


def base_of(entry: str) -> str:
    """`name@variant` → `name`: the reasoning variant is not part of the name."""
    return entry.partition("@")[0].strip()


def _hints(name: str, pool: list[str]) -> list[str]:
    """Up to 3 names that `name` may have meant: a missing suffix, then close ones."""
    by_lower: dict[str, str] = {}
    for item in sorted(pool):
        by_lower.setdefault(item.lower(), item)
    low = name.lower()
    out = [by_lower[c] for c in (f"{low}:free", f"{low}-free") if c in by_lower]
    for close in difflib.get_close_matches(low, list(by_lower), n=3):
        if by_lower[close] not in out:
            out.append(by_lower[close])
    return out[:3]


def _check_known(entries: list[str], records: list[dict]) -> Optional[str]:
    """`None` when every entry is an exact model name, else the first refusal line."""
    names = {r["model"] for r in records}
    fulls = {f"{r['provider']}/{r['model']}" for r in records}
    for entry in entries:
        base = base_of(entry)
        if base in names or base in fulls:
            continue
        hints = _hints(base, sorted(fulls if "/" in base else names))
        if hints:
            return f"{base!r} is not a model — did you mean: {', '.join(hints)}"
        return f"{base!r} is not a model (arena model available --search {base})"
    return None


def resolve_names(repo: Path, entries: list[str],
                  now: Optional[float] = None) -> list[str]:
    """Check every entry against the model list; return them as typed.

    The cache answers first; Kilo is called only when it has no record for a
    name, and what it lists is merged back into the cache. Raises `ModelError`
    with the one-line refusal and a hint (`did you mean`, or the `--search`
    line). No name is ever substituted: the operator retypes it.
    """
    for entry in entries:
        variant = entry.partition("@")[2]
        if "@" in entry and not _VARIANT_RE.fullmatch(variant):
            raise ModelError(f"{entry!r} has a bad variant (letters, digits, _ . - only)")
        if not base_of(entry) or re.search(r"\s", entry):
            raise ModelError(f"{entry!r} is not a model (arena model available --search ...)")
    cache = model_cache(repo)
    records = cache.load(now)
    known = {r["model"] for r in records} | {f"{r['provider']}/{r['model']}" for r in records}
    if any(base_of(e) not in known for e in entries):
        stamp = time.time() if now is None else now
        records = _refresh(repo, [], stamp)[1]
    refusal = _check_known(entries, records)
    if refusal:
        raise ModelError(refusal)
    return entries


def judge_models(repo: Path) -> dict[str, str]:
    """`{model id as the ini spells it: role}` for the gate, ticket writer, reviewer.

    Empty when no ini names one — a fresh checkout has no roster to read.
    """
    parser = _read_ini(repo)
    found: dict[str, str] = {}
    for key, role in JUDGE_KEYS:
        section = parser.get("contest", key, fallback="").strip()
        model = os.path.expandvars(parser.get(section, "model", fallback="")).strip() \
            if section and parser.has_section(section) else ""
        if model:
            found.setdefault(model, role)
    return found


def check_not_judge(repo: Path, entries: list[str]) -> None:
    """Refuse a gate / writer / reviewer model: a judge does not compete."""
    judges = judge_models(repo)
    for entry in entries:
        base = base_of(entry)
        for model, role in judges.items():
            if base in (model, model.partition("/")[2]) or base.partition("/")[2] == model:
                raise ModelError(f"{base!r} is {role} — a judge cannot be one of the round's models")


# ── the profile's `models =` line, in contest.local.ini only ─────────────────
def _with_models(text: str, name: str, value: str) -> str:
    """*text* (a `contest.local.ini`) with `[arena.profile.NAME]`'s `models =` set.

    An edit of the text, not a parse and re-print: every other line, comment and
    key (an `api_key` among them) stays exactly as it was. A missing section is
    appended; a missing `models` key is added at the end of its section.
    """
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    header = re.compile(r"^\[" + re.escape(profile.SECTION_PREFIX + name) + r"\]\s*(?:[;#].*)?$")
    start = next((i for i, ln in enumerate(lines) if header.match(ln.rstrip("\r\n"))), None)
    new_line = f"models = {value}{nl}"
    if start is None:
        if text and not text.endswith("\n"):
            text += nl
        return text + (nl if text else "") + f"[{profile.SECTION_PREFIX}{name}]{nl}" + new_line
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    key = re.compile(r"^models\s*[=:]", re.IGNORECASE)
    at = next((i for i in range(start + 1, end) if key.match(lines[i])), None)
    if at is not None:
        stop = at + 1
        # an indented line under the key is the rest of its value
        while stop < end and lines[stop].strip() and lines[stop][0] in " \t" \
                and not lines[stop].lstrip().startswith(("#", ";")):
            stop += 1
        lines[at:stop] = [new_line]
    else:
        last = start
        for i in range(start + 1, end):
            if lines[i].strip():
                last = i
        if not lines[last].endswith("\n"):
            lines[last] += nl
        lines.insert(last + 1, new_line)
    return "".join(lines)


def write_models(repo: Path, name: str, value: str) -> None:
    """Set the profile's `models =` in `contest.local.ini`, atomically, mode kept."""
    path = repo / roster.LOCAL_FILENAME
    try:
        text = path.read_bytes().decode("utf-8") if path.exists() else ""
        data = _with_models(text, name, value).encode("utf-8")
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(repo))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            if path.exists():
                shutil.copymode(str(path), tmp)
            os.replace(tmp, str(path))
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except (OSError, UnicodeDecodeError) as err:
        raise ModelError(f"cannot write {roster.LOCAL_FILENAME}: {err}") from err


def _confirm(name: str, before: str, after: str, yes: bool) -> bool:
    """Print `models =` before → after; ask unless *yes*. EOF is a no."""
    print(f"profile {name!r} — models =")
    print(f"  before: {before or '(none)'}")
    print(f"  after:  {after}")
    if yes:
        return True
    try:
        return input("apply? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ── the three verbs ──────────────────────────────────────────────────────────
def _selected_profile(repo: Path, args: argparse.Namespace) -> tuple[dict, str]:
    """(profiles, the name `use` / `drop` act on): `-p`, else `[arena] profile`, else `default`."""
    profiles, active = profile.load_profiles(repo)
    return profiles, args.profile or active


def available(repo: Path, args: argparse.Namespace) -> int:
    """`arena model available [PROVIDER…] [--free] [--search TEXT]`."""
    try:
        records = list_models(repo, args.providers)
    except ModelError as err:
        return output.refuse(str(err))
    try:  # IN-PROFILE is a courtesy: a missing or broken ini only leaves it empty
        profiles, active = profile.load_profiles(repo)
        chosen = profiles.get(args.profile or active, {})
    except profile.ProfileError:
        chosen = {}
    in_profile = {base_of(e) for e in split_names(chosen.get("models", ""))}
    needle = (args.search or "").lower()
    rows = []
    for r in records:
        full = f"{r['provider']}/{r['model']}"
        if args.free and r["free"] == FREE_NO:
            continue
        if needle and needle not in full.lower():
            continue
        rows.append({
            "NAME": r["model"],
            "PROVIDER": r["provider"],
            "FREE": r["free"],
            "CTX": r["ctx"] or None,
            "IN-PROFILE": "yes" if {r["model"], full} & in_profile else "",
            "LAST-TEST": "",
        })
    if not rows:
        print("arena: no model matches", file=sys.stderr)
        return EXIT_NOTHING
    output.emit(rows, AVAILABLE_COLUMNS, args.output)
    return EXIT_OK


def use(repo: Path, args: argparse.Namespace) -> int:
    """`arena model use NAME[,NAME…] [-p PROFILE] [-y]`: set the profile's models."""
    try:
        entries = split_names(args.names)
        if not entries:
            raise ModelError("no model named (arena model available)")
        profiles, name = _selected_profile(repo, args)
        if not _PROFILE_NAME_RE.fullmatch(name):
            raise ModelError(f"{name!r} is not a usable profile name")
        resolve_names(repo, entries)
        check_not_judge(repo, entries)
        before = profiles.get(name, {}).get("models", "").strip()
        after = ",".join(entries)
        if before == after:
            print(f"profile {name!r}: models = {after} (unchanged)")
            return EXIT_OK
        if not _confirm(name, before, after, args.yes):
            raise ModelError(f"not applied — {roster.LOCAL_FILENAME} unchanged")
        write_models(repo, name, after)
    except (ModelError, profile.ProfileError) as err:
        return output.refuse(str(err))
    return EXIT_OK


def drop(repo: Path, args: argparse.Namespace) -> int:
    """`arena model drop NAME[,NAME…] [-p PROFILE] [-y]`: remove names from the profile."""
    try:
        entries = split_names(args.names)
        if not entries:
            raise ModelError("no model named (arena model available)")
        profiles, name = _selected_profile(repo, args)
        if name not in profiles:
            known = ", ".join(sorted(profiles)) or "none"
            raise ModelError(f"unknown profile {name!r} (known: {known})")
        resolve_names(repo, entries)
        current = split_names(profiles[name].get("models", ""))
        gone = {base_of(e) for e in entries}
        for entry in entries:
            if base_of(entry) not in {base_of(c) for c in current}:
                raise ModelError(
                    f"profile {name!r} has no {base_of(entry)!r} "
                    f"(its models: {', '.join(current) or 'none'})"
                )
        kept = [c for c in current if base_of(c) not in gone]
        if not kept:
            raise ModelError(f"profile {name!r} would be empty — 1 model must stay")
        before, after = ",".join(current), ",".join(kept)
        if not _confirm(name, before, after, args.yes):
            raise ModelError(f"not applied — {roster.LOCAL_FILENAME} unchanged")
        write_models(repo, name, after)
    except (ModelError, profile.ProfileError) as err:
        return output.refuse(str(err))
    return EXIT_OK
