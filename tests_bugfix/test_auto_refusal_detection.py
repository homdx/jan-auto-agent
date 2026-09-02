"""tests/test_auto_refusal_detection.py — validator robustness on empty/odd responses.

Regression suite for failure modes observed in production:

  * The validator model returns an empty body (network hiccup or silent
    refusal).  Before the guard: json.loads("") raised "Expecting value: line 1
    column 1 (char 0)" — the exact error seen in the crashed session.
    After the guard: a clear ValueError("validator model returned an empty
    response …") is raised and caught, logging a readable message.

  * A coder failure (of any kind) must keep retrying up to max_attempts; there
    is no phrase-based refusal special-casing — that heuristic was removed as
    brittle (English-only, easily bypassed). A genuine refusal simply fails to
    parse as JSON and is treated like any other failed attempt.

Test groups
-----------
  TestInnerLoopRetry       — coder failure retries to max_attempts (no bail-out)
  TestValidatorEmptyGuard  — LLMGate2Validator.approve handles "" / None / prose
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.coder import Coder
from tools.auto.inner_loop import (
    InnerLoop,
    LLMGate2Validator,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_coder() -> Coder:
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "true"},
        "api_local": {"base_url": "http://localhost:9999", "model": "x", "api_key": ""},
        "coder":     {"temperature": "0.2", "max_tokens": "1024"},
        "loop":      {"timeout_seconds": "60"},
    })
    return Coder(
        config=cfg, base_url="http://localhost:9999",
        api_key="", model="x", api_format="openai", verify_ssl=True,
    )


@dataclass
class _FakeCoderResult:
    succeeded: bool = True
    files_written: list = field(default_factory=lambda: ["mod.py"])
    error: str = ""
    missing_context: list = field(default_factory=list)
    context_satisfied: bool = True


@dataclass
class _FakeExecResult:
    passed: bool = True
    exit_code: int = 0
    stdout: str = "1 passed"
    stderr: str = ""
    traceback: str = ""
    timed_out: bool = False


_TASK = {
    "id": "T-REF-01",
    "title": "test task",
    "instruction": "add a docstring",
    "target_files": ["mod.py"],
    "acceptance_check": "echo ok",
    "cited_locations": [{"file": "mod.py", "symbol": "foo", "line_start": 1}],
}

# ─────────────────────────────────────────────────────────────────────────────
# 1. InnerLoop retry behaviour on coder failure
# ─────────────────────────────────────────────────────────────────────────────

class TestInnerLoopRetry:
    """A coder failure must keep looping up to max_attempts (no special-casing)."""

    def _make_stub_executor(self):
        class _Ex:
            def run(self, task):
                from types import SimpleNamespace
                return SimpleNamespace(passed=False, exit_code=1, stdout="",
                                       stderr="", traceback="stub", timed_out=False)
        return _Ex()

    def _make_stub_validator(self):
        class _V:
            last_missing_context = []
            def approve(self, *a, **kw):
                return False, "stub validator"
        return _V()

    def test_coder_failure_retries_to_max(self, tmp_path: Path) -> None:
        """Every coder failure (including a former [REFUSAL]) retries to max_attempts."""
        call_counts = [0]

        class _FailCoder:
            def generate(self, task, base_dir, prior_feedback=None,
                         prefetched_context=""):
                call_counts[0] += 1
                from types import SimpleNamespace
                return SimpleNamespace(
                    succeeded=False,
                    files_written=[],
                    error="JSON decode failed: foo",
                    missing_context=[],
                    context_satisfied=True,
                )

        loop = InnerLoop(
            coder=_FailCoder(),
            executor=self._make_stub_executor(),
            validator=self._make_stub_validator(),
            max_attempts=3,
        )
        loop.run_task(_TASK, tmp_path)
        assert call_counts[0] == 3, (
            f"expected 3 retries for a coder failure, got {call_counts[0]}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Validator empty-response and refusal guards
# ─────────────────────────────────────────────────────────────────────────────

class TestValidatorEmptyGuard:
    """LLMGate2Validator.approve must surface clear errors for empty / refused responses."""

    def _make_validator(self, tmp_path: Path) -> LLMGate2Validator:
        return LLMGate2Validator(
            base_url="http://x/v1", model="m",
            base_dir=str(tmp_path),
        )

    def _fake_coder_result(self, tmp_path: Path) -> object:
        (tmp_path / "mod.py").write_text("def foo(): pass\n", encoding="utf-8")
        return _FakeCoderResult()

    def test_empty_string_response_is_fail_closed(self, tmp_path: Path) -> None:
        """Empty response → (False, 'validator unavailable: …') not a crash."""
        v = self._make_validator(tmp_path)
        cr = self._fake_coder_result(tmp_path)
        with patch("tools.llm_stream.request_completion", return_value=""):
            approved, fb = v.approve(_TASK, _FakeExecResult(), cr)
        assert approved is False
        assert "validator unavailable" in fb

    def test_empty_response_message_is_descriptive(self, tmp_path: Path) -> None:
        """The error must mention 'empty response', not the cryptic char-0 message."""
        v = self._make_validator(tmp_path)
        cr = self._fake_coder_result(tmp_path)
        with patch("tools.llm_stream.request_completion", return_value=""):
            _, fb = v.approve(_TASK, _FakeExecResult(), cr)
        assert "empty" in fb.lower(), (
            f"expected 'empty' in feedback, got: {fb!r}"
        )
        # Must NOT surface the cryptic json error text
        assert "line 1 column 1" not in fb

    def test_none_response_is_fail_closed(self, tmp_path: Path) -> None:
        """None from request_completion is treated identically to empty string."""
        v = self._make_validator(tmp_path)
        cr = self._fake_coder_result(tmp_path)
        with patch("tools.llm_stream.request_completion", return_value=None):
            approved, fb = v.approve(_TASK, _FakeExecResult(), cr)
        assert approved is False
        assert "validator unavailable" in fb

    def test_whitespace_only_response_is_fail_closed(self, tmp_path: Path) -> None:
        v = self._make_validator(tmp_path)
        cr = self._fake_coder_result(tmp_path)
        with patch("tools.llm_stream.request_completion", return_value="   \n\t  "):
            approved, fb = v.approve(_TASK, _FakeExecResult(), cr)
        assert approved is False

    def test_validator_refusal_prose_is_fail_closed(self, tmp_path: Path) -> None:
        """If the validator model itself refuses (prose instead of JSON) → fail-closed."""
        v = self._make_validator(tmp_path)
        cr = self._fake_coder_result(tmp_path)
        refusal = "I cannot assist with evaluating this content."
        with patch("tools.llm_stream.request_completion", return_value=refusal):
            approved, fb = v.approve(_TASK, _FakeExecResult(), cr)
        assert approved is False
        assert "validator unavailable" in fb

    def test_valid_approval_still_works(self, tmp_path: Path) -> None:
        """The guards must not break the happy path."""
        v = self._make_validator(tmp_path)
        cr = self._fake_coder_result(tmp_path)
        with patch("tools.llm_stream.request_completion",
                   return_value='{"approved": true, "feedback": ""}'):
            approved, fb = v.approve(_TASK, _FakeExecResult(), cr)
        assert approved is True
        assert fb == ""

    def test_valid_rejection_still_works(self, tmp_path: Path) -> None:
        v = self._make_validator(tmp_path)
        cr = self._fake_coder_result(tmp_path)
        resp = '{"approved": false, "feedback": "missing docstring", "hints": []}'
        with patch("tools.llm_stream.request_completion", return_value=resp):
            approved, fb = v.approve(_TASK, _FakeExecResult(), cr)
        assert approved is False
        assert "missing docstring" in fb

    def test_network_error_still_fail_closed(self, tmp_path: Path) -> None:
        """Existing network-error handling must be unaffected by the new guards."""
        v = self._make_validator(tmp_path)
        cr = self._fake_coder_result(tmp_path)
        with patch("tools.llm_stream.request_completion",
                   side_effect=RuntimeError("connection refused")):
            approved, fb = v.approve(_TASK, _FakeExecResult(), cr)
        assert approved is False
        assert "validator unavailable" in fb


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
