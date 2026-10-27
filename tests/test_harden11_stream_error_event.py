"""tests/test_harden11_stream_error_event.py — HARDEN-11 B and C: an error
event on a live stream fails the call, and one bad byte costs one line.

Story
-----
The SSE loop of ``request_completion`` stepped over a chunk carrying a
top-level ``error`` object and took a final ``finish_reason: "error"`` for an
ordinary finish. A provider that died after two tokens returned
``'def f():\\n return 1'`` — the half reply — as a success, and one that died
before any token returned ``''``. Neither raised, so the retry ladder and the
429 logic never ran and every caller (the Coder, the summariser, the search
agent, the validator) took a truncated reply for the model's answer.

Each stream line was also decoded strictly, inside a ``try`` that caught only
network errors: one byte that was not UTF-8 lost the whole reply — the tokens
already read included — with a raw ``UnicodeDecodeError`` no caller expects.

What this file pins
-------------------
* an ``error`` object (or a final ``finish_reason: "error"``) is this call
  failing: ``RuntimeError`` naming the error message once tokens were emitted;
  a retry on the same budget, a 429 code like a 429 status, while none were;
* the wait a 429's own message asks for is honoured, a longer one than the cap
  is not slept through, and a non-429 code waits the fixed backoff;
* one line that is not valid UTF-8 is decoded with replacement characters: the
  tokens on both sides of it are kept, a bad byte inside a token keeps the token
  with U+FFFD, a BOM on the first line still works, and the fault is logged once;
* a normal stream, ``finish_reason: "length"``, an empty ``choices`` list and a
  role-only stream behave exactly as before.

Every test fakes ``urllib.request.urlopen`` — the one layer every call goes
through — with a scripted SSE body; nothing here opens a socket.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.llm_stream as L  # noqa: E402
from tools.llm_stream import request_completion  # noqa: E402

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


def _error_chunk(message: str, code=None) -> dict:
    err = {"message": message}
    if code is not None:
        err["code"] = code
    return {"error": err}


def _sse(chunks) -> bytes:
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


def _sse_raw(*parts: bytes) -> bytes:
    """A stream whose events are exactly *parts*, one per line-pair — used for
    the lines that are not valid UTF-8."""
    return b"".join(p if p.endswith(b"\n\n") else p + b"\n\n" for p in parts)


class _Response:
    def __init__(self, body: bytes):
        self._body = body
        self._lines = body.splitlines(keepends=True)
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return self._body


class FakeUrlopen:
    """Answers each request with the next scripted body, and records the bodies
    it was sent, so a retry is visible as a second request."""

    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.sent: list[dict] = []

    def __call__(self, req, timeout=None, context=None):
        self.sent.append(json.loads(req.data.decode("utf-8")))
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        return _Response(body)


@pytest.fixture
def fake(monkeypatch):
    def _install(*bodies):
        f = FakeUrlopen(*bodies)
        monkeypatch.setattr(urllib.request, "urlopen", f)
        return f
    return _install


# The streams of the ticket's own table: the half reply, and the empty one.
HALF_REPLY = _sse([
    _chunk({"role": "assistant"}),
    _chunk({"content": "def f():\n"}),
    _chunk({"content": " return 1"}),
    _error_chunk("upstream died", 502),
    _chunk({}, "error"),
])
ERROR_ONLY = _sse([_error_chunk("rate limited", 429)])
NORMAL = _sse([_chunk({"role": "assistant"}), _chunk({"content": "ok"}, "stop")])


# ── HARDEN-11 B: an error event is a failure of the call ────────────────────

class TestAnErrorEventIsAFailureOfTheCall:

    def test_a_mid_stream_error_after_tokens_raises_and_names_the_message(self, fake):
        """The table's first row: two tokens, an error object, then
        `finish_reason: "error"` — used to return the half reply as a success."""
        fake(HALF_REPLY)
        with pytest.raises(RuntimeError) as exc:
            request_completion(URL, HEADERS, _payload(), 10, stream=True,
                               error_retries=0)
        message = str(exc.value)
        assert "upstream died" in message
        assert "def f()" not in message
        assert not isinstance(exc.value, UnicodeDecodeError)

    def test_the_half_reply_is_never_returned_as_a_success(self, fake):
        fake(HALF_REPLY)
        tokens: list[str] = []
        with pytest.raises(RuntimeError):
            request_completion(URL, HEADERS, _payload(), 10, stream=True,
                               on_token=tokens.append, error_retries=0)
        # the tokens were passed on as they arrived — the caller saw the answer
        # being typed — but the call itself did not report success
        assert tokens == ["def f():\n", " return 1"]

    def test_an_error_only_stream_raises_instead_of_an_empty_reply(self, fake):
        """The table's second row: used to return `''`, which every caller read
        as the model's answer (or as a degraded provider) instead of a failure."""
        fake(ERROR_ONLY)
        with pytest.raises(RuntimeError) as exc:
            request_completion(URL, HEADERS, _payload(), 10, stream=True,
                               error_retries=0)
        assert "rate limited" in str(exc.value)

    def test_a_final_finish_reason_of_error_is_a_failure(self, fake):
        fake(_sse([_chunk({}, "error")]))
        with pytest.raises(RuntimeError) as exc:
            request_completion(URL, HEADERS, _payload(), 10, stream=True,
                               error_retries=0)
        assert "finish_reason" in str(exc.value)

    def test_a_final_finish_reason_of_error_after_tokens_raises(self, fake):
        fake(_sse([_chunk({"content": "half"}, "error")]))
        with pytest.raises(RuntimeError) as exc:
            request_completion(URL, HEADERS, _payload(), 10, stream=True,
                               error_retries=0)
        assert "finish_reason" in str(exc.value)


class TestAnErrorEventWithNoTokenYetIsRetried:

    def test_it_is_retried_like_a_dropped_connection(self, fake):
        f = fake(ERROR_ONLY, NORMAL)
        seen: list[str] = []
        waits: list[float] = []
        text = request_completion(URL, HEADERS, _payload(), 10, stream=True,
                                  _sleep_fn=waits.append, on_retry=seen.append)
        assert text == "ok"
        assert len(f.sent) == 2, "the request was made again"
        assert any("rate limited" in line for line in seen)
        assert waits == [L.DEFAULT_ERROR_RETRY_WAIT_SEC]

    def test_a_429_code_waits_the_wait_its_own_message_asks_for(self, fake):
        """A 429 on a stream has no HTTP headers to carry a Retry-After, so the
        "try again in Ns" in the error message is the only hint there is."""
        f = fake(_sse([_error_chunk("rate limited, try again in 0.25s", 429)]),
                 NORMAL)
        waits: list[float] = []
        text = request_completion(URL, HEADERS, _payload(), 10, stream=True,
                                  _sleep_fn=waits.append)
        assert text == "ok" and len(f.sent) == 2
        assert waits == [0.25]

    def test_a_non_429_code_waits_the_fixed_backoff(self, fake):
        f = fake(_sse([_error_chunk("upstream died, try again in 0.25s", 502)]),
                 NORMAL)
        waits: list[float] = []
        text = request_completion(URL, HEADERS, _payload(), 10, stream=True,
                                  _sleep_fn=waits.append, error_retries=1)
        assert text == "ok" and waits == [L.DEFAULT_ERROR_RETRY_WAIT_SEC]

    def test_a_wait_longer_than_the_cap_is_not_slept_through(self, fake):
        """A 429 that asks for an hour is a quota reset, not a rate limit: the
        call fails at once rather than holding the run (AUTO-RATE-1)."""
        f = fake(_sse([_error_chunk("quota spent, try again in 3600s", 429)]))
        waits: list[float] = []
        with pytest.raises(RuntimeError):
            request_completion(URL, HEADERS, _payload(), 10, stream=True,
                               _sleep_fn=waits.append, on_retry=lambda m: None,
                               max_retry_after_sec=180.0)
        assert waits == [] and len(f.sent) == 1

    def test_the_retry_budget_is_spent_and_the_call_reports_it(self, fake):
        fake(ERROR_ONLY)
        waits: list[float] = []
        with pytest.raises(RuntimeError) as exc:
            request_completion(URL, HEADERS, _payload(), 10, stream=True,
                               _sleep_fn=waits.append, error_retries=2,
                               error_retry_wait_sec=0.0)
        assert len(waits) == 2
        assert "after 2 retries" in str(exc.value) and "rate limited" in str(exc.value)

    def test_the_retry_budget_is_not_spent_after_tokens_were_emitted(self, fake):
        """A half reply is not retried into a paid second call — it is reported."""
        f = fake(HALF_REPLY, NORMAL)
        with pytest.raises(RuntimeError):
            request_completion(URL, HEADERS, _payload(), 10, stream=True,
                               _sleep_fn=lambda w: None, error_retries=5)
        assert len(f.sent) == 1


# ── HARDEN-11 B: what is unchanged ──────────────────────────────────────────

class TestTheStreamsThatAreNotErrors:

    def test_a_normal_stream_is_unchanged(self, fake):
        fake(NORMAL)
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == "ok"

    def test_a_finish_reason_of_length_is_unchanged(self, fake):
        fake(_sse([_chunk({"role": "assistant"}), _chunk({"content": "hi"}, "length")]))
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == "hi"

    def test_a_finish_reason_of_stop_is_unchanged(self, fake):
        fake(_sse([_chunk({"content": "hi"}, "stop")]))
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == "hi"

    def test_an_empty_choices_chunk_is_still_skipped(self, fake):
        """The usage chunk of `include_usage`, and any other choice-less chunk:
        skipped, not an error and not a lost reply."""
        fake(_sse([{"choices": [], "usage": {"completion_tokens": 7}},
                   _chunk({"content": "ok"}, "stop")]))
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == "ok"

    def test_a_role_only_stream_is_still_an_empty_reply(self, fake):
        fake(_sse([_chunk({"role": "assistant"}), _chunk({}, "stop")]))
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == ""

    def test_an_error_field_that_is_not_an_error_is_not_a_failure(self, fake):
        """`{"error": null}` is a provider's empty placeholder field."""
        fake(_sse([_chunk({"content": "ok"}, "stop"), {"error": None}]))
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == "ok"


# ── HARDEN-11 C: one bad byte costs one line ────────────────────────────────

class TestOneBadByteCostsOneLine:

    def test_the_reply_around_the_bad_byte_survives(self, fake):
        body = _sse_raw(b"data: " + json.dumps(_chunk({"content": "ok"})).encode(),
                        b"data: \xff\xfe bad",
                        b"data: [DONE]")
        fake(body)
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == "ok"

    def test_the_tokens_on_both_sides_of_the_bad_byte_are_kept(self, fake):
        body = _sse_raw(b"data: " + json.dumps(_chunk({"content": "a"})).encode(),
                        b"data: \xff\xfe bad",
                        b"data: " + json.dumps(_chunk({"content": "b"}, "stop")).encode())
        fake(body)
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == "ab"

    def test_a_bad_byte_inside_a_token_keeps_the_token_with_a_replacement(self, fake):
        body = _sse_raw(
            b"data: " + json.dumps(_chunk({"content": "ok"})).encode() + b"\n\n",
            b'data: {"choices":[{"delta":{"content":"to\xffken"}}]}',
            b"data: [DONE]")
        fake(body)
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == "okto\ufffdken"

    def test_a_bom_on_the_first_line_still_works(self, fake):
        fake(b"\xef\xbb\xbf" + _sse([_chunk({"content": "ok"}, "stop")]))
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == "ok"

    def test_the_fault_is_logged_once_per_call(self, fake, caplog):
        body = _sse_raw(b"data: \xff bad", b"data: \xfe worse",
                        b"data: " + json.dumps(_chunk({"content": "ok"}, "stop")).encode())
        fake(body)
        with caplog.at_level(logging.WARNING, logger="tools.llm_stream"):
            assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == "ok"
        logged = [r for r in caplog.records
                  if "not valid UTF-8" in r.getMessage()]
        assert len(logged) == 1, "one warning, not one per damaged line"

    def test_a_blocking_reply_still_retries_a_garbled_body(self, fake):
        """The blocking branch keeps its own behaviour: a body that will not
        decode is retried and reported as the RuntimeError callers expect."""
        f = fake(b"\xff\xfe not json", json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "ok"},
                          "finish_reason": "stop"}]}).encode())
        waits: list[float] = []
        text = request_completion(URL, HEADERS, _payload(), 10, stream=False,
                                  _sleep_fn=waits.append)
        assert text == "ok" and len(f.sent) == 2
