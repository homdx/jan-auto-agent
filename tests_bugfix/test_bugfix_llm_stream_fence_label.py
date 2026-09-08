"""A8: a fenced block labelled with anything other than `json` must still
have its label stripped, not left glued to the returned text.

strip_json_fence() special-cased only the literal token "json" (now
case-insensitively). A block labelled ```python, ```text, ```js, ... fell
through to the generic "```" branch, whose ``text.partition("```")`` cut at
the marker but kept everything after it — so the label came back glued to
the body:

    >>> strip_json_fence('```python\n{"a": 1}\n```')
    'python\n{"a": 1}'

json.loads() then failed, so a purely cosmetic model habit (labelling the
fence) turned a recoverable format slip into a parse failure that burned
one of the limited retries the caller has for it.

The label after the opening fence is markdown's language tag and carries
no meaning for the JSON payload, so it should be dropped for any label,
not only "json".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.llm_stream import strip_json_fence


LABELS = [
    "json", "JSON", "Json",
    "python", "py", "javascript", "js", "typescript", "ts",
    "text", "txt", "plain", "yaml", "yml", "toml", "sql", "sh", "bash",
    "json5", "jsonc", "c++", "c#", "cpp", "_x", "json3",
]


@pytest.mark.parametrize("label", LABELS)
class TestLabelStrippedForAnyLabel:
    def test_closed_fence_parses(self, label):
        raw = f"```{label}\n{{\"a\": 1}}\n```"
        assert json.loads(strip_json_fence(raw)) == {"a": 1}

    def test_unclosed_fence_parses(self, label):
        """Truncated stream / omitted closing fence with a complete body."""
        raw = f"```{label}\n{{\"a\": 1}}\n"
        assert json.loads(strip_json_fence(raw)) == {"a": 1}

    def test_label_is_not_in_the_result(self, label):
        raw = f"```{label}\n{{\"a\": 1}}\n```"
        assert label.lower() not in strip_json_fence(raw).lower()


class TestMultiLinePayload:
    def test_python_label_multiline_json(self):
        raw = '```python\n{\n  "status": "approved",\n  "feedback": "fine"\n}\n```'
        assert json.loads(strip_json_fence(raw)) == {"status": "approved", "feedback": "fine"}

    def test_prose_before_the_fence_is_still_dropped(self):
        raw = 'Here you go:\n```python\n{"a": 1}\n```'
        out = strip_json_fence(raw)
        assert "Here you go" not in out
        assert json.loads(out) == {"a": 1}

    def test_text_after_the_closing_fence_is_still_dropped(self):
        raw = '```python\n{"a": 1}\n```\nDone!'
        assert strip_json_fence(raw) == '{"a": 1}'


class TestPreviouslyWorkingPathsUnchanged:
    def test_closed_json_fence(self):
        assert strip_json_fence('```json\n{"result": "ok"}\n```') == '{"result": "ok"}'

    def test_closed_generic_fence(self):
        assert strip_json_fence('```\n{"result": "ok"}\n```') == '{"result": "ok"}'

    def test_no_fence_plain_json_untouched(self):
        raw = '{"a": 1}'
        assert strip_json_fence(raw) == raw

    def test_no_fence_plain_prose_untouched(self):
        raw = "sorry, I cannot help with that"
        assert strip_json_fence(raw) == raw

    def test_unclosed_generic_fence(self):
        assert json.loads(strip_json_fence('```\n{"a": 1}\n')) == {"a": 1}

    def test_truncated_body_still_fails_cleanly(self):
        with pytest.raises(json.JSONDecodeError):
            json.loads(strip_json_fence('```python\n{"a":'))

    def test_content_casing_elsewhere_is_preserved(self):
        raw = '```JSON\n{"Key": "Value"}\n```'
        assert strip_json_fence(raw) == '{"Key": "Value"}'


# ── 2nd pass: fence runs longer than three backticks ──────────────────────────
#
# The label fix matched EXACTLY three backticks, so a four-backtick opener --
# valid markdown for a block that itself contains ``` (common when the JSON
# payload carries a code sample) -- matched only its first three and left a
# stray backtick glued to the body:
#
#     '````json\n{"a":1}\n````'  ->  '`json\n{"a":1}'
#
# json.loads rejects that, so the label fix was a strict regression against
# the pre-audit code, which stripped this input correctly. Verified by
# running origin/pullv3's strip_json_fence against this suite.

class TestFencesLongerThanThreeBackticks:
    @pytest.mark.parametrize("n", [4, 5, 6])
    def test_longer_opening_fence_strips_the_label(self, n):
        fence = "`" * n
        raw = f"{fence}json\n{{\"a\": 1}}\n{fence}"
        assert json.loads(strip_json_fence(raw)) == {"a": 1}

    def test_four_backtick_fence_with_no_label(self):
        assert json.loads(strip_json_fence('````\n{"a": 1}\n````')) == {"a": 1}

    def test_mismatched_closer_still_parses(self):
        """Markdown wants the closer >= the opener, but models emit a
        shorter one. The pre-audit code cut at any three-backtick run, so
        enforcing the length rule strictly would have broken this common
        shape."""
        assert json.loads(strip_json_fence('````json\n{"a": 1}\n```')) == {"a": 1}

    def test_longer_closer_than_opener(self):
        assert json.loads(strip_json_fence('```json\n{"a": 1}\n````')) == {"a": 1}

    def test_unclosed_four_backtick_fence(self):
        """Truncation after a four-backtick opener must still recover the
        body rather than leaving a backtick attached."""
        assert json.loads(strip_json_fence('````json\n{"a": 1}')) == {"a": 1}

    def test_unclosed_five_backtick_fence_with_label(self):
        assert json.loads(strip_json_fence('`````json\n{"a": 1}')) == {"a": 1}


class TestLabelCharactersBeyondAlnum:
    """Language tags with punctuation, e.g. ```js,compact or ```js+jsx."""

    @pytest.mark.parametrize("label", ["js,compact", "js+jsx", "c++", "json5", "ts-x"])
    def test_punctuated_label_is_dropped(self, label):
        assert json.loads(
            strip_json_fence(f"```{label}\n{{\"a\": 1}}\n```")
        ) == {"a": 1}
