"""tests/test_cr21_3_prosody_wiring.py — AUTO-CR-21-3 acceptance tests.

Validates the inner-loop wiring for the prosody gate:

  - test_verse_task_with_bad_rhyme_triggers_revision
      Stub coder: attempt 1 → bad poem, attempt 2 → good poem.
      Prosody gate REVISE on attempt 1, APPROVED on attempt 2.
      Result: passed=True on attempt 2, feedback contains "prosody rejected".

  - test_non_verse_task_skips_prosody
      Instruction has no ритм/рифм → prosody validator is a no-op even on
      a bad poem.

  - test_cap_accepts_with_warning
      Always-bad poem, max_prosody_revisions=1 → stops at cap, accepted,
      warning logged.

  - test_disabled_skips
      prosody_check_creative=false → make_prosody_validator returns None.

  - test_code_mode_unaffected
      Regression: code-mode task with a verse instruction never touches the
      prosody gate.
"""

from __future__ import annotations

import configparser
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tools.auto.inner_loop import InnerLoop, InnerLoopResult
from tools.auto.prosody import (
    ProsodyValidator,
    ProsodyVerdict,
    make_prosody_validator,
)


# ── Helpers / stubs ───────────────────────────────────────────────────────────

def _make_config(*, enabled: bool = True, max_rev: int = 2) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "validator_agent": {
            "prosody_check_creative": str(enabled).lower(),
            "max_prosody_revisions": str(max_rev),
            "prosody_min_scheme": "ABCB",
            "prosody_syllable_tolerance": "2",
        }
    })
    return cfg


def _approved_coder_result(files_written=None):
    return SimpleNamespace(
        succeeded=True,
        files_written=files_written or [],
        missing_context=[],
        context_satisfied=True,
        error=None,
    )


def _passed_exec_result():
    return SimpleNamespace(
        passed=True, exit_code=0, stdout="ok", stderr="",
        traceback="", timed_out=False, command="",
    )


def _failed_exec_result():
    return SimpleNamespace(
        passed=False, exit_code=1, stdout="", stderr="fail",
        traceback="", timed_out=False, command="",
    )


class _SequentialCoder:
    """Coder that cycles through a list of per-attempt (files_written) results."""

    def __init__(self, sequence: list[list[str]]):
        self._seq = sequence
        self._idx = 0

    def generate(self, task, base_dir, prior_feedback=None, prefetched_context=""):
        files = self._seq[min(self._idx, len(self._seq) - 1)]
        self._idx += 1
        return _approved_coder_result(files_written=files)


class _AlwaysPassExecutor:
    def run(self, task):
        return _passed_exec_result()


class _AlwaysApproveValidator:
    last_missing_context: list = []

    def approve(self, task, exec_result, coder_result, *, base_dir=None):
        return True, ""


def _build_loop(
    tmp_path: Path,
    *,
    poems: list[str],           # one poem per attempt (written to target file)
    task: dict,
    max_prosody_revisions: int = 2,
    max_attempts: int = 5,
    task_mode: str = "creative",
    enabled: bool = True,
) -> tuple[InnerLoop, dict]:
    """Build a fully-stubbed InnerLoop with on-disk poem files."""
    target = "chapter.txt"
    target_path = tmp_path / target
    task = {**task, "id": "t1", "target_files": [target]}

    # Write the first poem so the file exists before attempt 1
    target_path.write_text(poems[0], encoding="utf-8")

    call_count = [0]

    class _PoemCoder:
        def generate(self, task, base_dir, prior_feedback=None, prefetched_context=""):
            idx = min(call_count[0], len(poems) - 1)
            (Path(base_dir) / target).write_text(poems[idx], encoding="utf-8")
            call_count[0] += 1
            return _approved_coder_result(files_written=[target])

    pv = ProsodyValidator(max_prosody_revisions=max_prosody_revisions) if enabled else None

    loop = InnerLoop(
        coder=_PoemCoder(),
        executor=_AlwaysPassExecutor(),
        validator=_AlwaysApproveValidator(),
        max_attempts=max_attempts,
        prosody_validator=pv,
        task_mode=task_mode,
    )
    return loop, task


# ── Poem fixtures ─────────────────────────────────────────────────────────────

# Clean ABCB poem — only lines 2&4 rhyme → should pass min_scheme=ABCB
_GOOD_POEM = """\
Трещит январский холод дня
Горит вдали ночная луна
Сидит медведь в берлоге тёмной
Шумит прибой — морская волна

Морозный ветер, снег, метель
Блестит в ночи родная луна
Лежит сугроб у старой ели
В ночи шумит морская волна
"""

# Prose-like text with no rhyme at all
_BAD_POEM = """\
Мороз трещит в лесу суровом
Медведь сидит в берлоге тёмной
Летит снежинка над сугробом
Стоит сосна в лесу огромном

Буря мглою небо кроет
Вихри снежные крутя
Плачет ива над рекою
Словно плачет у дитя
"""

_VERSE_TASK = {
    "goal": "написать стихи с ритмом и рифмой",
    "instruction": "Напиши красивое стихотворение про зиму с рифмой",
}

_PROSE_TASK = {
    "goal": "написать рассказ о природе",
    "instruction": "Напиши короткий рассказ о зиме в прозе",
}


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestVersePoemTriggersRevision:
    def test_bad_then_good_passes_on_attempt_2(self, tmp_path):
        loop, task = _build_loop(
            tmp_path,
            poems=[_BAD_POEM, _GOOD_POEM],
            task=_VERSE_TASK,
            max_prosody_revisions=2,
            max_attempts=5,
        )
        result = loop.run_task(task, tmp_path)
        assert result.passed is True
        assert result.attempts_used == 2

    def test_feedback_contains_prosody_rejected(self, tmp_path):
        loop, task = _build_loop(
            tmp_path,
            poems=[_BAD_POEM, _GOOD_POEM],
            task=_VERSE_TASK,
            max_prosody_revisions=2,
            max_attempts=5,
        )
        result = loop.run_task(task, tmp_path)
        all_fb = " ".join(r.feedback for r in result.records)
        assert "prosody rejected" in all_fb

    def test_good_poem_approved_immediately(self, tmp_path):
        loop, task = _build_loop(
            tmp_path,
            poems=[_GOOD_POEM],
            task=_VERSE_TASK,
            max_prosody_revisions=2,
            max_attempts=5,
        )
        result = loop.run_task(task, tmp_path)
        assert result.passed is True
        assert result.attempts_used == 1


class TestNonVerseTaskSkipsProsody:
    def test_prose_task_skips_gate(self, tmp_path):
        """Non-verse task: prosody gate is a no-op even with a bad poem."""
        loop, task = _build_loop(
            tmp_path,
            poems=[_BAD_POEM],
            task=_PROSE_TASK,
            max_prosody_revisions=2,
            max_attempts=3,
        )
        result = loop.run_task(task, tmp_path)
        assert result.passed is True
        assert result.attempts_used == 1

    def test_prose_task_no_prosody_feedback(self, tmp_path):
        loop, task = _build_loop(
            tmp_path,
            poems=[_BAD_POEM],
            task=_PROSE_TASK,
        )
        result = loop.run_task(task, tmp_path)
        all_fb = " ".join(r.feedback for r in result.records)
        assert "prosody" not in all_fb


class TestCapAcceptsWithWarning:
    def test_cap_stops_and_accepts(self, tmp_path, caplog):
        """Always-bad poem + cap=1 → accepted after 2 attempts (1 rejection + cap accept)."""
        loop, task = _build_loop(
            tmp_path,
            poems=[_BAD_POEM, _BAD_POEM, _BAD_POEM],
            task=_VERSE_TASK,
            max_prosody_revisions=1,
            max_attempts=5,
        )
        with caplog.at_level(logging.WARNING, logger="tools.auto.inner_loop"):
            result = loop.run_task(task, tmp_path)

        # Cap reached → accepted (passed=True) so the pipeline can move on
        assert result.passed is True

    def test_cap_logs_warning(self, tmp_path, caplog):
        loop, task = _build_loop(
            tmp_path,
            poems=[_BAD_POEM, _BAD_POEM, _BAD_POEM],
            task=_VERSE_TASK,
            max_prosody_revisions=1,
            max_attempts=5,
        )
        with caplog.at_level(logging.WARNING, logger="tools.auto.inner_loop"):
            loop.run_task(task, tmp_path)

        warning_text = " ".join(caplog.messages)
        assert "prosody" in warning_text.lower() or "cap" in warning_text.lower()

    def test_first_attempt_records_rejection(self, tmp_path):
        loop, task = _build_loop(
            tmp_path,
            poems=[_BAD_POEM, _BAD_POEM],
            task=_VERSE_TASK,
            max_prosody_revisions=1,
            max_attempts=5,
        )
        result = loop.run_task(task, tmp_path)
        feedbacks = [r.feedback for r in result.records]
        assert any("prosody rejected" in fb for fb in feedbacks)


class TestDisabledSkips:
    def test_make_prosody_validator_disabled(self):
        cfg = _make_config(enabled=False)
        pv = make_prosody_validator(cfg)
        assert pv is None

    def test_make_prosody_validator_enabled(self):
        cfg = _make_config(enabled=True, max_rev=3)
        pv = make_prosody_validator(cfg)
        assert pv is not None
        assert pv.max_prosody_revisions == 3

    def test_disabled_validator_none_means_no_gate(self, tmp_path):
        """With prosody_validator=None, bad verse poem is APPROVED immediately."""
        loop, task = _build_loop(
            tmp_path,
            poems=[_BAD_POEM],
            task=_VERSE_TASK,
            enabled=False,
        )
        result = loop.run_task(task, tmp_path)
        assert result.passed is True
        assert result.attempts_used == 1


class TestCodeModeUnaffected:
    def test_code_mode_ignores_prosody_validator(self, tmp_path):
        """Regression: code-mode tasks never hit the prosody gate."""
        target = "module.py"
        (tmp_path / target).write_text("def foo(): pass\n", encoding="utf-8")
        task = {
            "id": "code-t1",
            "target_files": [target],
            "goal": "написать функцию с ритмом",   # contains ритм — should not matter
            "instruction": "add a function",
        }
        pv = ProsodyValidator(max_prosody_revisions=2)
        loop = InnerLoop(
            coder=_SequentialCoder([[target]]),
            executor=_AlwaysPassExecutor(),
            validator=_AlwaysApproveValidator(),
            max_attempts=3,
            prosody_validator=pv,
            task_mode="code",   # ← code mode, not creative
        )
        result = loop.run_task(task, tmp_path)
        assert result.passed is True
        assert result.attempts_used == 1
        # No prosody feedback in any record
        all_fb = " ".join(r.feedback for r in result.records)
        assert "prosody" not in all_fb


class TestProsodyValidatorUnit:
    """Unit tests for ProsodyValidator.check()."""

    def test_non_verse_task_returns_approved(self):
        pv = ProsodyValidator()
        # AUTO-CR-22-2 widened is_verse_task to catch poem nouns; this
        # control case must use text with no verse signal at all (the
        # original instruction "без стихов" itself contains "стихов" and
        # would now correctly activate the gate).
        task = {"goal": "рассказ о природе", "instruction": "напиши прозу о лесе"}
        v = pv.check(task, _BAD_POEM)
        assert v.approved is True

    def test_verse_task_bad_poem_returns_revise(self):
        pv = ProsodyValidator()
        task = _VERSE_TASK
        v = pv.check(task, _BAD_POEM)
        assert v.approved is False

    def test_verse_task_good_poem_returns_approved(self):
        pv = ProsodyValidator()
        task = _VERSE_TASK
        v = pv.check(task, _GOOD_POEM)
        assert v.approved is True

    def test_empty_text_approved(self):
        pv = ProsodyValidator()
        v = pv.check(_VERSE_TASK, "")
        assert v.approved is True

    def test_check_never_raises(self):
        pv = ProsodyValidator()
        for text in ["", "\x00\x01", "рифм" * 200, None.__class__.__name__]:
            v = pv.check(_VERSE_TASK, text)
            assert isinstance(v, ProsodyVerdict)

    def test_max_prosody_revisions_stored(self):
        pv = ProsodyValidator(max_prosody_revisions=5)
        assert pv.max_prosody_revisions == 5

    def test_feedback_format(self):
        pv = ProsodyValidator()
        v = pv.check(_VERSE_TASK, _BAD_POEM)
        assert "PROSODY ISSUE" in v.feedback()

    def test_goal_checked_for_keyword(self):
        pv = ProsodyValidator()
        task_goal_only = {"goal": "написать с рифмой", "instruction": ""}
        v = pv.check(task_goal_only, _BAD_POEM)
        assert v.approved is False

    def test_instruction_checked_for_keyword(self):
        pv = ProsodyValidator()
        # AUTO-CR-22-2: rhyme/rhythm are now required independently — the
        # instruction must mention "рифм" (not just "ритм") for the
        # rhyme-violating _BAD_POEM to be rejected here.
        task_instr_only = {"goal": "", "instruction": "добавь рифмы к стихам"}
        v = pv.check(task_instr_only, _BAD_POEM)
        assert v.approved is False


class TestMakeProsodyValidatorConfig:
    def test_reads_max_revisions(self):
        cfg = _make_config(enabled=True, max_rev=7)
        pv = make_prosody_validator(cfg)
        assert pv.max_prosody_revisions == 7

    def test_reads_min_scheme(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"validator_agent": {
            "prosody_check_creative": "true",
            "prosody_min_scheme": "AABB",
        }})
        pv = make_prosody_validator(cfg)
        assert pv._min_scheme == "AABB"

    def test_reads_syllable_tolerance(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"validator_agent": {
            "prosody_check_creative": "true",
            "prosody_syllable_tolerance": "3",
        }})
        pv = make_prosody_validator(cfg)
        assert pv._syllable_tolerance == 3

    def test_returns_none_when_disabled(self):
        cfg = _make_config(enabled=False)
        assert make_prosody_validator(cfg) is None
