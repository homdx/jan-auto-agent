"""tests_bugfix/test_bugfix_llm_stream_bad_json_body.py

The non-streaming path of request_completion() is:

    with _open() as response:
        raw = json.loads(response.read().decode("utf-8"))
    return _extract_content(raw, api_format)
except (TimeoutError, ssl.SSLError, ConnectionError, urllib.error.URLError) as e:
    ... retry ladder ...

Two ways a *received* body can break that:

  1. It is not JSON at all. ``json.loads`` raises
     ``json.JSONDecodeError``, which is a ``ValueError`` — not in the caught
     tuple. The parse failure therefore escaped the retry ladder entirely as an
     uncaught exception, so a truncated or garbled body from a flaky proxy
     aborted the call instead of being treated like the dropped connection the
     ladder exists for.
  2. It starts with a UTF-8 BOM. ``decode("utf-8")`` keeps the BOM in the
     string, and ``json.loads`` refuses "\ufeff{...}" as "not a valid JSON
     text". The body was perfectly good JSON.

Fix under test: add ValueError to the caught tuple and decode with utf-8-sig.
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

BOM = "\ufeff"


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves one queued body per request, then repeats the last one."""

    bodies: list[bytes] = []
    seen: list[int] = []

    def do_POST(self):
        n = len(self.seen)
        body = self.bodies[min(n, len(self.bodies) - 1)]
        self.seen.append(n)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve(bodies):
    handler_cls = type("Handler", (_Handler,), {"bodies": list(bodies), "seen": []})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port, handler_cls


PAYLOAD = {"model": "x", "messages": []}


class TestNonJsonBodyUsesTheRetryLadder:
    def test_non_json_body_retried_then_reported(self):
        """THE BUG: JSONDecodeError is a ValueError and used to escape the
        networking-exception tuple, so the call died instead of retrying."""
        server, port, handler = _serve([b"not json at all"])
        try:
            with pytest.raises(RuntimeError) as exc_info:
                request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    PAYLOAD,
                    timeout=5, stream=False, api_format="openai",
                    error_retries=2, error_retry_wait_sec=0,
                    _sleep_fn=lambda _s: None,
                )
        finally:
            server.shutdown()
        assert "JSONDecodeError" in str(exc_info.value)
        assert "after 2 retries" in str(exc_info.value)
        assert len(handler.seen) == 3, "the body read must be retried like a network error"

    def test_bad_body_then_good_body_succeeds(self):
        """The retry ladder is only useful if a malformed body can actually be
        followed by a good one — a proxy that truncates once and succeeds on
        the retry."""
        good = b'{"choices": [{"message": {"content": "hello"}}]}'
        server, port, handler = _serve([b"truncated: {\"cho", good])
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                PAYLOAD,
                timeout=5, stream=False, api_format="openai",
                error_retries=3, error_retry_wait_sec=0,
                _sleep_fn=lambda _s: None,
            )
        finally:
            server.shutdown()
        assert result == "hello"
        assert len(handler.seen) == 2

    def test_extract_content_value_error_is_not_retried(self):
        """A well-formed JSON body with an unexpected SHAPE is a different
        outcome from an unreadable body: it must reach the caller immediately
        and must not be replayed error_retries times."""
        server, port, handler = _serve([b'{"message": null, "done": true}'])
        try:
            with pytest.raises(ValueError):
                request_completion(
                    f"http://127.0.0.1:{port}/api/chat",
                    {"Content-Type": "application/json"},
                    PAYLOAD,
                    timeout=5, stream=False, api_format="ollama",
                    error_retries=5, error_retry_wait_sec=0,
                    _sleep_fn=lambda _s: None,
                )
        finally:
            server.shutdown()
        assert len(handler.seen) == 1, (
            "a shape mismatch is deterministic — retrying it just wastes the "
            "budget without changing the outcome"
        )

    def test_truly_unreachable_host_still_retries(self):
        """Sanity: the original networking path must keep using the ladder."""
        with pytest.raises(RuntimeError) as exc_info:
            request_completion(
                "http://127.0.0.1:9/v1/chat/completions",
                {"Content-Type": "application/json"},
                PAYLOAD,
                timeout=1, stream=False, api_format="openai",
                error_retries=1, error_retry_wait_sec=0,
                _sleep_fn=lambda _s: None,
            )
        assert "reading response body" in str(exc_info.value) or "calling" in str(exc_info.value)


class TestUtf8BomBody:
    def test_bom_body_parses_openai(self):
        """A BOM at the head of the body used to break json.loads outright."""
        server, port, _h = _serve([BOM.encode() + b'{"choices": [{"message": {"content": "hello"}}]}'])
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                PAYLOAD,
                timeout=5, stream=False, api_format="openai",
                error_retries=0,
            )
        finally:
            server.shutdown()
        assert result == "hello"

    def test_bom_body_parses_ollama(self):
        server, port, _h = _serve([
            BOM.encode() + b'{"message": {"role": "assistant", "content": "hi"}, "done": true}'
        ])
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/api/chat",
                {"Content-Type": "application/json"},
                PAYLOAD,
                timeout=5, stream=False, api_format="ollama",
                error_retries=0,
            )
        finally:
            server.shutdown()
        assert result == "hi"

    def test_bom_free_body_unchanged(self):
        server, port, _h = _serve([b'{"choices": [{"message": {"content": "plain"}}]}'])
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                PAYLOAD,
                timeout=5, stream=False, api_format="openai",
                error_retries=0,
            )
        finally:
            server.shutdown()
        assert result == "plain"

    def test_bom_in_a_stream_keeps_the_first_token(self):
        """Streaming decodes line by line; a BOM on the first line used to be
        silently dropped by the JSONDecodeError `continue`, so the reply lost
        its first token."""
        first = BOM.encode() + b'{"message": {"content": "Hel"}, "done": false}\n'
        second = b'{"message": {"content": "lo"}, "done": true}\n'
        server, port, _h = _serve([first + second])
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/api/chat",
                {"Content-Type": "application/json"},
                PAYLOAD,
                timeout=5, stream=True, api_format="ollama",
                error_retries=0,
            )
        finally:
            server.shutdown()
        assert result == "Hello"
