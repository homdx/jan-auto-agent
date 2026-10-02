"""tools/arena/models.py — AR-59: `arena model available|use|drop`, the list from Kilo.

The model list of a round used to be typed into `--models` or a profile by
hand, copied from `kilo models` output, and a typo surfaced only when the
round's intake failed. Here the list comes from Kilo itself and a profile's
`models =` is written by name, every name checked first.

Where the list comes from: `kilo models PROVIDER --verbose`, through
`scripts/py_model_test.py`'s own `kilo_models` (imported, not copied), and the
price-0 rule through its `free_from_kilo`. `KILO_LIST` is the one seam the
tests replace: `KILO_LIST(repo, providers) -> {provider: {model: meta}}`, or a
`KiloError` whose message is the one refusal line.

Free is `yes` for a name ending in `:free` / `-free`, `maybe` for a price of 0
without that suffix (Kilo shows 0 for an unknown price too), else `no`.

The cache follows `tools/contest/context_memory.py` (KC-67):
`.arena/models-cache.json` is a JSON list of `{provider, model, free, ctx,
at}`; records older than `model_cache_days` (`[arena]`, default 7; 0 or junk
= no cache) are dropped on every read and every write; the write is a
`mkstemp` next to the file and an `os.replace`; a missing, unreadable or
non-list file is an empty cache, never an error. `available` refreshes the
providers it lists; `use` / `drop` ask Kilo only for a name the cache lacks.

Everything works with no `contest.ini` and no `contest.local.ini`: the list
needs no ini, `use` creates `contest.local.ini` holding only the profile it
writes, and the judge check has no roster to read so it refuses nothing.
Writes go to `contest.local.ini` only — never `contest.ini`, never
`agents_128k.ini`.
"""

from __future__ import annotations

import argparse
import configparser
import difflib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from scripts import py_model_test
from tools.contest import roster

from . import output, profile

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_NOTHING = 0, 1, 2, 3

#: The cache file, under the checkout's `.arena/`.
CACHE_FILE = Path(".arena") / "models-cache.json"
#: Days a cache record is kept when `[arena] model_cache_days` is unset.
DEFAULT_CACHE_DAYS = 7.0
#: A name that says it is free (`x:free`, `y-free`).
_FREE_SUFFIX = re.compile(r"[:\-]free$", re.IGNORECASE)

LIST_COLUMNS = ["NAME", "PROVIDER", "FREE", "CTX", "IN-PROFILE", "LAST-TEST"]


class KiloError(Exception):
    """Kilo could not list the models; the message is the one refusal line."""


class ModelError(Exception):
    """A `use` / `drop` refusal; the message is the one line the CLI prints."""


# ── the ini files (both optional) ────────────────────────────────────────────
def _parser(repo: Path) -> configparser.ConfigParser:
    """`contest.ini` then `contest.local.ini` in one roster parser; missing files skipped."""
    parser = roster._new_parser()
    for name in ("contest.ini", roster.LOCAL_FILENAME):
        path = Path(repo) / name
        if path.is_file():
            try:
                parser.read(path, encoding="utf-8")
            except configparser.Error as err:
                raise ModelError(f"{name}: {str(err).splitlines()[0]}") from err
    return parser


def cache_days(repo: Path) -> float:
    """`[arena] model_cache_days`, 7 when unset; 0 for 0, a negative or junk."""
    try:
        raw = _parser(repo).get("arena", "model_cache_days", fallback=None)
    except ModelError:
        raw = None
    if raw is None or not raw.strip():
        return DEFAULT_CACHE_DAYS
    try:
        days = float(raw)
    except ValueError:
        return 0.0
    return days if days > 0 else 0.0


def kilo_bin(repo: Path) -> str:
    """`[contest] kilo_bin` when set (and not `auto`), else `kilo` on PATH, else VS Code's."""
    try:
        explicit = _parser(repo).get("contest", "kilo_bin", fallback="").strip()
    except ModelError:
        explicit = ""
    if explicit and explicit != "auto":
        return explicit
    try:
        return py_model_test.find_kilo(None)
    except SystemExit:
        raise KiloError("kilo not found: set [contest] kilo_bin or put kilo on PATH") from None


# ── Kilo ─────────────────────────────────────────────────────────────────────
def _kilo_list(repo: Path, providers: list[str]) -> dict[str, dict[str, dict]]:
    """`{provider: {model: verbose meta}}` from Kilo; every provider when *providers* is empty."""
    kilo = kilo_bin(repo)
    if not providers:
        try:
            out, err, rc = py_model_test.run([kilo, "models", "--pure"], str(repo), 120)
        except OSError as exc:
            raise KiloError(f"kilo models: {exc}") from exc
        if rc != 0:
            first = (err.strip().splitlines() or [f"exit {rc}"])[0]
            raise KiloError(f"kilo models: {first}")
        providers = sorted({line.split("/", 1)[0] for line in out.splitlines()
                            if "/" in line.strip()})
    listing: dict[str, dict[str, dict]] = {}
    for provider in providers:
        try:
            # `kilo_models` reports a failing exit on stderr and returns {}
            meta = py_model_test.kilo_models(kilo, provider, True)
        except OSError as exc:
            raise KiloError(f"kilo models {provider}: {exc}") from exc
        if not meta:
            raise KiloError(f"kilo models {provider} listed nothing")
        listing[provider] = meta
    return listing


#: The seam the tests replace: `(repo, providers) -> {provider: {model: meta}}`.
KILO_LIST: Callable[[Path, list[str]], dict[str, dict[str, dict]]] = _kilo_list


def records_from(listing: dict[str, dict[str, dict]], now: float) -> list[dict]:
    """One cache record per listed model, freeness decided here."""
    records = []
    for provider, meta in listing.items():
        zero = {mid for mid, *_ in py_model_test.free_from_kilo(meta)}
        for model, info in meta.items():
            if _FREE_SUFFIX.search(model):
                free = "yes"
            elif model in zero:
                free = "maybe"
            else:
                free = "no"
            ctx = ((info or {}).get("limit") or {}).get("context") or 0
            records.append({"provider": provider, "model": model, "free": free,
                            "ctx": int(ctx) if isinstance(ctx, (int, float)) else 0,
                            "at": now})
    return records


# ── the cache ────────────────────────────────────────────────────────────────
def _fresh(records, days: float, now: float) -> list[dict]:
    cut = now - days * 86400
    keep = []
    for r in records:
        if (isinstance(r, dict) and isinstance(r.get("provider"), str)
                and isinstance(r.get("model"), str)
                and isinstance(r.get("at"), (int, float)) and r["at"] >= cut):
            keep.append(r)
    return keep


def load_cache(repo: Path, *, days: Optional[float] = None,
               now: Optional[float] = None) -> list[dict]:
    """The cache's fresh records; `[]` for no cache, a missing or a broken file."""
    days = cache_days(repo) if days is None else days
    if days <= 0:
        return []
    try:
        data = json.loads((Path(repo) / CACHE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return _fresh(data, days, time.time() if now is None else now)


def save_cache(repo: Path, records: list[dict], *, days: Optional[float] = None,
               now: Optional[float] = None) -> None:
    """Write *records* (aged out first) atomically; a write that fails is skipped."""
    days = cache_days(repo) if days is None else days
    if days <= 0:
        return
    path = Path(repo) / CACHE_FILE
    keep = _fresh(records, days, time.time() if now is None else now)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(keep, fh)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError:
        return  # fail-open: no cache is never a failed command


def merge(cache: list[dict], fresh: list[dict], providers: Optional[list[str]]) -> list[dict]:
    """*cache* with the refreshed providers' records replaced by *fresh*."""
    refreshed = {r["provider"] for r in fresh} | set(providers or [])
    return [r for r in cache if r["provider"] not in refreshed] + fresh


def list_models(repo: Path, providers: list[str]) -> list[dict]:
    """Kilo's list for *providers* (all when empty), merged into the cache."""
    now = time.time()
    fresh = records_from(KILO_LIST(Path(repo), list(providers)), now)
    cache = [] if not providers else load_cache(repo, now=now)
    save_cache(repo, merge(cache, fresh, providers), now=now)
    return fresh


# ── names ────────────────────────────────────────────────────────────────────
def _full(r: dict) -> str:
    return f"{r['provider']}/{r['model']}"


def _matches(name: str, r: dict) -> bool:
    """`provider/model` matches exactly; a bare name matches the model id."""
    return name == _full(r) or ("/" not in name and name == r["model"]) or (
        name.split("/", 1)[0] == r["provider"] and name.split("/", 1)[1] == r["model"])


def _same_entry(entry: str, name: str) -> bool:
    """A profile entry and a name are one model (a bare name ignores the provider)."""
    if entry == name:
        return True
    bare = lambda s: s.split("/", 1)[1] if "/" in s else s  # noqa: E731
    return ("/" not in name or "/" not in entry) and bare(entry) == bare(name)


def _hint(name: str, records: list[dict]) -> str:
    candidates = sorted({r["model"] for r in records} | {_full(r) for r in records})
    close = [c for c in candidates
             if c.startswith(name) and c[len(name):len(name) + 1] in (":", "-")]
    close = close[:3] or difflib.get_close_matches(name, candidates, n=3)
    if close:
        return f"{name!r} is not a model — did you mean: {', '.join(close)}"
    return f"{name!r} is not a model (arena model available --search {name})"


def resolve_names(repo: Path, names: list[str]) -> list[dict]:
    """The record of every name, Kilo asked only for names the cache lacks.

    `ModelError` with a hint for the first name that is no exact model; no name
    is ever substituted.
    """
    records = load_cache(repo)
    if any(not any(_matches(n, r) for r in records) for n in names):
        try:
            listed = list_models(repo, [])
        except KiloError as err:
            raise ModelError(str(err)) from err
        records = merge(records, listed, None)
    found = []
    for name in names:
        hit = next((r for r in records if _matches(name, r)), None)
        if hit is None:
            raise ModelError(_hint(name, records))
        found.append(hit)
    return found


def _split(names: str) -> list[str]:
    return [n.strip() for n in names.split(",") if n.strip()]


def judge_models(repo: Path) -> set[str]:
    """The `model` of every `*_llm` profile section but the agents' own (gate, reviewer, writer)."""
    parser = _parser(repo)
    found = set()
    for section in parser.sections():
        if section.endswith("_llm") and section != "contest_openrouter_llm":
            model = parser.get(section, "model", fallback="").strip()
            if model:
                found.add(model)
    return found


# ── the profile in contest.local.ini ─────────────────────────────────────────
def _selected(repo: Path, args: argparse.Namespace) -> tuple[dict[str, dict[str, str]], str]:
    try:
        profiles, active = profile.load_profiles(Path(repo))
    except profile.ProfileError as err:
        raise ModelError(str(err)) from err
    return profiles, (args.model_profile or args.profile or active)


def write_models(repo: Path, name: str, models: list[str]) -> None:
    """Set `[arena.profile.NAME] models =` in `contest.local.ini`, the rest of the text kept."""
    path = Path(repo) / roster.LOCAL_FILENAME
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = text.splitlines()
    header = f"[{profile.SECTION_PREFIX}{name}]"
    value = f"models = {','.join(models)}"
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == header)
    except StopIteration:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [header, value]
    else:
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].lstrip().startswith("[")), len(lines))
        at = next((i for i in range(start + 1, end)
                   if re.match(r"\s*models\s*[=:]", lines[i])), None)
        if at is None:
            lines.insert(start + 1, value)
        else:
            lines[at] = value
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _confirm(name: str, before: str, after: str, yes: bool) -> bool:
    print(f"[{profile.SECTION_PREFIX}{name}] models = {before or '(none)'} → {after}")
    if yes:
        return True
    try:
        return input("apply? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ── the verbs ────────────────────────────────────────────────────────────────
def available(repo: Path, args: argparse.Namespace) -> int:
    try:
        rows_in = list_models(repo, args.providers)
        profiles, _ = profile.load_profiles(Path(repo))
    except (KiloError, profile.ProfileError, ModelError) as err:
        return output.refuse(str(err))
    rows = []
    for r in sorted(rows_in, key=lambda r: (r["provider"], r["model"])):
        if args.free and r["free"] == "no":
            continue
        if args.search and args.search.lower() not in _full(r).lower():
            continue
        users = [p for p, values in sorted(profiles.items())
                 if any(_matches(m, r) for m in _split(values.get("models", "")))]
        rows.append({"NAME": r["model"], "PROVIDER": r["provider"], "FREE": r["free"],
                     "CTX": r["ctx"] or "", "IN-PROFILE": ",".join(users), "LAST-TEST": ""})
    if not rows:
        print("arena: no model matches", file=sys.stderr)
        return EXIT_NOTHING
    output.emit(rows, LIST_COLUMNS, args.output)
    return EXIT_OK


def use(repo: Path, args: argparse.Namespace) -> int:
    try:
        names = _split(args.names)
        if not names:
            raise ModelError("no model named")
        profiles, name = _selected(repo, args)
        resolve_names(repo, names)
        judges = judge_models(repo)
        for n in names:
            if any(n == j or _same_entry(j, n) for j in judges):
                raise ModelError(f"{n!r} is a judge model (gate / reviewer / writer) — "
                                 "not a round agent")
    except (ModelError, profile.ProfileError) as err:
        return output.refuse(str(err))
    before = profiles.get(name, {}).get("models", "")
    if not _confirm(name, before, ",".join(names), args.yes or args.model_yes):
        print("arena: nothing written", file=sys.stderr)
        return EXIT_FAILED
    write_models(repo, name, names)
    return EXIT_OK


def drop(repo: Path, args: argparse.Namespace) -> int:
    try:
        names = _split(args.names)
        if not names:
            raise ModelError("no model named")
        profiles, name = _selected(repo, args)
        if name not in profiles:
            raise ModelError(f"no profile {name!r} in contest.ini or contest.local.ini")
        resolve_names(repo, names)
        current = _split(profiles[name].get("models", ""))
        for n in names:
            if not any(_same_entry(e, n) for e in current):
                raise ModelError(f"{n!r} is not in profile {name!r} "
                                 f"(models: {', '.join(current) or 'none'})")
        left = [e for e in current if not any(_same_entry(e, n) for n in names)]
        if not left:
            raise ModelError(f"profile {name!r} would be empty — 1 model must stay")
    except (ModelError, profile.ProfileError) as err:
        return output.refuse(str(err))
    if not _confirm(name, ",".join(current), ",".join(left), args.yes or args.model_yes):
        print("arena: nothing written", file=sys.stderr)
        return EXIT_FAILED
    write_models(repo, name, left)
    return EXIT_OK


def available_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("providers", nargs="*", metavar="PROVIDER", help="providers (default: all)")
    p.add_argument("--free", action="store_true", help="free and maybe-free models only")
    p.add_argument("--search", metavar="TEXT", help="case-insensitive substring")


def names_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("names", metavar="NAME[,NAME…]", help="model names, comma-separated")
    # own dests: a sub-parser default of `profile`/`yes` would overwrite the globals
    p.add_argument("-p", "--profile", dest="model_profile", metavar="PROFILE",
                   help="profile to change (default: the global -p, then the active one)")
    p.add_argument("-y", "--yes", dest="model_yes", action="store_true",
                   help="apply without asking")
