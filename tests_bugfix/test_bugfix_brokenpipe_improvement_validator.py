"""tests_bugfix/test_bugfix_brokenpipe_improvement_validator.py

BrokenPipeError regression tests for ImprovementAgent and ValidatorAgent.

Commit 8813aec ("fix(faq_agent): swallow BrokenPipeError in on_token streaming
callback") added _safe_stdout_write to tools/improvement_agent.py and
tools/validator_agent.py alongside faq_agent.py — all three agents stream LLM
tokens via sys.stdout.write and all three would have discarded the already-
received reply when the write raised BrokenPipeError (e.g. stdout piped to
``head``).

test_bugfix_faq_legacy_stdout_no_retry.py covers faq_agent.py, but there are
no corresponding tests for improvement_agent.py or validator_agent.py. This
file closes that gap with two test strategies:

  A. Unit: call _safe_stdout_write directly with a broken stdout — must not
     raise; the caller (on_token lambda) must see a normal return.

  B. Integration: break stdout *inside* the request_completion fake (after
     the agent's own print statements have already run, before on_token fires)
     to simulate a pipe that closes mid-stream. The agent must still return a
     valid reply and call the LLM exactly once.
"""

from __future__ import annotations

import io
import sys
from unittest.mock import patch

import pytest

import tools.improvement_agent as _ia_mod
import tools.validator_agent as _va_mod
from tools.improvement_agent import ImprovementAgent
from tools.validator_agent import ValidatorAgent


# ── helpers ──────────────────────────────────────────────────────────────────

class _BrokenStdout(io.StringIO):
    """Stdout stand-in whose write() always raises BrokenPipeError."""
    def write(self, s: str) -> int:
        raise BrokenPipeError("pipe closed")
    def flush(self) -> None:
        pass


class _BrokenStdoutAfter(io.StringIO):
    """Stdout stand-in whose write() raises BrokenPipeError from the Nth call
    on — the pipe closes mid-stream, after N-1 tokens were printed fine."""
    def __init__(self, break_at: int):
        super().__init__()
        self._writes = 0
        self._break_at = break_at

    def write(self, s: str) -> int:
        self._writes += 1
        if self._writes >= self._break_at:
            raise BrokenPipeError("pipe closed")
        return super().write(s)

    def flush(self) -> None:
        pass


def _split_tokens(text: str, width: int = 4) -> list[str]:
    """Chunk *text* into fixed-width tokens, as a streamed reply would
    arrive on the wire."""
    return [text[i:i + width] for i in range(0, len(text), width)]


def _pipe_breaks_during_stream(answer: str, call_counter: list):
    """
    Return a request_completion stub that simulates a pipe closing
    *mid-stream* (i.e. after the agent's own print statements have run,
    right as the first token is delivered to on_token).

    The real stdout is still healthy when ImprovementAgent/ValidatorAgent
    print their headers; we only swap to _BrokenStdout for the duration of
    the on_token callback.  This mirrors the real failure mode — the pipe
    closes while streaming, not before the call starts.
    """
    def _fake(*args, **kwargs):
        call_counter.append(1)
        on_token = kwargs.get("on_token")
        if on_token is not None:
            real = sys.stdout
            sys.stdout = _BrokenStdout()
            try:
                on_token(answer)    # BrokenPipeError fires here; _safe_stdout_write must catch it
            finally:
                sys.stdout = real   # restore so post-call prints still work
        return answer
    return _fake


def _pipe_breaks_on_nth_token(answer: str, call_counter: list, break_at: int):
    """
    Return a request_completion stub that streams *answer* as several
    tokens and makes the pipe close on the *break_at*-th one: tokens
    1..break_at-1 print fine, token break_at raises BrokenPipeError, and
    _safe_stdout_write must swallow it so the caller still sees the whole
    reply returned and completes normally. This pins the Nth-token failure
    mode, not only the single-token-holding-the-whole-answer one.
    """
    def _fake(*args, **kwargs):
        call_counter.append(1)
        on_token = kwargs.get("on_token")
        if on_token is not None:
            real = sys.stdout
            sys.stdout = _BrokenStdoutAfter(break_at)
            try:
                for tok in _split_tokens(answer):
                    on_token(tok)
            finally:
                sys.stdout = real   # restore so post-call prints still work
        return answer
    return _fake


# ── A: unit tests for _safe_stdout_write directly ────────────────────────────

class TestSafeStdoutWriteUnit:
    """_safe_stdout_write must swallow BrokenPipeError and OSError without
    raising — it is the inner guard that protects on_token callers."""

    def test_improvement_agent_swallows_broken_pipe(self):
        real = sys.stdout
        sys.stdout = _BrokenStdout()
        try:
            _ia_mod._safe_stdout_write("token")   # must not raise
        finally:
            sys.stdout = real

    def test_validator_agent_swallows_broken_pipe(self):
        real = sys.stdout
        sys.stdout = _BrokenStdout()
        try:
            _va_mod._safe_stdout_write("token")   # must not raise
        finally:
            sys.stdout = real

    def test_improvement_agent_swallows_oserror(self):
        """OSError is in the same except clause — also must not propagate."""
        class _OSErrStdout(io.StringIO):
            def write(self, s): raise OSError("errno 32")
            def flush(self): pass
        real = sys.stdout
        sys.stdout = _OSErrStdout()
        try:
            _ia_mod._safe_stdout_write("token")
        finally:
            sys.stdout = real

    def test_validator_agent_swallows_oserror(self):
        class _OSErrStdout(io.StringIO):
            def write(self, s): raise OSError("errno 32")
            def flush(self): pass
        real = sys.stdout
        sys.stdout = _OSErrStdout()
        try:
            _va_mod._safe_stdout_write("token")
        finally:
            sys.stdout = real


# ── B: integration — agent-level behaviour when pipe closes mid-stream ───────

class TestImprovementAgentBrokenPipe:
    """ImprovementAgent.process() with stream=True: if the pipe closes during
    token delivery, _safe_stdout_write must swallow the error so process()
    completes normally with call_count == 1."""

    def _agent(self) -> ImprovementAgent:
        agent = ImprovementAgent(
            model="test-model",
            base_url="http://localhost:99999/v1",
            api_key="test",
            timeout=5,
        )
        agent.stream = True
        agent.temperature = 0.3
        agent.max_tokens = 512
        return agent

    def test_broken_pipe_during_on_token_is_single_call(self):
        """Pipe closes mid-stream → LLM called exactly once, no retry."""
        agent = self._agent()
        calls: list = []
        with patch(
            "tools.improvement_agent.request_completion",
            side_effect=_pipe_breaks_during_stream('{"content": "ok"}', calls),
        ):
            agent.process("improve", {
                "code": "def f(): pass",
                "feedback": "add docstring",
                "goal": "test",
                "prompt": "improve it",
            })
        assert len(calls) == 1, (
            f"Expected exactly 1 LLM call (pipe-close must not trigger retry), "
            f"got {len(calls)}"
        )

    def test_broken_pipe_does_not_raise_to_caller(self):
        """process() must not propagate BrokenPipeError — the caller must
        receive a normal (possibly error-flagged) return, not an exception."""
        agent = self._agent()
        calls: list = []
        # Patch request_completion so process() doesn't try a real network call.
        with patch(
            "tools.improvement_agent.request_completion",
            side_effect=_pipe_breaks_during_stream('{"content": "result"}', calls),
        ):
            # Must not raise — any exception here is a test failure
            agent.process("improve", {
                "code": "x = 1",
                "feedback": "rename",
                "goal": "test",
                "prompt": "fix it",
            })

    @pytest.mark.parametrize("break_at", [1, 2, 3])
    def test_broken_pipe_on_nth_token_is_single_call(self, break_at):
        """T29: the pipe closing on the Nth token — not only on a single
        token holding the whole answer — is still swallowed by
        _safe_stdout_write, so the LLM is called exactly once and process()
        completes with the full reply."""
        agent = self._agent()
        calls: list = []
        answer = '{"content": "ok"}'
        with patch(
            "tools.improvement_agent.request_completion",
            side_effect=_pipe_breaks_on_nth_token(answer, calls, break_at=break_at),
        ):
            result = agent.process("improve", {
                "code": "def f(): pass",
                "feedback": "add docstring",
                "goal": "test",
                "prompt": "improve it",
            })
        assert len(calls) == 1, (
            f"Expected exactly 1 LLM call (pipe-close on token {break_at} "
            f"must not trigger retry), got {len(calls)}"
        )
        assert result == {"content": "ok"}, (
            f"process() must return the whole reply even when the pipe closed "
            f"on token {break_at}, got {result!r}"
        )


class TestValidatorAgentBrokenPipe:
    """ValidatorAgent.validate() with stream=True: same property."""

    def _agent(self) -> ValidatorAgent:
        return ValidatorAgent(
            model="test-model",
            base_url="http://localhost:99999/v1",
            api_key="test",
            timeout=5,
            stream=True,
        )

    def test_broken_pipe_during_on_token_is_single_call(self):
        """Pipe closes mid-stream → LLM called exactly once."""
        agent = self._agent()
        calls: list = []
        valid_reply = '{"status": "approved", "feedback": "looks good"}'
        with patch(
            "tools.validator_agent.request_completion",
            side_effect=_pipe_breaks_during_stream(valid_reply, calls),
        ):
            agent.validate({
                "code": "def f(): pass",
                "instruction": "add docstring",
                "iteration": 1,
            })
        assert len(calls) == 1, (
            f"Expected exactly 1 LLM call, got {len(calls)}"
        )

    def test_broken_pipe_validate_returns_dict(self):
        """validate() must return a dict even when the pipe is broken."""
        agent = self._agent()
        calls: list = []
        valid_reply = '{"status": "approved", "feedback": "ok"}'
        with patch(
            "tools.validator_agent.request_completion",
            side_effect=_pipe_breaks_during_stream(valid_reply, calls),
        ):
            result = agent.validate({
                "code": "x = 1",
                "instruction": "fix",
                "iteration": 1,
            })
        assert isinstance(result, dict), (
            f"validate() must return a dict, got {type(result)}"
        )

    @pytest.mark.parametrize("break_at", [1, 2, 3])
    def test_broken_pipe_on_nth_token_returns_dict(self, break_at):
        """T29: the pipe closing on the Nth token is swallowed, the LLM is
        called exactly once, and validate() still returns a dict."""
        agent = self._agent()
        calls: list = []
        valid_reply = '{"status": "approved", "feedback": "ok"}'
        with patch(
            "tools.validator_agent.request_completion",
            side_effect=_pipe_breaks_on_nth_token(valid_reply, calls, break_at=break_at),
        ):
            result = agent.validate({
                "code": "x = 1",
                "instruction": "fix",
                "iteration": 1,
            })
        assert len(calls) == 1, (
            f"Expected exactly 1 LLM call, got {len(calls)}"
        )
        assert isinstance(result, dict), (
            f"validate() must return a dict even when the pipe closed on "
            f"token {break_at}, got {type(result)}"
        )
        assert result == {"status": "approved", "feedback": "ok"}
