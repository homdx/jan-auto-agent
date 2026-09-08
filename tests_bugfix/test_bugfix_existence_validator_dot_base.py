"""tests_bugfix/test_bugfix_existence_validator_dot_base.py — A6: hidden-file
filter must not be computed against the base path's own components.

Bug: ``ExistenceValidator._repo_has_tests`` does
``base_dir.rglob("*.py")`` and skips any path whose ``path.parts`` contains a
component starting with a dot.  But ``path.parts`` includes the base
directory's OWN components — so any base path containing a dot component
(``/home/u/.cache/repo``, ``/srv/.deploy/src``) causes EVERY file to be
skipped.  ``_repo_has_tests`` returns False for a repo that has tests
(phantom-test-suite flag), and the name-match fallback on the second call
site reports genuinely present files as missing — so the gate rejects valid
documents.  Silent and total, not intermittent.

Fix: compute the filter against ``path.relative_to(base).parts`` instead of
``path.parts``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.existence_validator import ExistenceValidator


def _make_repo(base: Path) -> Path:
    """A repo with a real test file, nested under a hidden dot-component path."""
    src = base / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "main.py").write_text("x = 1\n", encoding="utf-8")
    (src / "test_main.py").write_text("def test_x():\n    assert True\n",
                                       encoding="utf-8")
    return base


class TestRepoHasTestsDotBase:
    def test_dot_component_in_base_path_does_not_mask_tests(self, tmp_path):
        """A base path containing a hidden dot component must not hide every file.

        The bug predicate is ``any(part.startswith(".") for part in path.parts)``,
        and ``path.parts`` INCLUDES the base directory's own components — so a
        genuinely hidden component (``.cache``, ``.deploy``) makes the predicate
        True for EVERY file under it.  ``dot.base`` (a dot *inside* a component,
        which does not start with one) does NOT trigger the bug — only a real
        hidden component does, which is why the fixture uses ``.cache`` here.
        """
        root = tmp_path / ".cache"
        repo = _make_repo(root / "repo")
        assert ExistenceValidator._repo_has_tests(repo) is True, (
            "_repo_has_tests returned False for a repo that has tests, "
            "because the base path contains a hidden dot component (.cache) "
            "and the hidden-file filter was computed against path.parts — "
            "which includes the base's own components — so every file was "
            "skipped"
        )

    def test_plain_base_path_still_detects_tests(self, tmp_path):
        repo = _make_repo(tmp_path / "plain" / "repo")
        assert ExistenceValidator._repo_has_tests(repo) is True

    def test_repo_without_tests_reports_false(self, tmp_path):
        repo = tmp_path / ".cache" / "notests"
        src = repo / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "main.py").write_text("x = 1\n", encoding="utf-8")
        assert ExistenceValidator._repo_has_tests(repo) is False


class TestNameMatchFallbackDotBase:
    def test_name_match_finds_present_file_in_dot_base(self, tmp_path):
        """The name-match fallback must not report a present file as missing.

        A bare name referenced from a nested doc falls through to the
        rglob-based name match (existence_validator.py:213-216), which used
        to test path.parts — including the base's own hidden component — and
        so skipped every hit.
        """
        repo = _make_repo(tmp_path / ".cache" / "repo")
        ev = ExistenceValidator()
        verdict = ev.check(
            "Run `test_main.py` to exercise the suite.", base_dir=repo,
        )
        assert verdict.approved is True, verdict.feedback()
        assert verdict.missing == [], verdict.missing

    def test_genuinely_missing_file_still_reported(self, tmp_path):
        repo = _make_repo(tmp_path / ".cache" / "repo")
        ev = ExistenceValidator()
        verdict = ev.check(
            "Run `does_not_exist.py`.", base_dir=repo,
        )
        assert verdict.approved is False
        assert "does_not_exist.py" in verdict.missing