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

from tools.collect.model import ModuleRecord, Provenance


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
