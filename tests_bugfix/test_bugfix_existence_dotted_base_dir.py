"""A6: a dotted base_dir must not make every reference look hidden.

Real failure mode
-----------------
Both rglob() call sites in ExistenceValidator filtered with
``any(part.startswith(".") for part in path.parts)`` — but rglob() yields
ABSOLUTE paths, so every component of the base directory itself was part of
the test. A repository checked out under a dotted path (/home/u/.cache/repo,
/srv/.deploy/src) therefore made:

* `_repo_has_tests` return False for a repo that has tests, lighting the
  phantom-test-suite flag; and
* the name-match fallback skip genuinely present files, reporting them as
  missing.

The gate rejected valid documents. Silent and total, not intermittent.
"""

from __future__ import annotations

from pathlib import Path

from tools.auto.existence_validator import ExistenceValidator


def _dotted_repo(tmp_path: Path) -> Path:
    """A repo checked out under a dotted path, like ~/.cache/repo or /srv/.deploy/src."""
    base = tmp_path / ".cache" / "repo"
    (base / "tests").mkdir(parents=True)
    (base / "lib").mkdir(parents=True)
    (base / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (base / "tests" / "test_main.py").write_text("import unittest\n", encoding="utf-8")
    (base / "lib" / "helpers_util.py").write_text("def h():\n    pass\n", encoding="utf-8")
    return base


def _plain_repo(tmp_path: Path) -> Path:
    base = tmp_path / "repo"
    (base / "tests").mkdir(parents=True)
    (base / "lib").mkdir(parents=True)
    (base / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (base / "tests" / "test_main.py").write_text("import unittest\n", encoding="utf-8")
    (base / "lib" / "helpers_util.py").write_text("def h():\n    pass\n", encoding="utf-8")
    return base


class TestPhantomTestSuiteUnderDottedBase:

    def test_repo_with_tests_is_not_phantom(self, tmp_path):
        base = _dotted_repo(tmp_path)
        verdict = ExistenceValidator().check(
            "Run `pytest` to verify. The entry point is `main.py`.", base,
        )
        assert verdict.approved is True, f"unexpected: {verdict}"
        assert verdict.phantom_tests is False, (
            "a dotted checkout path must not make a real test suite look phantom"
        )

    def test_plain_base_dir_behaves_the_same(self, tmp_path):
        base = _plain_repo(tmp_path)
        verdict = ExistenceValidator().check(
            "Run `pytest` to verify. The entry point is `main.py`.", base,
        )
        assert verdict.approved is True
        assert verdict.phantom_tests is False


class TestNameMatchFallbackUnderDottedBase:

    def test_nested_file_is_found_by_name(self, tmp_path):
        """`helpers_util.py` lives at lib/helpers_util.py, so the
        name-match fallback is the only way to resolve it."""
        base = _dotted_repo(tmp_path)
        verdict = ExistenceValidator().check(
            "The helper lives in `helpers_util.py`.", base,
        )
        assert verdict.approved is True, f"unexpected: {verdict}"
        assert verdict.missing == []

    def test_genuinely_missing_file_still_reported(self, tmp_path):
        base = _dotted_repo(tmp_path)
        verdict = ExistenceValidator().check(
            "The helper lives in `no_such_module.py`.", base,
        )
        assert verdict.approved is False
        assert verdict.missing == ["no_such_module.py"]


class TestHiddenDirectoriesInsideTheRepoStillExcluded:
    """The fix must not over-broaden: dot dirs INSIDE the repo stay ignored."""

    def test_test_suite_only_in_dotted_dir_is_still_phantom(self, tmp_path):
        base = _plain_repo(tmp_path)
        (base / "tests" / "test_main.py").unlink()
        (base / ".venv" / "lib").mkdir(parents=True)
        (base / ".venv" / "lib" / "test_frozen.py").write_text(
            "import unittest\n", encoding="utf-8",
        )
        verdict = ExistenceValidator().check(
            "Run `pytest` to verify. The entry point is `main.py`.", base,
        )
        assert verdict.approved is False
        assert verdict.phantom_tests is True

    def test_file_only_in_dotted_dir_still_reported_missing(self, tmp_path):
        base = _plain_repo(tmp_path)
        (base / "lib" / "helpers_util.py").unlink()
        (base / ".git" / "objects").mkdir(parents=True)
        (base / ".git" / "objects" / "helpers_util.py").write_text(
            "def h():\n    pass\n", encoding="utf-8",
        )
        verdict = ExistenceValidator().check(
            "The helper lives in `helpers_util.py`.", base,
        )
        assert verdict.approved is False
        assert verdict.missing == ["helpers_util.py"]

    def test_repo_with_no_test_files_at_all_is_phantom(self, tmp_path):
        """The gate must still fire when there genuinely is no suite."""
        base = _dotted_repo(tmp_path)
        (base / "tests" / "test_main.py").unlink()
        verdict = ExistenceValidator().check(
            "Run `pytest` to verify. The entry point is `main.py`.", base,
        )
        assert verdict.approved is False
        assert verdict.phantom_tests is True
