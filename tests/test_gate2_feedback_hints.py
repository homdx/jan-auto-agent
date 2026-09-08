"""Regression: _format_gate2_feedback must handle a string "hints" field.

The Gate-2 validator's system prompt asks for "hints": ["hint1", "hint2"]
(a JSON array), but an LLM may return "hints": "fix the ending" (a string).
Without a type guard, [:max_hints] slices the string and enumerate() iterates
over individual characters ("1. f", "2. i", "3. x"), feeding the coder
nonsense instead of the intended hint.

The same class of bug affected last_missing_context (string iterated as
characters); both are guarded with isinstance(raw, list) now.
"""

from tools.auto.inner_loop import _format_gate2_feedback


def test_hints_as_list_normal():
    result = _format_gate2_feedback(
        {"feedback": "bad", "hints": ["fix the ending", "add tests", "refactor"]},
        max_hints=3,
    )
    assert "Reason: bad" in result
    assert "Hints:" in result
    assert "1. fix the ending" in result
    assert "2. add tests" in result
    assert "3. refactor" in result


def test_hints_as_string_treated_as_single_hint():
    """A string hints field must be treated as one hint, not iterated
    character by character."""
    result = _format_gate2_feedback(
        {"feedback": "bad", "hints": "fix the ending"},
        max_hints=3,
    )
    assert "Reason: bad" in result
    assert "Hints:" in result
    assert "1. fix the ending" in result
    # Character-level iteration would produce "2. i", "3. x" — must be absent
    assert "2. i" not in result
    assert "3. x" not in result
    # Only one hint line (the full string), not len("fix the ending") lines
    hint_lines = [
        line for line in result.split("\n")
        if line.strip() and line.strip()[0].isdigit()
    ]
    assert len(hint_lines) == 1
    assert "fix the ending" in hint_lines[0]


def test_hints_as_empty_string_no_hints():
    result = _format_gate2_feedback(
        {"feedback": "bad", "hints": ""},
        max_hints=3,
    )
    assert "Reason: bad" in result
    assert "Hints:" not in result


def test_hints_as_whitespace_string_no_hints():
    result = _format_gate2_feedback(
        {"feedback": "bad", "hints": "   "},
        max_hints=3,
    )
    assert "Reason: bad" in result
    assert "Hints:" not in result


def test_hints_missing_no_hints():
    result = _format_gate2_feedback({"feedback": "bad"}, max_hints=3)
    assert "Reason: bad" in result
    assert "Hints:" not in result


def test_hints_none_no_hints():
    result = _format_gate2_feedback(
        {"feedback": "bad", "hints": None},
        max_hints=3,
    )
    assert "Reason: bad" in result
    assert "Hints:" not in result


def test_hints_as_int_no_hints():
    """A non-list, non-string hints value is dropped, not iterated."""
    result = _format_gate2_feedback(
        {"feedback": "bad", "hints": 42},
        max_hints=3,
    )
    assert "Reason: bad" in result
    assert "Hints:" not in result


def test_hints_truncated_to_max_hints():
    result = _format_gate2_feedback(
        {"feedback": "bad", "hints": ["h1", "h2", "h3", "h4", "h5"]},
        max_hints=2,
    )
    assert "1. h1" in result
    assert "2. h2" in result
    assert "3. h3" not in result


def test_hints_string_not_truncated_by_max_hints():
    """A string hint is a single hint — max_hints should not slice it."""
    result = _format_gate2_feedback(
        {"feedback": "bad", "hints": "a very long hint string"},
        max_hints=2,
    )
    assert "1. a very long hint string" in result
    assert "2." not in result


def test_suggested_approach_present():
    result = _format_gate2_feedback(
        {"feedback": "bad", "hints": ["fix it"], "suggested_approach": "try X"},
        max_hints=3,
    )
    assert "Suggested approach: try X" in result


def test_suggested_approach_absent():
    result = _format_gate2_feedback(
        {"feedback": "bad", "hints": ["fix it"]},
        max_hints=3,
    )
    assert "Suggested approach" not in result


def test_feedback_default_when_missing():
    result = _format_gate2_feedback({"hints": ["fix it"]}, max_hints=3)
    assert "Reason: no reason given" in result
