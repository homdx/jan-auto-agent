"""A8: strip_json_fence must drop any fence label, not just "json".

Real failure mode
-----------------
Only the literal "```json" label was recognised. A fence carrying any
other label ("```python", "```json5", "```ts") fell through to the generic
"```" branch, which did not skip past the label — so the label stayed
glued to the returned text:

    strip_json_fence("```python\\n{\\\"a\\\": 1}\\n```")
        ->  "python\\n{\"a\": 1}"

...which then failed json.loads. That is common model behaviour, and it
turned a recoverable format slip into a parse failure that consumed one of
the precious retries.
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


@pytest.mark.parametrize("label", [
    "python",
    "py",
    "ts",
    "typescript",
    "javascript",
    "json5",
    "jsonc",
    "json ",
    "c++",
    "shell",
])
def test_labeled_fence_extracts_the_json(label):
    raw = f"```{label}\n{{\"a\": 1, \"b\": [2, 3]}}\n```"
    assert json.loads(strip_json_fence(raw)) == {"a": 1, "b": [2, 3]}


@pytest.mark.parametrize("label", ["python", "json5", "ts"])
def test_unclosed_labeled_fence_extracts_the_json(label):
    """Truncation that drops the closing fence must still yield the body."""
    raw = f"```{label}\n{{\"a\": 1}}"
    assert json.loads(strip_json_fence(raw)) == {"a": 1}


@pytest.mark.parametrize("label", ["python", "json5"])
def test_labeled_fence_drops_preceding_prose(label):
    raw = f"here is the answer:\n```{label}\n{{\"a\": 1}}\n```"
    out = strip_json_fence(raw)
    assert json.loads(out) == {"a": 1}
    assert "here is the answer" not in out
    assert label not in out


class TestExistingBehaviourUnaffected:

    def test_closed_json_fence(self):
        assert strip_json_fence('```json\n{"result": "ok"}\n```') == '{"result": "ok"}'

    def test_closed_capitalized_json_fence(self):
        assert strip_json_fence('```JSON\n{"result": "ok"}\n```') == '{"result": "ok"}'

    def test_closed_generic_fence(self):
        assert strip_json_fence('```\n{"result": "ok"}\n```') == '{"result": "ok"}'

    def test_unclosed_json_fence(self):
        assert json.loads(strip_json_fence('```json\n{"a": 1}\n')) == {"a": 1}

    def test_unclosed_generic_fence(self):
        assert json.loads(strip_json_fence('```\n{"a": 1}\n')) == {"a": 1}

    def test_unclosed_truncated_body_still_fails_cleanly(self):
        import pytest
        with pytest.raises(json.JSONDecodeError):
            json.loads(strip_json_fence('```json\n{"result": "ok", "value":'))

    def test_no_fence_plain_json_untouched(self):
        raw = '{"a": 1}'
        assert strip_json_fence(raw) == raw

    def test_no_fence_plain_prose_untouched(self):
        raw = "sorry, I cannot help with that"
        assert strip_json_fence(raw) == raw

    def test_label_is_not_stripped_from_the_body(self):
        """The body itself must survive verbatim."""
        raw = '```python\n{"note": "python is a language", "a": 1}\n```'
        out = strip_json_fence(raw)
        assert json.loads(out) == {"note": "python is a language", "a": 1}


class TestFirstFenceIsNotAlwaysTheAnswer:
    """REVIEW-FIX (A8): generalising the match from "```json" to "any label"
    by position alone regressed two replies that previously parsed, because
    the first fence in a reply is not necessarily the one wrapping the
    JSON. The labelled case above proves the label is dropped; these prove
    fences that worked BEFORE the generalisation still work.
    """

    def test_later_json_fence_wins_over_an_earlier_stray_fence(self):
        """A stray/example fence first, real answer second."""
        raw = '```text\nexample\n```json\n{"a":1}\n```'
        assert json.loads(strip_json_fence(raw)) == {"a": 1}

    def test_later_json_fence_wins_over_a_generic_earlier_fence(self):
        raw = '```\nsee this\n```json\n{"a": 1}\n```'
        assert json.loads(strip_json_fence(raw)) == {"a": 1}

    def test_inline_json_label_without_a_newline(self):
        """The label glued straight to the body must be skipped, not kept."""
        raw = '```json{"a":1}```'
        assert strip_json_fence(raw) == '{"a":1}'

    def test_inline_json_label_with_trailing_prose(self):
        raw = '```json{"a":1}```text after'
        assert json.loads(strip_json_fence(raw)) == {"a": 1}

    def test_unbroken_non_json_label_is_not_swallowed(self):
        """```123``` is one fence whose body is "123", not two fences."""
        assert strip_json_fence("```123```") == "123"

    def test_unclosed_json_fence_closed_by_a_labelled_marker(self):
        """A JSON fence whose bare closer was never emitted, followed by a
        labelled fence ("```text"), which is a legal closer by convention.

        Treating a label on its own line as unambiguously an opening makes the
        json fence run to the end of the reply, so its body becomes the JSON
        plus the whole following fence ('{"a": 1}\\n```text\\nexample\\n```')
        -- no longer parseable, where the pre-A8 split("```") returned just
        '{"a": 1}'. This is a regression the A8 rewrite introduced and
        silently shipped; the labelled marker must close the earlier opening
        AND open its own fence, so the parse-wins walk can still reach the
        JSON body.
        """
        raw = '```json\n{"a": 1}\n\n```text\nexample\n```'
        assert json.loads(strip_json_fence(raw)) == {"a": 1}

    def test_unclosed_json_fence_then_labelled_fence_json_body(self):
        """Same shape with no blank line between the fences."""
        raw = '```json\n{"a": 1}```text\nexample\n```'
        assert json.loads(strip_json_fence(raw)) == {"a": 1}

    def test_unclosed_unparseable_json_fence_then_labelled_fence(self):
        """The closing candidate is only used when it parses; a truncated
        JSON body must NOT win over the stray text fence it truncates into.
        Fallback stays the highest-ranked candidate's body."""
        raw = '```json\n{"a": 1, "b":\n```text\nexample\n```'
        out = strip_json_fence(raw)
        assert "```text" not in out
        assert out.startswith('{"a": 1')

    def test_no_json_anywhere_keeps_the_best_candidate(self):
        """Nothing parses -> the highest-ranked candidate's body is returned,
        unchanged by the multi-fence walk."""
        raw = '```python\ndef f(): pass\n```'
        assert strip_json_fence(raw) == "def f(): pass"

    def test_single_json_fence_untouched(self):
        raw = '```json\n{"a": 1, "b": [2, 3]}\n```'
        assert strip_json_fence(raw) == '{"a": 1, "b": [2, 3]}'
