"""HARDEN-20 judging gaps: a prompt with no source, a typed head in a .js file, an empty file's ending."""

import pytest

from tools.actions import _edit_line_ends
from tools.block_extractor import extract_block
from tools.prompt_parser import parse_prompt

_APP = "def handle_request(req):\n    return req\n\nclass parse_data:\n    pass\n"


@pytest.mark.parametrize("prompt, want", [
    ("what does the handle_request function return in app.py", "handle_request"),
    ("why does the handle_request function fail in app.py", "handle_request"),
    ("how does the handle_request function work in app.py", "handle_request"),
    ("what does the parse_data class represent in app.py", "parse_data"),
    ("tell me what the handle_request function returns in app.py", "handle_request"),
])
@pytest.mark.parametrize("source", ["", _APP])
def test_c_a_prompt_names_its_symbol_with_or_without_a_source(prompt, want, source):
    assert parse_prompt(prompt, source=source).target_name == want


def test_c_a_symbol_really_named_fail_is_found_when_the_source_defines_it():
    parsed = parse_prompt("explain the function fail in app.py", source="def fail():\n    pass\n")
    assert parsed.target_name == "fail"


def test_c_a_declaration_with_a_code_reference_is_taken_without_a_source():
    assert parse_prompt("explain def work(x) in app.py").target_name == "work"


@pytest.mark.parametrize("ext", [".js", ".jsx"])
@pytest.mark.parametrize("name, source", [
    ("onSave", "interface I {\n  onSave: (value: string) => void;\n}\n"),
    ("onOpen", "const t = {\n  onOpen: (id: number) => Promise<void>,\n};\n"),
    ("cb", "const t = {\n  cb: (a, b?: number) => void,\n};\n"),
])
def test_a_a_typed_parameter_in_a_js_file_is_still_a_type_member(ext, name, source):
    assert extract_block(source, name, ext) == ""


@pytest.mark.parametrize("ext", [".js", ".jsx"])
@pytest.mark.parametrize("name, source", [
    ("h", "const o = {\n  h: (a = {x: 1, y: 2}) => undefined,\n};\n"),
    ("g", "const o = {\n  g: ({a: b}, c) => null,\n};\n"),
    ("k", "const o = {\n  k: (cb = () => 1, d) => fallback,\n};\n"),
])
def test_a_an_object_default_or_a_destructuring_colon_is_not_an_annotation(ext, name, source):
    assert extract_block(source, name, ext).lstrip().startswith(name + ":")


def test_b_an_empty_file_has_no_style_so_the_reply_is_lf(tmp_path):
    dest = tmp_path / "empty.txt"
    dest.write_bytes(b"")
    assert _edit_line_ends(str(dest), "a\r\nb\r\n") == "a\nb\n"


@pytest.mark.parametrize("prompt, want", [("improve def get in api.py", "get"), ("improve func show in ui.py", "show")])
def test_c_a_prefix_keyword_names_its_word_even_when_the_source_has_not_defined_it_yet(prompt, want):
    assert parse_prompt(prompt, source="def other():\n    pass\n").target_name == want
