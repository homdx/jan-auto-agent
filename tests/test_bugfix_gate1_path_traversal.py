"""tests/test_bugfix_gate1_path_traversal.py — BUGFIX (audit): path
traversal in two Gate 1 helpers.

* gate1_filter.py's `_check_existence`: the `new_file` branch already
  confirmed its candidate path resolves inside base_dir before this fix;
  the normal (non-new_file) branch read straight from `base_dir /
  loc.file` with no such check, so a cited path like "../secret.py" read
  outside the repo and handed its content to Stage B.

* gate1_grounding.py's `instruction_file_context`: `_FILENAME_TOKEN_RE`
  allows "/" and "." inside a matched token, so an instruction
  containing something like "a/../../secret.py" matched and read
  outside base_dir the same way.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import Gate1Filter
from tools.auto.gate1_grounding import instruction_file_context


def _gate() -> Gate1Filter:
    gate = Gate1Filter.__new__(Gate1Filter)
    gate._task_mode = "code"
    gate._max_context_lines = 40
    gate._max_block_chars = 4000
    return gate


class TestGate1FilterPathTraversalBlocked:
    def test_escaping_cited_path_is_rejected(self, tmp_path):
        (tmp_path / "secret.py").write_text("SECRET = 1", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir()

        loc = CitedLocation(
            file="../secret.py", symbol=None, line_start=None, line_end=None,
            new_file=False,
        )
        cand = CandidateTask(
            title="t", instruction="i", target_files=["x.py"],
            acceptance_check="a", cited_location=loc, cluster="c",
        )
        ok, reason, block = _gate()._check_existence(cand, project)
        assert not ok
        assert "escapes base_dir" in reason
        assert block == ""

    def test_legitimate_cited_path_still_works(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "real.py").write_text("def f():\n    return 1\n", encoding="utf-8")

        loc = CitedLocation(
            file="real.py", symbol=None, line_start=1, line_end=2, new_file=False,
        )
        cand = CandidateTask(
            title="t", instruction="i", target_files=["x.py"],
            acceptance_check="a", cited_location=loc, cluster="c",
        )
        ok, reason, block = _gate()._check_existence(cand, project)
        assert ok


class TestGate1GroundingPathTraversalBlocked:
    def test_escaping_token_in_instruction_is_ignored(self, tmp_path):
        (tmp_path / "secret.py").write_text("SECRET_CONTENT_XYZ", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir()

        result = instruction_file_context(
            instruction="See a/../../secret.py for details",
            cited_file="main.py",
            base_dir=project,
        )
        assert result is None

    def test_legitimate_named_file_still_matches(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "utils.py").write_text("def helper():\n    pass\n", encoding="utf-8")

        result = instruction_file_context(
            instruction="match the pattern already used in utils.py",
            cited_file="main.py",
            base_dir=project,
        )
        assert result is not None
        assert "utils.py" in result
