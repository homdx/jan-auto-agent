r"""tests/test_harden23_truncated_stream.py — HARDEN-23: a streaming reply cut
by a dropped connection is returned as complete; an IncompleteRead on a
blocking reply is never retried; mask_api_key is quadratic on a long line.

Three defects, one ticket:

A — a dropped stream is a complete reply. A chunked stream that sends content
   then drops the connection (or ends cleanly without [DONE] and with no
   finish_reason) is returned as-is: no exception, no retry, in 0.0 s even
   with error_retries set. Gate 1 reads the bare finish_reason=None like "stop".
   The fix detects the truncation: it is retried like a dropped connection when
   no token reached on_token, and raises a RuntimeError naming the provider
   once one did — the same shape HARDEN-11 B already uses for error events.

B — a truncated blocking body is never retried. The blocking read's except
   tuple missed http.client.HTTPException, so an IncompleteRead (Content-Length
   larger than the body, or a chunked body cut after one chunk) propagated raw
   with no provider name and no retry. HTTPException now joins the tuple in the
   blocking read, the streaming read and _open().

C — mask_api_key is quadratic on a long line. The key-name patterns were not
   anchored at the start position the old `^` ones were, so every start position
   scanned the line to its end. The patterns now carry a (?<![\w.-]) (or
   (?<![\w$.]) for env writes) lookbehind that prevents the attempt from
   running inside a word run, restoring linear behaviour.

No live network access: streaming A/B tests fake urllib.request.urlopen with
scripted bodies (the same pattern as test_harden11_stream_error_event.py);
mask C tests call mask_api_key directly.
"""

from __future__ import annotations

import http.client
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.llm_stream as L  # noqa: E402
from tools.llm_stream import mask_api_key, request_completion  # noqa: E402

URL = "http://stub.local:1337/v1/chat/completions"
HEADERS = {"Content-Type": "application/json"}


def _payload(**over) -> dict:
    p = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}],
         "temperature": 0.0, "max_tokens": 4096}
    p.update(over)
    return p


def _chunk(delta: dict, finish_reason=None) -> dict:
    return {"id": "chatcmpl-x", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


def _sse(chunks) -> bytes:
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


def _sse_no_done(chunks) -> bytes:
    """Like _sse but with no trailing [DONE] — the stream just ends."""
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)


class _Response:
    """A minimal HTTPResponse stand-in that iterates lines and can read()."""

    def __init__(self, body: bytes, *, raises_on_read=None,
                 raises_on_iter_after=0, raises_on_iter=None):
        self._body = body
        self._lines = body.splitlines(keepends=True)
        self.status = 200
        self._raises_on_read = raises_on_read
        self._raises_on_iter = raises_on_iter
        self._iter_i = 0
        self._stop_after = raises_on_iter_after

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return self

    def __next__(self):
        if self._raises_on_iter is not None and self._iter_i == self._stop_after:
            exc = self._raises_on_iter
            self._raises_on_iter = None
            raise exc
        if self._iter_i >= len(self._lines):
            raise StopIteration
        line = self._lines[self._iter_i]
        self._iter_i += 1
        return line

    def read(self):
        if self._raises_on_read is not None:
            raise self._raises_on_read
        return self._body

    def read1(self, n=-1):
        if self._raises_on_read is not None:
            raise self._raises_on_read
        return self._body[:n] if n > 0 else self._body


class FakeUrlopen:
    """Answers each request with the next scripted body, and records the
    requests it was sent, so a retry is visible as a second call."""

    def __init__(self, *bodies_or_responses):
        self.responses = list(bodies_or_responses)
        self.sent: list[dict] = []

    def __call__(self, req, timeout=None, context=None):
        self.sent.append(json.loads(req.data.decode("utf-8")))
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


@pytest.fixture
def fake(monkeypatch):
    def _install(*bodies_or_responses):
        f = FakeUrlopen(*bodies_or_responses)
        monkeypatch.setattr(urllib.request, "urlopen", f)
        return f
    return _install


# ═══════════════════════════════════════════════════════════════════════════
# Part A — a dropped stream is a complete reply
# ═══════════════════════════════════════════════════════════════════════════

class TestADroppedStreamIsNotReturnedAsComplete:

    def test_clean_close_without_marker_after_tokens_raises(self, fake):
        """The ticket's headline: content was emitted, the connection closed
        cleanly with no [DONE] and no finish_reason, and the half reply was
        returned as a complete answer."""
        fake(_Response(_sse_no_done([
            _chunk({"content": "The answer is: "}),
            _chunk({"content": '{"verdict": "TR'}),
        ])))
        with pytest.raises(RuntimeError) as exc:
            request_completion(URL, HEADERS, _payload(), 10, stream=True,
                               error_retries=0)
        msg = str(exc.value)
        assert "mid-reply" in msg
        assert "no finish_reason" in msg
        assert "tokens already emitted" in msg

    def test_clean_close_without_marker_before_tokens_retries(self, fake):
        """No token reached on_token: retried like a dropped connection."""
        ok = _sse([_chunk({"role": "assistant"}), _chunk({"content": "ok"}, "stop")])
        f = fake(_Response(_sse_no_done([])), _Response(ok))
        waits: list[float] = []
        text = request_completion(URL, HEADERS, _payload(), 10, stream=True,
                                  _sleep_fn=waits.append)
        assert text == "ok"
        assert len(f.sent) == 2
        assert waits == [L.DEFAULT_ERROR_RETRY_WAIT_SEC]

    def test_clean_close_without_marker_before_tokens_exhausted_raises(self, fake):
        """Retry budget spent on empty, no-marker streams: RuntimeError naming
        the provider and the attempts."""
        fake(_Response(_sse_no_done([])))
        with pytest.raises(RuntimeError) as exc:
            request_completion(URL, HEADERS, _payload(), 10, stream=True,
                               error_retries=2, error_retry_wait_sec=0.0,
                               _sleep_fn=lambda w: None)
        msg = str(exc.value)
        assert URL in msg
        assert "after 2 retries" in msg

    def test_drop_mid_stream_after_tokens_raises(self, fake):
        """The server sends content then drops the connection (ConnectionError
        mid-iteration). The existing except handler already raises on a
        dropped stream when parts is non-empty; this pins it."""
        body = _sse_no_done([_chunk({"content": "def f():\n"}),
                             _chunk({"content": " return 1"})])
        # Yield the two data lines (4 splitlines entries: 2 data + 2 blank), then drop.
        f = fake(_Response(body, raises_on_iter=ConnectionError("connection reset"),
                           raises_on_iter_after=4))
        with pytest.raises(RuntimeError) as exc:
            request_completion(URL, HEADERS, _payload(), 10, stream=True,
                               error_retries=5, _sleep_fn=lambda w: None)
        msg = str(exc.value)
        assert "return 1" not in msg
        assert "ConnectionError" in msg
        assert "tokens already emitted" in msg
        assert len(f.sent) == 1

    def test_drop_mid_stream_before_tokens_retries(self, fake):
        """Connection drops before any token: retried."""
        ok = _sse([_chunk({"role": "assistant"}), _chunk({"content": "ok"}, "stop")])
        f = fake(_Response(_sse_no_done([]), raises_on_iter=ConnectionError("reset")),
                 _Response(ok))
        text = request_completion(URL, HEADERS, _payload(), 10, stream=True,
                                  _sleep_fn=lambda w: None)
        assert text == "ok"
        assert len(f.sent) == 2


class TestTheGoodShapesReturn:
    """Shapes the ticket says must still be accepted."""

    def test_final_finish_reason_stop_without_DONE_returns(self, fake):
        """A final finish_reason: "stop" with no [DONE] is a clean end."""
        fake(_Response(_sse_no_done([_chunk({"content": "hi"}, "stop")])))
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == "hi"

    def test_final_finish_reason_length_without_DONE_returns(self, fake):
        """finish_reason: "length" — the exhaustion signal — without [DONE]."""
        fake(_Response(_sse_no_done([_chunk({"content": "hi"}, "length")])))
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == "hi"

    def test_usage_only_final_chunk_without_DONE_returns(self, fake):
        """OpenAI include_usage sends a trailing chunk with empty choices and
        a usage object but no finish_reason and no [DONE] — accepted."""
        body = _sse_no_done([
            _chunk({"role": "assistant"}),
            _chunk({"content": "ok"}, "stop"),
            {"choices": [], "usage": {"completion_tokens": 7}},
        ])
        fake(_Response(body))
        text, meta = L.request_completion_ex(
            URL, HEADERS, _payload(), 10, stream=True)
        assert text == "ok"
        assert meta.completion_tokens == 7

    def test_keep_alive_comment_lines_are_ignored(self, fake):
        """A SSE comment line (": ping") and a non-data line are skipped,
        not mistaken for the end of the stream."""
        body = (b": ping\n\n" + b"x-foo: bar\n\n"
                + _sse([_chunk({"content": "ok"}, "stop")]))
        fake(_Response(body))
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == "ok"

    def test_crlf_endings_with_done_return(self, fake):
        """CRLF line endings are handled by strip() and don't hide [DONE]."""
        body = _sse([_chunk({"content": "ok"}, "stop")]).replace(b"\n", b"\r\n")
        fake(_Response(body))
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == "ok"

    def test_reasoning_then_content_with_stop_returns(self, fake):
        """A reasoning delta followed by content delta, ending with
        finish_reason: stop — accepted without [DONE]."""
        body = _sse_no_done([
            _chunk({"reasoning_content": "thinking..."}),
            _chunk({"content": "answer"}),
            _chunk({}, "stop"),
        ])
        fake(_Response(body))
        text, meta = L.request_completion_ex(
            URL, HEADERS, _payload(), 10, stream=True)
        assert text == "answer"
        assert meta.reasoning_chars == len("thinking...")
        assert meta.finish_reason == "stop"

    def test_a_normal_stream_with_done_is_unchanged(self, fake):
        """The everyday case: [DONE] terminates the stream."""
        fake(_Response(_sse([_chunk({"role": "assistant"}),
                             _chunk({"content": "ok"}, "stop")])))
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == "ok"

    def test_a_role_only_stream_without_done_is_retried(self, fake):
        """An empty stream with no [DONE] and no finish_reason — nothing was
        emitted — is a truncation that is retried."""
        ok = _sse([_chunk({"role": "assistant"}), _chunk({}, "stop")])
        f = fake(_Response(_sse_no_done([_chunk({"role": "assistant"})])),
                 _Response(ok))
        text = request_completion(URL, HEADERS, _payload(), 10, stream=True,
                                  _sleep_fn=lambda w: None)
        assert text == ""
        assert len(f.sent) == 2


# ═══════════════════════════════════════════════════════════════════════════
# Part B — a truncated blocking body (IncompleteRead) is retried and named
# ═══════════════════════════════════════════════════════════════════════════

class TestIncompleteReadOnBlockingReply:

    def test_content_length_mismatch_is_retried(self, fake):
        """Server sends Content-Length: 92 but only 40 bytes. The blocking
        read raises IncompleteRead — an HTTPException — which is now caught
        and retried."""
        truncated = _Response(b'{"choices":[{"message":{"content":"ok"}}]}',
                              raises_on_read=http.client.IncompleteRead(
                                  b'{"choices":[{"message":{"content":"ok"}}]}',
                                  52))
        good = _Response(json.dumps(
            {"choices": [{"message": {"content": "ok"},
                           "finish_reason": "stop"}]}).encode())
        f = fake(truncated, good)
        text = request_completion(URL, HEADERS, _payload(), 10, stream=False,
                                  _sleep_fn=lambda w: None)
        assert text == "ok"
        assert len(f.sent) == 2

    def test_content_length_mismatch_exhausted_names_provider_and_attempts(self, fake):
        """Retries exhausted: the final error names the provider and the
        attempt count, the shape the other branches already use."""
        f = fake(_Response(b'{"choices":[]}',
                           raises_on_read=http.client.IncompleteRead(b'{"x":1', 5)))
        with pytest.raises(RuntimeError) as exc:
            request_completion(URL, HEADERS, _payload(), 10, stream=False,
                               error_retries=2, error_retry_wait_sec=0.0,
                               _sleep_fn=lambda w: None)
        msg = str(exc.value)
        assert URL in msg
        assert "IncompleteRead" in msg
        assert "after 2 retries" in msg
        assert len(f.sent) == 3

    def test_incomplete_read_on_streaming_is_retried(self, fake):
        """An IncompleteRead during iteration (not just read()) is also caught:
        the streaming loop's except now includes HTTPException."""
        ok = _sse([_chunk({"content": "ok"}, "stop")])
        dropped_body = _sse_no_done([_chunk({"content": "x"})])
        dropped = _Response(dropped_body,
                            raises_on_iter=http.client.IncompleteRead(b"x", 10))
        f = fake(dropped, _Response(ok))
        text = request_completion(URL, HEADERS, _payload(), 10, stream=True,
                                  _sleep_fn=lambda w: None)
        assert text == "ok"
        assert len(f.sent) == 2

    def test_incomplete_read_after_tokens_in_stream_raises(self, fake):
        """Tokens already emitted — IncompleteRead in the streaming loop
        raises, doesn't silently return the half reply."""
        body = _sse_no_done([_chunk({"content": "half"})])
        # splitlines gives [data:..., \n]; raise after the data line so "half"
        # is already in parts when the drop happens.
        f = fake(_Response(body,
                           raises_on_iter=http.client.IncompleteRead(b"", 4),
                           raises_on_iter_after=1))
        with pytest.raises(RuntimeError) as exc:
            request_completion(URL, HEADERS, _payload(), 10, stream=True,
                               error_retries=5, _sleep_fn=lambda w: None)
        msg = str(exc.value)
        assert "IncompleteRead" in msg
        assert "tokens already emitted" in msg
        assert len(f.sent) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Part C — mask_api_key is linear, not quadratic, on a long line
# ═══════════════════════════════════════════════════════════════════════════

class TestMaskApiKeyIsLinearOnALongLine:

    _BOUND_SEC = 1.0

    @pytest.mark.parametrize("name,make", [
        ("word", lambda: "a" * 20000),
        ("hex", lambda: ("0123456789abcdef"[:16] + "X" + "Y") * 1176),
        ("dash", lambda: "ab-" * 6666),
    ])
    def test_three_slow_shapes_mask_under_a_second(self, name, make):
        text = make()
        t0 = time.monotonic()
        mask_api_key(text)
        elapsed = time.monotonic() - t0
        assert elapsed < self._BOUND_SEC, f"{name} took {elapsed:.3f}s"

    @pytest.mark.parametrize("name,make", [
        ("word", lambda: "a" * 20000),
        ("hex", lambda: ("0123456789abcdef"[:16] + "X" + "Y") * 1176),
        ("dash", lambda: "ab-" * 6666),
    ])
    def test_baseline_is_also_under_a_second(self, name, make):
        """The masking pass is fast on its own; this guards against the
        assertion being vacuous on a faster machine."""
        text = make()
        t0 = time.monotonic()
        for _ in range(5):
            mask_api_key(text)
        elapsed = time.monotonic() - t0
        assert elapsed < 5 * self._BOUND_SEC

    def test_representative_lines_are_unchanged(self):
        lines = [
            "api_key = test_or_key",
            'api_key = "sk-real-secret-999"',
            "API_KEY = something",
            "    api_key = indented_secret",
            'export OPENAI_API_KEY=sk-secret\n',
            '"api_key": "sk-secret"',
            "api_key: sk-abc123",
            "# api_key = secret",
            "  - api_key: gh_token_here",
            'os.environ["OPENAI_API_KEY"] = "gsk_xxx"',
            "os.environ['OPENAI_API_KEY'] = 'gsk_xxx'",
            "os.environ.setdefault('OPENAI_API_KEY', 'default')",
            "process.env.OPENAI_API_KEY = 'gsk_xxx'",
            "os.putenv('OPENAI_API_KEY', 'gsk_xxx')",
            "client_api_key = key",
            "self.api_key = api_key",
            "self.api_key = None",
            "api_key = config.api_key",
            '<api_key>gsk_xxx</api_key>',
            '{"api_key": "sk-secret"}',
            "Authorization: Bearer sk-or-v1-AbC123XyZ9abcDEF",
            "x-api-key: ghp_abcdefghijklmnopqrstuvwxyz12",
            "key = AIzaSyB1234567890abcdefghijklmnopqrstuv",
            "token = xoxb-1234567890-abcdef",
            "secret = eyJhbGci.eyJzdWIi.SflKxw",
            "api_key = your_key",
            "OPENAI_API_KEY = DEFAULT_API_KEY",
            "model = gpt-4",
            "base_url = http://localhost:11434",
            "not_api_key_at_all = foo",
            "the api_key is read here",
            'api_key = "apiKey"',
            "assert client.api_key == key",
            "api_key = 30",
            "api_key = test_or_key  # prod",
            "api_key => 'gsk_xxx'",
            "api_key >= 'sk-abc'",
        ]
        for line in lines:
            once = mask_api_key(line)
            # idempotent: masking the masked form is a no-op
            twice = mask_api_key(once)
            assert twice == once, f"not idempotent for {line!r}: {once!r} -> {twice!r}"
