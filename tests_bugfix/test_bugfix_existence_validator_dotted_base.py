"""A6: a base_dir whose own path contains a dot component must not hide files.

ExistenceValidator filters out files living in hidden directories by testing
``any(part.startswith(".") for part in path.parts)`` — but ``base_dir.rglob()``
yields paths that still carry the base directory's own components. So a base
path containing a dot component (``/home/u/.cache/repo``, ``/srv/.deploy/src``,
``/tmp/.venv/proj``) made the predicate match on the BASE, and every single
file in the repository was skipped.

Two consequences, both silent and total (not intermittent):

1. ``_repo_has_tests`` returned False for a repo that HAS tests →
   ``phantom_tests=True``, the phantom-test-suite flag, for a valid suite.
2. The name-match fallback in ``check()`` reported genuinely present files
   as missing.

So the gate rejected valid documentation purely because of where it happened
to be checked out. The filter must be computed against the path RELATIVE to
the base, so a ``.git/`` inside the repo is still skipped but a ``.cache/``
above it is irrelevant.
"""

from __future__ import annotations

import pytest

from tools.auto.existence_validator import ExistenceValidator


def _make_repo(root):
    """A repo with a real test suite and a hidden dir that must stay ignored."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (root / ".git" / "hidden_test.py").write_text("def test_hidden(): pass\n", encoding="utf-8")
    return root


@pytest.fixture
def validator() -> ExistenceValidator:
    return ExistenceValidator()


class TestDotComponentInBasePath:
    def test_repo_has_tests_under_a_dotted_base(self, tmp_path, validator):
        """A base path with a dot component must not void the test check."""
        base = _make_repo(tmp_path / ".cache" / "repo")
        assert validator._repo_has_tests(base) is True

    def test_no_phantom_test_suite_flag(self, tmp_path, validator):
        base = _make_repo(tmp_path / ".cache" / "repo")
        verdict = validator.check(
            "Run the tests with `pytest`.", base, rel_path=""
        )
        assert verdict.phantom_tests is False, (
            f"a repo that HAS a test suite was flagged as phantom: {verdict}"
        )
        assert verdict.approved is True

    def test_missing_file_not_reported_for_present_file(self, tmp_path, validator):
        """The name-match fallback used to call a present file missing."""
        base = _make_repo(tmp_path / ".venv" / "proj")
        verdict = validator.check(
            "The suite lives in `test_app.py`.", base, rel_path=""
        )
        assert verdict.missing == [], (
            f"`test_app.py` exists but was reported missing: {verdict}"
        )
        assert verdict.approved is True

    def test_deeply_dotted_base(self, tmp_path, validator):
        base = _make_repo(tmp_path / ".a" / ".b" / "src")
        verdict = validator.check("Run `pytest`.", base, rel_path="")
        assert verdict.approved is True
        assert verdict.phantom_tests is False

    def test_plain_base_unchanged(self, tmp_path, validator):
        """The already-working case must keep working."""
        base = _make_repo(tmp_path / "plain" / "repo")
        verdict = validator.check("Run `pytest`.", base, rel_path="")
        assert verdict.approved is True
        assert verdict.missing == []
        assert verdict.phantom_tests is False


class TestHiddenDirsInsideRepoStillIgnored:
    def test_hidden_test_inside_repo_is_not_counted(self, tmp_path, validator):
        """Only a hidden test file exists here → no suite."""
        base = tmp_path / "plain"
        (base / "src").mkdir(parents=True, exist_ok=True)
        (base / ".git").mkdir(parents=True, exist_ok=True)
        (base / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        (base / ".git" / "hidden_test.py").write_text("def test_hidden(): pass\n", encoding="utf-8")
        assert validator._repo_has_tests(base) is False

    def test_hidden_test_inside_repo_under_dotted_base(self, tmp_path, validator):
        """Both filters must compose correctly."""
        base = tmp_path / ".cache" / "repo"
        (base / "src").mkdir(parents=True, exist_ok=True)
        (base / ".github").mkdir(parents=True, exist_ok=True)
        (base / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        (base / ".github" / "dotfile_test.py").write_text("def test_d(): pass\n", encoding="utf-8")
        assert validator._repo_has_tests(base) is False
        verdict = validator.check(
            "The suite is `dotfile_test.py`.", base, rel_path=""
        )
        assert verdict.missing == ["dotfile_test.py"]

    def test_real_test_still_found_next_to_hidden_dir(self, tmp_path, validator):
        base = tmp_path / ".cache" / "repo"
        (base / "tests").mkdir(parents=True, exist_ok=True)
        (base / ".venv").mkdir(parents=True, exist_ok=True)
        (base / "tests" / "test_ok.py").write_text("def test_ok(): pass\n", encoding="utf-8")
        (base / ".venv" / "junk_test.py").write_text("def test_j(): pass\n", encoding="utf-8")
        assert validator._repo_has_tests(base) is True
        verdict = validator.check("The suite is `junk_test.py`.", base, rel_path="")
        assert verdict.missing == ["junk_test.py"]
        assert verdict.phantom_tests is False


class TestGenuinelyMissingFilesStillRejected:
    def test_absent_file_reported_missing_under_dotted_base(self, tmp_path, validator):
        base = _make_repo(tmp_path / ".cache" / "repo")
        verdict = validator.check(
            "See `tests/test_absent.py` for details.", base, rel_path=""
        )
        assert verdict.missing == ["tests/test_absent.py"]
        assert verdict.approved is False

    def test_genuinely_testless_repo_still_flagged_phantom(self, tmp_path, validator):
        base = tmp_path / ".cache" / "repo"
        base.mkdir(parents=True, exist_ok=True)
        (base / "main.py").write_text("print(1)\n", encoding="utf-8")
        verdict = validator.check("Run `pytest` to test.", base, rel_path="")
        assert verdict.phantom_tests is True
        assert verdict.approved is False

    def test_feedback_names_the_offenders(self, tmp_path, validator):
        base = _make_repo(tmp_path / ".cache" / "repo")
        verdict = validator.check("See `nope.py`.", base, rel_path="")
        assert "nope.py" in verdict.feedback()
