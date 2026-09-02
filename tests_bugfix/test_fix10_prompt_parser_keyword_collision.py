"""tests/test_fix10_prompt_parser_keyword_collision.py — AUTO-FIX-10.

Bug found via code reading + live execution (tools/prompt_parser.py had zero
test coverage before this file): `_has_kw()` inside `_parse_via_regex`
searched BOTH `remainder` (the prompt with the file-path match and any
def/class declaration already stripped out) AND the untouched original raw
prompt. The surrounding comment says the `\\b` word-boundary regex exists so
a keyword like "read" doesn't spuriously match inside a contiguous name like
"README.md" or "thread.py" — and it does prevent that. But it does nothing
for a keyword that is its own hyphen/dot-delimited token, which is an
entirely ordinary way to name a file or a symbol:

    show def helper in small-fix.py   -> was "show_and_improve", should be "show"
    show def helper in fix.py         -> was "show_and_improve", should be "show"
    improve def show in ui.py         -> was "show_and_improve", should be "improve"
    improve def get in api.py         -> was "show_and_improve", should be "improve"

Because checking raw re-introduces exactly the text `remainder` had already
stripped out (the file path / the declared symbol name), the only content it
adds beyond `remainder` is the filename/symbol text itself — which is
precisely where these accidental keyword collisions live.

Fix: `_has_kw` only searches `remainder`; the raw-string fallback is removed.

This also transitively fixes BUG-06 (main.py never running ImprovementAgent
for "show_and_improve") for the common phrasing "show and improve the X
function in Y.py", since without this fix Y.py containing a stray keyword
could silently downgrade the intent to plain "show" and never reach the
show_and_improve code path at all. The two bugs are independent and are
each covered by their own regression test (see
test_fix11_main_show_and_improve_gate.py for BUG-06).
"""

from __future__ import annotations

from tools.prompt_parser import parse_prompt


class TestFilenameKeywordCollision:
    """A keyword-shaped substring in the file path must not affect intent."""

    def test_show_with_hyphenated_fix_filename(self):
        r = parse_prompt("show def helper in small-fix.py")
        assert r.intent == "show"
        assert r.target_name == "helper"
        assert r.file_path == "small-fix.py"

    def test_show_with_bare_fix_filename(self):
        r = parse_prompt("show def helper in fix.py")
        assert r.intent == "show"

    def test_show_with_bare_read_filename(self):
        # "read" is a has_show keyword; read.py alone must not force "show"
        # when the user actually said "explain".
        r = parse_prompt("explain def helper in read.py")
        assert r.intent == "explain"

    def test_show_with_get_dash_filename(self):
        r = parse_prompt("explain def helper in get-data.py")
        assert r.intent == "explain"


class TestSymbolNameKeywordCollision:
    """A keyword-shaped *target symbol name* must not affect intent either."""

    def test_improve_function_literally_named_show(self):
        r = parse_prompt("improve def show in ui.py")
        assert r.intent == "improve"
        assert r.target_name == "show"

    def test_improve_function_literally_named_get(self):
        r = parse_prompt("improve def get in api.py")
        assert r.intent == "improve"
        assert r.target_name == "get"


class TestGenuineCombinedIntentStillWorks:
    """The fix must not suppress a *real* show_and_improve request."""

    def test_show_and_improve_explicit(self):
        r = parse_prompt(
            "show and improve the calculate_total function in orders.py"
        )
        assert r.intent == "show_and_improve"

    def test_show_and_optimize_explicit(self):
        r = parse_prompt("show me how to optimize load in app.py")
        assert r.intent == "show_and_improve"


class TestOrdinaryCasesUnaffected:
    """Baseline behaviour for prompts with no keyword collisions at all."""

    def test_plain_improve(self):
        assert parse_prompt("improve database.py").intent == "improve"

    def test_plain_explain(self):
        r = parse_prompt("explain the Foo class in bar.py")
        assert r.intent == "explain"
        assert r.target_name == "Foo"

    def test_no_target_show_becomes_show_imports(self):
        assert parse_prompt("show config.py").intent == "show_imports"

    def test_contiguous_letters_still_safe(self):
        # The original \b protection this comment describes must still hold.
        r = parse_prompt("improve def helper in thread.py")
        assert r.intent == "improve"
