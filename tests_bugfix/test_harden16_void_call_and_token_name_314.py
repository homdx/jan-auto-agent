"""tests_bugfix/test_harden16_void_call_and_token_name_314.py — HARDEN-16 pins two field
reports, both reproduced on 0815666.

* A: `extract_block` read `void sendMetrics(event);` in a TypeScript file as a declaration of
  `sendMetrics`. `void` is a return type in Java and C, so it was not in `_CALL_HEAD_WORDS`,
  and the declaration pass took the head for its own — Gate 1's Stage A then counted the call
  as a definition and let through a claim that the file never defines the function.
* B: the bare-token mask of HARDEN-12 C rewrote a provider token that is also an identifier —
  `if AKIA… in valid_keys`, `AKIA….split()`, `{AKIA…}` went out as `here_your_key`, and an
  `/edit` that touched a neighbouring line wrote the placeholder back into a working file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.block_extractor import extract_block  # noqa: E402
from tools.llm_stream import mask_api_key  # noqa: E402


_TRACK = (
    "export async function track(event: Event) {\n"
    "  void sendMetrics(event);\n"
    "  await save(event);\n"
    "}\n"
)


def test_a_void_call_is_not_read_as_a_declaration():
    """Before the fix the call answered as a declaration, so a missing function counted as present."""
    assert extract_block(_TRACK, "sendMetrics", ".ts") == ""


def test_void_is_a_return_type_in_java_and_c():
    """The same word in Java and C is a return type; the declaration must still be found."""
    assert extract_block("void main(void) { print(1); }", "main", ".c") == \
        "void main(void) { print(1); }"
    assert extract_block("class W {\n  void reset();\n}\n", "reset", ".c") == "  void reset();"


def test_a_token_used_as_a_name_is_not_rewritten():
    """Before the fix every one of these went out with `here_your_key` where a name stood."""
    for line in (
        "if AKIAIOSFODNN7EXAMPLE in valid_keys:",
        "assert resp.key == AKIAIOSFODNN7EXAMPLE",
        'print(f"Key: {AKIAIOSFODNN7EXAMPLE}")',
        'AKIAIOSFODNN7EXAMPLE.split(",")',
        "x = (AKIAIOSFODNN7EXAMPLE).strip()",
        "d = {AKIAIOSFODNN7EXAMPLE: 1}",
        "g = `Key: ${AKIAIOSFODNN7EXAMPLE}`",
    ):
        assert mask_api_key(line) == line


def test_a_token_in_a_value_position_is_still_masked():
    """Leaking is worse than a placeholder, so a value stays masked wherever it sits."""
    for line, secret in (
        ("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("key: ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
        ('token = "gsk_abcdefghijklmnopqrstuvwxyz0123456789"', "gsk_abcdefghijklmnopqrstuvwxyz0123456789"),
        ("Authorization: xai-abcdefghijklmnopqrstuvwxyz0123456789", "xai-abcdefghijklmnopqrstuvwxyz0123456789"),
        ("key is sk-live-aaaaaaaaaaaaaaaaaaa1 here", "sk-live-aaaaaaaaaaaaaaaaaaa1"),
        ("key AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ):
        out = mask_api_key(line)
        assert secret not in out
        assert "here_your_key" in out


def test_an_edit_of_a_neighbouring_line_keeps_the_name_line():
    """The model is shown the file as written, so it can only change what it was told to."""
    source = (
        "def check(value):\n"
        "    if AKIAIOSFODNN7EXAMPLE in valid_keys:\n"
        "        return True\n"
    )
    assert mask_api_key(source) == source
    edited = source.replace("return True", "return False")
    assert mask_api_key(edited) == edited
