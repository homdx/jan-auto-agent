"""tests/test_cr7_canon_validator.py — AUTO-CR-7 acceptance tests.

Covers:
  * periodic cadence (should_check)
  * contradiction detection → CONFLICT feedback
  * new fact allowed as NONE
  * revision bounded by max_canon_revisions (InnerLoop integration)
  * unparseable verdict fails open to INDIRECT (non-blocking)
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


from tools.auto.canon_validator import CanonValidator, CanonResult


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_validator(llm, base_dir, *, every=1, max_rev=1):
    return CanonValidator(
        llm,
        broker=None,
        base_dir=base_dir,
        canon_check_every=every,
        max_canon_revisions=max_rev,
        num_ctx=8192,
        max_tokens=512,
    )


def _write_synopsis(base: Path, body: str):
    (base / "synopsis.md").write_text(body, encoding="utf-8")


# ── Cadence ──────────────────────────────────────────────────────────────────

def test_runs_only_on_periodic_index(tmp_path):
    cv = _make_validator(lambda s, u: "DIRECT", tmp_path, every=3)
    # chapter 1 has nothing before it
    assert cv.should_check("chapter_01.md") is False
    # not a multiple of 3
    assert cv.should_check("chapter_02.md") is False
    assert cv.should_check("chapter_04.md") is False
    # multiples of 3 with predecessors
    assert cv.should_check("chapter_03.md") is True
    assert cv.should_check("chapter_06.md") is True


def test_every_one_checks_all_after_first(tmp_path):
    cv = _make_validator(lambda s, u: "DIRECT", tmp_path, every=1)
    assert cv.should_check("chapter_01.md") is False
    assert cv.should_check("chapter_02.md") is True
    assert cv.should_check("chapter_05.md") is True


# ── Conflict detection ───────────────────────────────────────────────────────

def test_detects_contradiction_emits_conflict_feedback(tmp_path):
    _write_synopsis(tmp_path, "## chapter_02.md\n- Captain Reyes died in the storm.\n")

    calls = {"n": 0}

    def llm(system, user):
        calls["n"] += 1
        # First call = claim extraction; rest = grounding verdicts.
        if "list its concrete factual claims" in system or "continuity checker" in system:
            return "Captain Reyes gives the order to advance."
        return "CONFLICT: canon says Reyes died in the storm, claim has him giving orders"

    cv = _make_validator(llm, tmp_path, every=1)
    res = cv.check("Captain Reyes barked the order.", "chapter_03.md")

    assert res.checked is True
    assert res.has_conflict is True
    assert "CANON CONFLICT" in res.feedback()
    assert "Reyes" in res.feedback()


def test_new_fact_allowed_as_none(tmp_path):
    _write_synopsis(tmp_path, "## chapter_02.md\n- The ship sailed north.\n")

    def llm(system, user):
        if "continuity checker" in system:
            return "A new character named Mira appears."
        return "NONE"

    cv = _make_validator(llm, tmp_path, every=1)
    res = cv.check("Mira stepped aboard.", "chapter_03.md")

    assert res.checked is True
    assert res.has_conflict is False
    assert res.none_facts == ["A new character named Mira appears."]
    assert res.feedback() == ""


def test_no_canon_skips_check(tmp_path):
    # No synopsis.md on disk → nothing to check against.
    cv = _make_validator(lambda s, u: "DIRECT", tmp_path, every=1)
    res = cv.check("Anything.", "chapter_03.md")
    assert res.checked is False
    assert res.has_conflict is False


# ── Fail-open ────────────────────────────────────────────────────────────────

def test_unparseable_verdict_fail_open_indirect(tmp_path):
    _write_synopsis(tmp_path, "## chapter_02.md\n- Some fact.\n")

    def llm(system, user):
        if "continuity checker" in system:
            return "Some claim about the world."
        return "well, it's hard to say, the vibes are off but maybe fine"

    cv = _make_validator(llm, tmp_path, every=1)
    res = cv.check("Some chapter text.", "chapter_03.md")
    # Unparseable grounding verdict must NOT raise and must NOT block.
    assert res.checked is True
    assert res.has_conflict is False


def test_llm_error_during_extraction_is_fail_open(tmp_path):
    _write_synopsis(tmp_path, "## chapter_02.md\n- Some fact.\n")

    def llm(system, user):
        raise RuntimeError("model offline")

    cv = _make_validator(llm, tmp_path, every=1)
    res = cv.check("text", "chapter_03.md")  # must not raise
    assert res.checked is False


# ── InnerLoop integration: revision is bounded ───────────────────────────────

class _OkCoder:
    def generate(self, task, base_dir, prior_feedback=None, prefetched_context=""):
        # Write the chapter file so the canon gate has something to read.
        target = (task.get("target_files") or ["chapter_03.md"])[0]
        (Path(base_dir) / target).write_text("Reyes gave the order.", encoding="utf-8")
        return SimpleNamespace(
            succeeded=True, files_written=[target], missing_context=[],
            context_satisfied=True, error="",
        )


class _OkExecutor:
    def run(self, task):
        return SimpleNamespace(passed=True, exit_code=0, stdout="", stderr="", traceback="")


class _OkValidator:
    last_missing_context: list = []

    def approve(self, task, exec_result, coder_result, base_dir=None):
        return True, ""


class _AlwaysConflictCanon:
    """Stub canon validator that always reports a conflict."""

    max_canon_revisions = 1

    def __init__(self):
        self.check_calls = 0

    def should_check(self, chapter_file):
        return True

    def check(self, text, chapter_file, base_dir=None):
        self.check_calls += 1
        r = CanonResult(checked=True)
        r.conflicts.append("Reyes is dead in canon but acting here.")
        return r


def test_revision_bounded_by_cap(tmp_path):
    from tools.auto.inner_loop import InnerLoop

    canon = _AlwaysConflictCanon()
    loop = InnerLoop(
        _OkCoder(), _OkExecutor(), _OkValidator(),
        max_attempts=5,
        canon_validator=canon,
        task_mode="creative",
    )
    task = {"id": "t1", "target_files": ["chapter_03.md"]}
    result = loop.run_task(task, tmp_path)

    # With cap=1: attempt 1 → canon reject (1 revision used); attempt 2 →
    # cap reached → accept-with-warning. The canon check fires once (it is
    # skipped once the cap is consumed), and the loop ends approved.
    assert canon.check_calls == 1
    assert result.passed is True
    assert result.attempts_used == 2


def test_no_canon_validator_is_noop(tmp_path):
    """task_mode=creative but no canon validator → behaves like a normal approve."""
    from tools.auto.inner_loop import InnerLoop

    loop = InnerLoop(
        _OkCoder(), _OkExecutor(), _OkValidator(),
        max_attempts=3,
        canon_validator=None,
        task_mode="creative",
    )
    task = {"id": "t1", "target_files": ["chapter_03.md"]}
    result = loop.run_task(task, tmp_path)
    assert result.passed is True
    assert result.attempts_used == 1


def test_code_mode_skips_canon_entirely(tmp_path):
    """In code mode the canon gate must never run, even if one is attached."""
    from tools.auto.inner_loop import InnerLoop

    canon = _AlwaysConflictCanon()
    loop = InnerLoop(
        _OkCoder(), _OkExecutor(), _OkValidator(),
        max_attempts=3,
        canon_validator=canon,
        task_mode="code",
    )
    task = {"id": "t1", "target_files": ["chapter_03.md"]}
    result = loop.run_task(task, tmp_path)
    assert canon.check_calls == 0
    assert result.passed is True
    assert result.attempts_used == 1
