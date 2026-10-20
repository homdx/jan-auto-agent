"""tests/test_llm_stream_review46_invalid_url_and_jwt_anchor.py — CC review fixes 46: two leftovers of HARDEN-23.

* B: `http.client.HTTPException` joined the retried transport errors, and `InvalidURL` is one —
  a malformed `base_url` (a port such as `11434x`, a control character) went round the whole
  retry budget, 60 waits of 10 s per call, where it used to end the call at once.
* C: the bare-JWT pattern was anchored with the Unicode `\\w` while its runs are ASCII, so a
  token glued to a Cyrillic or accented letter had no start the anchor allowed and reached the
  model unmasked; the pattern before HARDEN-23 masked it.
"""

from __future__ import annotations

import http.client

import pytest

from tools import llm_stream
from tools.llm_stream import mask_api_key, request_completion

_JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434x/v1/chat/completions",            # a typo in the port
    "http://127.0.0.1:1/v1/chat/completions\r\nX-Injected: 1",  # a control character
])
def test_a_malformed_url_ends_the_call_at_once(url, stream):
    waits = []
    with pytest.raises(http.client.InvalidURL):
        request_completion(url, {"Content-Type": "application/json"}, {"model": "m", "messages": []},
                           5, stream=stream, error_retries=3, error_retry_wait_sec=10.0,
                           _sleep_fn=waits.append)
    assert waits == []


def test_invalid_url_is_the_only_http_exception_taken_out_of_the_retries():
    assert llm_stream._PERMANENT_TRANSPORT_ERRORS == (http.client.InvalidURL,)
    for transient in (http.client.IncompleteRead, http.client.BadStatusLine, http.client.RemoteDisconnected):
        assert issubclass(transient, llm_stream._RETRYABLE_TRANSPORT_ERRORS)
        assert not issubclass(transient, llm_stream._PERMANENT_TRANSPORT_ERRORS)


@pytest.mark.parametrize("prefix", ["токен", "é", "ключ:", "Bearer ", "x=", "(", ""])
def test_a_jwt_is_masked_whatever_letter_it_is_glued_to(prefix):
    masked = mask_api_key(f"{prefix}{_JWT}")
    assert "eyJ" not in masked
    assert masked.startswith(prefix)


def test_a_dotted_name_after_a_cyrillic_word_is_not_taken_for_a_jwt():
    line = "смотри config.section.key_name"
    assert mask_api_key(line) == line
