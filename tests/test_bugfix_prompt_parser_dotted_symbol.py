"""tests/test_bugfix_prompt_parser_dotted_symbol.py

Bug: `_parse_via_regex`'s file-path regex matches the first `word.word`
token in the prompt. A dotted symbol reference such as "MyClass.foo" looks
identical to a bare filename to that regex, so a prompt like

    "explain MyClass.foo in app.py"

used to resolve file_path="MyClass.foo" (the wrong, non-existent "file")
instead of "app.py" (the file the user actually named, explicitly marked
with "in "). Fix: when at least one candidate match is explicitly
introduced by "in ", prefer it over any earlier bare match; only fall back
to the original first-match behavior when no "in " marker is present at
all, so prompts without that marker keep working exactly as before.
"""
from tools.prompt_parser import _parse_via_regex


def test_dotted_symbol_before_in_file_resolves_to_the_real_file():
    parsed = _parse_via_regex("explain MyClass.foo in app.py")
    assert parsed is not None
    assert parsed.file_path == "app.py"


def test_dotted_symbol_reference_with_def_still_extracts_target():
    parsed = _parse_via_regex("explain def foo in app.py")
    assert parsed is not None
    assert parsed.file_path == "app.py"
    assert parsed.target_name == "foo"


def test_bare_filename_with_no_in_marker_still_works():
    # No explicit "in " marker anywhere -> falls back to the original
    # first-match behavior, unchanged.
    parsed = _parse_via_regex("show me utils.py")
    assert parsed is not None
    assert parsed.file_path == "utils.py"


def test_multiple_in_markers_prefers_the_first_one():
    parsed = _parse_via_regex("explain foo.bar in app.py and in other.py")
    assert parsed is not None
    assert parsed.file_path == "app.py"
