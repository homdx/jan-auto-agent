"""tests/test_llm_stream_empty_choices.py — an SSE chunk with empty
`choices` must not crash the whole streaming request.

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

import pytest

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


class _NonStreamHandler(http.server.BaseHTTPRequestHandler):
    """Serves a single, complete (non-streaming) JSON response body."""

    body: dict = {}

    def do_POST(self):
        payload = json.dumps(self.body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def _serve_once(body):
    handler_cls = type("Handler", (_NonStreamHandler,), {"body": body})
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


class TestEmptyChoicesNonStreamingResponse:
    """The blocking (stream=False) path must handle an empty ``choices``
    array the same way the streaming path was just fixed to: with a clear,
    catchable error instead of an opaque IndexError.

    Real-world trigger: some OpenAI-compatible gateways/proxies return HTTP
    200 with ``{"choices": []}`` (no error field) when a request is
    blocked/filtered rather than raising an HTTPError — the exact "backend
    speaks the openai format but doesn't behave exactly like OpenAI" case
    this module's docstrings already call out elsewhere.
    """

    def test_empty_choices_raises_clear_error_not_indexerror(self):
        server, port = _serve_once({"choices": []})
        try:
            with pytest.raises(ValueError) as excinfo:
                request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "x", "messages": []},
                    timeout=5, stream=False, api_format="openai",
                )
        finally:
            server.shutdown()
        # Must NOT be the bare, uninformative IndexError this used to raise.
        assert not isinstance(excinfo.value, IndexError)
        assert "choices" in str(excinfo.value).lower()

    def test_normal_non_streaming_response_unaffected(self):
        server, port = _serve_once(
            {"choices": [{"message": {"content": "hello world"}}]}
        )
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=False, api_format="openai",
            )
        finally:
            server.shutdown()
        assert result == "hello world"
