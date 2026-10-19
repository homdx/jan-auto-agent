"""tools/collect/test_paths.py — L5: the one answer to "is this path test code?"

Two halves of the collect pipeline needed that answer and, before L5, each
kept its own:

* the producer, `test_map.build_test_map`, split the scanned modules with a
  single hard-coded `"tests/"` prefix;
* the consumer, `loader.CollectModel.callers_of`, excluded test importers
  from a caller list with six roots plus `conftest.py` (V1).

The import graph therefore counted a `tests_bugfix/` importer that the
coverage map had never looked at — and had filed as *uncovered shipped
code*. The V3 pack for `main.py` read, three lines apart, "13 test files do"
and "tests: 5 files": both true, and a contradiction. The roots now live
here once, and both sides import them.

Two predicates sit on the one list, because the two sides ask different
questions:

* `is_test_path` — *location*: is this shipped code at all? Nothing under a
  test root is, and neither is a `conftest.py` wherever it sits. This is the
  loader's filter, unchanged in effect.
* `is_test_module` — *coverage*: does this file assert on something? That
  is `is_test_path` **and** pytest's `test_*.py` discovery shape. A fixture
  under `tests/fixtures/`, `tests/_pass_a_stub.py` and every `conftest.py`
  are scaffolding: they sit in a test location and cover nothing, so
  crediting them would let a module look tested when nothing tests it.

A bare `test_*.py` filename is deliberately *not* a signal on its own:
shipped code can be named that (`tools/collect/test_map.py` is a shipped
module in this repo).

`.smoke_tests/` and `.regression_tests/` are symlink views onto `tests/`
(`scripts/sync_test_tiers.py`). `RepoIngestor.walk` prunes dotted
directories, so the scanner never sees them in this repo — the ticket's "four
roots" counts them, the graph actually sees three — but they stay on the
list: a consumer must still classify a path some other collector recorded,
and `test_map.build_test_map` collapses a mirror onto its real path so a
mirrored test can never count twice (`canonical_test_path`).

This module imports nothing else from the package on purpose: `loader`,
`risk` and `test_map` all need it, and a classifier living inside any of
them would be one import away from a cycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

#: Directory prefixes whose paths are test code, not shipped source (POSIX, each ends in /).
TEST_PATH_PREFIXES: Tuple[str, ...] = (
    "tests/", "tests_bugfix/", "tests_slow/", "stub-test/",
    ".smoke_tests/", ".regression_tests/",
)

#: Directory basenames that make a path test code at ANY depth, not just at
#: the root. `b/tests/test_b.py`, `src/pkg/tests/x.py` and
#: `src/test/java/...` are all test code — a test root is a test root no
#: matter how deep it sits. Hardcoded root prefixes above cover legacy
#: variants (`tests_bugfix/`, `.smoke_tests/`); these cover the plain
#: "a directory named tests" shape that every other layout uses.
TEST_PATH_DIRS: Tuple[str, ...] = ("tests", "test", "__tests__")

#: Test scaffolding file names, wherever they sit (pytest's hook file is at the repo root).
TEST_FILE_NAMES: Tuple[str, ...] = ("conftest.py",)

#: pytest's discovery shape; the naming half of `is_test_module`. `test_*`
#: and `*_test` are both collected by pytest's default patterns, so both
#: halves are test modules when they sit in a test location.
TEST_FILE_PREFIX = "test_"
TEST_FILE_SUFFIX = "_test"


def _is_test_dir_component(component: str) -> bool:
    """Is `component` a directory name that marks the enclosing path as test code?

    Exact-match only: `"contest"`, `"latest"`, `"attestation"` and `"my-tests"`
    do NOT contain `"tests"` or `"test"` as a whole component, so a shipped
    module whose name merely contains the word is not classified as test
    code.
    """
    return component in TEST_PATH_DIRS


def is_test_path(path: str) -> bool:
    """Is `path` test code rather than shipped code? (Location rule.)

    True under any `TEST_PATH_PREFIXES` root, for any `TEST_FILE_NAMES`
    basename, or when any path COMPONENT is one of `TEST_PATH_DIRS`
    (`tests`, `test`, `__tests__`) — at any depth. Fail open: an empty or
    non-string path is "not a test", never an exception.
    """
    if not isinstance(path, str) or not path:
        return False
    if path.startswith(TEST_PATH_PREFIXES):
        return True
    if path.rsplit("/", 1)[-1] in TEST_FILE_NAMES:
        return True
    # Split on `/` and reject any empty piece (leading `//` or trailing `/`).
    for component in path.split("/"):
        if _is_test_dir_component(component):
            return True
    return False


def is_test_module(path: str) -> bool:
    """Does `path` carry coverage? (Location rule **and** a pytest test name.)

    The TEST_MAP / RISK_INDEX split: True only for a pytest-discoverable
    `test_*.py` OR `*_test.py` inside a test location. `conftest.py`,
    fixtures and stub servers under a test root satisfy `is_test_path` but
    not this — they are scaffolding, and the map must not credit them as a
    covering test.
    """
    if not is_test_path(path):
        return False
    name = path.rsplit("/", 1)[-1]
    if not name.endswith(".py"):
        return False
    stem = name[:-3]
    return stem.startswith(TEST_FILE_PREFIX) or stem.endswith(TEST_FILE_SUFFIX)


def canonical_test_path(root: Path, path: str) -> str:
    """The repo-relative POSIX path `path` really lives at, symlinks resolved.

    A symlinked mirror of `tests/test_a.py` (`.smoke_tests/test_a.py`,
    `.regression_tests/test_a.py`) comes back as `tests/test_a.py`, so a
    covering test is credited once whichever name the scan saw it under. A
    path that is not a symlink comes back unchanged.

    Fails open: a dangling link, a link escaping `root`, or any OS surprise
    returns `path` as given — one bad link is a file credited under its own
    name, never a dropped covering test and never an exception.
    """
    try:
        root = Path(root)
        link = root / path
        if not link.is_symlink():
            return path
        # strict=True: a dangling link fails here and is returned as itself.
        return link.resolve(strict=True).relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path
