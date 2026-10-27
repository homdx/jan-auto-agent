"""tests_bugfix/test_harden1_a2_stream_null_choice.py — HARDEN-1 A2: a streamed
chunk whose first choice is null (or not an object) is skipped, not fatal.

The OpenAI SSE branch of ``request_completion`` did
``choices[0].get("delta")`` with only ``json.JSONDecodeError`` and ``KeyError``
guarded around it, so a gateway sending ``{"choices": [null]}`` — the exact
shape ``_meta_from_chunk`` right below it already tolerates — raised
``AttributeError`` from inside the read loop. The ``except`` around the loop
names only network errors, so the call died with every token already read and
the caller retried the whole request and re-billed it.

A chunk that is not an object at all (``null``, ``1``) is skipped the same way.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.llm_stream import request_completion  # noqa: E402

URL = "http://stub.local:1337/v1/chat/completions"
HEADERS = {"Content-Type": "application/json"}


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


def _sse(*chunks) -> bytes:
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


def _content_chunk(text: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


@pytest.fixture
def stream(monkeypatch):
    """Answer the next streamed request with *body*; return what came back."""
    def _call(body: bytes, api_format: str = "openai") -> str:
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda req, timeout=None, context=None: _Response(body))
        return request_completion(URL, HEADERS, {"model": "m", "messages": []},
                                  timeout=5, stream=True, api_format=api_format)
    return _call


class TestNullFirstChoiceIsSkipped:
    def test_a_null_choice_between_two_content_chunks_keeps_the_text(self, stream):
        assert stream(_sse(_content_chunk("hel"), {"choices": [None]}, _content_chunk("lo"))) == "hello"

    @pytest.mark.parametrize("chunk", [
        {"choices": []},
        {"choices": [None]},
        {"choices": [1]},
        {"choices": ["x"]},
        None,
        1,
        "text",
    ])
    def test_a_degenerate_chunk_never_raises(self, stream, chunk):
        assert stream(_sse(_content_chunk("a"), chunk, _content_chunk("b"))) == "ab"

    def test_a_null_delta_is_still_empty_not_an_error(self, stream):
        assert stream(_sse({"choices": [{"delta": None}]}, _content_chunk("z"))) == "z"


class TestTheNDJSONBranchSkipsTheSameShapes:
    """The Ollama branch reads ``chunk["message"]`` and had the same hole."""

    def _ndjson(self, *chunks) -> bytes:
        return b"".join(json.dumps(c).encode() + b"\n" for c in chunks)

    def test_a_chunk_that_is_not_an_object_is_skipped(self, stream):
        body = self._ndjson({"message": {"content": "he"}}, None, 1, "x",
                            {"message": {"content": "llo"}}, {"done": True})
        assert stream(body, api_format="ollama") == "hello"
