"""tests/test_llm_stream_completion_meta.py — RUN-9: stream metadata reaches
the caller.

Story background
----------------
Gate 1's presence check against ``sensenova-6.7-flash-lite`` (``../testtext``,
``../testtext6``, 2026-09-16) got 408 and 274 replies with ``raw=''``. The SSE
branch of ``request_completion`` collected ``delta.content`` and nothing
else — ``finish_reason``, ``usage`` and ``reasoning_content`` were dropped —
so a reply that spent its whole 32k budget thinking and a reply from a
degraded gateway (HTTP 200, a role-only chunk, single-digit tokens, 20–60 s)
were the same empty string, and the re-ask ladder treated both as a garbled
verdict.

What this file pins down
------------------------
* ``CompletionMeta`` — ``finish_reason`` (last non-null seen; Ollama
  ``done_reason``), ``completion_tokens`` from the usage chunk when the
  provider sends one (else ``None``; Ollama ``eval_count``), the number of
  reasoning characters seen (``reasoning_content``, OpenRouter's
  ``reasoning``, Ollama's ``thinking``), the number of content-bearing
  chunks and wall-clock ``elapsed``.
* Exposure without breaking the ``str`` return used everywhere:
  ``request_completion(..., on_meta=cb)`` and ``request_completion_ex(...)``
  → ``(text, CompletionMeta)``; the plain return value is byte-identical.
* ``build_chat_request(stream=True)`` sends ``stream_options:
  {"include_usage": true}`` on the openai branch, behind the same per-
  ``(url, model)`` "HTTP 400 — stop sending it" memory AUTO-JSONMODE-1 uses
  for ``response_format``; never on a blocking request, never to Ollama.

Every test fakes ``urllib.request.urlopen`` — the one layer every call goes
through — with a scripted SSE / NDJSON body; nothing here opens a socket.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.llm_stream as L  # noqa: E402
from tools.llm_stream import (  # noqa: E402
    CompletionMeta,
    build_chat_request,
    mark_stream_options_unsupported,
    request_completion,
    request_completion_ex,
    stream_options_is_supported,
)

URL = "http://stub.local:1337/v1/chat/completions"
OLLAMA_URL = "http://stub.local:11434/api/chat"
HEADERS = {"Content-Type": "application/json"}


# ─────────────────────────────────────────────────────────────────────────────
# Fake transport
# ─────────────────────────────────────────────────────────────────────────────

def _payload(**over) -> dict:
    p = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}],
         "temperature": 0.0, "max_tokens": 4096}
    p.update(over)
    return p


def _chunk(delta: dict, finish_reason=None) -> dict:
    return {"id": "chatcmpl-x", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


def _usage_chunk(completion_tokens: int) -> dict:
    return {"id": "chatcmpl-x", "object": "chat.completion.chunk", "choices": [],
            "usage": {"prompt_tokens": 100, "completion_tokens": completion_tokens,
                      "total_tokens": 100 + completion_tokens}}


def _sse(chunks) -> bytes:
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


def _ndjson(lines) -> bytes:
    return b"".join(json.dumps(l).encode() + b"\n" for l in lines)


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
    """Answers each request with the next scripted body; records the bodies
    it was sent. A `(status, text)` tuple in the script raises HTTPError."""

    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.sent: list[dict] = []

    def __call__(self, req, timeout=None, context=None):
        self.sent.append(json.loads(req.data.decode("utf-8")))
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        if isinstance(body, tuple):
            status, text = body
            raise urllib.error.HTTPError(req.full_url, status, f"HTTP {status}", {},
                                         io.BytesIO(text.encode()))
        return _Response(body)


@pytest.fixture
def fake(monkeypatch):
    def _install(*bodies):
        f = FakeUrlopen(*bodies)
        monkeypatch.setattr(urllib.request, "urlopen", f)
        return f
    return _install


@pytest.fixture(autouse=True)
def _clean_stream_options_memory():
    L._STREAM_OPTIONS_UNSUPPORTED_KEYS.clear()
    yield
    L._STREAM_OPTIONS_UNSUPPORTED_KEYS.clear()


ROLE_ONLY = _sse([_chunk({"role": "assistant"}), _chunk({}, "stop"), _usage_chunk(0)])
NORMAL = _sse([_chunk({"role": "assistant"}), _chunk({"content": '{"verdict": '}),
               _chunk({"content": '"rejected"}'}), _chunk({}, "stop"), _usage_chunk(12)])
REASONING_ONLY = _sse([_chunk({"role": "assistant"}), _chunk({"reasoning_content": "let me think "}),
                       _chunk({"reasoning_content": "about it"}), _chunk({}, "length"), _usage_chunk(4096)])


# ─────────────────────────────────────────────────────────────────────────────
# AC-1: the plain return value is byte-identical
# ─────────────────────────────────────────────────────────────────────────────

class TestPlainReturnUnchanged:

    def test_normal_stream_returns_the_text(self, fake):
        fake(NORMAL)
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == '{"verdict": "rejected"}'

    def test_role_only_stream_returns_empty_string(self, fake):
        fake(ROLE_ONLY)
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == ""

    def test_reasoning_only_stream_returns_empty_string(self, fake):
        fake(REASONING_ONLY)
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True) == ""

    def test_on_token_still_sees_every_content_piece(self, fake):
        fake(NORMAL)
        tokens: list[str] = []
        text = request_completion(URL, HEADERS, _payload(), 10, stream=True, on_token=tokens.append)
        assert "".join(tokens) == text and len(tokens) == 2

    def test_blocking_return_unchanged(self, fake):
        fake(json.dumps({"choices": [{"message": {"role": "assistant", "content": "hello"},
                                      "finish_reason": "stop"}],
                         "usage": {"completion_tokens": 3}}).encode())
        assert request_completion(URL, HEADERS, _payload(), 10, stream=False) == "hello"

    def test_ex_text_equals_plain_text(self, fake):
        f = fake(NORMAL)
        plain = request_completion(URL, HEADERS, _payload(), 10, stream=True)
        f.bodies = [NORMAL]
        text, meta = request_completion_ex(URL, HEADERS, _payload(), 10, stream=True)
        assert text == plain
        assert isinstance(meta, CompletionMeta)


# ─────────────────────────────────────────────────────────────────────────────
# AC-1: what the metadata says
# ─────────────────────────────────────────────────────────────────────────────

def _meta(fake, body, **kw) -> CompletionMeta:
    fake(body)
    seen: list[CompletionMeta] = []
    request_completion(URL, HEADERS, _payload(), 10, stream=True, on_meta=seen.append, **kw)
    assert len(seen) == 1, "on_meta is called exactly once"
    return seen[0]


class TestStreamMeta:

    def test_role_only_chunk_and_done(self, fake):
        m = _meta(fake, ROLE_ONLY)
        assert m.content_chunks == 0
        assert m.finish_reason == "stop"          # as sent
        assert m.completion_tokens == 0           # from the usage chunk
        assert m.reasoning_chars == 0

    def test_bare_role_chunk_no_finish_no_usage(self, fake):
        m = _meta(fake, _sse([_chunk({"role": "assistant"})]))
        assert m.content_chunks == 0
        assert m.finish_reason is None
        assert m.completion_tokens is None        # a legal value

    def test_empty_stream_only_done(self, fake):
        m = _meta(fake, b"data: [DONE]\n\n")
        assert (m.content_chunks, m.finish_reason, m.completion_tokens, m.reasoning_chars) == (0, None, None, 0)

    def test_role_chunk_with_empty_content_string_is_not_a_content_chunk(self, fake):
        m = _meta(fake, _sse([_chunk({"role": "assistant", "content": ""}), _chunk({}, "stop")]))
        assert m.content_chunks == 0

    def test_reasoning_only_deltas(self, fake):
        m = _meta(fake, REASONING_ONLY)
        assert m.content_chunks == 0
        assert m.reasoning_chars == len("let me think about it")
        assert m.finish_reason == "length"
        assert m.completion_tokens == 4096

    def test_openrouter_reasoning_key_counts_too(self, fake):
        m = _meta(fake, _sse([_chunk({"reasoning": "hmm hmm"}), _chunk({}, "stop")]))
        assert m.reasoning_chars == len("hmm hmm")

    def test_request_side_reasoning_object_is_not_counted(self, fake):
        """OpenRouter's request field `reasoning: {...}` is an object; only
        a streamed string counts as reasoning text."""
        m = _meta(fake, _sse([_chunk({"reasoning": {"effort": "low"}}), _chunk({}, "stop")]))
        assert m.reasoning_chars == 0

    def test_normal_reply_counts_content_chunks(self, fake):
        m = _meta(fake, NORMAL)
        assert m.content_chunks == 2
        assert m.finish_reason == "stop"
        assert m.completion_tokens == 12

    def test_finish_reason_on_content_chunk_survives_trailing_usage_chunk(self, fake):
        """The usage chunk has `choices: []` and no finish_reason — it must not
        overwrite the "stop" that came on the last content chunk."""
        m = _meta(fake, _sse([_chunk({"content": "x"}, "stop"), _usage_chunk(7)]))
        assert m.finish_reason == "stop"
        assert m.completion_tokens == 7

    def test_null_finish_reason_does_not_erase_the_last_seen(self, fake):
        m = _meta(fake, _sse([_chunk({}, "length"), _chunk({"content": ""}, None)]))
        assert m.finish_reason == "length"

    def test_usage_inline_on_the_finish_chunk(self, fake):
        chunk = _chunk({}, "length")
        chunk["usage"] = {"completion_tokens": 4096}
        m = _meta(fake, _sse([_chunk({"role": "assistant"}), chunk]))
        assert m.completion_tokens == 4096

    def test_non_numeric_usage_is_none(self, fake):
        m = _meta(fake, _sse([_chunk({}, "stop"), {"choices": [], "usage": {"completion_tokens": "n/a"}}]))
        assert m.completion_tokens is None

    def test_elapsed_is_measured(self, fake, monkeypatch):
        calls = []

        def _monotonic():
            calls.append(1)
            return 100.0 if len(calls) == 1 else 100.25   # first call = _started

        monkeypatch.setattr(L.time, "monotonic", _monotonic)
        m = _meta(fake, ROLE_ONLY)
        assert m.elapsed == pytest.approx(0.25)

    def test_on_meta_exception_is_swallowed(self, fake):
        fake(NORMAL)

        def boom(meta):
            raise RuntimeError("caller bug")

        assert request_completion(URL, HEADERS, _payload(), 10, stream=True, on_meta=boom) == '{"verdict": "rejected"}'

    def test_malformed_chunks_do_not_break_meta(self, fake):
        body = (b'data: {"choices": [{"delta": null, "finish_reason": 5}]}\n\n'
                b'data: {"usage": "x"}\n\n'
                b'data: not json\n\n'
                + _sse([_chunk({"content": "ok"}, "stop")]))
        fake(body)
        seen: list[CompletionMeta] = []
        assert request_completion(URL, HEADERS, _payload(), 10, stream=True, on_meta=seen.append) == "ok"
        assert seen[0].content_chunks == 1 and seen[0].finish_reason == "stop"


class TestBlockingMeta:

    def test_blocking_reply_carries_finish_and_usage(self, fake):
        fake(json.dumps({"choices": [{"message": {"role": "assistant", "content": ""},
                                      "finish_reason": "length"}],
                         "usage": {"completion_tokens": 4096}}).encode())
        seen: list[CompletionMeta] = []
        assert request_completion(URL, HEADERS, _payload(), 10, stream=False, on_meta=seen.append) == ""
        m = seen[0]
        assert (m.finish_reason, m.completion_tokens, m.content_chunks) == ("length", 4096, 0)

    def test_blocking_reply_reasoning_content(self, fake):
        fake(json.dumps({"choices": [{"message": {"role": "assistant", "content": "", "reasoning_content": "abc"},
                                      "finish_reason": "stop"}]}).encode())
        seen: list[CompletionMeta] = []
        request_completion(URL, HEADERS, _payload(), 10, stream=False, on_meta=seen.append)
        assert seen[0].reasoning_chars == 3 and seen[0].completion_tokens is None


class TestOllamaMeta:

    def test_done_reason_and_eval_count(self, fake):
        fake(_ndjson([{"message": {"role": "assistant", "content": "hel"}, "done": False},
                      {"message": {"role": "assistant", "content": "lo"}, "done": False},
                      {"message": {"role": "assistant", "content": ""}, "done": True,
                       "done_reason": "length", "eval_count": 77}]))
        seen: list[CompletionMeta] = []
        text = request_completion(OLLAMA_URL, HEADERS, _payload(), 10, stream=True,
                                  api_format="ollama", on_meta=seen.append)
        assert text == "hello"
        m = seen[0]
        assert (m.finish_reason, m.completion_tokens, m.content_chunks) == ("length", 77, 2)

    def test_ollama_thinking_counts_as_reasoning(self, fake):
        fake(_ndjson([{"message": {"role": "assistant", "content": "", "thinking": "deep"}, "done": False},
                      {"message": {"role": "assistant", "content": ""}, "done": True,
                       "done_reason": "stop", "eval_count": 0}]))
        seen: list[CompletionMeta] = []
        assert request_completion(OLLAMA_URL, HEADERS, _payload(), 10, stream=True,
                                  api_format="ollama", on_meta=seen.append) == ""
        assert seen[0].reasoning_chars == 4 and seen[0].completion_tokens == 0


class TestRequestCompletionEx:

    def test_returns_text_and_meta(self, fake):
        fake(REASONING_ONLY)
        text, meta = request_completion_ex(URL, HEADERS, _payload(), 10, stream=True)
        assert text == "" and meta.reasoning_chars > 0 and meta.finish_reason == "length"

    def test_stubbed_request_completion_is_still_intercepted(self, monkeypatch):
        """A test that stubs `tools.llm_stream.request_completion` (every
        existing Gate-1 test does) still catches calls made through the
        `_ex` wrapper — and gets a default CompletionMeta, never a socket."""
        monkeypatch.setattr(L, "request_completion", lambda *a, **k: "stubbed")
        text, meta = request_completion_ex(URL, HEADERS, _payload(), 10, stream=True)
        assert text == "stubbed"
        assert meta == CompletionMeta()

    def test_caller_on_meta_kwarg_is_replaced_not_forwarded_twice(self, fake):
        fake(NORMAL)
        text, meta = request_completion_ex(URL, HEADERS, _payload(), 10, stream=True, on_meta=lambda m: None)
        assert text and meta.content_chunks == 2


# ─────────────────────────────────────────────────────────────────────────────
# AC-6: stream_options.include_usage and its per-(url, model) memory
# ─────────────────────────────────────────────────────────────────────────────

def _build(**over):
    kw = dict(base_url="http://stub.local:1337/v1", api_key="k", model="test-model", api_format="openai",
              temperature=0.0, max_tokens=64, system="s", user_msg="u", think=False)
    kw.update(over)
    return build_chat_request(**kw)


class TestStreamOptions:

    def test_present_when_stream_true(self):
        _, _, payload = _build(stream=True)
        assert payload["stream_options"] == {"include_usage": True}

    def test_absent_by_default(self):
        _, _, payload = _build()
        assert "stream_options" not in payload

    def test_default_payload_is_byte_identical_to_before(self):
        assert _build() == _build(stream=False)

    def test_never_sent_to_ollama(self):
        _, _, payload = _build(api_format="ollama", base_url="http://stub.local:11434", stream=True)
        assert "stream_options" not in payload

    def test_blocking_request_strips_it_on_the_wire(self, fake):
        """OpenAI answers 400 to stream_options on a non-streamed request —
        a payload built for streaming and sent blocking loses the field."""
        f = fake(json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"},
                                          "finish_reason": "stop"}]}).encode())
        url, headers, payload = _build(stream=True)
        request_completion(url, headers, payload, 10, stream=False)
        assert "stream_options" not in f.sent[0]

    def test_streamed_request_keeps_it_on_the_wire(self, fake):
        f = fake(NORMAL)
        url, headers, payload = _build(stream=True)
        request_completion(url, headers, payload, 10, stream=True)
        assert f.sent[0]["stream_options"] == {"include_usage": True} and f.sent[0]["stream"] is True

    @pytest.mark.parametrize("detail", [
        '{"error": {"message": "Unrecognized request argument supplied: stream_options"}}',
        '{"error": {"message": "Unknown parameter: \'stream_options\'.", "param": "stream_options"}}',
        '{"error": "include_usage is not supported"}',
    ])
    def test_http_400_mentioning_the_field_strips_it_and_remembers(self, fake, detail):
        f = fake((400, detail), NORMAL)
        url, headers, payload = _build(stream=True)
        assert request_completion(url, headers, payload, 10, stream=True) == '{"verdict": "rejected"}'
        assert "stream_options" in f.sent[0] and "stream_options" not in f.sent[1]
        assert stream_options_is_supported(url, "test-model") is False
        # the next request built for this (url, model) never carries it
        _, _, again = _build(stream=True)
        assert "stream_options" not in again

    def test_memory_is_per_url_and_model(self):
        mark_stream_options_unsupported(URL, "a")
        assert stream_options_is_supported(URL, "a") is False
        assert stream_options_is_supported(URL, "b") is True
        assert stream_options_is_supported("http://other/v1/chat/completions", "a") is True

    def test_unrelated_400_does_not_strip_it(self, fake):
        f = fake((400, '{"error": {"message": "The model `test-model` does not exist"}}'))
        url, headers, payload = _build(stream=True)
        with pytest.raises(Exception):
            request_completion(url, headers, payload, 10, stream=True, error_retries=0)
        assert len(f.sent) == 1 and "stream_options" in f.sent[0]
        assert stream_options_is_supported(url, "test-model") is True

    def test_strip_fires_once_per_call(self, fake):
        """A second 400 after the strip is a real error, not another retry."""
        f = fake((400, "stream_options rejected"), (400, "stream_options rejected"))
        url, headers, payload = _build(stream=True)
        with pytest.raises(Exception):
            request_completion(url, headers, payload, 10, stream=True, error_retries=0)
        assert len(f.sent) == 2
