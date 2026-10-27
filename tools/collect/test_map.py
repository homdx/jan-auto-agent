"""tools/collect/test_map.py — COLLECT-12: TEST_MAP (module <-> tests,
thin/zero-list).

Derived over facts EPIC B already produced — no LLM anywhere,
`provenance="derived"` in spirit (see `model.Provenance.DERIVED`): every
entry here is a pure function of static facts. It reuses the same
import-resolution helper EPIC B/EPIC D already share
(`tools.collect.graph.resolve_import`, COLLECT-8), and, like
`graph.build_call_edges`, re-reads each test file's own source once more
(see `_rich_import_targets` below) rather than trusting
`ModuleRecord.imports` alone — that field is deliberately coarser than
what test-file matching needs (see the note there).

What "covered by a test" means
-------------------------------
A source module is considered exercised by a test file when that test file
*imports* it (directly or transitively-resolvable through the same
longest-prefix rule `graph.resolve_import` already uses for the import
graph). This mirrors how this repo's test suite is actually organized:
tests are grouped by feature/ticket (`test_auto_c5.py`,
`test_cr26_2_immutable_guard.py`, ...), not by one-test-file-per-module
naming, so a naming-based heuristic alone would badly undercount. Import-based
matching is also strictly a *static* fact — no guessing about what a test
"really" exercises, just what it imports.

Which files are tests (L5)
--------------------------
Not decided here. `is_test_module` is `tools.collect.test_paths`'s, the
same root list `loader.CollectModel.callers_of` excludes test importers by,
so the map and the graph cannot disagree about a directory again. Before L5
this module knew only `tests/`: a `tests_bugfix/` test was a *source module*
here (keyed, uncovered, on the zero-list) and a test importer to the graph,
and the V3 pack for `main.py` said "13 test files do" three lines above
"tests: 5 files". A covering test is credited once, under the path it
really lives at: `.smoke_tests/` and `.regression_tests/` are symlink views
onto `tests/`, and a mirror is collapsed onto its target
(`test_paths.canonical_test_path`) before it is credited.

Two derived worklists fall out of the same map:

* **zero-list** — source modules with *no* test file importing them at
  all. This is the audit worklist COLLECT-12's AC cares about: before the
  fixes referenced in this repo's history, `tools/backoff.py` and
  `tools/llm_stream.py` were exactly the kind of module that showed up
  here (import-only leaf modules nothing test-side referenced yet).
* **thin-list** — source modules referenced by only a small number of test
  files (`<= thin_threshold`, default 1), excluding modules already on the
  zero-list. A single covering test file is still a thin sliver of
  coverage for anything with real branching, so this list is the "look
  here next" companion to the zero-list rather than a synonym for it.

Both lists are sorted (COLLECT-3 determinism): same input tree, same
output, byte for byte.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

from tools.collect.graph import build_module_index, resolve_import
from tools.collect.model import ModuleRecord
# is_test_module lives in test_paths and is re-exported here for risk.py and other callers.
from tools.collect.test_paths import canonical_test_path, is_test_module  # noqa: F401

__all__ = [
    "build_test_map", "is_test_module", "thin_coverage", "zero_coverage",
]


def _rich_import_targets(source: str) -> Set[str]:
    """Every dotted name a `from X import Y` / `import X` statement could
    plausibly resolve to, including the submodule form `ModuleRecord.imports`
    (COLLECT-4's `extract_imports`) deliberately collapses away.

    `extract_imports` records only the *source* module of a `from` import
    (`from tools import backoff` -> `"tools"`): the thing actually being
    referenced structurally is the package. (Since L4 the import graph
    resolves the same finer candidates from `ModuleRecord.from_imports`, so
    graph and TEST_MAP now agree on `from pkg import module`.) For TEST_MAP that collapse is
    exactly the gap that would silently zero-list a module like
    `tools/backoff.py`, which real callers in this repo reach via
    `from tools import backoff`, not `import tools.backoff`. This function
    additionally reconstructs the finer `"tools.backoff"` form by joining
    the `from`-module with each imported alias — a candidate `resolve_import`
    below simply won't match against the index if it isn't real, so
    over-generating candidates here is harmless.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        # A null byte or truncated file makes ast.parse raise ValueError; treat it like a syntax error (no imports).
        return set()
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # Relative imports keep node.level, so a package-internal import is not mistaken for a top-level module.
            dots = "." * node.level
            module = node.module or ""
            if module:
                base = f"{dots}{module}"
                names.add(base)
                for alias in node.names:
                    names.add(f"{base}.{alias.name}")
            elif dots:
                # from . import X: only the imported name is a candidate, with no module part.
                for alias in node.names:
                    names.add(f"{dots}{alias.name}")
    return names


def build_test_map(
    root: Path, modules: Iterable[ModuleRecord]
) -> Dict[str, Tuple[str, ...]]:
    """`{source_module_path: sorted tuple(test file paths that import it)}`.

    Total over every *source* module in `modules` (test modules themselves
    are never keys) — a module with zero importing tests still gets an
    entry (empty tuple), so callers never need a defensive `.get(path, ())`
    (same totality convention `graph.import_edges`/`imported_by` follow).

    Test modules are the ones `test_paths.is_test_module` names — every
    root the loader's `callers_of` knows, not one — and each is credited
    under its canonical path (L5): a symlinked mirror of `tests/test_a.py`
    is read, resolved and recorded as `tests/test_a.py`, so the same file
    reaching the scan under two names is one covering test, not two. A
    mirror's relative imports are anchored at the real path for the same
    reason — nothing lives beside the link.

    `root` is used only to re-read each test file's source for the richer
    import-target extraction above (mirroring `graph.build_call_edges`,
    which re-reads for the same reason: `ModuleRecord` doesn't retain the
    parsed tree/source after Pass A — see `scanner.py`). A test file that
    fails to re-read/re-parse is silently skipped for matching purposes
    (same "one bad file can't take down the pass" convention COLLECT-4/8
    already follow) — it isn't dropped from the input, it just contributes
    no edges.
    """
    modules = list(modules)
    root = Path(root)
    source_modules = [m for m in modules if not is_test_module(m.path)]
    test_modules = [m for m in modules if is_test_module(m.path)]

    index = build_module_index(source_modules)
    covering: Dict[str, set] = {m.path: set() for m in source_modules}

    seen: Set[str] = set()
    for t in test_modules:
        # A symlinked mirror resolves to its target and is credited once, however many names reach the scan.
        real_path = canonical_test_path(root, t.path)
        if real_path in seen:
            continue
        seen.add(real_path)
        try:
            source = (root / real_path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            # A test file that is not valid UTF-8 is skipped for matching, as the docstring says, instead of crashing.
            continue
        for dotted in _rich_import_targets(source):
            # importer_path is passed, so relative names resolve correctly.
            resolved = resolve_import(dotted, index, importer_path=real_path)
            if resolved is not None:
                covering[resolved].add(real_path)

    return {path: tuple(sorted(tests)) for path, tests in covering.items()}


def zero_coverage(test_map: Dict[str, Tuple[str, ...]]) -> List[str]:
    """Sorted list of source module paths with no covering test at all."""
    return sorted(path for path, tests in test_map.items() if not tests)


def thin_coverage(
    test_map: Dict[str, Tuple[str, ...]], *, thin_threshold: int = 1
) -> List[str]:
    """Sorted list of source module paths covered by `1..thin_threshold`
    test files — i.e. *some* coverage, but little of it. Modules on the
    zero-list are never repeated here: thin and zero are disjoint
    worklists, so a consumer can concatenate them without deduplicating.
    """
    return sorted(
        path
        for path, tests in test_map.items()
        if 0 < len(tests) <= thin_threshold
    )
