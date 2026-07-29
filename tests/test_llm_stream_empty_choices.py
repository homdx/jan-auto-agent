"""tests/test_llm_stream_empty_choices.py — two complementary fixes:

1. SSE streaming: a chunk with empty `choices` must not crash the whole
   streaming request (original fix in "Fix llm stream" commit).

2. Non-streaming: a complete response with empty `choices` must raise a
   clear ValueError (not IndexError) so callers can distinguish a
   filtered/blocked response from a genuine network failure.
   See TestEmptyChoicesNonStreamingResponse below.

request_completion's OpenAI SSE branch supports any base_url speaking the
openai chat-completions format, not just literal OpenAI — that is the
whole point of api_format="openai" being configurable. Some backends send
a chunk with an empty choices array: OpenAI itself does this for its own
usage-reporting chunk (stream_options.include_usage), and various
proxy/gateway wrappers (LiteLLM, vLLM, Azure) send the same shape for other
reasons regardless of client request options.

`chunk["choices"][0]["delta"]` on an empty list raises IndexError, which
the surrounding `except (json.JSONDecodeError, KeyError)` did not catch —
so this ONE chunk crashed the ENTIRE streaming request with an unhandled
exception, discarding every token already collected in `parts`, instead of
just being skipped like any other unparseable/irrelevant chunk.
"""

from __future__ import annotations

import http.server
import json
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.llm_stream import request_completion


class _SSEHandler(http.server.BaseHTTPRequestHandler):
    chunks: list = []

    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in self.chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a):
        pass


def _serve(chunks):
    handler_cls = type("Handler", (_SSEHandler,), {"chunks": chunks})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


class TestEmptyChoicesChunkDoesNotCrashTheStream:
    def test_usage_only_chunk_is_skipped(self):
        server, port = _serve([
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": " world"}}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            {"choices": [{"delta": {"content": "!"}}]},
        ])
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=True, api_format="openai",
            )
        finally:
            server.shutdown()
        assert result == "Hello world!"

    def test_empty_choices_as_the_very_first_chunk(self):
        """The empty chunk arriving before any real content — must not
        prevent the tokens that follow from being collected."""
        server, port = _serve([
            {"choices": []},
            {"choices": [{"delta": {"content": "answer"}}]},
        ])
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=True, api_format="openai",
            )
        finally:
            server.shutdown()
        assert result == "answer"

    def test_empty_choices_as_the_last_chunk_before_done(self):
        server, port = _serve([
            {"choices": [{"delta": {"content": "answer"}}]},
            {"choices": [], "usage": {"prompt_tokens": 1}},
        ])
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=True, api_format="openai",
            )
        finally:
            server.shutdown()
        assert result == "answer"

    def test_normal_streaming_unaffected(self):
        """Sanity: ordinary multi-chunk streaming, no empty-choices chunks
        involved at all, must still work exactly as before."""
        server, port = _serve([
            {"choices": [{"delta": {"content": "a"}}]},
            {"choices": [{"delta": {"content": "b"}}]},
            {"choices": [{"delta": {"content": "c"}}]},
        ])
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=True, api_format="openai",
            )
        finally:
            server.shutdown()
        assert result == "abc"


# ---------------------------------------------------------------------------
# Non-streaming path — _extract_content fix
# ---------------------------------------------------------------------------

class _JSONHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler that returns a fixed JSON body (non-streaming)."""
    body: bytes = b""

    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


def _serve_json(body: dict):
    raw = json.dumps(body).encode()
    handler_cls = type("Handler", (_JSONHandler,), {"body": raw})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


class TestEmptyChoicesNonStreamingResponse:
    """Non-streaming path: _extract_content must raise a clear ValueError
    (not IndexError) when the backend returns choices: [], and must leave
    normal non-streaming responses completely unaffected."""

    def test_empty_choices_raises_value_error_not_index_error(self):
        """A response body of {"choices": [], ...} must raise ValueError with
        a message that identifies the filtered/blocked cause.  Before the fix
        this raised bare IndexError: list index out of range — impossible to
        distinguish from a programming error and swallowed by callers that
        only log `exc` without checking its type."""
        import pytest
        server, port = _serve_json(
            {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 0}}
        )
        try:
            with pytest.raises(ValueError, match="no choices"):
                request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "x", "messages": []},
                    timeout=5, stream=False, api_format="openai",
                )
        finally:
            server.shutdown()

    def test_normal_non_streaming_response_unaffected(self):
        """Sanity: an ordinary non-streaming response with a real choice must
        be returned verbatim — the guard must not disturb the happy path."""
        server, port = _serve_json({
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        })
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=False, api_format="openai",
            )
        finally:
            server.shutdown()
        assert result == "hello"
