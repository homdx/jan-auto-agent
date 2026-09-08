"""tests_bugfix/test_bugfix_strip_json_fence_generic_label.py

strip_json_fence() had two fences it understood — ```json (case-insensitive,
with an unclosed variant) and a bare ```. Any OTHER label fell through to the
generic branch, which split on the literal "```" and never skipped past the
label:

    ```python
    {"a": 1}
    ```
    →  "python\n{\"a\": 1}"

The label stayed glued to the body, so every caller's json.loads failed on
content that was perfectly valid JSON. That is common model behaviour — a
model that "thinks in code" wraps its JSON in ```python / ```text / ```js — so
a recoverable format slip consumed a retry instead of being stripped.

The same root cause hid in the json branch too: the regex only consumed the
literal "json", so ```json5 left a stray "5" in front of the body.

Fix under test: consume whatever alphanumeric label the opening fence carries,
not just the literal "json".
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

BODY = {"result": "ok", "value": 42}


@pytest.mark.parametrize(
    "label",
    [
        "python", "text", "js", "json5", "JSON5", "markdown", "md", "yaml",
        "c++", "c#", "json-object", "Plain-Json", "json ",
    ],
)
def test_non_json_label_is_stripped(label: str):
    """THE BUG: a label other than exactly "json" used to survive into the
    returned text and break json.loads."""
    raw = f"```{label}\n{json.dumps(BODY)}\n```"
    assert json.loads(strip_json_fence(raw)) == BODY


def test_non_json_label_unclosed():
    """A label plus a missing closing fence is the worst of both failures."""
    raw = f"```python\n{json.dumps(BODY)}"
    assert json.loads(strip_json_fence(raw)) == BODY


@pytest.mark.parametrize("label", ["PYTHON", "PyThOn", "JSON"])
def test_label_case_does_not_matter(label: str):
    raw = f"```{label}\n{json.dumps(BODY)}\n```"
    assert json.loads(strip_json_fence(raw)) == BODY


def test_preceding_prose_still_dropped_with_a_label():
    """Stripping the label must not resurrect the AUTO-FIX guarantee that text
    before the opening marker never leaks into the returned content."""
    raw = f"here is the answer:\n```python\n{json.dumps(BODY)}\n```"
    out = strip_json_fence(raw)
    assert json.loads(out) == BODY
    assert "here is the answer" not in out


def test_same_line_body_is_not_eaten():
    """The label must be consumed but not the body when the model writes both
    on one line — the reason the label regex stops at whitespace."""
    raw = f"```python {json.dumps(BODY)}"
    assert json.loads(strip_json_fence(raw)) == BODY
    raw = f"```json {json.dumps(BODY)}"
    assert json.loads(strip_json_fence(raw)) == BODY


def test_empty_label_with_spaces():
    raw = f"```\n{json.dumps(BODY)}\n```"
    assert json.loads(strip_json_fence(raw)) == BODY


def test_label_with_a_space_before_it():
    raw = f"``` python\n{json.dumps(BODY)}\n```"
    assert json.loads(strip_json_fence(raw)) == BODY


def test_non_json_body_in_a_labelled_fence_still_fails_cleanly():
    """Only the LABEL is stripped; a body that is genuinely not JSON must keep
    raising a normal JSONDecodeError rather than being mangled."""
    raw = "```python\ndef f():\n    pass\n```"
    with pytest.raises(json.JSONDecodeError):
        json.loads(strip_json_fence(raw))


class TestExistingBehavioursUnaffected:
    """Every path that worked before must behave identically."""

    def test_closed_json_fence(self):
        raw = f"```json\n{json.dumps(BODY)}\n```"
        assert strip_json_fence(raw) == json.dumps(BODY)

    def test_closed_capitalised_json_fence(self):
        raw = f"```JSON\n{json.dumps(BODY)}\n```"
        assert json.loads(strip_json_fence(raw)) == BODY

    def test_no_fence_plain_json_untouched(self):
        raw = json.dumps(BODY)
        assert strip_json_fence(raw) == raw

    def test_no_fence_plain_prose_untouched(self):
        raw = "sorry, I cannot help with that"
        assert strip_json_fence(raw) == raw

    def test_unclosed_json_fence(self):
        raw = f"```json\n{json.dumps(BODY)}"
        assert json.loads(strip_json_fence(raw)) == BODY

    def test_unclosed_json_fence_drops_preceding_prose(self):
        raw = f"thinking out loud\n```json\n{json.dumps(BODY)}"
        out = strip_json_fence(raw)
        assert json.loads(out) == BODY
        assert "thinking out loud" not in out

    def test_truncated_body_inside_a_labelled_fence_fails_cleanly(self):
        raw = '```python\n{"result": "ok", "value":'
        with pytest.raises(json.JSONDecodeError):
            json.loads(strip_json_fence(raw))

    def test_fences_without_a_body_are_not_invented(self):
        assert strip_json_fence("```\n```") == ""
        assert strip_json_fence("```python\n```") == ""
