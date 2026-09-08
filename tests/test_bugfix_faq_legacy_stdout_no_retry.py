"""tests/test_bugfix_faq_legacy_stdout_no_retry.py — BUGFIX (audit): a
stdout failure AFTER a successful legacy streaming LLM call must not
trigger a retry.

Confirmed live before the fix: FaqAgent._answer_legacy's trailing
``print()`` (the newline after streamed output) sat inside the same
try/except as ``request_completion()``. A BrokenPipeError from that
print() — e.g. output piped to ``head`` which closed the pipe early —
was indistinguishable from a failed LLM call, so it was caught by the
blanket ``except Exception`` and triggered a wasted duplicate
request_completion call (or, on the last attempt, silently discarded an
answer that had already streamed successfully).
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.faq_agent import FaqAgent


def _agent() -> FaqAgent:
    return FaqAgent(
        model="m", base_url="http://localhost:1337/v1", api_key="k",
        api_format="openai", timeout=30,
    )


class TestLegacyStreamingStdoutFailureDoesNotRetry:
    def test_broken_pipe_after_success_does_not_retry(self):
        agent = _agent()
        call_count = 0

        def _fake_request_completion(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            on_token = kwargs.get("on_token")
            if on_token is not None:
                on_token("the answer")
            return "the answer"

        with patch("tools.faq_agent.request_completion", side_effect=_fake_request_completion), \
             patch("builtins.print", side_effect=BrokenPipeError("pipe closed")):
            reply = agent._answer_legacy("question?", [("doc.md", "content")], stream=True)

        # The LLM call must have run exactly once — the print() failure
        # must not have triggered a retry.
        assert call_count == 1
        # The already-obtained reply must still be returned, not discarded.
        assert reply == "the answer"
        assert agent.llm_call_count == 1

    def test_normal_streaming_still_prints_trailing_newline(self, capsys):
        agent = _agent()

        def _fake_request_completion(*args, **kwargs):
            on_token = kwargs.get("on_token")
            if on_token is not None:
                on_token("hi")
            return "hi"

        with patch("tools.faq_agent.request_completion", side_effect=_fake_request_completion):
            reply = agent._answer_legacy("question?", [("doc.md", "content")], stream=True)

        assert reply == "hi"
        # Trailing newline still happens in the ordinary (no stdout error) case.
        assert capsys.readouterr().out.endswith("\n")


class TestLegacyStreamingOnTokenBrokenPipe:
    """on_token writes to stdout via sys.stdout.write — if the pipe is
    closed (e.g. output piped to ``head``), BrokenPipeError must be
    swallowed inside on_token, NOT propagate into retry_with_backoff
    which would waste a duplicate LLM call and then discard the answer.
    """

    def test_on_token_broken_pipe_does_not_retry(self):
        agent = _agent()
        call_count = 0

        def _fake_request_completion(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            on_token = kwargs.get("on_token")
            if on_token is not None:
                on_token("the answer")
            return "the answer"

        # Replace sys.stdout with a pipe that raises BrokenPipeError on write.
        import io
        class _BrokenStdout(io.StringIO):
            def write(self, s):
                raise BrokenPipeError("pipe closed")
            def flush(self):
                pass

        _real_stdout = sys.stdout
        sys.stdout = _BrokenStdout()
        try:
            with patch("tools.faq_agent.request_completion", side_effect=_fake_request_completion):
                reply = agent._answer_legacy("question?", [("doc.md", "content")], stream=True)
        finally:
            sys.stdout = _real_stdout

        # The LLM call must have run exactly once — the on_token
        # BrokenPipeError must not have triggered a retry.
        assert call_count == 1
        # The already-obtained reply must still be returned, not discarded.
        assert reply == "the answer"
        assert agent.llm_call_count == 1
