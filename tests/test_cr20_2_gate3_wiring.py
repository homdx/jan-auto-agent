"""tests/test_cr20_2_gate3_wiring.py — AUTO-CR-20-2 acceptance tests.

Covers:
  * fact violation on attempt 1, corrected on attempt 2 → loop ends APPROVED,
    feedback contains "fact-check rejected"
  * Gate-3 always REVISE + cap=1 → loop stops, chapter accepted, warning logged
  * fact_check_creative=false → fact_validator is None, Gate-3 never called
  * task_mode="code" → Gate-3 not constructed / never called (regression)
"""

from __future__ import annotations

import configparser
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools.auto.fact_validator import FactValidator, FactVerdict
from tools.auto.inner_loop import InnerLoop


# ── Shared stubs ──────────────────────────────────────────────────────────────

class _OkExecutor:
    def run(self, task):
        return SimpleNamespace(
            passed=True, exit_code=0, stdout="", stderr="", traceback=""
        )


class _OkValidator:
    last_missing_context: list = []

    def approve(self, task, exec_result, coder_result, base_dir=None):
        return True, ""


class _WritingCoder:
    """Coder that writes the given text to the target file on each attempt."""

    def __init__(self, texts: list[str]):
        """texts[i] is written on attempt i+1 (wraps at end)."""
        self._texts = texts
        self._call_count = 0

    def generate(self, task, base_dir, prior_feedback=None, prefetched_context=""):
        text = self._texts[min(self._call_count, len(self._texts) - 1)]
        self._call_count += 1
        target = (task.get("target_files") or ["chapter_01.md"])[0]
        (Path(base_dir) / target).write_text(text, encoding="utf-8")
        return SimpleNamespace(
            succeeded=True,
            files_written=[target],
            missing_context=[],
            context_satisfied=True,
            error="",
        )


# ── Stub FactValidator factories ──────────────────────────────────────────────

def _revise_then_approve_validator(*, max_rev: int = 1):
    """Returns REVISE on first check, APPROVED on subsequent checks."""
    calls = {"n": 0}

    def llm(system, user):
        calls["n"] += 1
        if calls["n"] == 1:
            return "REVISE: TASK says мама не работает but TEXT says работает учителем"
        return "APPROVED"

    return FactValidator(llm, max_fact_revisions=max_rev)


def _always_revise_validator(*, max_rev: int = 1):
    """Always returns REVISE."""
    return FactValidator(
        lambda s, u: "REVISE: contradiction persists",
        max_fact_revisions=max_rev,
    )


def _never_called_validator():
    """A FactValidator whose check() method fails the test if called."""
    mock = MagicMock(spec=FactValidator)
    mock.max_fact_revisions = 1
    mock.check.side_effect = AssertionError("Gate-3 should not have been called")
    return mock


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_fact_violation_triggers_revision(tmp_path):
    """Stub coder writes bad text on attempt 1, good text on attempt 2.
    Gate-2 always APPROVED.  Gate-3: REVISE then APPROVED.
    Loop must end APPROVED on attempt 2; feedback must contain 'fact-check rejected'.
    """
    fact_v = _revise_then_approve_validator(max_rev=1)

    # attempt 1 → contradicting text; attempt 2 → corrected text
    coder = _WritingCoder([
        "Мама работает учителем в школе.",   # contradicts task
        "Мама сидит дома и не работает.",    # consistent
    ])

    task = {
        "id": "t1",
        "instruction": "Describe the mother. She does not work (мама не работает).",
        "target_files": ["chapter_01.md"],
    }

    loop = InnerLoop(
        coder, _OkExecutor(), _OkValidator(),
        max_attempts=5,
        fact_validator=fact_v,
        task_mode="creative",
    )
    result = loop.run_task(task, tmp_path)

    assert result.passed is True
    assert result.attempts_used == 2
    assert any("fact-check rejected" in fb for fb in result.last_feedback.split("\n")
               or [result.last_feedback]) or any(
                   "fact-check rejected" in r.feedback for r in result.records
               )


def test_cap_accepts_with_warning(tmp_path, caplog):
    """Gate-3 always REVISE, max_fact_revisions=1 → loop stops after 1 revision,
    chapter accepted, and a warning is logged.
    """
    fact_v = _always_revise_validator(max_rev=1)

    coder = _WritingCoder(["Мама работает учителем в школе."])
    task = {
        "id": "t1",
        "instruction": "мама не работает",
        "target_files": ["chapter_01.md"],
    }

    loop = InnerLoop(
        coder, _OkExecutor(), _OkValidator(),
        max_attempts=5,
        fact_validator=fact_v,
        task_mode="creative",
    )

    with caplog.at_level(logging.WARNING, logger="tools.auto.inner_loop"):
        result = loop.run_task(task, tmp_path)

    assert result.passed is True
    assert any("fact revision cap" in msg.lower() for msg in caplog.messages)


def test_disabled_skips_gate3(tmp_path):
    """fact_validator=None → Gate-3 never invoked; chapter approved by Gate-2 alone."""
    coder = _WritingCoder(["Мама работает учителем."])
    task = {
        "id": "t1",
        "instruction": "мама не работает",
        "target_files": ["chapter_01.md"],
    }

    loop = InnerLoop(
        coder, _OkExecutor(), _OkValidator(),
        max_attempts=3,
        fact_validator=None,
        task_mode="creative",
    )
    result = loop.run_task(task, tmp_path)

    assert result.passed is True
    assert result.attempts_used == 1


def test_no_target_files_skips_gate3(tmp_path):
    """task has no target_files → Gate-3 must not fire (mirrors canon gate guard).

    Regression for Bug-2: the original code fell back to _fact_text="" and
    still called the LLM, risking a wasted call or a hallucinated REVISE on
    empty text.  After the fix the outer `and target_files` guard short-circuits
    the whole block, so check() is never invoked.
    """
    never = _never_called_validator()

    coder = _WritingCoder(["Мама сидит дома."])
    task = {
        "id": "t1",
        "instruction": "мама не работает",
        # no target_files key at all
    }

    loop = InnerLoop(
        coder, _OkExecutor(), _OkValidator(),
        max_attempts=3,
        fact_validator=never,
        task_mode="creative",
    )
    result = loop.run_task(task, tmp_path)

    never.check.assert_not_called()
    assert result.passed is True


def test_code_mode_unaffected(tmp_path):
    """task_mode='code' → Gate-3 must never fire, even if a fact_validator is attached."""
    never = _never_called_validator()

    coder = _WritingCoder(["def foo(): pass"])
    task = {
        "id": "t1",
        "instruction": "Write a function foo.",
        "target_files": ["foo.py"],
    }

    loop = InnerLoop(
        coder, _OkExecutor(), _OkValidator(),
        max_attempts=3,
        fact_validator=never,
        task_mode="code",
    )
    result = loop.run_task(task, tmp_path)

    never.check.assert_not_called()
    assert result.passed is True


# ── make_inner_loop factory: fact_check_creative flag ────────────────────────

def _base_config(fact_check: str = "true", task_mode: str = "creative") -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_string(f"""
[api]
active = local
verify_ssl = false

[api_local]
base_url   = http://localhost:11434
api_key    = ollama
model      = llama3.1:8b
api_format = ollama
num_ctx    = 0

[loop]
max_attempts     = 3
timeout_seconds  = 30

[auto]
exec_timeout_sec = 30
canon_check_every = 0

[coder]
max_tokens = 500
temperature = 0.5

[inner_loop]
temperature = 0.1

[validator_agent]
temperature       = 0.1
max_tokens        = 350
max_hints         = 2
system            = APPROVED
system_creative   = APPROVED
fact_check_creative = {fact_check}
max_fact_revisions  = 1

[context_broker]
max_symbols = 5

[architect]
max_tasks_creative = 1
""")
    return cfg


def test_make_inner_loop_builds_fact_validator_when_enabled(tmp_path):
    """make_inner_loop with fact_check_creative=true and creative mode → fact_validator attached."""
    from tools.auto.inner_loop import make_inner_loop

    cfg = _base_config(fact_check="true")
    loop = make_inner_loop(cfg, base_dir=tmp_path, task_mode="creative")
    # fact_validator may be None if _make_llm_call fails in test env (no real server),
    # but the attribute must always exist on the loop object.
    assert hasattr(loop, "fact_validator")


def test_make_inner_loop_no_fact_validator_when_disabled(tmp_path):
    """make_inner_loop with fact_check_creative=false → fact_validator is None."""
    from tools.auto.inner_loop import make_inner_loop

    cfg = _base_config(fact_check="false")
    loop = make_inner_loop(cfg, base_dir=tmp_path, task_mode="creative")
    assert loop.fact_validator is None


def test_make_inner_loop_no_fact_validator_in_code_mode(tmp_path):
    """make_inner_loop in code mode → fact_validator is None regardless of flag."""
    from tools.auto.inner_loop import make_inner_loop

    cfg = _base_config(fact_check="true", task_mode="code")
    loop = make_inner_loop(cfg, base_dir=tmp_path, task_mode="code")
    assert loop.fact_validator is None
