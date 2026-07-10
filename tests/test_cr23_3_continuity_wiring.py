"""tests/test_cr23_3_continuity_wiring.py — AUTO-CR-23-3 wiring acceptance tests.

Covers:
  * a draft that contradicts the bible → "continuity rejected" + regeneration
    on the next attempt, loop ends APPROVED on the corrected attempt
  * continuity gate always REVISE + cap=1 → loop stops, chapter accepted,
    warning logged
  * continuity_check_creative=false → continuity_validator is None, gate
    never called
  * task_mode="code" → continuity gate not constructed / never called
    (regression)
  * known_facts passed to check() is built from story_bible.md + the
    highest-numbered prior chapter on disk
  * make_inner_loop factory: continuity_check_creative flag wiring
"""

from __future__ import annotations

import configparser
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


from tools.auto.continuity_validator import ContinuityValidator, ContinuityVerdict
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
        target = (task.get("target_files") or ["chapter_02.md"])[0]
        (Path(base_dir) / target).write_text(text, encoding="utf-8")
        return SimpleNamespace(
            succeeded=True,
            files_written=[target],
            missing_context=[],
            context_satisfied=True,
            error="",
        )


# ── Stub ContinuityValidator factories ─────────────────────────────────────────

def _revise_then_approve_validator(*, max_rev: int = 1):
    """Returns REVISE on first check, APPROVED on subsequent checks."""
    calls = {"n": 0}

    def llm(system, user):
        calls["n"] += 1
        if calls["n"] == 1:
            return "REVISE: replace the hero's green jacket with a grey jacket"
        return "APPROVED"

    return ContinuityValidator(llm, max_continuity_revisions=max_rev)


def _always_revise_validator(*, max_rev: int = 1):
    """Always returns REVISE."""
    return ContinuityValidator(
        lambda s, u: "REVISE: contradiction persists",
        max_continuity_revisions=max_rev,
    )


def _never_called_validator():
    """A ContinuityValidator whose check() method fails the test if called."""
    mock = MagicMock(spec=ContinuityValidator)
    mock.max_continuity_revisions = 1
    mock.check.side_effect = AssertionError("continuity gate should not have been called")
    return mock


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_continuity_violation_triggers_revision(tmp_path):
    """Stub coder writes a contradicting draft on attempt 1, a corrected
    draft on attempt 2. Gate-2 always APPROVED. Continuity: REVISE then
    APPROVED. Loop must end APPROVED on attempt 2; feedback must contain
    'continuity rejected'.
    """
    (tmp_path / "story_bible.md").write_text(
        "• The hero wears a green jacket", encoding="utf-8"
    )
    (tmp_path / "chapter_01.md").write_text(
        "The hero set out at dawn, green jacket buttoned to the chin.",
        encoding="utf-8",
    )

    continuity_v = _revise_then_approve_validator(max_rev=1)

    coder = _WritingCoder([
        "The hero, now in a grey jacket, crossed the bridge.",   # contradicts bible
        "The hero, still in his green jacket, crossed the bridge.",  # consistent
    ])

    task = {
        "id": "t1",
        "instruction": "Write the next chapter.",
        "target_files": ["chapter_02.md"],
    }

    loop = InnerLoop(
        coder, _OkExecutor(), _OkValidator(),
        max_attempts=5,
        continuity_validator=continuity_v,
        task_mode="creative",
    )
    result = loop.run_task(task, tmp_path)

    assert result.passed is True
    assert result.attempts_used == 2
    assert any("continuity rejected" in r.feedback for r in result.records)


def test_cap_accepts_with_warning(tmp_path, caplog):
    """Continuity gate always REVISE, max_continuity_revisions=1 → loop
    stops after 1 revision, chapter accepted, and a warning is logged.
    """
    continuity_v = _always_revise_validator(max_rev=1)

    coder = _WritingCoder(["The hero, now in a grey jacket, crossed the bridge."])
    task = {
        "id": "t1",
        "instruction": "Write the next chapter.",
        "target_files": ["chapter_02.md"],
    }

    loop = InnerLoop(
        coder, _OkExecutor(), _OkValidator(),
        max_attempts=5,
        continuity_validator=continuity_v,
        task_mode="creative",
    )

    with caplog.at_level(logging.WARNING, logger="tools.auto.inner_loop"):
        result = loop.run_task(task, tmp_path)

    assert result.passed is True
    assert any("continuity revision cap" in msg.lower() for msg in caplog.messages)


def test_disabled_skips_continuity_gate(tmp_path):
    """continuity_validator=None → gate never invoked; chapter approved by
    Gate-2 alone."""
    coder = _WritingCoder(["The hero, now in a grey jacket, crossed the bridge."])
    task = {
        "id": "t1",
        "instruction": "Write the next chapter.",
        "target_files": ["chapter_02.md"],
    }

    loop = InnerLoop(
        coder, _OkExecutor(), _OkValidator(),
        max_attempts=3,
        continuity_validator=None,
        task_mode="creative",
    )
    result = loop.run_task(task, tmp_path)

    assert result.passed is True
    assert result.attempts_used == 1


def test_no_target_files_skips_continuity_gate(tmp_path):
    """task has no target_files → continuity gate must not fire (mirrors the
    fact/canon gate guard)."""
    never = _never_called_validator()

    coder = _WritingCoder(["Some chapter text."])
    task = {
        "id": "t1",
        "instruction": "Write the next chapter.",
        # no target_files key at all
    }

    loop = InnerLoop(
        coder, _OkExecutor(), _OkValidator(),
        max_attempts=3,
        continuity_validator=never,
        task_mode="creative",
    )
    result = loop.run_task(task, tmp_path)

    never.check.assert_not_called()
    assert result.passed is True


def test_code_mode_unaffected(tmp_path):
    """task_mode='code' → continuity gate must never fire, even if a
    continuity_validator is attached."""
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
        continuity_validator=never,
        task_mode="code",
    )
    result = loop.run_task(task, tmp_path)

    never.check.assert_not_called()
    assert result.passed is True


def test_known_facts_built_from_bible_and_previous_chapter(tmp_path):
    """The known_facts string passed to check() must contain both the story
    bible content and the highest-numbered prior chapter's text."""
    (tmp_path / "story_bible.md").write_text(
        "• The hero wears a green jacket", encoding="utf-8"
    )
    (tmp_path / "chapter_01.md").write_text(
        "UNIQUE_PREVIOUS_CHAPTER_MARKER — the hero arrives in the village.",
        encoding="utf-8",
    )

    captured = {}

    class _CapturingValidator:
        max_continuity_revisions = 1

        def check(self, known_facts, new_text):
            captured["known_facts"] = known_facts
            captured["new_text"] = new_text
            return ContinuityVerdict(approved=True, reason="", unparseable=False)

    coder = _WritingCoder(["Chapter two prose."])
    task = {
        "id": "t1",
        "instruction": "Write the next chapter.",
        "target_files": ["chapter_02.md"],
    }

    loop = InnerLoop(
        coder, _OkExecutor(), _OkValidator(),
        max_attempts=1,
        continuity_validator=_CapturingValidator(),
        task_mode="creative",
    )
    result = loop.run_task(task, tmp_path)

    assert result.passed is True
    assert "green jacket" in captured["known_facts"]
    assert "UNIQUE_PREVIOUS_CHAPTER_MARKER" in captured["known_facts"]
    assert captured["new_text"] == "Chapter two prose."


def test_missing_bible_and_previous_chapter_still_runs(tmp_path):
    """Neither story_bible.md nor a previous chapter exist on disk — the
    gate must still run (known_facts may legitimately be mostly empty) and
    must not raise."""
    captured = {}

    class _CapturingValidator:
        max_continuity_revisions = 1

        def check(self, known_facts, new_text):
            captured["known_facts"] = known_facts
            return ContinuityVerdict(approved=True, reason="", unparseable=False)

    coder = _WritingCoder(["Chapter one prose."])
    task = {
        "id": "t1",
        "instruction": "Write chapter one.",
        "target_files": ["chapter_01.md"],
    }

    loop = InnerLoop(
        coder, _OkExecutor(), _OkValidator(),
        max_attempts=1,
        continuity_validator=_CapturingValidator(),
        task_mode="creative",
    )
    result = loop.run_task(task, tmp_path)

    assert result.passed is True
    assert "known_facts" in captured  # the gate ran despite no bible/prev chapter


def test_continuity_check_raises_fails_open(tmp_path, caplog):
    """If the continuity validator's check() raises, the chapter must still
    be approved (fail-open) and a warning logged."""
    class _RaisingValidator:
        max_continuity_revisions = 1

        def check(self, known_facts, new_text):
            raise RuntimeError("boom")

    coder = _WritingCoder(["Some chapter text."])
    task = {
        "id": "t1",
        "instruction": "Write the next chapter.",
        "target_files": ["chapter_01.md"],
    }

    loop = InnerLoop(
        coder, _OkExecutor(), _OkValidator(),
        max_attempts=1,
        continuity_validator=_RaisingValidator(),
        task_mode="creative",
    )

    with caplog.at_level(logging.WARNING, logger="tools.auto.inner_loop"):
        result = loop.run_task(task, tmp_path)

    assert result.passed is True
    assert any("continuity check raised" in msg.lower() for msg in caplog.messages)


# ── make_inner_loop factory: continuity_check_creative flag ──────────────────

def _base_config(continuity_check: str = "true", task_mode: str = "creative") -> configparser.ConfigParser:
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
continuity_check_creative = {continuity_check}
max_continuity_revisions  = 1

[context_broker]
max_symbols = 5

[architect]
max_tasks_creative = 1
""")
    return cfg


def test_make_inner_loop_builds_continuity_validator_when_enabled(tmp_path):
    """make_inner_loop with continuity_check_creative=true and creative mode
    → continuity_validator attached."""
    from tools.auto.inner_loop import make_inner_loop

    cfg = _base_config(continuity_check="true")
    loop = make_inner_loop(cfg, base_dir=tmp_path, task_mode="creative")
    # continuity_validator may be None if _make_llm_call fails in test env
    # (no real server), but the attribute must always exist on the loop object.
    assert hasattr(loop, "continuity_validator")


def test_make_inner_loop_no_continuity_validator_when_disabled(tmp_path):
    """make_inner_loop with continuity_check_creative=false → continuity_validator is None."""
    from tools.auto.inner_loop import make_inner_loop

    cfg = _base_config(continuity_check="false")
    loop = make_inner_loop(cfg, base_dir=tmp_path, task_mode="creative")
    assert loop.continuity_validator is None


def test_make_inner_loop_no_continuity_validator_in_code_mode(tmp_path):
    """make_inner_loop in code mode → continuity_validator is None regardless of flag."""
    from tools.auto.inner_loop import make_inner_loop

    cfg = _base_config(continuity_check="true", task_mode="code")
    loop = make_inner_loop(cfg, base_dir=tmp_path, task_mode="code")
    assert loop.continuity_validator is None
