"""tests/test_context_broker_pull_model.py

Regression coverage for the pull-model context flow:
  * ContextBroker resolves requested symbols from target files / project.
  * Coder.generate populates missing_context from a context_request.
  * InnerLoop feeds the broker's resolved context into the NEXT attempt and
    sets InnerLoopResult.context_satisfied correctly.
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
from tools.auto.inner_loop import InnerLoop, InnerLoopResult


# ───────────────────────── ContextBroker ─────────────────────────

def test_broker_resolves_symbol_from_target_file():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "helper.py").write_text(
            "def my_helper(x):\n    return x + 1\n\nclass Other:\n    pass\n")
        broker = ContextBroker()
        out = broker.fetch(["my_helper"], ["helper.py"], d)
        assert "my_helper" in out
        assert "PREFETCHED CONTEXT" in out


def test_broker_returns_empty_for_unresolvable():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "helper.py").write_text("def a(): ...\n")
        assert ContextBroker().fetch(["does_not_exist"], ["helper.py"], d) == ""


# ───────────────────────── Coder context_request ─────────────────

def test_coder_result_carries_missing_context():
    # the field exists and defaults empty; population is covered by the
    # coder unit checks — here we lock the dataclass contract.
    assert CoderResult().missing_context == []
    assert CoderResult(missing_context=["Foo"]).missing_context == ["Foo"]


# ───────────────────────── InnerLoop pull-model ──────────────────

class _Exec:
    def run(self, task):
        return SimpleNamespace(passed=True, exit_code=0, stdout="ok",
                               stderr="", traceback="", command="")


class _Validator:
    last_missing_context: list = []
    def approve(self, task, exec_result, coder_result):
        return True, ""


class _ScriptedCoder:
    """Attempt 1 requests context and writes nothing; attempt 2 succeeds.
    Records the prefetched_context received on each call."""
    def __init__(self):
        self.calls = []
    def generate(self, task, base_dir, prior_feedback=None, prefetched_context="", **kw):
        self.calls.append(prefetched_context)
        if len(self.calls) == 1:
            return CoderResult(task_id="T", files_written=[], error="need ctx",
                               missing_context=["my_helper"])
        return CoderResult(task_id="T", files_written=["helper.py"])


def test_inner_loop_prefetches_requested_context_into_next_attempt():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "helper.py").write_text("def my_helper(x):\n    return x + 1\n")
        coder = _ScriptedCoder()
        loop = InnerLoop(coder, _Exec(), _Validator(), max_attempts=3)
        res = loop.run_task({"id": "T", "target_files": ["helper.py"]}, d)
        assert res.passed is True
        # attempt 1 got no prefetched context; attempt 2 received the resolved block
        assert coder.calls[0] == ""
        assert "my_helper" in coder.calls[1]
        assert "PREFETCHED CONTEXT" in coder.calls[1]
        # it passed → context considered satisfied
        assert res.context_satisfied is True


class _AlwaysNeedsContext:
    def generate(self, task, base_dir, prior_feedback=None, prefetched_context="", **kw):
        return CoderResult(task_id="T", files_written=[], error="need ctx",
                           missing_context=["never_found_symbol"])


def test_context_satisfied_false_when_last_attempt_still_requests():
    with tempfile.TemporaryDirectory() as d:
        loop = InnerLoop(_AlwaysNeedsContext(), _Exec(), _Validator(), max_attempts=2)
        res = loop.run_task({"id": "T", "target_files": []}, Path(d))
        assert res.passed is False
        assert res.context_satisfied is False


# ───────────────────────── outer_loop gate ───────────────────────

def test_outer_loop_gate_uses_context_satisfied():
    # The rewrite condition must include `getattr(res, "context_satisfied", True)`.
    src = (PROJECT_ROOT / "tools" / "auto" / "outer_loop.py").read_text()
    assert 'getattr(res, "context_satisfied", True)' in src


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
