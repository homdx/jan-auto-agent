"""tests_bugfix/test_llm_stream_json_fence_unclosed.py — strip_json_fence
must extract content from an UNCLOSED ```json/``` fence, not return the
fence-wrapped text untouched.

strip_json_fence() previously fell back to returning the original `text`
(fence marker still attached) whenever no closing "```" was found — a
deliberate AUTO-FIX for a different bug (discarding text that preceded the
opening marker). But when the model's JSON body is actually complete and
only the closing fence was never emitted (truncated stream, or the model
simply omits it), returning the untouched original left the leading
"```json" attached, so every caller's json.loads(strip_json_fence(...))
failed on content that was otherwise perfectly valid JSON — surfacing as
"validator unavailable: Expecting value: line 1 column 1 (char 0)" and
equivalent errors in actions.py, validator_agent.py, improvement_agent.py,
and tools/collect/summarizer.py.

The fix: on an unclosed fence, return the content AFTER the opening marker
(stripped) instead of the untouched original — this still discards nothing
that preceded the marker (the original bug this branch guards against),
while giving complete-but-unclosed JSON a real chance to parse. A body that
is itself truncated (not just missing the closing fence) is expected to
still fail to parse — there's no way to recover data that was never sent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.llm_stream import strip_json_fence


class TestUnclosedJsonFenceExtractsContent:
    def test_unclosed_json_fence_with_complete_json_parses(self):
        raw = '```json\n{"result": "ok", "value": 42}\n'
        out = strip_json_fence(raw)
        assert json.loads(out) == {"result": "ok", "value": 42}

    def test_unclosed_json_fence_no_trailing_newline(self):
        raw = '```json\n{"a": 1}'
        out = strip_json_fence(raw)
        assert json.loads(out) == {"a": 1}

    def test_unclosed_generic_fence_no_json_label(self):
        raw = '```\n{"a": 1}\n'
        out = strip_json_fence(raw)
        assert json.loads(out) == {"a": 1}

    def test_unclosed_capitalized_json_label(self):
        raw = '```JSON\n{"x": 1}\n'
        out = strip_json_fence(raw)
        assert json.loads(out) == {"x": 1}

    def test_unclosed_fence_still_drops_preceding_prose(self):
        """The original AUTO-FIX's guarantee — text before the opening
        marker must not leak into the returned content — must still hold."""
        raw = 'here is the answer:\n```\n{"a": 1}\n'
        out = strip_json_fence(raw)
        assert json.loads(out) == {"a": 1}
        assert "here is the answer" not in out

    def test_unclosed_fence_with_truncated_json_body_fails_cleanly(self):
        """A body that is ALSO truncated (not just missing the closing
        fence) has no recoverable content — must still raise a normal
        JSONDecodeError, not crash or silently return something bogus."""
        raw = '```json\n{"result": "ok", "value":'
        out = strip_json_fence(raw)
        import pytest
        with pytest.raises(json.JSONDecodeError):
            json.loads(out)


class TestClosedFenceAndNoFenceUnaffected:
    """Sanity: every previously-working path must behave identically."""

    def test_closed_json_fence(self):
        raw = '```json\n{"result": "ok"}\n```'
        assert strip_json_fence(raw) == '{"result": "ok"}'

    def test_closed_generic_fence(self):
        raw = '```\n{"result": "ok"}\n```'
        assert strip_json_fence(raw) == '{"result": "ok"}'

    def test_no_fence_plain_json_untouched(self):
        raw = '{"a": 1}'
        assert strip_json_fence(raw) == '{"a": 1}'

    def test_no_fence_plain_prose_untouched(self):
        raw = "sorry, I cannot help with that"
        assert strip_json_fence(raw) == raw
