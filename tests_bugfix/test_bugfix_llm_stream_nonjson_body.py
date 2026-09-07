"""A9: a non-JSON response body, or a body with a UTF-8 BOM, must enter the
retry ladder instead of escaping as an uncaught exception.

The non-streaming read path in request_completion() wraps
open+read+json.loads in a retry loop whose except clause lists only
networking exceptions:

    except (TimeoutError, ssl.SSLError, ConnectionError, urllib.error.URLError)

json.loads() raises json.JSONDecodeError, which is a ValueError subclass
and NOT one of those — so a provider that answered HTTP 200 with a prose
body, an HTML error page, or an empty body escaped the retry ladder
outright with a bare traceback. Same class of bug as the message: null one
(A4): a parse failure on a response that DID arrive should be surfaced the
same way every other failure in this function is.

Caught as json.JSONDecodeError / UnicodeDecodeError rather than bare
ValueError on purpose: _extract_content() raises its OWN descriptive
ValueError for a body that arrived as valid JSON but is unusable (empty
choices, null message). Those are not transient — retrying them would
stall the run for error_retries × error_retry_wait_sec (60 × 10s by
default) and then raise the same error anyway. They keep surfacing
immediately; see TestUnusableButWellFormedJsonIsNotRetried below.

Separately, a UTF-8 BOM at the start of the body breaks json.loads()
outright ("Unexpected UTF-8 BOM (0xefbvbdbf), use encoding utf-8-sig"),
and some gateways really do emit one.
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

_HEADERS = {"Content-Type": "application/json"}
_PAYLOAD = {"model": "x", "messages": []}
_OPENAI_URL = "/v1/chat/completions"
_OLLAMA_URL = "/api/chat"


def _serve(body_bytes):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def _call(port, api_format="openai", **kwargs):
    url = f"http://127.0.0.1:{port}{_OPENAI_URL if api_format == 'openai' else _OLLAMA_URL}"
    return request_completion(
        url, _HEADERS, _PAYLOAD, timeout=5, stream=False,
        api_format=api_format,
        error_retries=2, error_retry_wait_sec=0,
        _sleep_fn=lambda s: None,
        **kwargs,
    )


class TestNonJsonBodyGoesThroughRetryLadder:
    @pytest.mark.parametrize("body", [
        b'{"error": "gateway returned html"}<html>not json</html>',
        b"<html><body>502 Bad Gateway</body></html>",
        b"gateway timeout",
        b"",
        b"[1, 2, 3",
    ])
    def test_non_json_body_is_not_a_bare_decodeerror(self, body):
        server, port = _serve(body)
        try:
            with pytest.raises(RuntimeError) as excinfo:
                _call(port)
        finally:
            server.shutdown()
        # A bare JSONDecodeError would mean it escaped the retry loop as an
        # uncaught exception. After the retry budget is spent the failure
        # surfaces as the same RuntimeError every other failure does.
        assert not isinstance(excinfo.value, json.JSONDecodeError)
        assert not isinstance(excinfo.value, ValueError)
        assert "JSONDecodeError" in str(excinfo.value)

    def test_retries_then_raises_runtimeerror(self):
        """The failure must consume the retry budget and then raise the
        same RuntimeError every other failure in this function raises."""
        calls = []
        server, port = _serve(b"not json at all")
        try:
            with pytest.raises(RuntimeError) as excinfo:
                _call(port, on_retry=lambda msg: calls.append(msg))
        finally:
            server.shutdown()
        assert "2" in str(excinfo.value) or "retr" in str(excinfo.value).lower()
        assert len(calls) == 2, calls

    def test_recoverable_when_next_attempt_returns_json(self):
        """The ladder is only useful if a later attempt can still succeed."""
        state = {"n": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                state["n"] += 1
                body = b"not json" if state["n"] == 1 else json.dumps(
                    {"choices": [{"message": {"content": "ok"}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            assert _call(port) == "ok"
        finally:
            server.shutdown()
        assert state["n"] == 2

    @pytest.mark.parametrize("api_format", ["openai", "ollama"])
    def test_ollama_format_too(self, api_format):
        server, port = _serve(b"nope")
        try:
            with pytest.raises(RuntimeError) as excinfo:
                _call(port, api_format=api_format)
        finally:
            server.shutdown()
        assert not isinstance(excinfo.value, ValueError)


class TestUnusableButWellFormedJsonIsNotRetried:
    """The opposite edge of the same change: _extract_content() raises a
    descriptive ValueError for a body that arrived as valid JSON but is
    unusable ("no choices — likely blocked/filtered", "message is not an
    object"). That is not transient, so it must surface immediately instead
    of being retried — otherwise one filtered response would stall the run
    for error_retries × error_retry_wait_sec before raising the same error.
    """

    def test_empty_choices_is_immediate_not_retried(self):
        retries = []
        server, port = _serve(json.dumps({"choices": []}).encode())
        try:
            with pytest.raises(ValueError) as excinfo:
                request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    _HEADERS, _PAYLOAD, timeout=5, stream=False,
                    api_format="openai",
                    error_retries=5, error_retry_wait_sec=0,
                    _sleep_fn=lambda s: None, on_retry=retries.append,
                )
        finally:
            server.shutdown()
        assert len(retries) == 0, retries
        assert "choices" in str(excinfo.value).lower()

    def test_null_message_is_immediate_not_retried(self):
        retries = []
        server, port = _serve(json.dumps({"message": None}).encode())
        try:
            with pytest.raises(ValueError):
                request_completion(
                    f"http://127.0.0.1:{port}/api/chat",
                    _HEADERS, _PAYLOAD, timeout=5, stream=False,
                    api_format="ollama",
                    error_retries=5, error_retry_wait_sec=0,
                    _sleep_fn=lambda s: None, on_retry=retries.append,
                )
        finally:
            server.shutdown()
        assert len(retries) == 0, retries


class TestUtf8Bom:
    def test_bom_body_parses(self):
        """Some gateways emit a UTF-8 BOM; json.loads rejects it outright."""
        body = "\ufeff" + json.dumps({"choices": [{"message": {"content": "bom ok"}}]})
        server, port = _serve(body.encode("utf-8"))
        try:
            assert _call(port) == "bom ok"
        finally:
            server.shutdown()

    def test_bom_body_ollama_format(self):
        body = "\ufeff" + json.dumps({"message": {"content": "bom ok"}})
        server, port = _serve(body.encode("utf-8"))
        try:
            assert _call(port, api_format="ollama") == "bom ok"
        finally:
            server.shutdown()

    def test_plain_utf8_still_parses(self):
        server, port = _serve(
            json.dumps({"choices": [{"message": {"content": "fine"}}]}).encode("utf-8")
        )
        try:
            assert _call(port) == "fine"
        finally:
            server.shutdown()

    def test_unicode_content_survives(self):
        body = json.dumps({"choices": [{"message": {"content": "Привет, мир"}}]})
        server, port = _serve(body.encode("utf-8"))
        try:
            assert _call(port) == "Привет, мир"
        finally:
            server.shutdown()
