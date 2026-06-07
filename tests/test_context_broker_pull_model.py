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


# ───────────────────────── PIPE-104 additions ────────────────────


def test_project_scan_result_is_cached():
    """Pass-2 hit is cached; deleting the source file doesn't break a second
    resolve; reset_cache() forces a real re-scan (which then misses)."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        dep = d / "dep.py"
        dep.write_text("def cached_fn():\n    return 42\n")
        broker = ContextBroker()
        # First resolve — hits Pass-2 (dep.py is not a target file)
        result1 = broker.resolve(["cached_fn"], [], d)
        assert "cached_fn" in result1
        # Delete the source; second resolve must still be served from cache
        dep.unlink()
        result2 = broker.resolve(["cached_fn"], [], d)
        assert "cached_fn" in result2, "expected cache hit after source deleted"
        # After reset the broker must re-scan — file is gone, so it misses
        broker.reset_cache()
        result3 = broker.resolve(["cached_fn"], [], d)
        assert "cached_fn" not in result3, "expected cache miss after reset_cache()"


def test_target_file_results_stay_fresh():
    """Pass-1 (target-file) hits are never cached, so rewriting the file is
    reflected in the very next resolve() call."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        target = d / "target.py"
        target.write_text("def fresh_fn():\n    return 'original'\n")
        broker = ContextBroker()
        result1 = broker.resolve(["fresh_fn"], ["target.py"], d)
        assert "original" in result1.get("fresh_fn", "")
        # Overwrite the target file (simulates what the coder does each attempt)
        target.write_text("def fresh_fn():\n    return 'rewritten'\n")
        result2 = broker.resolve(["fresh_fn"], ["target.py"], d)
        assert "rewritten" in result2.get("fresh_fn", ""), (
            "Pass-1 result should reflect new file content, not a stale cache"
        )


def test_cap_applies_only_to_uncached():
    """Cached symbols are free; the _max_symbols cap only limits the number of
    *new* (uncached) symbols resolved in one call."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # Write 6 symbols into a dependency file (not a target file → Pass-2)
        lines = []
        for i in range(6):
            lines.append(f"def sym_{i}():\n    return {i}\n")
        (d / "lib.py").write_text("\n".join(lines))

        broker = ContextBroker(max_symbols=3)

        # First call — only 3 new symbols resolved (cap applied)
        first = broker.resolve([f"sym_{i}" for i in range(3)], [], d)
        assert len(first) == 3

        # Second call — first 3 are cached (free) + 3 new ones requested
        second = broker.resolve([f"sym_{i}" for i in range(6)], [], d)
        # Must return all 6: 3 from cache (free) + 3 new (within cap)
        assert len(second) == 6, (
            f"expected 6 symbols (3 cached + 3 new), got {len(second)}"
        )


class _AccumulatingValidator:
    """Rejects twice, citing a different symbol each time, then approves."""
    def __init__(self):
        self.n = 0
        self.last_missing_context: list[str] = []

    def approve(self, task, exec_result, coder_result):
        self.n += 1
        if self.n == 1:
            self.last_missing_context = ["alpha"]
            return False, "need alpha"
        if self.n == 2:
            self.last_missing_context = ["beta"]
            return False, "need beta"
        self.last_missing_context = []
        return True, ""


def test_inner_loop_accumulates_validator_context_across_attempts():
    """Symbols requested by the validator accumulate: attempt 3's
    prefetched_context must contain BOTH 'alpha' (from attempt 1) and
    'beta' (from attempt 2)."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "lib.py").write_text(
            "def alpha():\n    return 'a'\n\ndef beta():\n    return 'b'\n"
        )
        coder = _OkCoder()
        loop = InnerLoop(coder, _Exec(), _AccumulatingValidator(), max_attempts=4)
        res = loop.run_task({"id": "T", "target_files": []}, d)
        assert res.passed is True
        # attempt 3 (index 2) must have received both symbols
        ctx_attempt3 = coder.calls[2]
        assert "alpha" in ctx_attempt3, "alpha (from attempt-1 rejection) missing on attempt 3"
        assert "beta" in ctx_attempt3, "beta (from attempt-2 rejection) missing on attempt 3"


class _CoderRequestsThenSatisfied:
    """Reports context_request=['foo'] on attempts 1 & 2 (via CoderResult.missing_context)."""
    def __init__(self):
        self.calls: list = []
        self.n = 0
    def generate(self, task, base_dir, prior_feedback=None, prefetched_context="", **kw):
        self.calls.append(prefetched_context)
        self.n += 1
        mc = ["foo"] if self.n <= 2 else []
        return CoderResult(task_id="T", files_written=["lib.py"], missing_context=mc)


class _ValBarThenNoMissing:
    """Attempt1: reject + missing ['bar']; Attempt2: reject + NO missing; Attempt3: approve."""
    def __init__(self):
        self.n = 0
        self.last_missing_context: list = []
    def approve(self, task, exec_result, coder_result):
        self.n += 1
        if self.n == 1:
            self.last_missing_context = ["bar"]
            return False, "need bar"
        if self.n == 2:
            self.last_missing_context = []
            return False, "still wrong"
        self.last_missing_context = []
        return True, ""


def test_coder_pull_does_not_clobber_validator_accumulated_context():
    """Regression: coder-side context_request must accumulate, not overwrite.

    Reviewer requests 'bar' on attempt 1; coder requests 'foo' on attempts 1-2;
    attempt 2's rejection carries no missing_context. Attempt 3 must still see
    BOTH 'bar' (reviewer) and 'foo' (coder) — the coder pull previously
    overwrote prefetched_context and dropped 'bar'.
    """
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "lib.py").write_text(
            "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
        )
        coder = _CoderRequestsThenSatisfied()
        loop = InnerLoop(coder, _Exec(), _ValBarThenNoMissing(), max_attempts=4)
        res = loop.run_task({"id": "T", "target_files": []}, d)
        assert res.passed is True
        ctx3 = coder.calls[2]
        assert "bar" in ctx3, "reviewer-accumulated 'bar' was clobbered by the coder pull"
        assert "foo" in ctx3, "coder-requested 'foo' missing from accumulated context"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
