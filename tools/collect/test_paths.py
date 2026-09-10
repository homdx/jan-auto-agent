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

#: Directory prefixes under which every path is test-suite code, not shipped
#: source. Repo-relative POSIX paths; each entry ends in `/`.
TEST_PATH_PREFIXES: Tuple[str, ...] = (
    "tests/", "tests_bugfix/", "tests_slow/", "stub-test/",
    ".smoke_tests/", ".regression_tests/",
)

#: File names that mark test scaffolding wherever they sit — pytest's hook
#: file lives at this repo's root, not inside a test root.
TEST_FILE_NAMES: Tuple[str, ...] = ("conftest.py",)

#: pytest's discovery shape; the naming half of `is_test_module`.
TEST_FILE_PREFIX = "test_"


def is_test_path(path: str) -> bool:
    """Is `path` test code rather than shipped code? (Location rule.)

    True under any `TEST_PATH_PREFIXES` root or for any `TEST_FILE_NAMES`
    basename. Fail open: an empty or non-string path is "not a test", never
    an exception.
    """
    if not isinstance(path, str) or not path:
        return False
    if path.startswith(TEST_PATH_PREFIXES):
        return True
    return path.rsplit("/", 1)[-1] in TEST_FILE_NAMES


def is_test_module(path: str) -> bool:
    """Does `path` carry coverage? (Location rule **and** `test_*.py`.)

    The TEST_MAP / RISK_INDEX split: True only for a pytest-discoverable
    `test_*.py` inside a test root. `conftest.py`, fixtures and stub servers
    under a test root satisfy `is_test_path` but not this — they are
    scaffolding, and the map must not credit them as a covering test.
    """
    if not is_test_path(path):
        return False
    name = path.rsplit("/", 1)[-1]
    return name.startswith(TEST_FILE_PREFIX) and name.endswith(".py")


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
        # strict: a dangling link must come back as itself, not as the
        # name it points at — that name is not a file and would never be read.
        return link.resolve(strict=True).relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path
