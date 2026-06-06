"""tests/test_context_broker_pull_model.py

Regression coverage for the ContextBroker prefetch flow (SCTX architecture).

After SCTX-3 the coder no longer emits a separate ``context_request`` signal:
it probes and fetches its own missing context inside ``generate()`` (Signal A),
exposing the outcome via ``CoderResult.context_satisfied``.  The ContextBroker
is still used on the *validator* side — when the Gate-2 validator rejects an
attempt and reports ``missing_context``, the inner loop resolves those symbols
and feeds them into the NEXT attempt's prompt.

Covered here:
  * ContextBroker resolves requested symbols from target files / project.
  * CoderResult.missing_context field contract (retained; default empty).
  * InnerLoop feeds validator-requested context into the NEXT attempt.
  * InnerLoopResult.context_satisfied tracks the coder's reported gap.
  * outer_loop skips the TaskRewriter when context_satisfied is False.
"""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.context_broker import ContextBroker
from tools.auto.coder import CoderResult
from tools.auto.inner_loop import InnerLoop


# ───────────────────────── ContextBroker ─────────────────────────

def test_broker_resolves_symbol_from_target_file():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "helper.py").write_text(
            "def my_helper(x):\n    return x + 1\n\nclass Other:\n    pass\n")
        out = ContextBroker().fetch(["my_helper"], ["helper.py"], d)
        assert "my_helper" in out
        assert "PREFETCHED CONTEXT" in out


def test_broker_returns_empty_for_unresolvable():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "helper.py").write_text("def a(): ...\n")
        assert ContextBroker().fetch(["does_not_exist"], ["helper.py"], d) == ""


# ───────────────────────── CoderResult contract ──────────────────

def test_coder_result_missing_context_field_retained():
    # The field is kept on the dataclass (default empty) for the validator
    # side-channel / future use; the coder no longer populates it itself.
    assert CoderResult().missing_context == []
    assert CoderResult(missing_context=["Foo"]).missing_context == ["Foo"]


# ───────────────────────── InnerLoop (validator-side prefetch) ────

class _Exec:
    def run(self, task):
        return SimpleNamespace(passed=True, exit_code=0, stdout="ok",
                               stderr="", traceback="", command="")


class _OkCoder:
    """Always writes the file; records the prefetched_context per attempt."""
    def __init__(self):
        self.calls = []
    def generate(self, task, base_dir, prior_feedback=None, prefetched_context="", **kw):
        self.calls.append(prefetched_context)
        return CoderResult(task_id="T", files_written=["helper.py"])


class _RejectThenApprove:
    """Rejects attempt 1 reporting a missing symbol, approves attempt 2."""
    def __init__(self):
        self.n = 0
        self.last_missing_context: list = []
    def approve(self, task, exec_result, coder_result):
        self.n += 1
        if self.n == 1:
            self.last_missing_context = ["my_helper"]
            return False, "need to see my_helper"
        self.last_missing_context = []
        return True, ""


def test_inner_loop_prefetches_validator_requested_context_into_next_attempt():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "helper.py").write_text("def my_helper(x):\n    return x + 1\n")
        coder = _OkCoder()
        loop = InnerLoop(coder, _Exec(), _RejectThenApprove(), max_attempts=3)
        res = loop.run_task({"id": "T", "target_files": ["helper.py"]}, d)
        assert res.passed is True
        # attempt 1 got no prefetch; attempt 2 received the validator-requested block
        assert coder.calls[0] == ""
        assert "my_helper" in coder.calls[1]
        assert "PREFETCHED CONTEXT" in coder.calls[1]
        assert res.context_satisfied is True


# ───────────────────────── context_satisfied (Signal A) ──────────

class _CoderReportsGap:
    """SCTX: coder probed but still could not resolve a needed symbol."""
    def generate(self, task, base_dir, prior_feedback=None, prefetched_context="", **kw):
        return CoderResult(task_id="T", files_written=[], error="need ctx",
                           context_satisfied=False)


def test_context_satisfied_false_when_coder_reports_gap():
    with tempfile.TemporaryDirectory() as d:
        loop = InnerLoop(_CoderReportsGap(), _Exec(),
                         _RejectThenApprove(), max_attempts=2)
        res = loop.run_task({"id": "T", "target_files": []}, Path(d))
        assert res.passed is False
        assert res.context_satisfied is False


# ───────────────────────── outer_loop gate ───────────────────────

def test_outer_loop_gate_uses_context_satisfied():
    src = (PROJECT_ROOT / "tools" / "auto" / "outer_loop.py").read_text()
    assert 'getattr(res, "context_satisfied", True)' in src


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
