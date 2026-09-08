"""A4b: a response that is not an object, or whose `content` is not a
string, must surface as the same descriptive ValueError every other
`_extract_content` parse failure raises — not as a raw AttributeError.

(Same audit bucket as A4, test_bugfix_llm_stream_null_message.py, which
handled the `"message": null` case of this family.)

`_extract_content` already converts a malformed-but-valid-JSON response into a
descriptive ValueError: a missing "message" key, a null "message", an empty
`choices` list, a null `content` field. Two shapes still escaped that
convention with a bare, low-level AttributeError:

  1. `raw` itself is not a dict — a JSON *array* body (``[]``, or
     ``[{"choices": []}]``), or any other non-object (``null``, a number, a
     bare string). The openai branch did `raw.get("choices")` →
     `AttributeError: 'list' object has no attribute 'get'`, and the ollama
     branch tried to build its descriptive message with `list(raw.keys())` →
     `AttributeError: 'list' object has no attribute 'keys'`.

  2. `content` is a *list* — the real OpenAI multimodal shape,
     ``"content": [{"type": "text", "text": "hi"}]``, also seen from
     gateway wrappers that relay OpenAI's own list content verbatim. The
     `(message["content"] or "").strip()` idiom raised
     `AttributeError: 'list' object has no attribute 'strip'`.

AttributeError is not in request_completion()'s non-streaming except clause
(TimeoutError / ssl.SSLError / ConnectionError / URLError / JSONDecodeError /
UnicodeDecodeError) and is not a ValueError, so the exception escaped
request_completion() outright with no indication of which response shape had
arrived — easy to misread as a bug in this module rather than a malformed
provider reply, and it broke the established contract that
`_extract_content` never raises anything but a descriptive ValueError.

The fix raises ValueError in both cases, keeping the message informative
about what actually arrived. As with the other `_extract_content` ValueErrors,
those are deliberately NOT retried: the response arrived and is unusable, so
retrying would just re-fetch the same payload error_retries ×
error_retry_wait_sec (60 × 10s by default) before raising the same error.
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

from tools.llm_stream import _extract_content, request_completion

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")

_HEADERS = {"Content-Type": "application/json"}
_PAYLOAD = {"model": "x", "messages": []}
_OPENAI_URL = "/v1/chat/completions"
_OLLAMA_URL = "/api/chat"

# A response body that is valid JSON but not a JSON object.
NON_OBJECT_BODIES = [
    ("empty array", []),
    ("array wrapping an object", [{"choices": []}]),
    ("null", None),
    ("number", 123),
    ("boolean", True),
    ("bare string", "hi there"),
]

# A message `content` field that is not a string.
NON_STRING_CONTENTS = [
    ("number", 7),
    ("nested object", {"type": "text", "text": "hi"}),
]


def _serve(body):
    body_bytes = json.dumps(body).encode()

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


class TestNonObjectResponse:
    """`raw` is valid JSON but not a dict."""

    @pytest.mark.parametrize(
        "raw",
        [body for _, body in NON_OBJECT_BODIES],
        ids=[label for label, _ in NON_OBJECT_BODIES],
    )
    def test_openai_non_object_raises_valueerror_not_attributeerror(self, raw):
        with pytest.raises(ValueError) as excinfo:
            _extract_content(raw, api_format="openai")
        assert not isinstance(excinfo.value, AttributeError)

    @pytest.mark.parametrize(
        "raw",
        [body for _, body in NON_OBJECT_BODIES],
        ids=[label for label, _ in NON_OBJECT_BODIES],
    )
    def test_ollama_non_object_raises_valueerror_not_attributeerror(self, raw):
        with pytest.raises(ValueError) as excinfo:
            _extract_content(raw, api_format="ollama")
        assert not isinstance(excinfo.value, AttributeError)

    def test_message_names_the_shape_that_arrived(self):
        """The message should let an operator see what actually arrived."""
        with pytest.raises(ValueError) as excinfo:
            _extract_content([{"choices": []}], api_format="openai")
        msg = str(excinfo.value)
        assert "list" in msg or "array" in msg
        assert "not an object" in msg or "object" in msg

    def test_null_body_is_reported_as_null(self):
        with pytest.raises(ValueError) as excinfo:
            _extract_content(None, api_format="openai")
        assert "None" in str(excinfo.value)

    def test_a_number_is_reported_as_a_number(self):
        with pytest.raises(ValueError) as excinfo:
            _extract_content(123, api_format="openai")
        assert "int" in str(excinfo.value)


class TestNonStringContent:
    """`content` is a list (real OpenAI multimodal shape) or some other
    non-string value."""

    @pytest.mark.parametrize(
        "content",
        [content for _, content in NON_STRING_CONTENTS],
        ids=[label for label, _ in NON_STRING_CONTENTS],
    )
    def test_openai_non_string_content_raises_valueerror(self, content):
        raw = {"choices": [{"message": {"content": content}}]}
        with pytest.raises(ValueError) as excinfo:
            _extract_content(raw, api_format="openai")
        assert not isinstance(excinfo.value, AttributeError)

    @pytest.mark.parametrize(
        "content",
        [content for _, content in NON_STRING_CONTENTS],
        ids=[label for label, _ in NON_STRING_CONTENTS],
    )
    def test_ollama_non_string_content_raises_valueerror(self, content):
        raw = {"message": {"role": "assistant", "content": content}, "done": True}
        with pytest.raises(ValueError) as excinfo:
            _extract_content(raw, api_format="ollama")
        assert not isinstance(excinfo.value, AttributeError)

    def test_multimodal_list_names_the_field_and_type(self):
        raw = {"choices": [{"message": {"content": [{"type": "text"}]}}]}
        with pytest.raises(ValueError) as excinfo:
            _extract_content(raw, api_format="openai")
        msg = str(excinfo.value)
        assert "content" in msg
        assert "list" in msg or "array" in msg


class TestExistingGuardsStillWork:
    """None of the pre-existing guards may regress."""

    def test_valid_payloads_unaffected(self):
        assert _extract_content(
            {"message": {"role": "assistant", "content": "hi"}},
            api_format="ollama",
        ) == "hi"
        assert _extract_content(
            {"choices": [{"message": {"content": "yo"}}]}, api_format="openai",
        ) == "yo"

    def test_null_content_still_degrades_to_empty(self):
        assert _extract_content(
            {"message": {"content": None}}, api_format="ollama",
        ) == ""
        assert _extract_content(
            {"choices": [{"message": {"content": None}}]}, api_format="openai",
        ) == ""

    def test_empty_string_content_still_strips_to_empty(self):
        assert _extract_content(
            {"choices": [{"message": {"content": "  "}}]}, api_format="openai",
        ) == ""

    def test_missing_message_key_still_raises_valueerror(self):
        with pytest.raises(ValueError, match="missing expected key"):
            _extract_content({"done": True}, api_format="ollama")

    def test_null_message_still_raises_valueerror(self):
        with pytest.raises(ValueError, match="message"):
            _extract_content(
                {"message": None, "done": True}, api_format="ollama"
            )
        with pytest.raises(ValueError, match="message"):
            _extract_content(
                {"choices": [{"message": None}]}, api_format="openai"
            )

    def test_empty_choices_still_raises_valueerror(self):
        with pytest.raises(ValueError, match="no choices"):
            _extract_content({"choices": []}, api_format="openai")

    def test_message_not_an_object_still_raises_valueerror(self):
        with pytest.raises(ValueError, match="not a JSON object"):
            _extract_content(
                {"message": ["not", "a", "dict"]}, api_format="ollama"
            )
        with pytest.raises(ValueError, match="not a JSON object"):
            _extract_content(
                {"choices": [{"message": "nope"}]}, api_format="openai"
            )


class TestEndToEnd:
    """Through request_completion, the exception must arrive at the caller as
    the descriptive ValueError — not a raw AttributeError and not wrapped in
    the RuntimeError the retry ladder produces."""

    @pytest.mark.parametrize("api_format", ["openai", "ollama"])
    def test_json_array_body_surfaces_valueerror(self, api_format):
        server, port = _serve([{"choices": []}])
        try:
            with pytest.raises(ValueError) as excinfo:
                request_completion(
                    f"http://127.0.0.1:{port}"
                    + (_OPENAI_URL if api_format == "openai" else _OLLAMA_URL),
                    _HEADERS,
                    _PAYLOAD,
                    timeout=5,
                    stream=False,
                    api_format=api_format,
                    error_retries=0,
                    error_retry_wait_sec=0,
                )
        finally:
            server.shutdown()
        assert not isinstance(excinfo.value, AttributeError)

    def test_multimodal_content_is_joined_not_rejected(self):
        """The OpenAI multimodal shape is a VALID reply: join its text parts.

        It used to raise AttributeError ('list' has no attribute 'strip').
        Rejecting it with a descriptive ValueError would still lose a reply
        the backend actually sent, so the text blocks are concatenated.
        """
        server, port = _serve(
            {"choices": [{"message": {"content": [
                {"type": "text", "text": "hi "},
                {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                {"type": "text", "text": "there"},
            ]}}]}
        )
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}{_OPENAI_URL}",
                _HEADERS,
                _PAYLOAD,
                timeout=5,
                stream=False,
                api_format="openai",
                error_retries=0,
                error_retry_wait_sec=0,
            )
        finally:
            server.shutdown()
        assert result == "hi there"


    def test_not_wrapped_in_retry_runtimeerror(self):
        """An unusable-but-arrived response is not transient, so it must not
        be retried error_retries × error_retry_wait_sec — it surfaces on the
        first attempt, as every other _extract_content ValueError does."""
        calls = 0

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal calls
                calls += 1
                body = json.dumps([{"choices": []}]).encode()
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
            with pytest.raises(ValueError):
                request_completion(
                    f"http://127.0.0.1:{port}{_OPENAI_URL}",
                    _HEADERS,
                    _PAYLOAD,
                    timeout=5,
                    stream=False,
                    api_format="openai",
                    error_retries=3,
                    error_retry_wait_sec=0.01,
                )
        finally:
            server.shutdown()
        assert calls == 1

    def test_good_response_still_returns_text(self):
        server, port = _serve(
            {"message": {"role": "assistant", "content": "fine"}, "done": True}
        )
        try:
            result = request_completion(
                f"http://127.0.0.1:{port}{_OLLAMA_URL}",
                _HEADERS,
                _PAYLOAD,
                timeout=5,
                stream=False,
                api_format="ollama",
                error_retries=0,
                error_retry_wait_sec=0,
            )
        finally:
            server.shutdown()
        assert result == "fine"
