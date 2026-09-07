"""tests_bugfix/test_bugfix_existence_validator_dot_base_dir.py

The two hidden-directory filters in ExistenceValidator test `path.parts`:

    for path in base_dir.rglob("*.py"):                      # _repo_has_tests
        if any(part.startswith(".") for part in path.parts):
            continue

    if any(p.name == name and not any(q.startswith(".") for q in p.parts)
           for p in base.rglob(glob.escape(name))):          # check()

`Path.parts` is rooted at the filesystem root, so it contains the BASE
DIRECTORY'S OWN COMPONENTS as well as everything inside it. Any base path that
contains a dot component — `/home/u/.cache/repo`, `/srv/.deploy/src`,
`/tmp/.venv/proj` — therefore makes every single file in the repository look
hidden, and both filters skip everything.

Two silent, total (not intermittent) failures follow:

  * `_repo_has_tests` returns False for a repo that has tests, so a document
    that says "run `pytest`" trips the phantom-test-suite flag and gets
    rejected.
  * the name-match fallback in `check()` reports genuinely present files as
    missing, so the gate rejects valid documents.

The dot-detection is there for a real reason — `.git/`, `.venv/`,
`__pycache__` inside the repo must stay ignored. What is wrong is only WHICH
parts are inspected: it must be the path relative to the base dir, i.e. what
is actually inside the repository.

Fix under test: compute the filter against `path.relative_to(base).parts`.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.existence_validator import ExistenceValidator


def _base_with(base_name: str, tree: dict[str, str]) -> Path:
    """Create a repository at *base_name* with the given relative files.

    ``tree`` maps a relative path to its contents. Dot components can live in
    the base name itself (the bug) or inside the tree (legitimately hidden).
    """
    parts = Path(base_name)
    for rel, content in tree.items():
        path = parts / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return parts


class TestRepoHasTestsUnderDotBaseDir:
    def test_tests_found_when_base_dir_has_a_dot_component(self, tmp_path):
        """THE BUG: `.cache` is a dot component, so every file looked hidden
        and the repo was reported as having no test suite."""
        base = _base_with(tmp_path / ".cache" / "repo", {
            "src/main.py": "def main(): pass\n",
            "tests/test_main.py": "def test_it(): pass\n",
        })
        assert (base / "tests" / "test_main.py").exists(), "precondition"
        assert ExistenceValidator._repo_has_tests(base) is True, (
            "a repo whose base dir sits under a dot-directory has real tests; "
            "path.parts includes the base's own components, so the hidden-file "
            "filter must judge the path relative to base"
        )

    def test_deployment_style_base_dir(self, tmp_path):
        base = _base_with(tmp_path / "srv" / ".deploy" / "src", {
            "pkg/app.py": "x = 1\n",
            "app_test.py": "def test_it(): pass\n",
        })
        assert ExistenceValidator._repo_has_tests(base) is True

    def test_conftest_counts_too(self, tmp_path):
        base = _base_with(tmp_path / ".venv" / "proj", {
            "tests/conftest.py": "import pytest\n",
        })
        assert ExistenceValidator._repo_has_tests(base) is True


class TestNameMatchFallbackUnderDotBaseDir:
    def test_bare_filename_in_subdir_is_found(self, tmp_path):
        """THE BUG, second half: the reference resolves only through the
        rglob name-match fallback, which had the identical predicate."""
        base = _base_with(tmp_path / ".cache" / "repo", {
            "helpers/thingy.py": "def f(): pass\n",
        })
        v = ExistenceValidator()
        verdict = v.check("Use `thingy.py` for the helper.", base)
        assert verdict.approved, (
            f"`thingy.py` exists at helpers/thingy.py but was reported "
            f"missing: {verdict.reason}"
        )
        assert verdict.missing == []


class TestPhantomTestFlagUnderDotBaseDir:
    def test_test_command_does_not_trip_phantom_flag(self, tmp_path):
        """THE BUG, as the operator sees it: a valid document is rejected."""
        base = _base_with(tmp_path / ".cache" / "repo", {
            "src/main.py": "def main(): pass\n",
            "tests/test_main.py": "def test_it(): pass\n",
        })
        v = ExistenceValidator()
        verdict = v.check("Run `python -m unittest discover` to check.", base)
        assert verdict.approved, (
            f"the repo has a real test suite, so the phantom-test flag must "
            f"not fire: {verdict.reason}"
        )
        assert verdict.phantom_tests is False

    def test_phantom_flag_still_fires_when_there_is_no_suite(self, tmp_path):
        base = _base_with(tmp_path / ".cache" / "repo", {
            "src/main.py": "def main(): pass\n",
        })
        v = ExistenceValidator()
        verdict = v.check("Run `python -m unittest discover` to check.", base)
        assert verdict.phantom_tests is True
        assert "phantom test suite" in verdict.reason


class TestHiddenDirsInsideTheRepoStillIgnored:
    """The dot-filter exists for a reason: repo-internal dot directories must
    not count as tests and must not satisfy a reference."""

    def test_git_dir_is_not_a_test_suite(self, tmp_path):
        base = _base_with(tmp_path / "plain" / "repo", {
            ".git/tests/test_fake.py": "def test_it(): pass\n",
        })
        assert ExistenceValidator._repo_has_tests(base) is False

    def test_dotenv_dir_inside_repo_is_not_a_test_suite(self, tmp_path):
        base = _base_with(tmp_path / "plain" / "repo", {
            ".coverage/test_fake.py": "def test_it(): pass\n",
        })
        assert ExistenceValidator._repo_has_tests(base) is False

    def test_hidden_file_reference_still_reported_missing(self, tmp_path):
        base = _base_with(tmp_path / "plain" / "repo", {
            ".venv/lib/thingy.py": "def f(): pass\n",
        })
        v = ExistenceValidator()
        verdict = v.check("Use `thingy.py` for the helper.", base)
        assert verdict.missing == ["thingy.py"], (
            "a file that only exists inside a hidden directory must still be "
            "reported missing — that is what the filter is for"
        )

    def test_hidden_reference_ok_when_a_real_copy_exists(self, tmp_path):
        base = _base_with(tmp_path / "plain" / "repo", {
            ".venv/lib/thingy.py": "def f(): pass\n",
            "helpers/thingy.py": "def f(): pass\n",
        })
        v = ExistenceValidator()
        verdict = v.check("Use `thingy.py` for the helper.", base)
        assert verdict.approved


class TestNonDotBaseDirUnaffected:
    def test_normal_base_dir_still_finds_tests(self, tmp_path):
        base = _base_with(tmp_path / "workspace" / "proj", {
            "tests/test_main.py": "def test_it(): pass\n",
        })
        assert ExistenceValidator._repo_has_tests(base) is True

    def test_normal_base_dir_name_match_still_works(self, tmp_path):
        base = _base_with(tmp_path / "workspace" / "proj", {
            "helpers/thingy.py": "def f(): pass\n",
        })
        v = ExistenceValidator()
        assert v.check("Use `thingy.py`.", base).approved

    def test_genuinely_missing_still_reported(self, tmp_path):
        base = _base_with(tmp_path / ".cache" / "repo", {
            "src/main.py": "def main(): pass\n",
        })
        v = ExistenceValidator()
        verdict = v.check("Use `nope.py` for the helper.", base)
        assert verdict.missing == ["nope.py"]

    def test_real_relative_reference_still_works(self, tmp_path):
        base = _base_with(tmp_path / ".cache" / "repo", {
            "helpers/thingy.py": "def f(): pass\n",
        })
        v = ExistenceValidator()
        verdict = v.check("Use `helpers/thingy.py` for the helper.", base)
        assert verdict.approved
