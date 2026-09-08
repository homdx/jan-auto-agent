"""tests/test_main_resume_checkpoint.py — checkpoint resume key safety.

_resume_from_checkpoint must use .get(key, default) for every key it reads
from the saved checkpoint dict, so a partial/corrupt checkpoint (missing
user_input, base_dir, question, etc.) degrades gracefully instead of
raising KeyError mid-resume.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main import _resume_from_checkpoint


class _FakeOrch:
    """Minimal stand-in for Orchestrator that records calls."""

    def __init__(self):
        self.pipeline_calls: list = []
        self.text_qa_calls: list = []
        self.edit_calls: list = []

    def run_pipeline(self, user_input, base_dir, resume_state=None):
        self.pipeline_calls.append((user_input, base_dir, resume_state))

    def run_text_qa(self, question, file_path, source, base_dir, resume_state=None):
        self.text_qa_calls.append((question, file_path, source, base_dir, resume_state))

    def run_edit(self, user_input, base_dir, resume_state=None):
        self.edit_calls.append((user_input, base_dir, resume_state))


class TestResumeCheckpointMissingKeys:
    """A checkpoint dict missing required keys must not crash."""

    def test_run_pipeline_missing_user_input(self):
        orch = _FakeOrch()
        saved = {"loop": "run_pipeline", "base_dir": "/tmp"}
        _resume_from_checkpoint(saved, orch, "/default")
        assert len(orch.pipeline_calls) == 1
        user_input, base_dir, _ = orch.pipeline_calls[0]
        assert user_input == ""
        assert base_dir == "/tmp"

    def test_run_pipeline_missing_base_dir(self):
        orch = _FakeOrch()
        saved = {"loop": "run_pipeline", "user_input": "fix the bug"}
        _resume_from_checkpoint(saved, orch, "/default")
        assert len(orch.pipeline_calls) == 1
        user_input, base_dir, _ = orch.pipeline_calls[0]
        assert user_input == "fix the bug"
        assert base_dir == "/default"

    def test_run_pipeline_missing_both(self):
        orch = _FakeOrch()
        saved = {"loop": "run_pipeline"}
        _resume_from_checkpoint(saved, orch, "/default")
        assert len(orch.pipeline_calls) == 1
        user_input, base_dir, _ = orch.pipeline_calls[0]
        assert user_input == ""
        assert base_dir == "/default"

    def test_run_text_qa_missing_question(self):
        orch = _FakeOrch()
        saved = {"loop": "run_text_qa", "file_path": "src.py", "base_dir": "/tmp"}
        _resume_from_checkpoint(saved, orch, "/default")
        assert len(orch.text_qa_calls) == 1
        question, file_path, source, base_dir, _ = orch.text_qa_calls[0]
        assert question == ""
        assert file_path == "src.py"

    def test_run_edit_missing_user_input(self):
        orch = _FakeOrch()
        saved = {"loop": "run_edit", "base_dir": "/tmp"}
        _resume_from_checkpoint(saved, orch, "/default")
        assert len(orch.edit_calls) == 1
        user_input, base_dir, _ = orch.edit_calls[0]
        assert user_input == ""
        assert base_dir == "/tmp"

    def test_unknown_loop_does_not_crash(self):
        orch = _FakeOrch()
        saved = {"loop": "something_else"}
        _resume_from_checkpoint(saved, orch, "/default")
        assert orch.pipeline_calls == []
        assert orch.text_qa_calls == []
        assert orch.edit_calls == []

    def test_empty_checkpoint_does_not_crash(self):
        orch = _FakeOrch()
        _resume_from_checkpoint({}, orch, "/default")
        assert orch.pipeline_calls == []
        assert orch.text_qa_calls == []
        assert orch.edit_calls == []

    def test_run_pipeline_passes_resume_state_through(self):
        orch = _FakeOrch()
        saved = {"loop": "run_pipeline", "user_input": "test", "base_dir": "/tmp",
                 "iteration": 3}
        _resume_from_checkpoint(saved, orch, "/default")
        assert orch.pipeline_calls[0][2] is saved

    def test_run_text_qa_missing_file_path(self):
        orch = _FakeOrch()
        saved = {"loop": "run_text_qa", "question": "what does it do?"}
        _resume_from_checkpoint(saved, orch, "/default")
        assert len(orch.text_qa_calls) == 1
        question, file_path, source, base_dir, _ = orch.text_qa_calls[0]
        assert question == "what does it do?"
        assert file_path == ""
        assert base_dir == "/default"

    def test_run_pipeline_complete_checkpoint_unchanged(self):
        """A fully-populated checkpoint must still work correctly."""
        orch = _FakeOrch()
        saved = {"loop": "run_pipeline", "user_input": "fix all bugs",
                 "base_dir": "/project", "iteration": 5}
        _resume_from_checkpoint(saved, orch, "/default")
        user_input, base_dir, resume_state = orch.pipeline_calls[0]
        assert user_input == "fix all bugs"
        assert base_dir == "/project"
        assert resume_state is saved
