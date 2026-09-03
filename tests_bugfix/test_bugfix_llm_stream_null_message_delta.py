"""tests_bugfix/test_bugfix_llm_stream_null_message_delta.py

The streaming path in request_completion crashes on chunks where
``message`` or ``delta`` is JSON ``null`` instead of a dict:

  * Ollama: ``{"message": null, "done": false}`` —
    ``chunk.get("message", {})`` returns ``None`` (the key exists, so
    the default is not used), and ``None.get("content", "")`` raises
    ``AttributeError``, which is NOT caught by the surrounding
    ``except json.JSONDecodeError``.

  * OpenAI: ``{"choices": [{"delta": null, "role": "assistant"}]}`` —
    ``choices[0]["delta"]`` returns ``None``, and the same
    ``None.get("content", "")`` raises ``AttributeError``, not caught
    by ``except (json.JSONDecodeError, KeyError)``.

The non-streaming path already handles null content (test_llm_stream_null_content).
This test covers the streaming path.
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

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")


class _StreamHandler(http.server.BaseHTTPRequestHandler):
    """Serve a sequence of newline-delimited (ollama) or SSE (openai)
    chunks, then close the connection."""

    chunks: list = []
    api_format: str = "ollama"

    def do_POST(self):
        if self.api_format == "ollama":
            body = "\n".join(json.dumps(c) for c in self.chunks) + "\n"
        else:
            body = "".join(f"data: {json.dumps(c)}\n\n" for c in self.chunks)
            body += "data: [DONE]\n\n"
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type",
                         "application/x-ndjson" if self.api_format == "ollama"
                         else "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _serve_stream(chunks, api_format="ollama"):
    handler_cls = type(
        "Handler",
        (_StreamHandler,),
        {"chunks": chunks, "api_format": api_format},
    )
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


class TestOllamaStreamNullMessage:
    def test_null_message_in_stream_does_not_crash(self):
        """A chunk with message=null must not crash the streaming loop."""
        chunks = [
            {"message": {"role": "assistant", "content": "hello "}, "done": False},
            {"message": None, "done": False},
            {"message": {"role": "assistant", "content": "world"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True},
        ]
        server, port = _serve_stream(chunks, api_format="ollama")
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/api/chat",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=True, api_format="ollama",
            )
        finally:
            server.shutdown()
        assert "hello" in result
        assert "world" in result

    def test_null_message_alone_does_not_crash(self):
        """A stream where every chunk has message=null must return empty."""
        chunks = [
            {"message": None, "done": False},
            {"message": None, "done": True},
        ]
        server, port = _serve_stream(chunks, api_format="ollama")
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/api/chat",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=True, api_format="ollama",
            )
        finally:
            server.shutdown()
        assert result == ""


class TestOpenAIStreamNullDelta:
    def test_null_delta_in_stream_does_not_crash(self):
        """A chunk with delta=null must not crash the streaming loop."""
        chunks = [
            {"choices": [{"delta": {"content": "hello "}, "index": 0}]},
            {"choices": [{"delta": None, "index": 0}]},
            {"choices": [{"delta": {"content": "world"}, "index": 0}]},
        ]
        server, port = _serve_stream(chunks, api_format="openai")
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=True, api_format="openai",
            )
        finally:
            server.shutdown()
        assert "hello" in result
        assert "world" in result

    def test_null_delta_alone_does_not_crash(self):
        """A stream where every chunk has delta=null must return empty."""
        chunks = [
            {"choices": [{"delta": None, "role": "assistant"}]},
        ]
        server, port = _serve_stream(chunks, api_format="openai")
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=True, api_format="openai",
            )
        finally:
            server.shutdown()
        assert result == ""
