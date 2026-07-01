"""tests/test_cr27_stage_trace.py — AUTO-CR-27 acceptance tests.

Before this CR, InnerLoop.run_task() had zero trace footprint: every Gate-2 /
Gate-3 decision (coder, executor, the LLM quality validator, canon, fact,
prosody, continuity) was invisible in trace_<run_id>.jsonl. analyze_logs.py
therefore had no way to report per-stage retry counts or quality-gate
outcomes for either task mode, and creative mode's extra gates were entirely
unobservable.

Covers:
  * coder / executor / gate2 reject and error decisions are traced
  * canon / fact / prosody / continuity reject and accepted-at-cap decisions
    are traced, each tagged with its own ``stage`` name
  * a final overall APPROVED decision is traced on success
  * a final overall EXHAUSTED decision is traced when all attempts are used
  * tracing is best-effort: it never changes InnerLoop's return value
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.agent_trace import tracer
from tools.auto.canon_validator import CanonResult
from tools.auto.continuity_validator import ContinuityVerdict
from tools.auto.fact_validator import FactVerdict
from tools.auto.inner_loop import InnerLoop
from tools.auto.prosody import ProsodyVerdict


# ── Tracer harness ────────────────────────────────────────────────────────────

@pytest.fixture()
def traced(tmp_path):
    """Enable the global tracer for the duration of one test, writing to a
    private file; always restore the disabled state afterwards so later
    tests in the same process never see a stray enabled tracer."""
    trace_path = tmp_path / "trace_test.jsonl"
    tracer.configure(enabled=True, path=str(trace_path))
    tracer._run_id = "testrun0001"
    try:
        yield trace_path
    finally:
        tracer.configure(enabled=False)


def _read_events(trace_path: Path) -> list[dict]:
    if not trace_path.exists():
        return []
    return [json.loads(l) for l in trace_path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _decisions(events: list[dict], stage: str | None = None) -> list[dict]:
    out = [e for e in events if e.get("kind") == "decision" and e.get("target") == "inner_loop"]
    if stage is not None:
        out = [e for e in out if e.get("params", {}).get("stage") == stage]
    return out


# ── Fakes ──────────────────────────────────────────────────────────────────────

class _WritingCoder:
    def __init__(self, results=None, text="chapter text"):
        self._results = list(results or [])
        self._text = text
        self.calls = 0

    def generate(self, task, base_dir, prior_feedback=None, prefetched_context=""):
        self.calls += 1
        if self._results:
            return self._results.pop(0)
        target = (task.get("target_files") or ["chapter_01.md"])[0]
        (Path(base_dir) / target).write_text(self._text, encoding="utf-8")
        return SimpleNamespace(succeeded=True, files_written=[target],
                                missing_context=[], context_satisfied=True, error="")


class _OkExecutor:
    def run(self, task):
        return SimpleNamespace(passed=True, exit_code=0, stdout="", stderr="", traceback="")


class _FailingExecutor:
    def run(self, task):
        return SimpleNamespace(passed=False, exit_code=1, stdout="", stderr="boom", traceback="")


class _OkValidator:
    last_missing_context: list = []

    def approve(self, task, exec_result, coder_result, base_dir=None):
        return True, ""


class _RejectingValidator:
    last_missing_context: list = []

    def approve(self, task, exec_result, coder_result, base_dir=None):
        return False, "incomplete"


TASK_CODE = {"id": "AUTO-T1", "target_files": ["f.py"]}
TASK_CREATIVE = {"id": "AUTO-T2", "target_files": ["chapter_01.md"], "goal": "g"}


# ── Coder / executor / gate2 (apply to every task mode) ──────────────────────

def test_coder_rejected_is_traced(traced):
    coder = _WritingCoder(results=[
        SimpleNamespace(succeeded=False, files_written=[], missing_context=[],
                        context_satisfied=True, error="bad json"),
    ])
    loop = InnerLoop(coder, _OkExecutor(), _OkValidator(), max_attempts=2)
    result = loop.run_task(TASK_CODE, Path(tempfile.mkdtemp()))
    assert result.passed is True
    events = _read_events(traced)
    coder_events = _decisions(events, "coder")
    assert len(coder_events) == 1
    assert coder_events[0]["content"] == "REJECTED"
    assert coder_events[0]["params"]["task"] == "AUTO-T1"


def test_executor_rejected_is_traced(traced):
    loop = InnerLoop(_WritingCoder(), _FailingExecutor(), _OkValidator(), max_attempts=1)
    result = loop.run_task(TASK_CODE, Path(tempfile.mkdtemp()))
    assert result.passed is False
    events = _read_events(traced)
    ex_events = _decisions(events, "executor")
    assert len(ex_events) == 1
    assert ex_events[0]["content"] == "REJECTED"
    assert ex_events[0]["params"]["exit_code"] == "1"  # tracer stringifies params


def test_gate2_rejected_then_overall_approved_is_traced(traced):
    coder = _WritingCoder()
    loop = InnerLoop(coder, _OkExecutor(),
                     SimpleNamespace_validator := _SequencedValidator([(False, "no"), (True, "")]),
                     max_attempts=3)
    result = loop.run_task(TASK_CODE, Path(tempfile.mkdtemp()))
    assert result.passed is True and result.attempts_used == 2
    events = _read_events(traced)
    gate2_events = _decisions(events, "gate2")
    overall_events = _decisions(events, "overall")
    assert [e["content"] for e in gate2_events] == ["REJECTED"]
    assert [e["content"] for e in overall_events] == ["APPROVED"]
    assert overall_events[0]["params"]["attempt"] == "2"


class _SequencedValidator:
    last_missing_context: list = []

    def __init__(self, verdicts):
        self._v = list(verdicts)

    def approve(self, task, exec_result, coder_result, base_dir=None):
        return self._v.pop(0) if self._v else (True, "")


def test_overall_exhausted_is_traced(traced):
    loop = InnerLoop(_WritingCoder(), _OkExecutor(), _RejectingValidator(), max_attempts=2)
    result = loop.run_task(TASK_CODE, Path(tempfile.mkdtemp()))
    assert result.passed is False
    events = _read_events(traced)
    overall_events = _decisions(events, "overall")
    assert [e["content"] for e in overall_events] == ["EXHAUSTED"]


# ── Creative-only gates: canon / fact / prosody / continuity ──────────────────

class _AlwaysConflictCanon:
    max_canon_revisions = 1

    def should_check(self, chapter_file):
        return True

    def check(self, text, chapter_file, base_dir=None):
        r = CanonResult(checked=True)
        r.conflicts.append("conflict")
        return r


def test_canon_reject_then_accept_at_cap_is_traced(traced):
    loop = InnerLoop(
        _WritingCoder(), _OkExecutor(), _OkValidator(),
        max_attempts=3, canon_validator=_AlwaysConflictCanon(), task_mode="creative",
    )
    result = loop.run_task(TASK_CREATIVE, Path(tempfile.mkdtemp()))
    assert result.passed is True
    events = _read_events(traced)
    canon_events = _decisions(events, "canon")
    assert [e["content"] for e in canon_events] == ["REJECTED", "ACCEPTED_AT_CAP"]
    assert canon_events[0]["params"]["cap"] == "1"


class _RevisingFact:
    max_fact_revisions = 1

    def __init__(self):
        self.n = 0

    def check(self, task, text):
        self.n += 1
        if self.n == 1:
            return FactVerdict(approved=False, reason="contradiction", unparseable=False)
        return FactVerdict(approved=True, reason="", unparseable=False)


def test_fact_reject_then_approve_is_traced(traced):
    loop = InnerLoop(
        _WritingCoder(), _OkExecutor(), _OkValidator(),
        max_attempts=3, fact_validator=_RevisingFact(), task_mode="creative",
    )
    result = loop.run_task(TASK_CREATIVE, Path(tempfile.mkdtemp()))
    assert result.passed is True
    events = _read_events(traced)
    fact_events = _decisions(events, "fact")
    assert [e["content"] for e in fact_events] == ["REJECTED"]
    overall_events = _decisions(events, "overall")
    assert overall_events[-1]["content"] == "APPROVED"


class _AlwaysRevisingProsody:
    max_prosody_revisions = 1

    def check(self, task, text):
        return ProsodyVerdict(approved=False, reason="meter")


def test_prosody_reject_then_accept_at_cap_is_traced(traced):
    loop = InnerLoop(
        _WritingCoder(), _OkExecutor(), _OkValidator(),
        max_attempts=3, prosody_validator=_AlwaysRevisingProsody(), task_mode="creative",
    )
    result = loop.run_task(TASK_CREATIVE, Path(tempfile.mkdtemp()))
    assert result.passed is True
    events = _read_events(traced)
    prosody_events = _decisions(events, "prosody")
    assert [e["content"] for e in prosody_events] == ["REJECTED", "ACCEPTED_AT_CAP"]


class _AlwaysRevisingContinuity:
    max_continuity_revisions = 1

    def check(self, known_facts, text):
        return ContinuityVerdict(approved=False, reason="contradiction")


def test_continuity_reject_then_accept_at_cap_is_traced(traced, monkeypatch):
    import tools.auto.continuity_validator as cvmod
    monkeypatch.setattr(cvmod, "read_story_bible", lambda base_dir: "")
    monkeypatch.setattr(cvmod, "find_previous_chapter_text", lambda f, base_dir: "")

    loop = InnerLoop(
        _WritingCoder(), _OkExecutor(), _OkValidator(),
        max_attempts=3, continuity_validator=_AlwaysRevisingContinuity(), task_mode="creative",
    )
    result = loop.run_task(TASK_CREATIVE, Path(tempfile.mkdtemp()))
    assert result.passed is True
    events = _read_events(traced)
    continuity_events = _decisions(events, "continuity")
    assert [e["content"] for e in continuity_events] == ["REJECTED", "ACCEPTED_AT_CAP"]


def test_code_mode_emits_no_creative_stage_events(traced):
    """Regression: code-mode tasks must never emit canon/fact/prosody/continuity
    stage events, even if a validator instance is (incorrectly) attached."""
    loop = InnerLoop(
        _WritingCoder(), _OkExecutor(), _OkValidator(),
        max_attempts=1, canon_validator=_AlwaysConflictCanon(), task_mode="code",
    )
    result = loop.run_task(TASK_CODE, Path(tempfile.mkdtemp()))
    assert result.passed is True
    events = _read_events(traced)
    assert _decisions(events, "canon") == []


def test_tracing_disabled_by_default_does_not_write_file(tmp_path):
    """When the tracer was never configured(enabled=True), InnerLoop must run
    identically and write nothing — tracing is purely additive."""
    tracer.configure(enabled=False)
    loop = InnerLoop(_WritingCoder(), _OkExecutor(), _OkValidator(), max_attempts=1)
    result = loop.run_task(TASK_CODE, tmp_path)
    assert result.passed is True
    assert not (tmp_path / "trace_test.jsonl").exists()
