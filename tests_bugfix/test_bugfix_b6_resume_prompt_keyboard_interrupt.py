"""B6 -- Ctrl-C at the checkpoint-resume prompt raised a bare traceback.

The prompt was wrapped in ``except EOFError`` only. The interactive REPL
further down main() has always caught ``(KeyboardInterrupt, EOFError)``, but
this one prompt -- which fires unconditionally whenever a checkpoint exists,
including under non-interactive stdin -- did not, so the most obvious way to
answer "no" produced a raw traceback.

The interrupt is reported as ``None``, deliberately distinct from the
``False`` that EOF and an explicit "n" produce. Mapping Ctrl-C to "no" would
clear the checkpoint, so interrupting the prompt would destroy the very
session it is offering to resume. The caller exits 130 and leaves the
checkpoint alone; that split is what ``test_interrupt_is_distinct_from_no``
pins.

Without the fix ``_confirm_resume_checkpoint`` does not exist and the whole
module fails to import the symbol.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402


class TestConfirmResumeCheckpoint:
    def test_yes_returns_true(self) -> None:
        with patch("builtins.input", return_value="y"):
            assert main._confirm_resume_checkpoint() is True

    def test_yes_is_case_and_space_insensitive(self) -> None:
        with patch("builtins.input", return_value="  Y  "):
            assert main._confirm_resume_checkpoint() is True

    def test_no_returns_false(self) -> None:
        with patch("builtins.input", return_value="n"):
            assert main._confirm_resume_checkpoint() is False

    def test_empty_answer_returns_false(self) -> None:
        with patch("builtins.input", return_value=""):
            assert main._confirm_resume_checkpoint() is False

    def test_eof_returns_false_without_raising(self, capsys) -> None:
        with patch("builtins.input", side_effect=EOFError):
            assert main._confirm_resume_checkpoint() is False
        assert "no input available" in capsys.readouterr().out

    def test_keyboard_interrupt_does_not_propagate(self) -> None:
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            main._confirm_resume_checkpoint()  # must not raise

    def test_keyboard_interrupt_returns_none(self) -> None:
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            assert main._confirm_resume_checkpoint() is None

    def test_interrupt_is_distinct_from_no(self) -> None:
        """None, not False.

        The caller clears the checkpoint on False. Collapsing Ctrl-C into
        False would make interrupting the prompt delete the session the
        prompt exists to offer back.
        """
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            interrupted = main._confirm_resume_checkpoint()
        with patch("builtins.input", return_value="n"):
            declined = main._confirm_resume_checkpoint()
        assert interrupted is None
        assert declined is False

    def test_interrupt_tells_the_user_the_checkpoint_is_kept(self, capsys) -> None:
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            main._confirm_resume_checkpoint()
        assert "checkpoint kept" in capsys.readouterr().out
