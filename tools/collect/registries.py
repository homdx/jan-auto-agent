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
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

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
