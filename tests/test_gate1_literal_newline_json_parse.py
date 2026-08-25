"""tests/test_gate1_literal_newline_json_parse.py

Field report (run log, 2026-08-21): Gate1's presence-check LLM answered
with syntactically-almost-valid JSON where the "evidence" string value
contained a literal, unescaped newline (copy-pasted straight from a
multi-line docstring in the source) instead of an escaped "\\n":

    {"verdict": "confirmed", "evidence": "class Foo(RuntimeError):
        \"\"\"docstring...\"\"\"", "reason": "..."}

`json.loads` in its default *strict* mode rejects any literal control
character (raw newline, tab, etc.) inside a JSON string per the JSON
spec, raising "Invalid control character at: line 1 column N". This is
NOT a truncation problem (more max_tokens does nothing) and, at
temperature=0.0, not reliably a sampling problem either — the model's
habit of pasting a real newline instead of writing "\\n" can repeat
across retries regardless of temperature, since a slightly different
sample can still contain the exact same substring copied from the
(deterministic) source code shown to it.

This test:
  1. Reproduces the failure directly against
     Gate1Filter._parse_presence_response with a minimal instance
     (no network/config wiring needed beyond what the constructor
     requires).
  2. Asserts the specific failure signature from the log ("Invalid
     control character" from a plain json.loads) is exactly what's
     happening, so this test can't silently pass for an unrelated
     reason.
  3. Asserts the *fixed* behaviour: the same raw text must parse
     successfully (unparseable=False) and produce the correct verdict,
     with evidence lenient-matched against a supplied code_block.
"""

from __future__ import annotations

import configparser
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.gate1_filter import Gate1Filter


def _make_filter() -> Gate1Filter:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url":   "http://localhost:1337/v1",
            "api_key":    "test",
            "model":      "test-model",
            "api_format": "openai",
        },
        "gate1": {
            "temperature": "0.0",
            "max_tokens":  "512",
            "skip_llm":    "false",
        },
        "loop": {"timeout_seconds": "10"},
    })
    return Gate1Filter(
        config=cfg, base_url="http://localhost:1337/v1",
        api_key="test", model="test-model", api_format="openai", verify_ssl=False,
    )


# The exact shape from the field log: a docstring pasted verbatim (with a
# real newline) into the "evidence" string value instead of "\\n". Uses
# single-quoted triple-quotes so the ONLY JSON defect under test is the
# literal newline — embedding literal double quotes here would be a
# second, unrelated defect (unescaped `"` breaking the string boundary)
# that strict=False does not and should not paper over.
_EVIDENCE_SNIPPET = (
    "class NonDeterministicPayload(RuntimeError):\n"
    "    '''Raised when a structural payload contains a clock-dependent "
    "field.'''"
)
_RAW_WITH_LITERAL_NEWLINE = (
    '{"verdict": "confirmed", '
    '"evidence": "' + _EVIDENCE_SNIPPET + '", '
    '"reason": "missing context param on the exception"}'
)


def test_raw_text_is_rejected_by_plain_strict_json_loads():
    """Sanity check the fixture itself reproduces the log's exact
    failure signature, so a change to the fixture can't silently make
    this test meaningless."""
    with pytest.raises(json.JSONDecodeError) as exc_info:
        json.loads(_RAW_WITH_LITERAL_NEWLINE)
    assert "Invalid control character" in str(exc_info.value)


def test_literal_newline_in_evidence_is_parsed_not_failed_closed():
    """This is the behaviour that must hold after the fix: a response
    whose only defect is an unescaped literal newline inside a string
    value must NOT be treated as unparseable/fail-closed — it must be
    read successfully."""
    filt = _make_filter()
    code_block = (
        "def build():\n"
        "    " + _EVIDENCE_SNIPPET + "\n"
        "    return NonDeterministicPayload\n"
    )

    confirmed, reason, unparseable = filt._parse_presence_response(
        _RAW_WITH_LITERAL_NEWLINE, "Add custom exception for determinism violations",
        code_block=code_block,
    )

    assert unparseable is False, (
        f"literal-newline JSON was failed-closed as unparseable instead "
        f"of being read successfully; reason={reason!r}"
    )
    assert confirmed is True
