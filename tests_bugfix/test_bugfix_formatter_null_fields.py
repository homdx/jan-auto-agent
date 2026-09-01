"""tests/test_bugfix_formatter_null_fields.py

Bug: `improvement.get("explanation", "").strip()` (and the identical
`improved_code` line right below it) only falls back to "" when the key is
*absent*. A model that emits otherwise-valid JSON with `"explanation": null`
makes `.get()` return `None`, and `None.strip()` raises AttributeError,
which propagated uncaught out of the formatter and crashed the whole
interactive session (main.py:1079 does not catch it). Fix: use
`(improvement.get(...) or "").strip()` so both an absent key and an
explicit `None` value fall back to the empty string.
"""
import io
import contextlib

from tools.formatter import OutputFormatter
from tools.prompt_parser import ParsedPrompt


def _parsed(intent="improve"):
    return ParsedPrompt(
        file_path="app.py",
        target_name="foo",
        target_type="function",
        intent=intent,
        raw="improve foo in app.py",
    )


def _render(improvement):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        OutputFormatter.render(
            parsed=_parsed(),
            imports=[],
            block="def foo(): pass",
            search_result={},
            improvement=improvement,
            elapsed_time=1.0,
            iteration=1,
            output_config={},
        )
    return buf.getvalue()


def test_null_explanation_does_not_crash():
    improvement = {
        "explanation": None,
        "issues": [],
        "improved_code": "def foo():\n    return 1\n",
        "changes": [],
    }
    # Must not raise AttributeError, and must simply omit the section.
    output = _render(improvement)
    assert "# EXPLANATION" not in output


def test_null_improved_code_does_not_crash():
    improvement = {
        "explanation": "fine",
        "issues": [],
        "improved_code": None,
        "changes": [],
    }
    output = _render(improvement)
    assert "# IMPROVED CODE" not in output


def test_normal_string_fields_still_render():
    improvement = {
        "explanation": "This fixes the bug.",
        "issues": [],
        "improved_code": "def foo():\n    return 1\n",
        "changes": [],
    }
    output = _render(improvement)
    assert "# EXPLANATION" in output
    assert "This fixes the bug." in output
    assert "# IMPROVED CODE" in output
