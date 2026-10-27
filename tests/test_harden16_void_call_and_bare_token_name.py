"""tests/test_harden16_void_call_and_bare_token_name.py — HARDEN-16: a `void f(x);` in
JavaScript or TypeScript is the void operator on a call, and a provider token used as an
identifier is not a value.

Two reports, both reproduced on 0815666:

* A: `extract_block` read `void sendMetrics(event);` as a declaration of `sendMetrics`, so
  Gate 1's Stage A counted the call as a definition and let through a claim that the file
  never defines it. In Java and C the same word is a return type, and there the
  declaration must still be found.
* B: the bare-token mask of HARDEN-12 C rewrote a token that is also a name —
  `if AKIA… in keys`, `AKIA….split()`, `{AKIA…}` went out as `here_your_key`, and an
  `/edit` wrote the placeholder back where a name stood. A token in a value position is
  still masked.
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


# ── Part A: `void f(x);` is the void operator on a call ──────────────────────

# The reviewer's file. `sendMetrics` is called with `void` to discard its Promise, `save`
# with `await`; neither is defined in this file.
_TRACK = (
    "export async function track(event: Event) {\n"
    "  void sendMetrics(event);\n"
    "  await save(event);\n"
    "}\n"
)

_JS_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx")


class TestAVoidIsTheVoidOperator:
    """`void` before a name in a JavaScript or TypeScript file is an operator, not a return
    type, so the head is a call and the declaration is not in the file at all."""

    def test_a_void_call_is_not_a_declaration(self):
        assert extract_block(_TRACK, "sendMetrics", ".ts") == ""

    def test_a_call_with_await_is_not_a_declaration_either(self):
        assert extract_block(_TRACK, "save", ".ts") == ""

    @pytest.mark.parametrize("ext", _JS_EXTENSIONS)
    def test_every_js_and_ts_extension_reads_void_as_a_call(self, ext):
        assert extract_block(_TRACK, "sendMetrics", ext) == ""

    def test_a_void_call_before_a_real_definition_does_not_hide_it(self):
        source = (
            "export async function track(event: Event) {\n"
            "  void sendMetrics(event);\n"
            "}\n"
            "function sendMetrics(event) {\n"
            "  log(event);\n"
            "}\n"
        )
        assert extract_block(source, "sendMetrics", ".ts") == (
            "function sendMetrics(event) {\n  log(event);\n}"
        )

    @pytest.mark.parametrize("prefix", ["await", "typeof", "delete", "new", "yield", "!"])
    def test_the_other_call_operators_already_read_as_calls(self, prefix):
        # `await`, `typeof`, `delete`, `new`, `yield` are in _CALL_HEAD_WORDS and `!` fails the
        # return-type character test, so only `void` needed a rule of its own.
        source = (
            "export async function track(event: Event) {\n"
            f"  {prefix} sendMetrics(event);\n"
            "  await save(event);\n"
            "}\n"
        )
        assert extract_block(source, "sendMetrics", ".ts") == ""


class TestAVoidIsStillAReturnType:
    """The same word in Java and C, and after a parameter list, is a return type: the
    declaration must still be found."""

    @pytest.mark.parametrize("source, name, ext, block", [
        ("void main(void) { print(1); }", "main", ".c", "void main(void) { print(1); }"),
        ("void sendMetrics(Event e) {\n  log(e);\n}\n", "sendMetrics", ".c",
         "void sendMetrics(Event e) {\n  log(e);\n}"),
        ("class W {\n  void reset();\n}\n", "reset", ".c", "  void reset();"),
        ("class T {\n  abstract void run();\n}\n", "run", ".c", "  abstract void run();"),
        ("class J {\n  void reset();\n}\n", "reset", ".java", "  void reset();"),
        ("interface S {\n  reset(): void;\n}\n", "reset", ".ts", "  reset(): void;"),
        ("class T {\n  abstract run(): void;\n}\n", "run", ".ts", "  abstract run(): void;"),
        ("declare function sendMetrics(e: Event): void;", "sendMetrics", ".ts",
         "declare function sendMetrics(e: Event): void;"),
    ])
    def test_a_void_return_type_still_finds_the_declaration(self, source, name, ext, block):
        found = extract_block(source, name, ext)
        assert found == block
        assert found.rstrip().endswith(";") or "{" in found


# ── Part B: a bare token that is a name is not a value ───────────────────────

_AWS = "AKIAIOSFODNN7EXAMPLE"
_GITHUB = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
_GROQ = "gsk_abcdefghijklmnopqrstuvwxyz0123456789"
_XAI = "xai-abcdefghijklmnopqrstuvwxyz0123456789"
_GITLAB = "glpat-abcdefghijklmnopqrstuvwxyz1234"
_HF = "hf_abcdefghijklmnopqrstuvwxyz012345"


class TestABareTokenUsedAsAnIdentifier:
    """The syntax proves the token is a name, so the mask leaves it alone and an `/edit` of
    the file can only change the line it was asked to change."""

    @pytest.mark.parametrize("line", [
        # the six lines of the ticket
        f"if {_AWS} in valid_keys:",
        f"assert resp.key == {_AWS}",
        f'print(f"Key: {{{_AWS}}}")',
        f"{_AWS}.split(\",\")",
        f"x = ({_AWS}).strip()",
        f"d = {{{_AWS}: 1}}",
        # a call and an index, in either order
        f"{_AWS}(1)",
        f"y = {_AWS}[0]",
        f"z = {_AWS}.split(\",\")[0]",
        # an attribute of that name, in code and in a comparison
        f"k = cfg.{_AWS}",
        f"assert cfg.{_AWS} == saved",
        # the rest of the exemption list
        f"o = {{{_AWS}}}",
        f"import {_AWS} as m",
        f"from {_AWS} import helper",
        f"def {_AWS}(x):",
        f"class {_AWS}:",
        f"const {_AWS} = fetch()",
        f"let {_AWS} = 1",
        f"var {_AWS} = null",
        f"function {_AWS}(x) {{",
        f"if {_AWS} not in keys:",
        f"if {_AWS} is DEFAULT:",
        f"assert {_AWS} != DEFAULT",
        "g = `Key: ${AKIAIOSFODNN7EXAMPLE}`",
    ])
    def test_the_ticket_rows_are_untouched(self, line):
        assert mask_api_key(line) == line

    def test_the_shorter_sibling_token_is_untouched_too(self):
        # `AKIA11111` is too short to be a token, and it is a name either way.
        line = f"known = [akia11111, {_AWS}.split(\",\")]"
        assert mask_api_key(line) == line


class TestABareTokenInAValuePositionIsStillMasked:
    """Nothing changes for a value: after an `=` or a `:`, inside a quote, in a config line,
    in a header, in a comment and in prose."""

    @pytest.mark.parametrize("text, secret", [
        (f"AWS_ACCESS_KEY_ID={_AWS}", _AWS),
        (f"key: {_GITHUB}", _GITHUB),
        (f'token = "{_GROQ}"', _GROQ),
        (f"Authorization: {_XAI}", _XAI),
        (f"print(\"{_AWS}\")", _AWS),
        (f"x = 1  # const {_AWS} = 2", _AWS),
        (f"use {_GROQ} to log in", _GROQ),
        (f"token {_GITHUB}", _GITHUB),
        (f"key {_AWS}", _AWS),
        (f"export AWS_KEY {_AWS}", _AWS),
        (f"xai={_XAI}", _XAI),
        (f"glpat {_GITLAB}", _GITLAB),
        (f"hf {_HF} here", _HF),
        # the reviewer's rule as written would let these through; they are values
        (f"if \"{_AWS}\" in valid_keys:", _AWS),
        (f"if {_AWS} == \"a\":", _AWS),
        (f"if {_AWS} in [\"a\"]:", _AWS),
        # a token in a list literal stays masked: the rule has no list exemption
        (f"known = [{_AWS}, akia11111]", _AWS),
        # `in` and `is` are English too, so prose keeps its token masked
        (f"the key {_GITHUB} in config.yml was rotated", _GITHUB),
        ("key is sk-live-aaaaaaaaaaaaaaaaaaa1 here", "sk-live-aaaaaaaaaaaaaaaaaaa1"),
    ])
    def test_the_value_positions_stay_masked(self, text, secret):
        out = mask_api_key(text)
        assert secret not in out
        assert "here_your_key" in out

    def test_the_mask_is_idempotent_on_both_sides_of_the_rule(self):
        for line in (
            f"if {_AWS} in valid_keys:",
            f"AWS_ACCESS_KEY_ID={_AWS}",
            f'print(f"Key: {{{_AWS}}}")',
            f"d = {{{_AWS}: 1}}",
        ):
            once = mask_api_key(line)
            assert mask_api_key(once) == once

    def test_an_edit_of_another_line_leaves_the_name_line_byte_identical(self):
        # The model is shown the file as written, so it can only change what it was told to.
        source = (
            "def check(value):\n"
            f"    if {_AWS} in valid_keys:\n"
            "        return True\n"
            "    return False\n"
        )
        assert mask_api_key(source) == source
        edited = source.replace("return True", "return False")
        assert mask_api_key(edited) == edited
        assert mask_api_key(source).splitlines()[1] == mask_api_key(edited).splitlines()[1]


# ── The file as the model sees it ────────────────────────────────────────────


def test_a_file_with_both_shapes_comes_back_as_written():
    source = (
        "import config\n"
        f"if {_AWS} in valid_keys:\n"
        f'    log(f"Key: {{{_AWS}}}")\n'
        f"    log(cfg.{_AWS})\n"
        f'    log("{_AWS}")\n'
        f'    log(f"key {_AWS} ok")\n'
    )
    out = mask_api_key(source)
    for index in (1, 2, 3):
        assert out.splitlines()[index] == source.splitlines()[index]
    # a quoted value is a value, and so is the token inside the text of an f-string that is not
    # its own interpolation: leaking is worse than a placeholder.
    assert out.splitlines()[4] == f'    log("here_your_key")'
    assert out.splitlines()[5] == f'    log(f"key here_your_key ok")'
