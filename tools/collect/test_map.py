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

#: Every path under this prefix is treated as test-suite code, not
#: production source, when splitting the module list in two.
TEST_DIR_PREFIX = "tests/"


def is_test_module(path: str) -> bool:
    """A module counts as "a test" for TEST_MAP purposes when it lives
    under `tests/` *and* its filename starts with `test_` — the same
    naming convention pytest itself discovers by. This deliberately
    excludes non-test helpers that happen to live in `tests/`
    (`tests/_pass_a_stub.py`, anything under `tests/fixtures/`): those
    are fixtures/scaffolding, not coverage, and counting them as "a test"
    would let a module look covered when nothing actually asserts on it.
    """
    if not path.startswith(TEST_DIR_PREFIX):
        return False
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") and name.endswith(".py")


def _rich_import_targets(source: str) -> Set[str]:
    """Every dotted name a `from X import Y` / `import X` statement could
    plausibly resolve to, including the submodule form `ModuleRecord.imports`
    (COLLECT-4's `extract_imports`) deliberately collapses away.

    `extract_imports` records only the *source* module of a `from` import
    (`from tools import backoff` -> `"tools"`) because that's the right
    convention for the import graph (COLLECT-8): the thing actually being
    referenced structurally is the package. For TEST_MAP that collapse is
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
    except SyntaxError:
        return set()
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return names


def build_test_map(
    root: Path, modules: Iterable[ModuleRecord]
) -> Dict[str, Tuple[str, ...]]:
    """`{source_module_path: sorted tuple(test file paths that import it)}`.

    Total over every *source* module in `modules` (test modules themselves
    are never keys) — a module with zero importing tests still gets an
    entry (empty tuple), so callers never need a defensive `.get(path, ())`
    (same totality convention `graph.import_edges`/`imported_by` follow).

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

    for t in test_modules:
        try:
            source = (root / t.path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # BUGFIX: same class of bug as scanner.scan_repo/graph.
            # build_call_edges/risk._loc/cli._sources_for — only OSError
            # was caught here, so a test file that isn't valid UTF-8
            # raised a bare UnicodeDecodeError out of build_test_map
            # instead of being "silently skipped for matching purposes",
            # exactly the contract this function's own docstring
            # describes for a test file that fails to re-read/re-parse.
            continue
        for dotted in _rich_import_targets(source):
            resolved = resolve_import(dotted, index)
            if resolved is not None:
                covering[resolved].add(t.path)

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
