"""tests/test_llm_stream_null_content.py — a non-streaming reply whose
message `content` comes back JSON `null` (rather than an empty string or
an absent key) must not crash `request_completion`.

Some OpenAI-compatible gateways return HTTP 200 with
`"message": {"content": null, ...}` (ollama api_format) or
`"choices": [{"message": {"content": null, ...}}]` (openai api_format) for
a reasoning-only / tool-call-only / filtered turn. `_extract_content` used
to call `.strip()` directly on that field, raising
`AttributeError: 'NoneType' object has no attribute 'strip'` for a
response that arrived successfully — instead of degrading to an empty
reply the same way the empty-`choices` case already does.
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


class _NonStreamHandler(http.server.BaseHTTPRequestHandler):
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


class TestNullContentDoesNotCrash:
    def test_openai_format_null_content_returns_empty_string(self):
        server, port = _serve_once(
            {"choices": [{"message": {"role": "assistant", "content": None}}]}
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
        assert result == ""

    def test_ollama_format_null_content_returns_empty_string(self):
        server, port = _serve_once(
            {"message": {"role": "assistant", "content": None}, "done": True}
        )
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/api/chat",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=False, api_format="ollama",
            )
        finally:
            server.shutdown()
        assert result == ""

    def test_openai_format_normal_content_unaffected(self):
        server, port = _serve_once(
            {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
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
        assert result == "hello"
