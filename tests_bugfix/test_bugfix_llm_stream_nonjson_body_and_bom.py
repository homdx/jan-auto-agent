"""tests_bugfix/test_bugfix_llm_stream_nonjson_body_and_bom.py — a body that
is not JSON (or that is prefixed with a UTF-8 BOM) must enter the retry
ladder instead of escaping as a bare json.JSONDecodeError.

request_completion's non-streaming branch read the body like this:

    with _open() as response:
        raw = json.loads(response.read().decode("utf-8"))
    return _extract_content(raw, api_format)

inside an `except (TimeoutError, ssl.SSLError, ConnectionError,
urllib.error.URLError)`. json.loads raises json.JSONDecodeError, which is a
ValueError, not one of those — so a gateway that answers HTTP 200 with
HTML/prose/plain text crashed the whole call with an uncaught decode error
instead of triggering the retry/backoff ladder that already exists for
every other parse failure. Same class as A4.

A UTF-8 BOM (\\xef\\xbb\\xbf at the start of the body, common for UTF-8-SIG
emitted by Windows tooling) broke json.loads the same way, on an otherwise
perfectly valid body.
"""

from __future__ import annotations

import http.server
import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.llm_stream import request_completion

# AUTO-XDIST-PORT-RACE-1: this module binds a real http.server on an
# OS-assigned ephemeral port. See tests_bugfix/test_llm_stream_empty_choices.py
# for why every port-binding module shares this group name.
pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")

_HEADERS = {"Content-Type": "application/json"}
_PAYLOAD = {"model": "x", "messages": []}
_URL = "/v1/chat/completions"


class _BytesHandler(http.server.BaseHTTPRequestHandler):
    """Serves raw bytes, one scripted body per request.

    ``bodies`` is a list of bytes; request N gets bodies[min(N, len-1)], so a
    single-entry list repeats forever.
    """

    bodies: list = [b"{}"]
    count: int = 0

    def do_POST(self):
        idx = min(type(self).count, len(type(self).bodies) - 1)
        type(self).count += 1
        payload = type(self).bodies[idx]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def _serve(bodies):
    handler_cls = type("Handler", (_BytesHandler,), {"bodies": bodies, "count": 0})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port, handler_cls


def _url(port):
    return f"http://127.0.0.1:{port}{_URL}"


class TestNonJsonBodyEntersTheRetryLadder:

    def test_non_json_body_is_retried_then_raises(self):
        """Repro: HTML/prose instead of JSON. Must not escape as a bare
        json.JSONDecodeError."""
        server, port, handler_cls = _serve([b"<html>502 Bad Gateway</html>"])
        sleep = MagicMock()
        try:
            with pytest.raises(Exception) as excinfo:
                request_completion(
                    _url(port), _HEADERS, _PAYLOAD, timeout=5,
                    stream=False, api_format="openai",
                    error_retries=1, error_retry_wait_sec=0.1,
                    _sleep_fn=sleep,
                )
        finally:
            server.shutdown()
        # The whole point: it was a retryable read failure, not an escape.
        assert not isinstance(excinfo.value, json.JSONDecodeError)
        assert handler_cls.count == 2, "the bad body must have been retried"
        sleep.assert_called_once()

    def test_bad_then_good_body_recovers(self):
        server, port, handler_cls = _serve([
            b"gateway returned prose, not json",
            json.dumps({"choices": [{"message": {"content": "hello"}}]}).encode(),
        ])
        sleep = MagicMock()
        try:
            result = request_completion(
                _url(port), _HEADERS, _PAYLOAD, timeout=5,
                stream=False, api_format="openai",
                error_retries=1, error_retry_wait_sec=0.1,
                _sleep_fn=sleep,
            )
        finally:
            server.shutdown()
        assert result == "hello"
        assert handler_cls.count == 2

    def test_empty_body_is_retried(self):
        server, port, handler_cls = _serve([b""])
        sleep = MagicMock()
        try:
            with pytest.raises(Exception) as excinfo:
                request_completion(
                    _url(port), _HEADERS, _PAYLOAD, timeout=5,
                    stream=False, api_format="openai",
                    error_retries=0, _sleep_fn=sleep,
                )
        finally:
            server.shutdown()
        assert not isinstance(excinfo.value, json.JSONDecodeError)
        assert handler_cls.count == 1


class TestUtf8BomIsAccepted:

    def test_bom_prefixed_body_parses(self):
        body = b"\xef\xbb\xbf" + json.dumps(
            {"choices": [{"message": {"content": "hi"}}]}
        ).encode()
        server, port, handler_cls = _serve([body])
        try:
            result = request_completion(
                _url(port), _HEADERS, _PAYLOAD, timeout=5,
                stream=False, api_format="openai",
            )
        finally:
            server.shutdown()
        assert result == "hi"
        assert handler_cls.count == 1  # no retries burned on a valid body

    def test_bom_prefixed_ollama_body_parses(self):
        body = b"\xef\xbb\xbf" + json.dumps(
            {"message": {"content": "ollama answer"}}
        ).encode()
        server, port, handler_cls = _serve([body])
        try:
            result = request_completion(
                _url(port), _HEADERS, _PAYLOAD, timeout=5,
                stream=False, api_format="ollama",
            )
        finally:
            server.shutdown()
        assert result == "ollama answer"
        assert handler_cls.count == 1


class TestPayloadShapeErrorStillFailsFast:
    """Design guard: a VALID JSON body with the wrong SHAPE is not a
    transient read failure. Retrying it would burn the default budget of
    60 x 10s for a guaranteed repeat, and it would change the ValueError
    this codebase already relies on (see
    tests_bugfix/test_llm_stream_empty_choices.py, which asserts the
    ValueError with the default retry settings and no fake sleep).

    So json.loads stays inside the retry loop and _extract_content stays
    outside it: the ladder is for unreadable/unparseable bodies, not for
    bodies we already understood and rejected.
    """

    def test_valueerror_from_extract_content_does_not_consume_retry_budget(self):
        server, port, handler_cls = _serve(
            [json.dumps({"choices": []}).encode()]
        )
        sleep = MagicMock()
        try:
            with pytest.raises(ValueError):
                request_completion(
                    _url(port), _HEADERS, _PAYLOAD, timeout=5,
                    stream=False, api_format="openai",
                    error_retries=60, error_retry_wait_sec=10.0,
                    _sleep_fn=sleep,
                )
        finally:
            server.shutdown()
        assert handler_cls.count == 1, "a shape error must not be retried"
        sleep.assert_not_called()
