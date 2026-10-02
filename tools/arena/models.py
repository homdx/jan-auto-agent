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

AR-60 — direct providers with a key, `arena model test`, results on record
  A provider with a key *and* a base URL is listed and tested directly
  (`free_from_direct_api` / `ask_direct` of `py_model_test.py`, imported); any
  other goes through Kilo as above. The key is `[arena.provider.NAME] api_key =
  ${ENV}` (a literal key is a refusal) or env `ARENA_KEY_<NAME>`; the URL is
  `--url` (one provider only), env `ARENA_URL_<NAME>`, or the section's
  `base_url`. No ini file is needed and none is made. A key never reaches
  stdout, stderr, `-o json` or a record: errors are scrubbed and the key
  replaced before they are printed or written.
  `arena model test NAME[,NAME…]` runs the 15-check code task per model (at
  most 4 at once) through the `RUN_TEST` seam and writes
  `.arena/model-scores.json` — `{provider, model, via, score, max, error, at}`,
  kept by the cache's rules, the newest record per model winning — which
  `available` shows in LAST-TEST.

`use` and `drop` write `contest.local.ini` only — never `contest.ini`, never
`agents_128k.ini` — by editing the file's text in place, so a `[contest_gate_llm]`
`api_key` and every comment in it come back byte for byte.
"""

from __future__ import annotations

import argparse
import configparser
import concurrent.futures
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
import threading
import time
import urllib.error
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

#: The columns of `arena model available`.
AVAILABLE_COLUMNS = ["NAME", "PROVIDER", "FREE", "CTX", "IN-PROFILE", "LAST-TEST"]

#: The scores file, next to the model cache.
SCORES_RELPATH = Path(".arena") / "model-scores.json"
#: `arena model test` runs at most this many models at once: every `kilo run`
#: writes the one shared Kilo store (`py_model_test.py -j` is capped the same).
TEST_JOBS = 4
#: Seconds a model has to answer, and attempts on a rate limit (429 / 403).
TEST_TIMEOUT = 240
TEST_ATTEMPTS = 3
#: The columns of `arena model test`.
TEST_COLUMNS = ["NAME", "PROVIDER", "VIA", "SCORE", "ERROR"]

_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_STATUS_RE = re.compile(r"\b([45]\d\d)\b")

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
        _write_json_atomic(self.path, merged)
        return merged


def _write_json_atomic(path: Path, data: list) -> None:
    """*data* as JSON at *path*: `mkstemp` next to it + `os.replace`; an OSError is swallowed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, str(path))
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError:
        pass


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
    _resolve_records(repo, entries, now)
    return entries


def _resolve_records(repo: Path, entries: list[str], now: Optional[float]) -> list[dict]:
    """`resolve_names`'s work; answers the records the names were checked against."""
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
    return records


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


# ── AR-60: direct providers, the key, the scores ─────────────────────────────
def _env_name(provider: str) -> str:
    """`my-prov` → `MY_PROV`: upper-cased, every non-alphanumeric character a `_`."""
    return re.sub(r"[^A-Za-z0-9]", "_", provider).upper()


def _hint(message: str) -> None:
    """One stderr hint line; the command goes on."""
    print(f"arena: {' '.join(output.scrub(message).splitlines())}", file=sys.stderr)


def provider_config(repo: Path, provider: str,
                    url_flag: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """`(base_url, api_key)` of *provider*; either is `None` when nothing gives it.

    Key: `[arena.provider.NAME] api_key` — which must be a `${ENV}` reference,
    a literal key is a refusal naming the section — else env `ARENA_KEY_<NAME>`.
    URL: `--url`, else env `ARENA_URL_<NAME>`, else the section's `base_url`.
    """
    parser = _read_ini(repo)
    wanted = f"arena.provider.{provider}".lower()
    section = next((sec for sec in parser.sections() if sec.lower() == wanted), None)
    ini_key = ini_url = ""
    if section:
        ini_key = parser.get(section, "api_key", fallback="").strip()
        ini_url = parser.get(section, "base_url", fallback="").strip()
    key = ""
    if ini_key:
        ref = _ENV_REF_RE.fullmatch(ini_key)
        if not ref:
            raise ModelError(
                f"[{section}] api_key must be a ${{ENV}} reference, not the key itself"
            )
        key = os.environ.get(ref.group(1), "").strip()
    else:
        key = os.environ.get(f"ARENA_KEY_{_env_name(provider)}", "").strip()
    url = (url_flag or os.environ.get(f"ARENA_URL_{_env_name(provider)}", "")
           or os.path.expandvars(ini_url)).strip()
    return (url or None), (key or None)


def _hide(text: str, key: Optional[str]) -> str:
    """*text* with the key (and URL credentials) taken out, on one line."""
    if key:
        text = text.replace(key, output.MASK)
    return " ".join(output.scrub(text).split())


def _direct_records(url: str, key: str, now: float) -> list[dict]:
    """What the provider's own `/models` lists, as records (`via` = direct).

    `free_from_direct_api` prints a progress line; it is swallowed. It raises on
    a network or HTTP error (the caller turns that into a hint).
    """
    check = _model_check()
    with contextlib.redirect_stdout(io.StringIO()):
        rows = check.free_from_direct_api(url, key)
    return [{
        "provider": "", "model": mid,
        "free": FREE_YES if status == check.FREE else FREE_MAYBE,
        "ctx": ctx if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0 else 0,
        "at": now, "via": "direct",
    } for mid, status, ctx, _note in rows]


def _direct_for(provider: str, url: str, key: str, now: float) -> list[dict]:
    """`_direct_records` for *provider*, the failure raised as a one-line `ModelError`."""
    try:
        recs = _direct_records(url, key, now)
    except urllib.error.HTTPError as err:
        raise ModelError(f"{provider}: direct list failed: HTTP {err.code}") from err
    except (OSError, ValueError) as err:
        raise ModelError(f"{provider}: direct list failed: {_hide(str(err), key)}") from err
    for r in recs:
        r["provider"] = provider
    return recs


def _note_missing_from_kilo(repo: Path, provider: str, found: Iterable[str]) -> None:
    """A model found directly but not in `kilo.jsonc`: a round cannot run it."""
    try:
        known = set(_ask_kilo(repo, [provider]).get(provider, {}))
    except KiloError:
        return  # Kilo not reachable: nothing to compare against, no hint
    for model in found:
        if model not in known:
            _hint(f"{provider}/{model} is not in kilo.jsonc — a round cannot run it "
                  "until it is added there")


def _refresh_known(repo: Path, providers: list[str], now: float) -> list[dict]:
    """`_refresh` of *providers*, skipping the ones Kilo does not know.

    `kilo models NAME` fails for a provider missing from `kilo.jsonc`; that is
    the no-key / no-URL hint's case, and the command goes on with the others.
    Only when Kilo itself fails (its full list fails too, or every provider is
    known) is it the refusal it was in AR-59.
    """
    try:
        return _refresh(repo, providers, now)[0]
    except KiloError as err:
        if not providers:
            raise
        try:
            known = set(_ask_kilo(repo, []))
        except KiloError:
            raise err from None
        rest = [p for p in providers if p in known]
        if len(rest) == len(providers):
            raise
        return _refresh(repo, rest, now)[0] if rest else []


def collect_models(repo: Path, providers: list[str], url_flag: Optional[str],
                   now: float) -> tuple[list[dict], dict[str, tuple[str, str]]]:
    """`(records, {direct provider: (url, key)})` for *providers* (none: all of Kilo).

    A provider with a key and a URL is listed directly; any other goes through
    Kilo. Records carry `via`. Hints go to stderr one line each. Raises `ModelError`.
    """
    if url_flag and len(set(providers)) != 1:
        raise ModelError("--url needs exactly one provider")
    direct: dict[str, tuple[str, str]] = {}
    via_kilo: list[str] = []
    keyless: list[str] = []
    for provider in dict.fromkeys(providers):
        url, key = provider_config(repo, provider, url_flag)
        if url and key:
            direct[provider] = (url, key)
            continue
        via_kilo.append(provider)
        if key and not url:
            name = _env_name(provider)
            _hint(f"no URL for '{provider}' — export ARENA_URL_{name}=… or pass --url")
        elif not key:
            keyless.append(provider)
    records: list[dict] = []
    if via_kilo or not providers:
        fresh = _refresh_known(repo, via_kilo, now)
        records += [dict(r, via="kilo") for r in fresh]
        listed = {r["provider"] for r in fresh}
        for provider in keyless:
            if provider not in listed:
                name = _env_name(provider)
                _hint(f"no key for '{provider}' — export ARENA_KEY_{name}=… or add "
                      f"[arena.provider.{provider}] api_key = ${{ARENA_KEY_{name}}} to "
                      f"{roster.LOCAL_FILENAME}")
    for provider, (url, key) in list(direct.items()):
        try:
            recs = _direct_for(provider, url, key, now)
        except ModelError as err:
            _hint(f"{err} — trying Kilo")
            fresh = _refresh_known(repo, [provider], now)
            records += [dict(r, via="kilo") for r in fresh]
            del direct[provider]
            continue
        records += recs
        _note_missing_from_kilo(repo, provider, [r["model"] for r in recs])
    return sorted(records, key=lambda r: (r["provider"], r["model"])), direct


class ScoreStore:
    """`.arena/model-scores.json`: the newest test of each model, for `model_cache_days`."""

    def __init__(self, path: Path, days: float) -> None:
        self.path = Path(path)
        self.days = days
        self._lock = threading.Lock()

    def load(self, now: Optional[float] = None) -> list[dict]:
        """Records inside the age cut, the newest per provider+model; `[]` if unreadable."""
        if self.days <= 0:
            return []
        stamp = time.time() if now is None else now
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        newest: dict[tuple[str, str], dict] = {}
        for entry in data:
            rec = _clean_score(entry)
            if rec is None or stamp - rec["at"] > self.days * _DAY:
                continue
            key = (rec["provider"], rec["model"])
            if key not in newest or rec["at"] >= newest[key]["at"]:
                newest[key] = rec
        return list(newest.values())

    def add(self, record: dict, now: Optional[float] = None) -> None:
        """Add one record: age the file, keep the newest per model, write atomically."""
        if self.days <= 0:
            return
        with self._lock:
            kept = [r for r in self.load(now)
                    if (r["provider"], r["model"]) != (record["provider"], record["model"])]
            _write_json_atomic(self.path, kept + [record])


def _clean_score(entry: object) -> Optional[dict]:
    if not isinstance(entry, dict):
        return None
    provider, model, at = entry.get("provider"), entry.get("model"), entry.get("at")
    if not (isinstance(provider, str) and provider and isinstance(model, str) and model):
        return None
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return None

    def count(value: object) -> Optional[int]:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    error = entry.get("error")
    return {
        "provider": provider, "model": model,
        "via": "direct" if entry.get("via") == "direct" else "kilo",
        "score": count(entry.get("score")), "max": count(entry.get("max")),
        "error": error if isinstance(error, str) else "", "at": at,
    }


def score_store(repo: Path) -> ScoreStore:
    return ScoreStore(repo / SCORES_RELPATH, cache_days(repo))


def _age(at: float, now: float) -> str:
    """`2d`, `5h`, `7m`: how long ago, in the biggest whole unit."""
    secs = max(0.0, now - at)
    for unit, size in (("d", _DAY), ("h", 3600.0), ("m", 60.0)):
        if secs >= size:
            return f"{int(secs // size)}{unit}"
    return "0m"


def _short_error(error: str) -> str:
    """`HTTP 401: …` → `401`; a timeout → `timeout`; the rest, its first words."""
    status = _STATUS_RE.search(error)
    if status:
        return status.group(1)
    if error.startswith("timeout"):
        return "timeout"
    if error.startswith(("no ```", "empty")):
        return "no code"
    return " ".join(error.split()[:2])[:14] or "failed"


def last_test_cell(record: Optional[dict], now: float) -> str:
    """The LAST-TEST cell: `14/15 2d`, `401 2d`, or empty with no record."""
    if record is None:
        return ""
    if record["score"] is not None and record["max"]:
        what = f"{record['score']}/{record['max']}"
    else:
        what = _short_error(record["error"])
    return f"{what} {_age(record['at'], now)}"


def _run_test(repo: Path, spec: dict) -> dict:
    """The default `RUN_TEST`: one 15-check code task, `{score, max, error}`.

    `spec` is `{provider, model, via, url, key}`. Direct: `ask_direct` against
    the provider; Kilo: `kilo run -m provider/model`. Both use `py_model_test.py`'s
    prompt, answer extraction and checker, imported.
    """
    check = _model_check()
    provider, model, key = spec["provider"], spec["model"], spec.get("key")
    with tempfile.TemporaryDirectory(prefix="arena-mtest-") as work:
        raw, error = "", ""
        if spec["via"] == "direct":
            try:
                raw = check.ask_direct(spec["url"], key, model, check.PROMPT, TEST_TIMEOUT)
            except urllib.error.HTTPError as err:
                error = f"HTTP {err.code}"
            except Exception as err:  # noqa: BLE001 — any failure is the model's reason
                error = str(err) or type(err).__name__
        else:
            kilo = find_kilo_bin(repo)
            for attempt in range(1, TEST_ATTEMPTS + 1):
                out, err_text, rc = check.run(
                    [kilo, "run", "--pure", "--format", "json", "-m", f"{provider}/{model}",
                     check.PROMPT], work, TEST_TIMEOUT, dict(os.environ))
                raw, _got, ev_err = check.parse_events(check.ANSI_RE.sub("", out))
                err_text = (ev_err + "\n" + err_text) if ev_err else err_text
                if check.pick_code(raw) or rc is None or attempt == TEST_ATTEMPTS \
                        or not check.RETRY_RE.search(err_text):
                    break
                time.sleep(min(30 * attempt, 300))
            if not check.pick_code(raw):
                if rc is None:
                    error = f"timeout {TEST_TIMEOUT}s"
                elif rc != 0:
                    error = f"kilo exit {rc}: {check.tail(err_text, 1)[:100]}"
        if error:
            return {"score": None, "max": None, "error": _hide(error, key)}
        checker = os.path.join(work, "checker.py")
        with open(checker, "w", encoding="utf-8") as fh:
            fh.write(check.CHECKER)
        score, detail = check._score_raw(raw, checker, work)
        match = re.fullmatch(r"(\d+)/(\d+)", score)
        if match:
            return {"score": int(match.group(1)), "max": int(match.group(2)), "error": ""}
        return {"score": None, "max": None, "error": _hide(detail or score, key)}


#: The seam. Called `RUN_TEST(repo, spec)`; a test replaces it.
RUN_TEST: Callable[[Path, dict], dict] = _run_test


def run_tests(repo: Path, specs: list[dict], now: Optional[float] = None) -> list[dict]:
    """Test every spec (at most `TEST_JOBS` at once); write each record as it lands.

    Returns the records in the order of *specs*. A test that raises is a record
    with an `error`, never a traceback.
    """
    store = score_store(repo)

    def one(spec: dict) -> dict:
        try:
            got = RUN_TEST(repo, spec)
            score, top, error = got.get("score"), got.get("max"), str(got.get("error") or "")
        except Exception as err:  # noqa: BLE001 — a crashed test is a reason, not a crash
            score, top, error = None, None, str(err) or type(err).__name__
        if score is None:
            error = error or "no score"
        record = {
            "provider": spec["provider"], "model": spec["model"], "via": spec["via"],
            "score": score, "max": top if score is not None else None,
            "error": _hide(error, spec.get("key")),
            "at": time.time() if now is None else now,
        }
        store.add(record, record["at"])
        return record

    with concurrent.futures.ThreadPoolExecutor(max_workers=TEST_JOBS) as pool:
        return list(pool.map(one, specs))


def resolve_test_targets(repo: Path, entries: list[str], url_flag: Optional[str],
                         now: float) -> list[dict]:
    """The specs `model test` runs; every name checked first. Raises `ModelError`."""
    for entry in entries:
        if "@" in entry:
            raise ModelError(f"{entry!r}: a variant is not tested — name the model")
    names = list(dict.fromkeys(entries))
    providers = {n.partition("/")[0] for n in names if "/" in n}
    if url_flag and len(providers) != 1:
        raise ModelError("--url needs exactly one provider")
    configs: dict[str, tuple[str, str]] = {}
    for provider in sorted(providers):
        url, key = provider_config(repo, provider, url_flag)
        if url and key:
            configs[provider] = (url, key)
        elif key and not url:
            name = _env_name(provider)
            _hint(f"no URL for '{provider}' — export ARENA_URL_{name}=… or pass --url")
    # direct names are checked against the provider's own list, the rest against Kilo's
    direct_pool: dict[str, list[dict]] = {}
    for provider, (url, key) in configs.items():
        direct_pool[provider] = _direct_for(provider, url, key, now)
    direct_names = [n for n in names if "/" in n and n.partition("/")[0] in configs]
    kilo_names = [n for n in names if n not in direct_names]
    for provider, recs in direct_pool.items():
        mine = [n for n in direct_names if n.partition("/")[0] == provider]
        refusal = _check_known(mine, recs)
        if refusal:
            raise ModelError(refusal)
    kilo_records = _resolve_records(repo, kilo_names, now) if kilo_names else []
    specs: list[dict] = []
    for name in names:
        if name in direct_names:
            provider, _, model = name.partition("/")
            url, key = configs[provider]
            specs.append({"provider": provider, "model": model, "via": "direct",
                          "url": url, "key": key})
            continue
        if "/" in name and any(f"{r['provider']}/{r['model']}" == name for r in kilo_records):
            provider, _, model = name.partition("/")
        else:
            owners = [r for r in kilo_records if r["model"] == name]
            if len(owners) > 1:
                where = ", ".join(sorted(f"{r['provider']}/{r['model']}" for r in owners))
                raise ModelError(f"{name!r} is on several providers ({where}) — name one")
            if not owners:
                raise ModelError(f"{name!r} is not a model (arena model available --search {name})")
            provider, model = owners[0]["provider"], owners[0]["model"]
        specs.append({"provider": provider, "model": model, "via": "kilo",
                      "url": None, "key": None})
    for provider in direct_pool:
        _note_missing_from_kilo(repo, provider,
                                [s["model"] for s in specs
                                 if s["provider"] == provider and s["via"] == "direct"])
    return specs


def _test_rows(records: list[dict]) -> list[dict]:
    return [{
        "NAME": r["model"], "PROVIDER": r["provider"], "VIA": r["via"],
        "SCORE": f"{r['score']}/{r['max']}" if r["score"] is not None else "-",
        "ERROR": r["error"],
    } for r in records]


def test(repo: Path, args: argparse.Namespace) -> int:
    """`arena model test NAME[,NAME…] [--url URL]`: run the code task per model."""
    try:
        entries = split_names(args.names)
        if not entries:
            raise ModelError("no model named (arena model available)")
        specs = resolve_test_targets(repo, entries, getattr(args, "url", None), time.time())
        records = run_tests(repo, specs)
    except (ModelError, profile.ProfileError) as err:
        return output.refuse(str(err))
    output.emit(_test_rows(records), TEST_COLUMNS, args.output)
    return EXIT_OK if all(r["score"] is not None for r in records) else EXIT_FAILED


def available(repo: Path, args: argparse.Namespace) -> int:
    """`arena model available [PROVIDER…] [--free] [--search TEXT] [--test] [--url URL]`."""
    stamp = time.time()
    try:
        records, direct = collect_models(repo, args.providers, getattr(args, "url", None), stamp)
    except ModelError as err:
        return output.refuse(str(err))
    try:  # IN-PROFILE is a courtesy: a missing or broken ini only leaves it empty
        profiles, active = profile.load_profiles(repo)
        chosen = profiles.get(args.profile or active, {})
    except profile.ProfileError:
        chosen = {}
    in_profile = {base_of(e) for e in split_names(chosen.get("models", ""))}
    needle = (args.search or "").lower()
    shown = []
    for r in records:
        full = f"{r['provider']}/{r['model']}"
        if args.free and r["free"] == FREE_NO:
            continue
        if needle and needle not in full.lower():
            continue
        shown.append(r)
    if not shown:
        print("arena: no model matches", file=sys.stderr)
        return EXIT_NOTHING
    store = score_store(repo)
    if getattr(args, "test", False):
        specs = [{"provider": r["provider"], "model": r["model"], "via": r["via"],
                  "url": direct.get(r["provider"], (None, None))[0],
                  "key": direct.get(r["provider"], (None, None))[1]} for r in shown]
        run_tests(repo, specs)
    scores = {(r["provider"], r["model"]): r for r in store.load()}
    now = time.time()
    rows = []
    for r in shown:
        full = f"{r['provider']}/{r['model']}"
        rows.append({
            "NAME": r["model"],
            "PROVIDER": r["provider"],
            "FREE": r["free"],
            "CTX": r["ctx"] or None,
            "IN-PROFILE": "yes" if {r["model"], full} & in_profile else "",
            "LAST-TEST": last_test_cell(scores.get((r["provider"], r["model"])), now),
        })
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
