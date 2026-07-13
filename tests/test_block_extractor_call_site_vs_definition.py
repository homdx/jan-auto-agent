"""tests/test_block_extractor_call_site_vs_definition.py

Bug found during review of tools/block_extractor.py's brace-language
strategy (used to ground/extract a named function or class for every
non-Python language the tool supports — the same code path gate1's
existence check, the /show and /edit commands, and auto mode's context
broker all rely on).

_brace_candidate_patterns()'s "(...)"-shaped patterns (function/func/
modifier-prefixed/return-typed/bare-name — every one that ends in a
literal "(") match a plain function CALL just as easily as a definition:
"helper(1, 2);" matches the exact same regex as "function helper(1, 2) {".
Likewise the arrow-function pattern matches a brace-less single-expression
body ("const add = (a, b) => a + b;") just as easily as a braced one.

_extract_brace_block used to handle this by scanning forward from ANY such
match for the next "{" with no bound and no way to tell a false positive
apart from a real one. If the false-positive match's line came earlier in
the file than the real definition (an ordinary, common situation — e.g. a
helper function called before it's defined, or hoisted, or just declared
below its first use), that unbounded scan would walk straight past the
rest of the enclosing function, across any unrelated code in between, and
return whichever "{" happened to come first: a block starting at the call
site and improperly spanning into unrelated code, or an entirely
unrelated function's body, while the real target definition was never
reached at all.

The existing tests/test_block_extractor_brace_langs.py::
test_no_false_positive_on_call_site did not catch this: its call site sits
two lines above the real definition with nothing else in between, so the
buggy unbounded scan happens to land on the real definition's own brace by
coincidence, and its assertion (`"int helper(int n)" in blk`, a substring
check) still passes even though the returned block actually starts at the
call site and improperly includes the end of the *calling* function too.
That pre-existing test still passes after this fix — now for the right
reason (see TestExistingCallSiteTestNowExact below).

Fix: tools.block_extractor._find_definition_open_brace requires a "{" to
follow a signature match within a short, bounded window (after first
skipping a balanced argument list, so nested parens/default values can't
confuse it) — and rejects the match outright if a ";" comes first, or if
nothing conclusive appears within the bound. This can only make matches
*more* conservative: every case that used to correctly find a definition
immediately followed by "{" still does (verified below across
Java/C/Kotlin/Rust/Go/TS, multi-line signatures, throws/generic clauses,
and nested-paren default values); the only behaviour removed is
confidently returning the wrong block.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.block_extractor import extract_block  # noqa: E402


class TestCallSiteBeforeDefinition:
    """A plain call to `name(...)` earlier in the file must never be
    mistaken for `name`'s own definition."""

    def test_call_in_different_function_does_not_hijack_extraction(self):
        src = (
            "function main() {\n"
            "    helper(1, 2);\n"
            "    console.log(\"done\");\n"
            "}\n"
            "\n"
            "function unrelated() {\n"
            "    doSomethingElse();\n"
            "    doMore();\n"
            "}\n"
            "\n"
            "function helper(a, b) {\n"
            "    return a + b;\n"
            "}\n"
        )
        blk = extract_block(src, "helper", ".js")
        assert "return a + b" in blk
        assert "doSomethingElse" not in blk
        assert blk.strip().startswith("function helper")

    def test_call_immediately_before_real_definition_extracts_tight_block(self):
        # Regression fixture for the exact scenario in
        # test_block_extractor_brace_langs.py::test_no_false_positive_on_call_site,
        # asserted more precisely here (see TestExistingCallSiteTestNowExact).
        src = (
            "class X {\n"
            "    void caller() {\n"
            "        helper(1);\n"
            "    }\n"
            "    int helper(int n) {\n"
            "        return n;\n"
            "    }\n"
            "}\n"
        )
        blk = extract_block(src, "helper", ".java")
        assert blk.strip().startswith("int helper(int n)")
        assert "caller" not in blk

    def test_call_with_no_definition_anywhere_returns_empty(self):
        src = "function main() {\n    helper(1, 2);\n}\n"
        assert extract_block(src, "helper", ".js") == ""


class TestBraceLessArrowFunction:
    """A single-expression arrow body (no braces) has nothing this
    strategy can extract — it must fail closed (empty), not grab an
    unrelated later block."""

    def test_braceless_arrow_does_not_hijack_unrelated_block(self):
        src = (
            "const add = (a, b) => a + b;\n"
            "\n"
            "function unrelatedThing() {\n"
            "    doStuffA();\n"
            "    doStuffB();\n"
            "}\n"
        )
        blk = extract_block(src, "add", ".js")
        assert "doStuffA" not in blk

    def test_braced_arrow_function_still_works(self):
        src = "const add = (a, b) => {\n    return a + b;\n};\n"
        blk = extract_block(src, "add", ".js")
        assert "return a + b" in blk


class TestGenuineDefinitionsStillFound:
    """The bounded, arg-list-aware check must not reject any real,
    ordinarily-formatted definition."""

    def test_java_throws_clause_between_paren_and_brace(self):
        src = (
            "public class App {\n"
            "    public String getName() throws java.io.IOException {\n"
            "        return this.name;\n"
            "    }\n"
            "}\n"
        )
        blk = extract_block(src, "getName", ".java")
        assert "return this.name" in blk

    def test_multiline_parameter_list(self):
        src = (
            "function veryLongName(\n"
            "    argOne,\n"
            "    argTwo,\n"
            "    argThree\n"
            ") {\n"
            "    return argOne + argTwo + argThree;\n"
            "}\n"
        )
        blk = extract_block(src, "veryLongName", ".js")
        assert "return argOne" in blk

    def test_typescript_generic_return_type(self):
        src = (
            "class Repo {\n"
            "    async findAll(): Promise<Array<User>> {\n"
            "        return this.db.query();\n"
            "    }\n"
            "}\n"
        )
        blk = extract_block(src, "findAll", ".ts")
        assert "return this.db.query" in blk

    def test_go_method_with_receiver(self):
        src = (
            "func (s *Server) Handle(w http.ResponseWriter, r *http.Request) {\n"
            "    w.Write([]byte(\"ok\"))\n"
            "}\n"
        )
        blk = extract_block(src, "Handle", ".go")
        assert "w.Write" in blk

    def test_nested_paren_default_value_does_not_confuse_arglist_skip(self):
        src = (
            "function withDefault(x = compute(1, 2)) {\n"
            "    return x;\n"
            "}\n"
            "function compute(a, b) { return a + b; }\n"
        )
        blk = extract_block(src, "withDefault", ".js")
        assert "return x;" in blk
        assert blk.strip().startswith("function withDefault")


class TestExistingCallSiteTestNowExact:
    """The pre-existing regression test's weak (substring) assertion let a
    bloated, wrong block slip through; confirm the fix makes it exact."""

    def test_block_starts_at_real_definition_not_the_call_site(self):
        src = (
            "class X {\n"
            "    void caller() {\n"
            "        helper(1);\n"
            "    }\n"
            "    int helper(int n) {\n"
            "        return n;\n"
            "    }\n"
            "}\n"
        )
        blk = extract_block(src, "helper", ".java")
        # Before the fix, this started with "        helper(1);" (the call
        # site) and included caller()'s closing brace before ever reaching
        # the real definition.
        assert blk.strip().startswith("int helper(int n) {")
        assert blk.strip().endswith("}")
        assert "void caller" not in blk


class TestGetContextLinesUsesRealDefinition:
    """get_context_lines()/_find_block_start_line_fallback had the same
    call-site false-positive bug, but worse: no genuineness check and no
    earliest-match comparison at all — just the first pattern (in a fixed
    list order) that matched anywhere in the file. This fed misleading
    "what comes before this function" context to main.py's ImprovementAgent
    call for every non-Python file."""

    def test_context_is_taken_from_before_the_real_definition(self):
        from tools.block_extractor import get_context_lines

        src = (
            "public class App {\n"
            "    public void setup() {\n"
            "        getName();\n"
            "    }\n"
            "\n"
            "    public String getName() {\n"
            "        return this.name;\n"
            "    }\n"
            "}\n"
        )
        ctx = get_context_lines(src, "getName", before=2, file_ext=".java")
        # The 2 lines immediately before the REAL definition are setup()'s
        # closing brace and the blank line after it — not anything from
        # *inside* setup(), which is what the call-site false positive
        # returned instead.
        assert "public void setup() {" not in ctx
        assert "getName();" not in ctx
        assert ctx == "    }\n\n"

    def test_context_before_definition_that_follows_much_later_call(self):
        from tools.block_extractor import get_context_lines

        src = (
            "function main() {\n"
            "    helper(1, 2);\n"
            "}\n"
            "\n"
            "// marker line right before helper\n"
            "function helper(a, b) {\n"
            "    return a + b;\n"
            "}\n"
        )
        ctx = get_context_lines(src, "helper", before=1, file_ext=".js")
        assert ctx == "// marker line right before helper\n"


class TestBlankLineBeforeDefinitionNotSweptIn:
    """A "^\\s*"-shaped pattern prefix can match starting on a blank line
    before the real signature (regex \\s crosses newlines), putting
    match.start() on that blank line instead of the signature's own line.
    Must not produce a spurious leading blank line in the extracted block,
    or shift get_context_lines' "before" window by one line."""

    def test_no_spurious_leading_blank_line_in_extracted_block(self):
        src = (
            "function unrelated() {\n"
            "    doStuffA();\n"
            "}\n"
            "\n"
            "function helper(a, b) {\n"
            "    return a + b;\n"
            "}\n"
        )
        blk = extract_block(src, "helper", ".js")
        assert not blk.startswith("\n")
        assert blk.startswith("function helper")

    def test_context_lines_not_shifted_by_blank_line(self):
        from tools.block_extractor import get_context_lines

        src = (
            "public class App {\n"
            "    public void setup() {\n"
            "        getName();\n"
            "    }\n"
            "\n"
            "    public String getName() {\n"
            "        return this.name;\n"
            "    }\n"
            "}\n"
        )
        ctx = get_context_lines(src, "getName", before=2, file_ext=".java")
        assert ctx == "    }\n\n"
