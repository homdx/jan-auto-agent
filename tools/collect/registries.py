"""tools/collect/registries.py — COLLECT-9: FAIL_OPEN_REGISTRY builder.

Turns COLLECT-6's per-module `except_sites` into one flat, cross-repo
registry of the sites that are known to swallow an exception silently
(`ExceptSite.is_fail_open`) — the "already-safe/already-known" surface
EPIC C exists to give the consumer side (COLLECT-22's bughunt-suppression)
something authoritative to check a candidate bug against before it's ever
shown to anyone.

This module adds no new *kind* of fact. It only:

1. **Selects** — filters each module's `except_sites` down to the
   fail-open ones. A site that logs, re-raises, `continue`s, or `return`s
   is not silent and does not belong in this registry (COLLECT-6 already
   decided that; COLLECT-9 does not re-decide it).
2. **Annotates, best-effort** — if the source has a comment sitting right
   on the `except` line or on the first line of its body (the common
   `except Exception:  # deliberately silent, see #123` / `pass  # legacy,
   see ticket` idioms), that literal comment text is attached as
   `rationale`. This is never inferred or summarized — it's the exact
   comment string already in the file, found by tokenizing the source, not
   by asking an LLM to characterize the site. When no such comment exists,
   `rationale` is `None`; that's a normal, common outcome, not a failure.

Because both steps only read facts COLLECT-6 already tagged `static` (plus
literal source text), every `FailOpenEntry` this module produces keeps
`provenance="static"` — there is no LLM anywhere in this file (COLLECT-1's
isolation guarantee holds transitively).
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import yaml

from tools.collect.model import ContractRecord, ModuleRecord, Provenance

#: Default location of the hand-maintained seed-contract data file
#: (COLLECT-10). Relative to this module, not the CWD, so
#: `build_seed_contracts` works regardless of where the caller's process
#: happens to be running from.
DEFAULT_CONTRACTS_SEED_PATH = Path(__file__).parent / "contracts_seed.yaml"


class ContractCitationError(RuntimeError):
    """Raised when a seed contract's `known_edge` no longer names a real
    top-level symbol in the scanned repo.

    This is COLLECT-10's whole point: a seed is *data*, not code, so
    nothing stops it from silently rotting as the codebase changes out
    from under it — a renamed/removed function, a typo'd path. Rather than
    let a stale citation sit there looking authoritative, loading a seed
    whose citation doesn't resolve is a hard failure, not a warning.
    """


@dataclass(frozen=True)
class FailOpenEntry:
    """One row of the FAIL_OPEN_REGISTRY: a location known to fail open,
    plus whatever rationale (if any) the source states for it.

    `location` and `exception_type` are carried through unchanged from the
    `ExceptSite` COLLECT-6 produced; `rationale` is either literal comment
    text pulled from the source next to that site, or `None`.
    """

    location: str
    exception_type: str
    rationale: Optional[str] = None
    provenance: str = Provenance.STATIC

    def to_dict(self) -> Dict[str, object]:
        return {
            "location": self.location,
            "exception_type": self.exception_type,
            "rationale": self.rationale,
            "provenance": self.provenance,
        }


def _line_comments(source: str) -> Dict[int, str]:
    """`{line_number: comment_text}` for every `#`-comment token in
    `source`, text stripped of the leading `#` and surrounding whitespace.

    Tokenizing (rather than a naive `"#" in line` scan) means a `#` inside
    a string literal is never mistaken for a comment. A source that fails
    to tokenize (rare, e.g. an encoding declaration edge case) yields an
    empty map rather than raising — a missing rationale is not an error,
    it's just the common case.
    """
    comments: Dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                text = tok.string.lstrip("#").strip()
                if text:
                    comments[tok.start[0]] = text
    except (tokenize.TokenizeError, SyntaxError, IndentationError):
        return {}
    return comments


def _handler_rationale(
    tree: ast.Module,
    comments: Dict[int, str],
    except_lineno: int,
) -> Optional[str]:
    """Rationale comment for the `except` handler starting at
    `except_lineno`, if one exists directly on that line or on the first
    line of the handler's body (covers both `except X:  # why` and
    `except X:\\n    pass  # why`). No other lines are considered — a
    comment several lines away is not reliably *about* this site, so it's
    left as `None` rather than guessed at.
    """
    if except_lineno in comments:
        return comments[except_lineno]
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.lineno == except_lineno:
            for stmt in node.body:
                if stmt.lineno in comments:
                    return comments[stmt.lineno]
            break
    return None


def _sort_key(entry: FailOpenEntry):
    path, _, line = entry.location.rpartition(":")
    return (path, int(line) if line.isdigit() else -1)


def build_fail_open_registry(
    modules: Iterable[ModuleRecord],
    root: Optional[Path] = None,
) -> List[FailOpenEntry]:
    """The FAIL_OPEN_REGISTRY: one `FailOpenEntry` per fail-open
    `ExceptSite` across every module in `modules`, sorted by `(path, line)`
    for determinism (COLLECT-3).

    `root` is optional: without it (or when a module has a `parse_error`,
    or its file can no longer be read/parsed under `root`), entries are
    still produced — just with `rationale=None` — since the registry's
    core job (COLLECT-9 AC: every fail-open site is *in* the registry) does
    not depend on comment-extraction succeeding. Rationale is a best-effort
    bonus, never a gate on membership.
    """
    modules = list(modules)
    entries: List[FailOpenEntry] = []
    for m in modules:
        fail_open_sites = [s for s in m.except_sites if s.is_fail_open]
        if not fail_open_sites:
            continue

        comments: Dict[int, str] = {}
        tree: Optional[ast.Module] = None
        if root is not None and not m.parse_error:
            try:
                source = (Path(root) / m.path).read_text(encoding="utf-8")
                tree = ast.parse(source, filename=m.path)
                comments = _line_comments(source)
            except (OSError, SyntaxError, UnicodeDecodeError):
                tree = None
                comments = {}

        for site in fail_open_sites:
            rationale = None
            if tree is not None:
                lineno_str = site.location.rpartition(":")[-1]
                if lineno_str.isdigit():
                    rationale = _handler_rationale(tree, comments, int(lineno_str))
            entries.append(
                FailOpenEntry(
                    location=site.location,
                    exception_type=site.exception_type,
                    rationale=rationale,
                )
            )

    entries.sort(key=_sort_key)
    return entries


def fail_open_locations(registry: Iterable[FailOpenEntry]) -> frozenset:
    """The bare set of `location` strings in `registry` — the cheap
    membership check `is_fail_open_at` (COLLECT-11) and, eventually,
    bughunt-suppression (COLLECT-22) actually want, without having to walk
    `FailOpenEntry` objects themselves.
    """
    return frozenset(e.location for e in registry)


# ── COLLECT-10: CONTRACTS builder (seed) ───────────────────────────────────────


def _known_symbols(modules: Iterable[ModuleRecord]) -> frozenset:
    """`{"path:Name", ...}` for every top-level symbol Pass A found, across
    `modules` — the citation-check's source of truth. Only structural facts
    (COLLECT-4's `public_symbols`) count; there is nothing LLM-derived in
    this set, so a citation that resolves here is resolving against ground
    truth, not prose.
    """
    return frozenset(sym.qualname for m in modules for sym in m.public_symbols)


def _load_seed_entries(seed_path: Path) -> List[Dict[str, object]]:
    """Raw `{name, description, known_edge}` dicts from the seed YAML.

    An absent or empty file yields an empty list rather than raising —
    seeding is optional data, not a required input — but a *present*,
    non-empty file that fails to parse as YAML is a real authoring error
    and is allowed to raise.
    """
    if not seed_path.exists():
        return []
    raw = yaml.safe_load(seed_path.read_text(encoding="utf-8"))
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ContractCitationError(
            f"{seed_path}: expected a YAML list of contract entries, got {type(raw).__name__}"
        )
    return raw


def build_seed_contracts(
    modules: Iterable[ModuleRecord],
    seed_path: Path = DEFAULT_CONTRACTS_SEED_PATH,
) -> List[ContractRecord]:
    """The seed half of CONTRACTS (COLLECT-10): load `seed_path`, run each
    entry's `known_edge` through a citation-check against `modules`' own
    symbol index, and return one `ContractRecord` (provenance=static) per
    entry — sorted by `name` for determinism (COLLECT-3).

    Every entry's `known_edge` must resolve to a real top-level symbol in
    `modules`, or this raises `ContractCitationError` — per COLLECT-10's
    AC, a seed contract can never quietly go stale; it fails the run
    instead. This also means an entry missing `name`/`description` or
    lacking a `known_edge` entirely is itself a citation failure: a
    "known" contract with nothing to check it against isn't a fact, it's
    an assertion, and this builder doesn't traffic in those.
    """
    modules = list(modules)
    known = _known_symbols(modules)
    entries = _load_seed_entries(seed_path)

    contracts: List[ContractRecord] = []
    for entry in entries:
        name = entry.get("name")
        description = entry.get("description")
        known_edge = entry.get("known_edge")
        if not name or not description or not known_edge:
            raise ContractCitationError(
                f"{seed_path}: seed entry {entry!r} is missing one of "
                "name/description/known_edge"
            )
        if known_edge not in known:
            raise ContractCitationError(
                f"{seed_path}: seed contract {name!r} cites {known_edge!r}, "
                "which does not resolve to a top-level symbol in the "
                "scanned repo (renamed, removed, or a typo — this seed is "
                "stale and must be fixed or dropped)"
            )
        contracts.append(
            ContractRecord(
                name=str(name),
                description=str(description).strip(),
                kind="seed",
                known_edge=str(known_edge),
            )
        )

    contracts.sort(key=lambda c: c.name)
    return contracts


# ── COLLECT-11: "already-safe" query surface ───────────────────────────────────


@dataclass(frozen=True)
class SafetyAnswer:
    """The result of asking "is `location` already safe?" — COLLECT-11's
    единый query, stitched together from three static/derived sources
    (guarded_accesses, FAIL_OPEN_REGISTRY, CONTRACTS) so the consumer side
    (loader's `safe ли X?` in EPIC G, bughunt-suppression in COLLECT-22)
    never has to walk all three itself.

    `reason` is one of "guarded", "fail_open", "contract", "unguarded",
    "ambiguous_location", or "unknown". `detail` carries whatever concrete
    evidence backs that reason (the guard description, the
    rationale/exception type, the contract name(s), or the bare access
    expression) so a caller can explain *why* a candidate was suppressed,
    not just that it was.
    `provenance` is always `static` or `derived` — never `llm` — because
    every source this draws from is (COLLECT-1's isolation holds
    transitively).
    """

    safe: bool
    reason: str
    detail: Optional[str] = None
    provenance: str = Provenance.STATIC


def _enclosing_symbol_qualname(module: ModuleRecord, line: int) -> Optional[str]:
    """The qualname of the top-level symbol in `module` that `line` falls
    inside, or `None` if `line` precedes every symbol (module-level code
    before the first def/class) or `module` has no symbols at all.

    COLLECT-4 only inventories top-level defs/classes and doesn't track
    each symbol's end line, so this is necessarily approximate: it picks
    the closest-preceding symbol, which is exact for well-formed source
    (no top-level statements interleaved between two defs at the same
    indent) and is the same approximation COLLECT-4's own ordering already
    relies on.
    """
    enclosing = None
    for sym in module.public_symbols:  # already sorted by lineno (COLLECT-4)
        if sym.lineno <= line:
            enclosing = sym
        else:
            break
    return enclosing.qualname if enclosing is not None else None


_METHOD_REF_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")


def _referenced_method(description: str, class_short_name: str) -> Optional[str]:
    """If `description` mentions `<class_short_name>.<method>` (e.g. the
    `prompt_store_atomic_save` seed entry's "PromptStore._save writes
    through..."), return `<method>`; else `None`.

    Exists because COLLECT-4 only inventories top-level defs/classes, so a
    contract whose actual guarantee is about one method is forced to cite
    its *enclosing class* as `known_edge` (`contracts_seed.yaml`'s own
    comment says as much). Left unnarrowed, that class-level reference
    would make `AlreadySafeIndex.query()` report "safe: contract" for
    *any* line anywhere in the class — every other method included — since
    nothing separates one method's lines from another's once COLLECT-4
    has collapsed them all into a single class-wide symbol. This detects
    when a narrower target was actually intended, so the caller can go
    look for it instead of trusting the class-wide range at face value.
    """
    for cls, method in _METHOD_REF_RE.findall(description):
        if cls == class_short_name:
            return method
    return None


def _method_line_range(source: str, class_name: str, method_name: str) -> Optional[Tuple[int, int]]:
    """`(first_line, last_line)` (inclusive) of `method_name` inside
    `class_name` in `source`, via a direct, one-off AST scan — or `None`
    if either isn't found or `source` doesn't parse.

    Deliberately independent of COLLECT-4's `public_symbols` (which, as
    above, never tracks methods at all): this is the narrower fact that
    data structurally can't represent, computed fresh from the same
    source text Pass A already read. `end_lineno` is populated by
    `ast.parse` on every Python version this project targets, so the
    range is exact, not an approximation the way `_enclosing_symbol_
    qualname`'s "next top-level symbol" inference is.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == method_name
                ):
                    end = getattr(item, "end_lineno", None) or item.lineno
                    return (item.lineno, end)
    return None


class AlreadySafeIndex:
    """The unified "already-safe" surface COLLECT-11 builds: one object
    that answers `query(location)` by checking, in order, whether
    `location` is a documented `GUARDED` access, a registered fail-open
    site, or covered by a seed/derived contract — before ever falling
    back to "unguarded" or "unknown".

    Built once via `build_already_safe_index` and then queried as many
    times as the consumer needs; nothing here re-scans the repo or talks
    to an LLM.
    """

    def __init__(
        self,
        modules: Iterable[ModuleRecord],
        fail_open_registry: Iterable[FailOpenEntry],
        contracts: Iterable[ContractRecord],
        *,
        root: Optional[Path] = None,
    ) -> None:
        self._modules_by_path: Dict[str, ModuleRecord] = {}
        self._guarded_by_location: Dict[str, List] = {}
        for m in modules:
            self._modules_by_path[m.path] = m
            for g in m.guarded_accesses:
                self._guarded_by_location.setdefault(g.location, []).append(g)

        fail_open_registry = list(fail_open_registry)
        self._fail_open_locations = fail_open_locations(fail_open_registry)
        self._fail_open_by_location = {e.location: e for e in fail_open_registry}

        self._contracts_by_edge: Dict[str, List[ContractRecord]] = {}
        # (known_edge, contract.name) -> (first_line, last_line), populated
        # only for contracts whose description names a specific method of
        # a class-shaped known_edge — see `_referenced_method`. A
        # class-level known_edge with no such reference never enters
        # either of the two sets below: it means the contract genuinely
        # intends its whole known_edge range, not one method within it, so
        # `query()` keeps matching at that (correct, class-wide) grain.
        self._contract_line_range: Dict[Tuple[str, str], Tuple[int, int]] = {}
        # (known_edge, contract.name) pairs whose description named a
        # specific method but whose range this __init__ could not resolve
        # (no `root`, file missing, method not found). `query()` must
        # treat these as *not* matching rather than silently falling back
        # to the class-wide range — that fallback is exactly the false
        # "safe: contract" claim for unrelated methods this fix exists to
        # close (found on the real `PromptStore`/`_save` seed contract:
        # every line in every other method of the class matched too).
        self._contract_wants_narrowing: set = set()
        for c in contracts:
            if not c.known_edge:
                continue
            self._contracts_by_edge.setdefault(c.known_edge, []).append(c)

            path, _, short = c.known_edge.rpartition(":")
            method = _referenced_method(c.description, short)
            if method is None:
                continue
            self._contract_wants_narrowing.add((c.known_edge, c.name))
            if root is None:
                continue
            src_path = root / path
            if not src_path.is_file():
                continue
            try:
                source = src_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rng = _method_line_range(source, short, method)
            if rng is not None:
                self._contract_line_range[(c.known_edge, c.name)] = rng

    def query(self, location: str, access: Optional[str] = None) -> SafetyAnswer:
        """Is `location` (a `"path/to/module.py:line"` string, matching
        `GuardedAccess.location`/`ExceptSite.location`) already known to be
        safe? Checked in order — guard, fail-open, contract — since each
        is independently sufficient; `unguarded` is only returned when
        Pass A recorded an indexed access here and marked it unguarded,
        and `unknown` when this location isn't in any of the three
        sources at all (e.g. it's not an indexed access or except site).

        `access` (e.g. `"stack[-1]"`) disambiguates a location that has
        *more than one* distinct indexed access on the same physical line
        — real in this codebase (e.g. `data[agent_name][...] = stack[-1]
        [...]`, one guarded, one not) — by filtering to the exact access
        cited before deciding. Callers that already know which expression
        they're asking about (COLLECT-17's `Claim.access`, a future
        COLLECT-22 candidate) should always pass it.

        Without `access`, a location where every recorded entry agrees
        (all `GUARDED`, or duplicates of one real site — COLLECT-7's
        determinism guarantee) still answers unambiguously. A location
        where recorded entries *disagree* — some `GUARDED`, some
        `UNGUARDED`, for genuinely different accesses sharing a line — has
        no single honest yes/no answer: optimistically returning "guarded"
        risks suppressing a real finding about the other, unguarded access
        on that same line, which is exactly the wrong direction for a
        tool whose purpose is to never let a static fact wrongly vouch for
        something it doesn't actually cover. That case answers
        `ambiguous_location` instead — `safe=False`, so a caller who
        ignores `reason` and only checks `safe` still fails closed.
        """
        accesses = self._guarded_by_location.get(location)
        relevant = accesses
        if access is not None:
            relevant = [a for a in accesses if a.access == access] if accesses else []

        if relevant:
            statuses = {a.status for a in relevant}
            if statuses == {"GUARDED"}:
                guarded = relevant[0]
                return SafetyAnswer(True, "guarded", detail=guarded.guard)
            if "GUARDED" in statuses and "UNGUARDED" in statuses:
                # Only reachable with access=None on a genuinely mixed
                # line: distinct accesses whose guard status disagrees.
                cited = ", ".join(sorted({f"{a.access}={a.status}" for a in relevant}))
                return SafetyAnswer(
                    False,
                    "ambiguous_location",
                    detail=(
                        f"{location} has multiple distinct accesses with differing "
                        f"guard status ({cited}); pass `access=` to disambiguate"
                    ),
                )

        if location in self._fail_open_locations:
            entry = self._fail_open_by_location[location]
            return SafetyAnswer(
                True,
                "fail_open",
                detail=entry.rationale or f"except {entry.exception_type}",
            )

        path, _, line_str = location.rpartition(":")
        module = self._modules_by_path.get(path)
        if module is not None and line_str.isdigit():
            line_num = int(line_str)
            symbol = _enclosing_symbol_qualname(module, line_num)
            if symbol is not None and symbol in self._contracts_by_edge:
                matched = []
                for c in self._contracts_by_edge[symbol]:
                    key = (symbol, c.name)
                    rng = self._contract_line_range.get(key)
                    if rng is not None:
                        if rng[0] <= line_num <= rng[1]:
                            matched.append(c)
                    elif key not in self._contract_wants_narrowing:
                        # No specific method named in the description at
                        # all — the class-wide known_edge is genuinely
                        # what this contract means to cover.
                        matched.append(c)
                    # else: description named a method but the range
                    # couldn't be resolved (no root, file missing, method
                    # not found) — do not match; see __init__ comment.
                if matched:
                    names = ", ".join(c.name for c in matched)
                    return SafetyAnswer(True, "contract", detail=names, provenance=Provenance.DERIVED)

        if relevant:
            # Every entry reaching here is UNGUARDED (the GUARDED-only and
            # mixed-status cases already returned above) — and, when
            # `access` was given, specifically the entry for that access,
            # not some other one sharing the same location.
            return SafetyAnswer(False, "unguarded", detail=relevant[0].access)

        return SafetyAnswer(False, "unknown")


def build_already_safe_index(
    modules: Iterable[ModuleRecord],
    fail_open_registry: Iterable[FailOpenEntry],
    contracts: Iterable[ContractRecord],
    *,
    root: Optional[Path] = None,
) -> AlreadySafeIndex:
    """Construct the COLLECT-11 "already-safe" query surface from Pass A's
    modules (for `guarded_accesses`), COLLECT-9's `FAIL_OPEN_REGISTRY`, and
    COLLECT-10's `CONTRACTS` — the three static/derived sources EPIC G's
    loader and bughunt-suppression (COLLECT-21/22) will query against.

    `root`, when given, lets a class-level contract whose description
    names one specific method (e.g. `prompt_store_atomic_save`'s
    "PromptStore._save writes through...") be verified against that
    method's actual line range instead of matching anywhere in the whole
    class — see `AlreadySafeIndex.__init__`. Omitting `root` still works;
    such contracts just won't match at all rather than over-matching.
    """
    return AlreadySafeIndex(modules, fail_open_registry, contracts, root=root)
