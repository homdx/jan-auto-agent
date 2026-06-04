"""tests/test_auto_c3.py — AUTO-C3: inner attempt loop + Gate 2.

ACs:
  * Passes on the first attempt when coder+exec+validator all succeed.
  * "Passes on attempt 2" — a task that fails once then succeeds stops at 2.
  * Gate 2 requires BOTH halves: exec exit 0 AND validator approval.
  * Validator is fail-closed: an infra/parse error never yields a false pass.
  * Exhaustion after max_attempts returns passed=False with last feedback.
  * Feedback from a failed attempt is fed into the next coder call, and
    prior_feedback (from an earlier round) seeds the first attempt.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.inner_loop import (
    InnerLoop, InnerLoopResult, LLMGate2Validator, make_inner_loop,
)


# ── fakes ────────────────────────────────────────────────────────────────────

@dataclass
class FakeCoderResult:
    succeeded: bool = True
    files_written: list = field(default_factory=lambda: ["f.py"])
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
    """Records the prior_feedback it receives on each call."""
    def __init__(self, results):
        self._results = list(results)
        self.calls = []                 # list of prior_feedback lists seen
    def generate(self, task, base_dir, prior_feedback=None):
        self.calls.append(list(prior_feedback or []))
        return self._results.pop(0) if self._results else FakeCoderResult()


class FakeExecutor:
    def __init__(self, results):
        self._results = list(results)
        self.runs = 0
    def run(self, task):
        self.runs += 1
        return self._results.pop(0) if self._results else FakeExecResult()


class FakeValidator:
    def __init__(self, verdicts):
        self._v = list(verdicts)        # list of (approved, feedback)
        self.calls = 0
    def approve(self, task, exec_result, coder_result):
        self.calls += 1
        return self._v.pop(0) if self._v else (True, "")


TASK = {"id": "AUTO-T1", "title": "fix it", "instruction": "do x",
        "target_files": ["f.py"], "acceptance_check": "pytest -q"}


# ── tests ────────────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_pass_first_attempt(self, tmp_path):
        loop = InnerLoop(FakeCoder([FakeCoderResult()]),
                         FakeExecutor([FakeExecResult()]),
                         FakeValidator([(True, "")]))
        r = loop.run_task(TASK, tmp_path)
        assert r.passed and r.attempts_used == 1
        assert r.records[-1].passed

    def test_pass_on_attempt_two(self, tmp_path):
        # attempt 1: exec fails; attempt 2: all good
        coder = FakeCoder([FakeCoderResult(), FakeCoderResult()])
        ex = FakeExecutor([FakeExecResult(passed=False, exit_code=1, traceback="boom"),
                           FakeExecResult()])
        loop = InnerLoop(coder, ex, FakeValidator([(True, "")]))
        r = loop.run_task(TASK, tmp_path)
        assert r.passed and r.attempts_used == 2
        # the 2nd coder call must have seen the failure feedback from attempt 1
        assert any("boom" in fb for fb in coder.calls[1])


class TestGate2:
    def test_exec_pass_but_validator_rejects_is_not_pass(self, tmp_path):
        loop = InnerLoop(FakeCoder([FakeCoderResult()] * 5),
                         FakeExecutor([FakeExecResult()] * 5),
                         FakeValidator([(False, "incomplete")] * 5))
        r = loop.run_task(TASK, tmp_path)
        assert r.passed is False
        assert r.attempts_used == 5
        assert "incomplete" in r.last_feedback

    def test_validator_not_called_when_exec_fails(self, tmp_path):
        val = FakeValidator([(True, "")])
        loop = InnerLoop(FakeCoder([FakeCoderResult()] * 5),
                         FakeExecutor([FakeExecResult(passed=False, exit_code=2)] * 5),
                         val)
        r = loop.run_task(TASK, tmp_path)
        assert r.passed is False
        assert val.calls == 0        # objective half failed → never judged

    def test_both_halves_required(self, tmp_path):
        # exec passes, validator approves only on attempt 3
        loop = InnerLoop(
            FakeCoder([FakeCoderResult()] * 3),
            FakeExecutor([FakeExecResult()] * 3),
            FakeValidator([(False, "a"), (False, "b"), (True, "")]),
        )
        r = loop.run_task(TASK, tmp_path)
        assert r.passed and r.attempts_used == 3


class TestCoderFailure:
    def test_coder_failure_feeds_back_and_continues(self, tmp_path):
        coder = FakeCoder([FakeCoderResult(succeeded=False, files_written=[], error="bad json"),
                           FakeCoderResult()])
        loop = InnerLoop(coder, FakeExecutor([FakeExecResult()]),
                         FakeValidator([(True, "")]))
        r = loop.run_task(TASK, tmp_path)
        assert r.passed and r.attempts_used == 2
        assert any("bad json" in fb for fb in coder.calls[1])


class TestExhaustion:
    def test_all_attempts_fail(self, tmp_path):
        loop = InnerLoop(FakeCoder([FakeCoderResult()] * 5),
                         FakeExecutor([FakeExecResult(passed=False, exit_code=1,
                                                      traceback="still broken")] * 5),
                         FakeValidator([]))
        r = loop.run_task(TASK, tmp_path)
        assert r.passed is False
        assert r.attempts_used == 5
        assert len(r.records) == 5
        assert "still broken" in r.last_feedback

    def test_respects_max_attempts(self, tmp_path):
        loop = InnerLoop(FakeCoder([FakeCoderResult()] * 9),
                         FakeExecutor([FakeExecResult(passed=False)] * 9),
                         FakeValidator([]), max_attempts=3)
        r = loop.run_task(TASK, tmp_path)
        assert r.attempts_used == 3


class TestPriorFeedback:
    def test_prior_feedback_seeds_first_attempt(self, tmp_path):
        coder = FakeCoder([FakeCoderResult()])
        loop = InnerLoop(coder, FakeExecutor([FakeExecResult()]),
                         FakeValidator([(True, "")]))
        loop.run_task(TASK, tmp_path, prior_feedback=["round-1 said: handle None"])
        assert "round-1 said: handle None" in coder.calls[0]


class TestFailClosedValidator:
    def test_llm_validator_fail_closed_on_network_error(self, monkeypatch):
        # request_completion raising → approve() must return (False, …), never True
        import tools.llm_stream as _ls
        monkeypatch.setattr(_ls, "request_completion",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("conn refused")))
        v = LLMGate2Validator(base_url="http://x/v1", model="m")
        approved, fb = v.approve(TASK, FakeExecResult(), FakeCoderResult())
        assert approved is False
        assert "validator unavailable" in fb

    def test_llm_validator_fail_closed_on_bad_json(self, monkeypatch):
        import tools.llm_stream as _ls
        monkeypatch.setattr(_ls, "request_completion", lambda **kw: "not json")
        v = LLMGate2Validator(base_url="http://x/v1", model="m")
        approved, _ = v.approve(TASK, FakeExecResult(), FakeCoderResult())
        assert approved is False

    def test_llm_validator_strips_think_and_fence(self, monkeypatch):
        import tools.llm_stream as _ls
        monkeypatch.setattr(
            _ls, "request_completion",
            lambda **kw: '<think>ok</think>\n```json\n{"approved": true, "feedback": ""}\n```')
        v = LLMGate2Validator(base_url="http://x/v1", model="m")
        approved, _ = v.approve(TASK, FakeExecResult(), FakeCoderResult())
        assert approved is True


class TestFactory:
    def test_make_inner_loop_with_injected_parts(self, tmp_path):
        import configparser
        cfg = configparser.ConfigParser()
        cfg["auto"] = {"max_attempts_per_task": "4"}
        loop = make_inner_loop(cfg, tmp_path,
                               coder=FakeCoder([FakeCoderResult()]),
                               executor=FakeExecutor([FakeExecResult()]),
                               validator=FakeValidator([(True, "")]))
        assert loop.max_attempts == 4
        r = loop.run_task(TASK, tmp_path)
        assert r.passed


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
