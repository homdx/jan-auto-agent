"""FIX-2 #11 — the max_files budget was spent on files that were discarded.

``_find_def_in_repo`` walks the repo for a ``def <name>(`` and gives up after
``max_files`` files. The ``.agent``/``node_modules`` exclusion used to run
*after* ``count += 1``, so every file the walk was about to throw away still
consumed budget. ``.agent/`` holds the run's own state and ``node_modules/``
holds thousands of vendored files, so whenever either was walked before the
file that actually defines the symbol, the search hit the cap having read
nothing but files it was discarding and returned ``None`` for a definition
that was sitting right there — Stage B then reasoned about a callee it had
never seen.

The fix is a one-line reorder: exclude first, count second. Never read,
never counted.

Two things beyond "it finds the definition" are pinned here:

* the budget must still bound *real* work
  (``test_budget_still_stops_a_runaway_walk``) — one candidate fix for this
  bug deleted the counter outright, which removes the cap on an arbitrarily
  large repo rather than fixing where it is spent;
* an excluded file must never be returned even when it does contain a
  matching definition (``test_definition_inside_an_excluded_tree_is_skipped``).

Walk order drives this bug, and ``Path.rglob`` order is filesystem-defined,
so the ordering tests drive the function through a stub whose ``rglob``
yields real files in a fixed order. ``TestAgainstRealFilesystem`` covers the
same function over a genuine directory tree so the stub cannot mask a
signature or behaviour change.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.gate1_grounding import _find_def_in_repo  # noqa: E402

TARGET_SRC = "def target_fn(a, b):\n    return a + b\n"
FILLER_SRC = "def something_else():\n    return None\n"


class _StubBase:
    """Stands in for ``base_dir``, yielding *paths* from rglob in order.

    ``_find_def_in_repo`` only ever calls ``base_dir.rglob(...)``; the paths
    it yields are real ``Path`` objects, so ``str(p)`` and ``p.read_text()``
    behave exactly as in production.
    """

    def __init__(self, paths: list[Path]) -> None:
        self._paths = paths

    def rglob(self, pattern: str):  # noqa: ARG002 — always "*.py" here
        return iter(self._paths)


def _write(root: Path, rel: str, src: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")
    return p


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return tmp_path


class TestExcludedFilesDoNotConsumeBudget:
    @pytest.mark.parametrize("excluded_dir", ["node_modules", ".agent"])
    def test_excluded_tree_ahead_of_the_target_still_finds_it(
        self, repo: Path, excluded_dir: str
    ) -> None:
        """The reported defect. With the exclusion below the increment these
        five files exhaust a budget of 2 and the search returns None."""
        noise = [
            _write(repo, f"{excluded_dir}/pkg/f{i}.py", FILLER_SRC)
            for i in range(5)
        ]
        target = _write(repo, "src/target.py", TARGET_SRC)

        found = _find_def_in_repo(
            "target_fn", _StubBase(noise + [target]), max_files=2
        )

        assert found is not None
        assert found[0] == target
        assert found[1] == TARGET_SRC

    def test_both_excluded_trees_together(self, repo: Path) -> None:
        noise = [_write(repo, f"node_modules/a{i}.py", FILLER_SRC) for i in range(4)]
        noise += [_write(repo, f".agent/workspace/b{i}.py", FILLER_SRC) for i in range(4)]
        target = _write(repo, "target.py", TARGET_SRC)

        found = _find_def_in_repo("target_fn", _StubBase(noise + [target]), max_files=1)

        assert found is not None and found[0] == target

    def test_definition_inside_an_excluded_tree_is_skipped(
        self, repo: Path
    ) -> None:
        """Excluding must mean "not a result", not merely "not counted"."""
        decoy = _write(repo, "node_modules/dep/target.py", TARGET_SRC)
        target = _write(repo, "src/target.py", TARGET_SRC)

        found = _find_def_in_repo(
            "target_fn", _StubBase([decoy, target]), max_files=10
        )

        assert found is not None
        assert found[0] == target


class TestBudgetIsStillEnforced:
    def test_budget_still_stops_a_runaway_walk(self, repo: Path) -> None:
        """Real, non-excluded files must still consume the budget — the cap
        exists so a huge repo cannot stall Stage B."""
        noise = [_write(repo, f"src/f{i}.py", FILLER_SRC) for i in range(5)]
        target = _write(repo, "src/target.py", TARGET_SRC)

        found = _find_def_in_repo(
            "target_fn", _StubBase(noise + [target]), max_files=2
        )

        assert found is None

    def test_target_within_budget_is_found(self, repo: Path) -> None:
        noise = [_write(repo, f"src/f{i}.py", FILLER_SRC) for i in range(2)]
        target = _write(repo, "src/target.py", TARGET_SRC)

        found = _find_def_in_repo(
            "target_fn", _StubBase(noise + [target]), max_files=10
        )

        assert found is not None and found[0] == target


class TestAgainstRealFilesystem:
    """Same function, real rglob — guards against the stub hiding a change."""

    def test_finds_a_plain_definition(self, repo: Path) -> None:
        target = _write(repo, "pkg/mod.py", TARGET_SRC)
        found = _find_def_in_repo("target_fn", repo)
        assert found is not None and found[0] == target

    def test_absent_name_returns_none(self, repo: Path) -> None:
        _write(repo, "pkg/mod.py", FILLER_SRC)
        assert _find_def_in_repo("target_fn", repo) is None

    def test_only_match_lives_in_node_modules(self, repo: Path) -> None:
        _write(repo, "node_modules/dep/mod.py", TARGET_SRC)
        assert _find_def_in_repo("target_fn", repo) is None

    def test_only_match_lives_in_agent_dir(self, repo: Path) -> None:
        _write(repo, ".agent/workspace/mod.py", TARGET_SRC)
        assert _find_def_in_repo("target_fn", repo) is None

    def test_unreadable_file_does_not_abort_the_search(self, repo: Path) -> None:
        """A vanished/unreadable file is skipped, not fatal."""
        missing = repo / "pkg" / "ghost.py"
        target = _write(repo, "pkg/mod.py", TARGET_SRC)
        found = _find_def_in_repo("target_fn", _StubBase([missing, target]))
        assert found is not None and found[0] == target

    def test_indented_method_def_is_matched(self, repo: Path) -> None:
        """The pattern allows leading whitespace, so a method counts."""
        target = _write(
            repo, "pkg/cls.py", "class C:\n    def target_fn(self):\n        pass\n"
        )
        found = _find_def_in_repo("target_fn", repo)
        assert found is not None and found[0] == target

    def test_name_is_regex_escaped(self, repo: Path) -> None:
        _write(repo, "pkg/mod.py", "def target_fn(a):\n    pass\n")
        assert _find_def_in_repo("target.fn", repo) is None
