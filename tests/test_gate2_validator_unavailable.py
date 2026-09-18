"""tests/test_gate2_validator_unavailable.py — RUN-7: "validator unavailable"
is not a rejection.

``LLMGate2Validator.approve`` is fail-closed by design: any transport or
parse error comes back as ``(False, "validator unavailable: …")``. Until
RUN-7 the inner loop could not tell that ``False`` from a real
``{"approved": false}``, so a provider outage was scored as a rejection —
the attempt was charged, the coder was told "attempt N: validator rejected",
the error text was fed back to the reviewer as its own prior critique, the
auto-tuner scored the validator prompt on it, and five in a row BLOCKED the
task with a knowledge note and a ticket for code nobody reviewed (AUTO-T6 on
../testtext2, HTTP 429 "monthly usage limit").

ACs (ticket 35):
  * every call unavailable → ``run_task`` ends ``unavailable=True``,
    ``attempts_used == 0``, no "validator rejected" line, coder called once
  * unavailable twice, then approved → passes on attempt 1; three Gate-2
    calls, one attempt
  * unavailable twice, then ``{"approved": false}`` → a real rejection on
    attempt 1, feedback line present, attempt 2 runs
  * outer loop: task back to ``todo``, no ``feedback_round_N.md``, no
    knowledge/ticket, round counter unchanged, offered again by the next run
  * ``metrics.json`` row says ``validator_status: unavailable``; the tuner's
    score is unchanged by it
  * ``_prior_validator_critique`` is untouched by an unavailable call

Fully offline: the reviewer is the real ``LLMGate2Validator`` over a stubbed
``request_completion``, or a fake object — no provider is ever contacted.
"""
from __future__ import annotations

import configparser
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.llm_stream as llm_stream
import tools.auto.inner_loop as inner_loop_mod
from tools.agent_trace import tracer
from tools.auto.auto_metrics import AutoMetricsStream
from tools.auto.controller import AutoController, RunLimits
from tools.auto.inner_loop import (
    Gate2Verdict, InnerLoop, InnerLoopResult, LLMGate2Validator, make_inner_loop,
)
from tools.auto.outer_loop import OuterLoop, OuterLoopResult
from tools.auto.state import StateStore, make_task, STATUS_BLOCKED, STATUS_TODO
from tools.metrics_collector import MetricsCollector, RunRecord
from tools.prompt_evaluator import PromptEvaluator


# ── fakes ────────────────────────────────────────────────────────────────────

@dataclass
class FakeCoderResult:
    succeeded: bool = True
    files_written: list = field(default_factory=lambda: ["f.py"])
    files_skipped: list = field(default_factory=list)
    error: str = ""
    raw_response: str = ""


@dataclass
class FakeExecResult:
    passed: bool = True
    exit_code: int = 0
    stdout: str = "ok"
    stderr: str = ""
    traceback: str = ""
    timed_out: bool = False


class FakeCoder:
    def __init__(self):
        self.calls = []                 # prior_feedback seen per call
    def generate(self, task, base_dir, prior_feedback=None, **kw):
        self.calls.append(list(prior_feedback or []))
        return FakeCoderResult()


class FakeExecutor:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.runs = 0
    def run(self, task):
        self.runs += 1
        return self._results.pop(0) if self._results else FakeExecResult()


class StubProvider:
    """``request_completion`` stand-in. Script items: ``"down"`` raises a
    transport error; a dict is returned as the model's JSON reply. Records
    every user message so a test can see which prior critique travelled."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.user_messages = []
    def __call__(self, url, headers, payload, timeout, api_format, ssl_context=None, **kw):
        self.calls += 1
        self.user_messages.append(payload["messages"][-1]["content"])
        item = self.script.pop(0) if self.script else {"approved": True}
        if item == "down":
            raise ConnectionError("HTTP 429 — you have reached your monthly usage limit")
        return json.dumps(item)


TASK = {"id": "AUTO-T1", "title": "t", "instruction": "x",
        "target_files": ["f.py"], "acceptance_check": "pytest -q"}
REJECT_1 = {"approved": False, "feedback": "C1: helper never called"}
REJECT_2 = {"approved": False, "feedback": "C2: still never called"}
APPROVE = {"approved": True}


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(inner_loop_mod.time, "sleep", lambda s: slept.append(s))
    return slept


def _loop(monkeypatch, script, *, max_attempts=5, executor=None, **kw):
    """A real InnerLoop over a real LLMGate2Validator whose HTTP call is *script*."""
    stub = StubProvider(script)
    monkeypatch.setattr(llm_stream, "request_completion", stub)
    validator = LLMGate2Validator(base_url="http://stub/v1", model="m", api_format="openai")
    coder = FakeCoder()
    loop = InnerLoop(coder, executor or FakeExecutor(), validator,
                     max_attempts=max_attempts, **kw)
    return loop, coder, stub


def _no_rejection_reached_anyone(result, coder):
    assert "validator rejected" not in (result.last_feedback or "")
    assert not any("validator rejected" in (rec.feedback or "") for rec in result.records)
    assert not any("validator rejected" in line for call in coder.calls for line in call)


# ── the outage is not a rejection ───────────────────────────────────────────

class TestUnavailableIsNotARejection:
    def test_every_call_unavailable_leaves_the_round_unreviewed(self, tmp_path, monkeypatch, no_sleep):
        loop, coder, stub = _loop(monkeypatch, ["down"] * 10)
        r = loop.run_task(TASK, tmp_path)
        assert r.unavailable is True and r.passed is False
        assert r.attempts_used == 0, "an attempt nobody reviewed is not charged"
        assert r.records == []
        assert r.last_feedback == ""
        assert len(coder.calls) == 1, "only the validator call is re-run, never the coder"
        assert stub.calls == 3, "one call + validator_unavailable_retries (2)"
        assert no_sleep == [60.0, 60.0], "the [collect] error_retry_wait_sec default between calls"
        _no_rejection_reached_anyone(r, coder)

    def test_unavailable_twice_then_approved_passes_on_attempt_one(self, tmp_path, monkeypatch, no_sleep):
        loop, coder, stub = _loop(monkeypatch, ["down", "down", APPROVE])
        r = loop.run_task(TASK, tmp_path)
        assert r.passed is True and r.unavailable is False
        assert r.attempts_used == 1
        assert stub.calls == 3 and len(coder.calls) == 1

    def test_unavailable_twice_then_rejected_is_a_real_rejection(self, tmp_path, monkeypatch, no_sleep):
        loop, coder, stub = _loop(monkeypatch, ["down", "down", REJECT_1, APPROVE])
        r = loop.run_task(TASK, tmp_path)
        assert r.passed is True and r.attempts_used == 2
        assert len(coder.calls) == 2, "the real rejection sends the coder back"
        assert any("attempt 1: validator rejected" in line and "C1" in line
                   for line in coder.calls[1])

    def test_zero_retries_is_one_call_and_still_no_charge(self, tmp_path, monkeypatch, no_sleep):
        loop, coder, stub = _loop(monkeypatch, ["down"] * 3, validator_unavailable_retries=0)
        r = loop.run_task(TASK, tmp_path)
        assert r.unavailable is True and r.attempts_used == 0
        assert stub.calls == 1 and no_sleep == []

    def test_attempts_that_reached_a_verdict_are_still_counted(self, tmp_path, monkeypatch, no_sleep):
        """attempt 1: exec fails (a verdict); attempt 2: the reviewer is down."""
        ex = FakeExecutor([FakeExecResult(passed=False, exit_code=1, traceback="boom"),
                           FakeExecResult()])
        loop, coder, stub = _loop(monkeypatch, ["down"] * 3, executor=ex)
        r = loop.run_task(TASK, tmp_path)
        assert r.unavailable is True
        assert r.attempts_used == 1
        assert [rec.attempt_num for rec in r.records] == [1]
        assert "exec failed" in r.records[0].feedback
        _no_rejection_reached_anyone(r, coder)

    def test_configured_wait_is_honoured(self, tmp_path, monkeypatch, no_sleep):
        loop, coder, stub = _loop(monkeypatch, ["down", "down", APPROVE],
                                  validator_unavailable_wait_sec=2.5)
        loop.run_task(TASK, tmp_path)
        assert no_sleep == [2.5, 2.5]

    def test_one_trace_decision_per_outage_with_the_call_count(self, tmp_path, monkeypatch, no_sleep):
        events = []
        monkeypatch.setattr(tracer, "event",
                            lambda source, target, kind, params=None, content=None, **kw:
                            events.append((source, content, dict(params or {}))))
        loop, coder, stub = _loop(monkeypatch, ["down"] * 3)
        loop.run_task(TASK, tmp_path)
        gate2 = [(c, p) for s, c, p in events if s == "gate2"]
        assert gate2 == [("UNAVAILABLE", {"task": "AUTO-T1", "attempt": 1,
                                          "stage": "gate2", "calls": 3})]


# ── the prior critique ──────────────────────────────────────────────────────

class TestPriorCritiqueUntouched:
    def test_outage_does_not_replace_the_prior_critique(self, tmp_path, monkeypatch, no_sleep):
        """attempt 1 rejected (C1); attempt 2: down, down, rejected (C2);
        attempt 3 approved. The two failed calls and the one that finally
        answered on attempt 2 must all see C1 — never the error text — and
        attempt 3 sees C2."""
        loop, coder, stub = _loop(monkeypatch, [REJECT_1, "down", "down", REJECT_2, APPROVE])
        r = loop.run_task(TASK, tmp_path)
        assert r.passed and r.attempts_used == 3
        msgs = stub.user_messages
        assert len(msgs) == 5
        assert "C1" not in msgs[0]
        for m in msgs[1:4]:
            assert "C1" in m and "unavailable" not in m
        assert "C2" in msgs[4] and "unavailable" not in msgs[4]


# ── the flag itself ─────────────────────────────────────────────────────────

class TestGate2VerdictFlag:
    def _validator(self, monkeypatch, script):
        monkeypatch.setattr(llm_stream, "request_completion", StubProvider(script))
        return LLMGate2Validator(base_url="http://stub/v1", model="m", api_format="openai")

    def test_default_is_false(self):
        assert Gate2Verdict(approved=False, reason="Reason: no").unavailable is False

    def test_transport_error_sets_it(self, tmp_path, monkeypatch):
        v = self._validator(monkeypatch, ["down"])
        verdict = v.approve_verdict(TASK, FakeExecResult(), FakeCoderResult(), base_dir=tmp_path)
        assert verdict.unavailable is True and verdict.approved is False
        assert verdict.reason.startswith("validator unavailable:")

    def test_unparseable_reply_sets_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(llm_stream, "request_completion", lambda *a, **k: "")
        v = LLMGate2Validator(base_url="http://stub/v1", model="m", api_format="openai")
        verdict = v.approve_verdict(TASK, FakeExecResult(), FakeCoderResult(), base_dir=tmp_path)
        assert verdict.unavailable is True and verdict.approved is False

    def test_a_real_rejection_is_not_unavailable(self, tmp_path, monkeypatch):
        v = self._validator(monkeypatch, [{"approved": False}])   # however terse
        verdict = v.approve_verdict(TASK, FakeExecResult(), FakeCoderResult(), base_dir=tmp_path)
        assert verdict.unavailable is False and verdict.approved is False

    def test_the_flag_resets_between_calls(self, tmp_path, monkeypatch):
        v = self._validator(monkeypatch, ["down", APPROVE])
        assert v.approve_verdict(TASK, FakeExecResult(), FakeCoderResult(), base_dir=tmp_path).unavailable
        verdict = v.approve_verdict(TASK, FakeExecResult(), FakeCoderResult(), base_dir=tmp_path)
        assert verdict.approved is True and verdict.unavailable is False

    def test_approve_tuple_contract_is_unchanged(self, tmp_path, monkeypatch):
        v = self._validator(monkeypatch, ["down"])
        approved, reason = v.approve(TASK, FakeExecResult(), FakeCoderResult(), base_dir=tmp_path)
        assert approved is False and reason.startswith("validator unavailable:")


# ── fail-open ───────────────────────────────────────────────────────────────

class _ApproveOnlyValidator:
    """A fake with today's two-tuple interface and no approve_verdict."""
    def __init__(self):
        self.calls = 0
    def approve(self, task, exec_result, coder_result, *, base_dir=None):
        self.calls += 1
        return False, "validator unavailable: stubbed"


class _RaisingValidator:
    def approve_verdict(self, task, exec_result, coder_result, *, base_dir=None, prior_critique=""):
        raise RuntimeError("programming error, not an outage")
    approve = approve_verdict


class TestFailOpen:
    def test_approve_only_validator_keeps_todays_behaviour(self, tmp_path, no_sleep):
        v = _ApproveOnlyValidator()
        r = InnerLoop(FakeCoder(), FakeExecutor(), v, max_attempts=3).run_task(TASK, tmp_path)
        assert r.unavailable is False and r.passed is False
        assert r.attempts_used == 3 and v.calls == 3
        assert "validator unavailable" in r.last_feedback

    def test_a_validator_that_raises_is_still_a_charged_validator_error(self, tmp_path, no_sleep):
        r = InnerLoop(FakeCoder(), FakeExecutor(), _RaisingValidator(), max_attempts=2).run_task(TASK, tmp_path)
        assert r.unavailable is False and r.attempts_used == 2
        assert "validator error" in r.last_feedback

    def test_prior_critique_is_detected_on_the_method_actually_called(self, tmp_path, no_sleep):
        # approve() takes prior_critique, approve_verdict() does not: the
        # loop calls approve_verdict(), so it must not pass the kwarg there.
        class V:
            def __init__(self):
                self.kwargs = []
            def approve(self, task, exec_result, coder_result, *, base_dir=None, prior_critique=""):
                return True, ""
            def approve_verdict(self, task, exec_result, coder_result, *, base_dir=None):
                self.kwargs.append(base_dir)
                return Gate2Verdict(True, "")
        v = V()
        r = InnerLoop(FakeCoder(), FakeExecutor(), v, max_attempts=2).run_task(TASK, tmp_path)
        assert r.passed is True and r.attempts_used == 1 and len(v.kwargs) == 1

    def test_malformed_config_degrades_to_the_defaults(self, tmp_path):
        cfg = configparser.ConfigParser()
        cfg.add_section("auto"); cfg.set("auto", "validator_unavailable_retries", "twice")
        cfg.add_section("collect"); cfg.set("collect", "error_retry_wait_sec", "soon")
        loop = make_inner_loop(cfg, tmp_path, coder=FakeCoder(), executor=FakeExecutor(),
                               validator=_ApproveOnlyValidator())
        assert loop._validator_unavailable_retries == 2
        assert loop._validator_unavailable_wait_sec == 60.0

    def test_negative_config_values_are_floored(self, tmp_path):
        cfg = configparser.ConfigParser()
        cfg.add_section("auto"); cfg.set("auto", "validator_unavailable_retries", "-3")
        cfg.add_section("collect"); cfg.set("collect", "error_retry_wait_sec", "-5")
        loop = make_inner_loop(cfg, tmp_path, coder=FakeCoder(), executor=FakeExecutor(),
                               validator=_ApproveOnlyValidator())
        assert loop._validator_unavailable_retries == 0
        assert loop._validator_unavailable_wait_sec == 0.0

    def test_direct_constructor_guards_bad_values(self, tmp_path):
        loop = InnerLoop(FakeCoder(), FakeExecutor(), _ApproveOnlyValidator(),
                         validator_unavailable_retries="many", validator_unavailable_wait_sec=None)
        assert loop._validator_unavailable_retries == 2
        assert loop._validator_unavailable_wait_sec == 60.0


# ── outer loop ──────────────────────────────────────────────────────────────

def _unavailable_result():
    return InnerLoopResult(task_id="AUTO-T1", passed=False, attempts_used=0,
                           last_feedback="", records=[], unavailable=True,
                           unavailable_reason="validator unavailable: HTTP 429")


def _failed_result(text):
    return InnerLoopResult(task_id="AUTO-T1", passed=False, attempts_used=5, last_feedback=text)


class FakeInnerLoop:
    def __init__(self, results):
        self._results = list(results)
        self.seen_prior = []
    def run_task(self, task, base_dir, prior_feedback=None, prior_implementations=None, **kw):
        self.seen_prior.append(list(prior_feedback or []))
        return self._results.pop(0)


def _state(tmp_path) -> StateStore:
    st = StateStore(tmp_path / ".agent")
    st.initialise("goal", tmp_path)
    st.upsert_task(make_task(id="AUTO-T1", title="t", instruction="x", target_files=["f.py"]))
    return st


class TestOuterLoopLeavesTodo:
    def test_fresh_task_goes_back_to_todo_with_nothing_written(self, tmp_path):
        st = _state(tmp_path)
        rewriter = MagicMock()
        res = OuterLoop(FakeInnerLoop([_unavailable_result()]), st, max_rounds=10,
                        task_rewriter=rewriter).run_task(TASK, tmp_path)
        assert res.unavailable is True and res.passed is False and res.exhausted is False
        assert res.rounds_used == 0 and res.feedback_files == []
        assert res.impl_versions_used == []
        assert "validator unavailable" in res.summary()
        t = st.get_task("AUTO-T1")
        assert t["status"] == STATUS_TODO
        assert t["round"] == 0 and t["impl_version"] == 1
        assert not list(st.task_dir("AUTO-T1").glob("feedback_round_*.md"))
        assert not list(st.task_dir("AUTO-T1").glob("rewrite_round_*.md"))
        rewriter.rewrite.assert_not_called()
        assert any(x["id"] == "AUTO-T1" for x in st.resume_info()["pending"])

    def test_resumed_task_keeps_its_completed_rounds_and_resumes_there(self, tmp_path):
        st = _state(tmp_path)
        inner = FakeInnerLoop([_failed_result("err A"), _failed_result("err B"), _unavailable_result()])
        res = OuterLoop(inner, st, max_rounds=10).run_task(TASK, tmp_path)
        assert res.unavailable is True and res.rounds_used == 2
        files = sorted(p.name for p in st.task_dir("AUTO-T1").glob("feedback_round_*.md"))
        assert files == ["feedback_round_1.md", "feedback_round_2.md"]
        t = st.get_task("AUTO-T1")
        assert t["status"] == STATUS_TODO and t["round"] == 2
        # the next run picks the task up at round 3 — the outage burned nothing
        again = FakeInnerLoop([InnerLoopResult(task_id="AUTO-T1", passed=True, attempts_used=1)])
        res2 = OuterLoop(again, st, max_rounds=10).run_task(TASK, tmp_path)
        assert res2.passed and res2.rounds_used == 3
        assert len(again.seen_prior[0]) == 2

    def test_a_result_without_the_flag_is_a_failed_round_as_before(self, tmp_path):
        st = _state(tmp_path)
        old_shape = SimpleNamespace(passed=False, attempts_used=5, last_feedback="still broken",
                                    records=[], context_satisfied=True)
        res = OuterLoop(FakeInnerLoop([old_shape] * 2), st, max_rounds=2).run_task(TASK, tmp_path)
        assert res.exhausted and not res.unavailable
        assert st.get_task("AUTO-T1")["status"] == STATUS_BLOCKED
        assert len(list(st.task_dir("AUTO-T1").glob("feedback_round_*.md"))) == 2


# ── controller ──────────────────────────────────────────────────────────────

def _controller(tmp_path):
    import time
    base = tmp_path / "repo"; base.mkdir()
    ctrl = AutoController.__new__(AutoController)
    ctrl.goal = "test"; ctrl.base_dir = base; ctrl.config_path = "agents.ini"
    ctrl.task_mode = "code"; ctrl.dry_run = False
    ctrl.agent_dir = base / ".agent"; ctrl.workspace_dir = ctrl.agent_dir / "workspace"
    ctrl._time_fn = time.monotonic; ctrl._start_time = time.monotonic
    ctrl.limits = RunLimits(max_runtime_sec=0, max_tasks_per_run=0)
    ctrl.state = StateStore(ctrl.agent_dir); ctrl.state.initialise("test", base)
    ctrl.state.upsert_task(make_task(id="AUTO-T1", title="t", instruction="x", target_files=["f.py"]))
    ctrl.git = MagicMock()
    ctrl.run_trace = MagicMock(); ctrl.progress_display = MagicMock()
    ctrl.metrics_stream = MagicMock(); ctrl.auto_tuner = MagicMock()
    ctrl.auto_tuner.maybe_tune.return_value = SimpleNamespace(promoted=False, new_prompt_score=0.0)
    return ctrl


class _ScriptedOuter:
    def __init__(self, result): self.result = result
    def run_task(self, task, base_dir, **kw): return self.result


def _run(ctrl, outer_result):
    with patch("tools.auto.outer_loop.make_outer_loop", return_value=_ScriptedOuter(outer_result)), \
         patch("tools.auto.commit_on_success.CommitOnSuccess"):
        return ctrl._run_task_loop()


class TestControllerUnavailablePath:
    def test_no_knowledge_no_ticket_honest_metric_residue_discarded(self, tmp_path):
        ctrl = _controller(tmp_path)
        result = OuterLoopResult("AUTO-T1", False, 0, False, [], [_unavailable_result()], [],
                                 unavailable=True)
        stop, done = _run(ctrl, result)
        assert stop is None and done == 0
        assert not (ctrl.agent_dir / "tasks" / "AUTO-T1" / "knowledge.md").exists()
        assert not list((ctrl.agent_dir / "tickets").glob("*"))
        assert ctrl.state.get_task("AUTO-T1")["status"] == STATUS_TODO
        ctrl.run_trace.log_task_blocked.assert_not_called()
        ctrl.git.discard_working_changes.assert_called_once()
        kwargs = ctrl.metrics_stream.record_gate2.call_args.kwargs
        assert kwargs["unavailable"] is True and kwargs["approved"] is False

    def test_exhausted_task_still_gets_its_ticket(self, tmp_path):
        ctrl = _controller(tmp_path)
        result = OuterLoopResult("AUTO-T1", False, 10, True, [], [_failed_result("still broken")], [])
        _run(ctrl, result)
        assert (ctrl.agent_dir / "tasks" / "AUTO-T1" / "knowledge.md").exists()
        assert ctrl.state.get_task("AUTO-T1")["status"] == STATUS_BLOCKED
        assert ctrl.metrics_stream.record_gate2.call_args.kwargs["unavailable"] is False


# ── metrics and the tuner ───────────────────────────────────────────────────

def _rec(status, iterations):
    return RunRecord(timestamp="t", intent="i", prompt_version="v", iterations_used=iterations,
                     validator_status=status, validator_feedback="",
                     improvement_json_ok=None, elapsed_seconds=0.0)


class TestMetricsAndTuner:
    def test_row_says_unavailable(self, tmp_path):
        stream = AutoMetricsStream(tmp_path / ".agent")
        stream.record_gate2("AUTO-T1", approved=False, feedback="", attempts_used=0, unavailable=True)
        stream.record_gate2("AUTO-T2", approved=False, feedback="no", attempts_used=5)
        stream.record_gate2("AUTO-T3", approved=True, feedback="", attempts_used=1)
        rows = stream.collector.load_recent(5)
        assert [r.validator_status for r in rows] == ["unavailable", "rejected", "approved"]

    def test_score_is_unchanged_by_unavailable_rows(self, tmp_path):
        pe = PromptEvaluator(None, MetricsCollector(tmp_path / "m.json"), None, max_iter=3)
        base = [_rec("approved", 1), _rec("rejected", 3), _rec("approved", 2)]
        assert pe._score_from_records(base) == pe._score_from_records(base + [_rec("unavailable", 0)] * 4)

    def test_only_outages_is_no_baseline_not_a_zero_score(self, tmp_path):
        mc = MetricsCollector(tmp_path / "m.json")
        for _ in range(3):
            mc.record(_rec("unavailable", 0))
        pe = PromptEvaluator(MagicMock(), mc, None, max_iter=3)
        with patch("tools.prompt_evaluator._validate_candidate_placeholders", return_value=None):
            out = pe.evaluate("validator", "candidate prompt")
        assert out.promoted is False and "No baseline runs" in out.reason

    def test_projection_ignores_unavailable_rows_too(self, tmp_path):
        # _projected_score reads load_recent(5) itself: four outages padding
        # one real rejection must project exactly what the rejection alone
        # projects — not a window of four "0-iteration" rows.
        mc = MetricsCollector(tmp_path / "m.json")
        mc.record(_rec("rejected", 3))
        pe = PromptEvaluator(None, mc, None, max_iter=3)
        alone = pe._projected_score()
        for _ in range(4):
            mc.record(_rec("unavailable", 0))
        assert pe._projected_score() == alone


# ── scripts/trace_round_snapshot.py: the g2 columns ─────────────────────────

def _snapshot_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "trace_round_snapshot", PROJECT_ROOT / "scripts" / "trace_round_snapshot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_trace(base, decisions):
    agent = base / ".agent"
    agent.mkdir(parents=True, exist_ok=True)
    with open(agent / "trace_r1.jsonl", "w", encoding="utf-8") as fh:
        rows = [{"kind": "run_start", "params": {"goal": "g"}, "content": ""}]
        rows += [{"kind": "decision", "content": c,
                  "params": {"task": "AUTO-T1", "attempt": 1, "stage": "gate2", **p}}
                 for c, p in decisions]
        for i, r in enumerate(rows):
            r.update({"seq": i, "ts": "2026-09-16T00:00:00+00:00", "run_id": "r1",
                      "source": "gate2", "target": "inner_loop"})
            fh.write(json.dumps(r) + "\n")


class TestSnapshotColumns:
    def test_read_run_counts_every_gate2_status_separately(self, tmp_path):
        _write_trace(tmp_path, [("UNAVAILABLE", {"calls": 3}), ("REJECTED", {}),
                                ("REJECTED", {}), ("ERROR", {"error": "boom"})])
        snap = _snapshot_module().read_run(tmp_path)
        assert snap["gate2"] == {"UNAVAILABLE": 1, "REJECTED": 2, "ERROR": 1}

    def test_main_prints_the_three_g2_columns_and_totals(self, tmp_path, monkeypatch, capsys):
        _write_trace(tmp_path / "r1", [("UNAVAILABLE", {"calls": 3}), ("REJECTED", {})])
        monkeypatch.setattr(sys, "argv", ["trace_round_snapshot.py", str(tmp_path / "r1")])
        _snapshot_module().main()
        out = capsys.readouterr().out
        hdr, _, row, _, total = [ln for ln in out.splitlines() if ln.strip()][:5]
        for col, want in (("g2 rej", "1"), ("g2 err", "0"), ("g2 unavail", "1")):
            end = hdr.index(col) + len(col)
            assert row[:end].split()[-1] == want and total[:end].split()[-1] == want

    def test_a_pre_run7_trace_prints_zero_unavailable(self, tmp_path):
        _write_trace(tmp_path, [("REJECTED", {})])
        snap = _snapshot_module().read_run(tmp_path)
        assert snap["gate2"].get("UNAVAILABLE", 0) == 0 and snap["gate2"]["REJECTED"] == 1
