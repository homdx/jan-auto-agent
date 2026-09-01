"""tests/test_llm_stream_retry.py — AUTO-RATE-1: rate-limit / transient-
error retry in request_completion().

Story background
-----------------
Field report: --validate-plan (and, by the same code path, the original
plan-time Gate 1 pass) against a real kenari.id free-tier endpoint hit two
failures on a 31-35 task batch:

    AUTO-T4 (presence): LLM call failed: HTTP 429 from
        https://kenari.id/v1/chat/completions: {"error":{"code":
        "free_quota_rpm","message":"free-model rate limit reached; slow
        down and retry", ...}} — server says retry in 17.0s
    AUTO-T3 (presence): JSON decode failed (Expecting value: line 1
        column 1 (char 0)) — failing closed

request_completion() raised on the very first non-2xx response, with no
concept of a rate limit being transient — so a single HTTP 429 from a
free-tier gateway made Gate1Filter._check_presence fail a real, still-
needed task CLOSED (indistinguishable from "the LLM confirmed this is
already fixed"), purely because of API throttling.

This ports the (already battle-tested, in the sibling learn-in-play1
project's llm_client.py) retry-after parsing and wait-and-retry logic back
into request_completion() — the single shared choke point used by all 16
LLM call sites in this codebase (Gate1Filter, ClusterReviewer, Coder,
OuterLoop, FaqAgent, ...) — so every one of them gets rate-limit resilience
without individual changes.

No live network access; all HTTP is a real (but local, in-process)
http.server, matching the existing test_llm_stream_empty_choices.py
pattern — no mocking of urllib itself, so the retry loop is exercised
exactly as it runs in production, only the sleep is stubbed out (real
seconds are never actually waited by this test file).
"""

from __future__ import annotations

import http.server
import json
import sys
import threading
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.llm_stream import _is_retryable_status, _parse_retry_after, request_completion

# AUTO-XDIST-PORT-RACE-1: this module binds a real http.server on an
# OS-assigned ephemeral port. Under pytest-xdist with 2+ workers, two
# DIFFERENT worker processes can genuinely bind/release overlapping
# ports at the same wall-clock moment -- confirmed live as the cause
# of an intermittent failure in this exact file, full-suite-only,
# never standalone. xdist_group pins every test in this module (and
# the 8 sibling files that also bind real servers, sharing this same
# group name) to the SAME worker, so none of them can ever race
# another for a port across concurrent workers.
pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")



# ─────────────────────────────────────────────────────────────────────────────
# _parse_retry_after — pure unit tests, no server needed
# ─────────────────────────────────────────────────────────────────────────────

def _http_error(body: str, headers: "dict | None" = None, code: int = 429):
    """Build a real urllib.error.HTTPError the way urlopen() would raise it."""
    import email.message
    import io
    h = email.message.Message()
    for k, v in (headers or {}).items():
        h[k] = v
    return urllib.error.HTTPError("https://example.test/x", code, "error", h,
                                  io.BytesIO(body.encode()))


class TestParseRetryAfter:

    def test_retry_after_header_takes_priority(self):
        e = _http_error("no timing info in body", headers={"Retry-After": "19"})
        assert _parse_retry_after(e, "no timing info in body") == 19.0

    def test_malformed_header_falls_through_to_body(self):
        e = _http_error("Please try again in 820ms", headers={"Retry-After": "not-a-number"})
        assert _parse_retry_after(e, "Please try again in 820ms") == pytest.approx(0.82)

    def test_groq_style_milliseconds_in_body(self):
        body = ('{"error":{"message":"Rate limit reached... Please try '
                'again in 820ms. Need more tokens?","code":"rate_limit_exceeded"}}')
        e = _http_error(body)
        assert _parse_retry_after(e, body) == pytest.approx(0.82)

    def test_gemini_style_seconds_in_body(self):
        """RATE-4 equivalent: Gemini says 'retry in Ns.', not 'try again in Nms'."""
        body = "Please retry in 57.062042596s."
        e = _http_error(body)
        assert _parse_retry_after(e, body) == pytest.approx(57.062042596)

    def test_gemini_style_without_decimal(self):
        body = "Please retry in 5s."
        e = _http_error(body)
        assert _parse_retry_after(e, body) == pytest.approx(5.0)

    def test_no_timing_info_anywhere_returns_none(self):
        body = "free-model rate limit reached; slow down and retry"
        e = _http_error(body)
        assert _parse_retry_after(e, body) is None

    def test_zero_ms_clamped_to_minimum(self):
        body = "Please try again in 0ms"
        e = _http_error(body)
        assert _parse_retry_after(e, body) == pytest.approx(0.1)


class TestIsRetryableStatus:

    def test_429_is_retryable(self):
        assert _is_retryable_status(429) is True

    def test_402_is_retryable(self):
        assert _is_retryable_status(402) is True

    def test_5xx_is_retryable(self):
        assert _is_retryable_status(500) is True
        assert _is_retryable_status(503) is True
        assert _is_retryable_status(599) is True

    def test_400_is_not_retryable(self):
        """A malformed request stays malformed — retrying it just wastes
        error_retry_wait_sec for a guaranteed repeat failure."""
        assert _is_retryable_status(400) is False

    def test_401_403_404_are_not_retryable(self):
        assert _is_retryable_status(401) is False
        assert _is_retryable_status(403) is False
        assert _is_retryable_status(404) is False


# ─────────────────────────────────────────────────────────────────────────────
# request_completion() end-to-end retry behavior — real local HTTP server
# ─────────────────────────────────────────────────────────────────────────────

class _ScriptedHandler(http.server.BaseHTTPRequestHandler):
    """Serves a scripted sequence of responses, one per request received.

    Each script entry is either:
      ("error", status_code, body_str, headers_dict)  -> non-2xx response
      ("ok", body_dict)                                -> 200 JSON response
    """
    script: list = []
    request_count = 0

    def do_POST(self):
        idx = min(type(self).request_count, len(type(self).script) - 1)
        type(self).request_count += 1
        entry = type(self).script[idx]
        if entry[0] == "error":
            _, code, body, extra_headers = entry
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            payload = body.encode()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            _, body = entry
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, *a):
        pass


def _serve_script(script):
    handler_cls = type("Handler", (_ScriptedHandler,), {"script": script, "request_count": 0})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, handler_cls


_OK_BODY = {"choices": [{"message": {"content": "hello"}}]}


class TestRetrySucceedsAfter429:

    def test_default_settings_retry_a_single_429_and_succeed(self):
        """The exact regression this exists for: --validate-plan's default
        call (no explicit error_retries passed) must now survive one 429,
        not fail closed on it."""
        server, port, handler_cls = _serve_script([
            ("error", 429, '{"error":{"message":"slow down and retry"}}',
             {"Retry-After": "0"}),  # 0 -> clamped to 0.1s, keeps the test fast
            ("ok", _OK_BODY),
        ])
        sleep = MagicMock()
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=False, api_format="openai",
                _sleep_fn=sleep,
            )
        finally:
            server.shutdown()
        assert result == "hello"
        assert handler_cls.request_count == 2
        sleep.assert_called_once_with(pytest.approx(0.1))

    def test_retry_after_header_value_is_what_gets_slept(self):
        server, port, handler_cls = _serve_script([
            ("error", 429, "rate limited", {"Retry-After": "17"}),
            ("ok", _OK_BODY),
        ])
        sleep = MagicMock()
        try:
            request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=False, api_format="openai",
                _sleep_fn=sleep,
            )
        finally:
            server.shutdown()
        sleep.assert_called_once_with(pytest.approx(17.0))

    def test_second_consecutive_429_also_parses_body_not_fixed_wait(self):
        body = ('{"error":{"message":"Rate limit... Please try again in '
                '820ms","code":"rate_limit_exceeded"}}')
        server, port, handler_cls = _serve_script([
            ("error", 429, "rate limited", {"Retry-After": "0"}),
            ("error", 429, body, {}),  # no header this time — only body text
            ("ok", _OK_BODY),
        ])
        sleep = MagicMock()
        try:
            request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=False, api_format="openai",
                error_retries=2, _sleep_fn=sleep,
            )
        finally:
            server.shutdown()
        waits = [c.args[0] for c in sleep.call_args_list]
        assert waits == pytest.approx([0.1, 0.82])

    def test_5xx_retried_with_fixed_wait(self):
        server, port, handler_cls = _serve_script([
            ("error", 503, "service unavailable", {}),
            ("ok", _OK_BODY),
        ])
        sleep = MagicMock()
        try:
            request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=False, api_format="openai",
                error_retry_wait_sec=5.0, _sleep_fn=sleep,
            )
        finally:
            server.shutdown()
        sleep.assert_called_once_with(5.0)

    def test_402_retried_with_fixed_wait_not_body_parsing(self):
        """402/5xx must NOT try to parse a 'try again in' hint out of the
        body — that phrasing convention is specific to 429 responses."""
        server, port, handler_cls = _serve_script([
            ("error", 402, "Please try again in 5ms, no credits", {}),
            ("ok", _OK_BODY),
        ])
        sleep = MagicMock()
        try:
            request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=False, api_format="openai",
                error_retry_wait_sec=45.0, _sleep_fn=sleep,
            )
        finally:
            server.shutdown()
        sleep.assert_called_once_with(45.0)


class TestRetryExhaustionAndOptOut:

    def test_retries_exhausted_raises_with_http_code_in_message(self):
        server, port, handler_cls = _serve_script([
            ("error", 429, "rate limited", {"Retry-After": "0"}),
            ("error", 429, "rate limited", {"Retry-After": "0"}),
            ("error", 429, "still limited", {"Retry-After": "0"}),
        ])
        sleep = MagicMock()
        try:
            with pytest.raises(RuntimeError, match="HTTP 429"):
                request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "x", "messages": []},
                    timeout=5, stream=False, api_format="openai",
                    error_retries=2, _sleep_fn=sleep,
                )
        finally:
            server.shutdown()
        assert handler_cls.request_count == 3  # 1 original + 2 retries
        assert sleep.call_count == 2

    def test_error_retries_zero_restores_old_fail_fast_behavior(self):
        """Explicit opt-out: exactly the pre-AUTO-RATE-1 behavior."""
        server, port, handler_cls = _serve_script([
            ("error", 429, "rate limited", {"Retry-After": "5"}),
        ])
        sleep = MagicMock()
        try:
            with pytest.raises(RuntimeError, match="HTTP 429"):
                request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "x", "messages": []},
                    timeout=5, stream=False, api_format="openai",
                    error_retries=0, _sleep_fn=sleep,
                )
        finally:
            server.shutdown()
        assert handler_cls.request_count == 1
        sleep.assert_not_called()

    def test_400_is_never_retried_even_with_retries_available(self):
        server, port, handler_cls = _serve_script([
            ("error", 400, "bad request", {}),
        ])
        sleep = MagicMock()
        try:
            with pytest.raises(RuntimeError, match="HTTP 400"):
                request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "x", "messages": []},
                    timeout=5, stream=False, api_format="openai",
                    error_retries=3, _sleep_fn=sleep,
                )
        finally:
            server.shutdown()
        assert handler_cls.request_count == 1
        sleep.assert_not_called()


class TestMaxRetryAfterCap:

    def test_wait_past_cap_raises_immediately_without_sleeping(self):
        """RATE-3 equivalent: a daily/monthly quota reset (e.g. Retry-After
        in the thousands of seconds) must not block the caller for hours."""
        server, port, handler_cls = _serve_script([
            ("error", 429, "rate limited", {"Retry-After": "1754"}),
        ])
        sleep = MagicMock()
        try:
            with pytest.raises(RuntimeError, match="HTTP 429"):
                request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "x", "messages": []},
                    timeout=5, stream=False, api_format="openai",
                    max_retry_after_sec=180, _sleep_fn=sleep,
                )
        finally:
            server.shutdown()
        assert handler_cls.request_count == 1
        sleep.assert_not_called()

    def test_wait_just_under_the_cap_still_sleeps_normally(self):
        server, port, handler_cls = _serve_script([
            ("error", 429, "rate limited", {"Retry-After": "170"}),
            ("ok", _OK_BODY),
        ])
        sleep = MagicMock()
        try:
            request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=False, api_format="openai",
                max_retry_after_sec=180, _sleep_fn=sleep,
            )
        finally:
            server.shutdown()
        sleep.assert_called_once_with(pytest.approx(170.0))

    def test_second_consecutive_429_also_respects_the_cap(self):
        server, port, handler_cls = _serve_script([
            ("error", 429, "rate limited", {"Retry-After": "0"}),
            ("error", 429, "rate limited", {"Retry-After": "1754"}),
        ])
        sleep = MagicMock()
        try:
            with pytest.raises(RuntimeError, match="HTTP 429"):
                request_completion(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    {"model": "x", "messages": []},
                    timeout=5, stream=False, api_format="openai",
                    error_retries=2, max_retry_after_sec=180, _sleep_fn=sleep,
                )
        finally:
            server.shutdown()
        assert sleep.call_count == 1  # only the first (0.1s) wait happened
        assert handler_cls.request_count == 2


class TestOnRetryCallback:

    def test_on_retry_called_with_message_before_each_wait(self):
        server, port, handler_cls = _serve_script([
            ("error", 429, "rate limited", {"Retry-After": "0"}),
            ("ok", _OK_BODY),
        ])
        sleep = MagicMock()
        messages = []
        try:
            request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "x", "messages": []},
                timeout=5, stream=False, api_format="openai",
                _sleep_fn=sleep, on_retry=messages.append,
            )
        finally:
            server.shutdown()
        assert len(messages) == 1
        assert "429" in messages[0]
        assert "retrying" in messages[0]


class TestNormalPathUnaffected:
    """Sanity: a plain, immediate 200 response is completely unaffected by
    AUTO-RATE-1 — no sleep import surprises, no retry-loop overhead beyond
    an extra `while True` iteration that exits on the first try."""

    def test_success_on_first_attempt_streaming(self):
        # Reuses the streaming SSE path exercised by
        # test_llm_stream_empty_choices.py; here only to confirm the retry
        # wrapper didn't change its behavior.
        import io

        class _SSEHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for tok in ("a", "b", "c"):
                    chunk = {"choices": [{"delta": {"content": tok}}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _SSEHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
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

    def test_success_on_first_attempt_non_streaming(self):
        server, port, handler_cls = _serve_script([("ok", _OK_BODY)])
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
        assert handler_cls.request_count == 1
