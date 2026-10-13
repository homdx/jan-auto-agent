"""tests/test_llm_stream_deadline_flow.py — request_completion's deadline bounds a reply that keeps coming (review batch m).

№66: the deadline only shortened the connect and the per-read timeouts. A stream kept open with keep-alive
comments, a slow token stream and a body trickled a few bytes at a time each ran past it — the Gate 1 reader's
`presence_deadline_sec` ("the bound on this whole check") did not hold. №67: a 429 whose wait outlasts the
deadline logged "waiting 100.0s and retrying", then refused at once.

Servers are bound on port 0 in this process (no xdist group needed). Each would run 30 s without the fix; the
bound asserted is far below that, so a loaded machine does not make it a flake.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.llm_stream as llm_stream

RUNS = 30.0           # how long each server keeps going without the fix
PAYLOAD = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


def _serve(handler):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                handler(self)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions"


def _stream_head(h):
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Connection", "close")
    h.end_headers()


def _keepalive(h):
    _stream_head(h)
    end = time.monotonic() + RUNS
    while time.monotonic() < end:
        h.wfile.write(b": keep-alive\n\n")
        h.wfile.flush()
        time.sleep(0.2)


def _slow_tokens(h):
    _stream_head(h)
    end = time.monotonic() + RUNS
    while time.monotonic() < end:
        h.wfile.write(b'data: {"choices": [{"delta": {"content": "x"}}]}\n\n')
        h.wfile.flush()
        time.sleep(0.2)


def _trickled_body(h):
    body = json.dumps({"choices": [{"message": {"content": "ok " * 200}, "finish_reason": "stop"}]}).encode()
    h.send_response(200)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    step = max(1, len(body) // int(RUNS / 0.2))
    for i in range(0, len(body), step):
        h.wfile.write(body[i:i + step])
        h.wfile.flush()
        time.sleep(0.2)


@pytest.mark.parametrize("handler, stream, what", [
    (_keepalive, True, "the stream"),
    (_slow_tokens, True, "the stream"),
    (_trickled_body, False, "the response body"),
], ids=["keep-alive-comments", "slow-token-stream", "trickled-body"])
def test_a_reply_that_keeps_coming_stops_at_the_deadline(handler, stream, what):
    srv, url = _serve(handler)
    try:
        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match=f"deadline reached reading {what}"):
            llm_stream.request_completion(url, {"Content-Type": "application/json"}, PAYLOAD, 10, stream=stream,
                                          error_retries=0, deadline=time.monotonic() + 0.5)
        assert time.monotonic() - t0 < RUNS / 3
    finally:
        srv.shutdown()
        srv.server_close()


def test_without_a_deadline_the_body_is_read_whole():
    """No deadline: the old path (one read), the reply complete."""
    body = json.dumps({"choices": [{"message": {"content": "fine"}, "finish_reason": "stop"}]}).encode()

    def quick(h):
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    srv, url = _serve(quick)
    try:
        assert llm_stream.request_completion(url, {}, PAYLOAD, 10, error_retries=0) == "fine"
        assert llm_stream.request_completion(url, {}, PAYLOAD, 10, error_retries=0,
                                             deadline=time.monotonic() + 10) == "fine"
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_429_whose_wait_outlasts_the_deadline_does_not_log_a_wait(caplog):
    def limited(h):
        out = b'{"error": "slow down"}'
        h.send_response(429)
        h.send_header("Retry-After", "100")
        h.send_header("Content-Length", str(len(out)))
        h.end_headers()
        h.wfile.write(out)

    srv, url = _serve(limited)
    retries = []
    try:
        with caplog.at_level(logging.WARNING, logger=llm_stream.logger.name):
            with pytest.raises(RuntimeError, match="deadline reached calling .*not retrying"):
                llm_stream.request_completion(url, {}, PAYLOAD, 10, error_retries=3, max_retry_after_sec=300,
                                              on_retry=retries.append, deadline=time.monotonic() + 3)
    finally:
        srv.shutdown()
        srv.server_close()
    assert "waiting 100.0s" not in caplog.text and not retries
