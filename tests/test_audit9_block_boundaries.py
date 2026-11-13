"""AUDIT-9 TypeScript, call-head and C++ lexical boundary regressions."""

import pytest

from tools.block_extractor import extract_block, get_context_lines


def _block(source: str, ext: str = ".ts") -> str:
    return extract_block(source, "f", ext)


def test_typescript_result_types_and_generic_constraints_keep_body():
    cases = [
        "function f(x: number): { a: number; b: string } { return {a:1,b:'x'}; }\nfunction other() {}",
        "function f(x: number): Promise<Array<{ a: number }>> { return x; }\nfunction other() {}",
        "function f<T extends { id: number }>(x: T): T { return x; }\nfunction other() {}",
        "function f<T extends (a: number) => void>(x: T): T { return x; }\nfunction other() {}",
        "class C {\n  m<T extends { a: 1 }>(x: T): { b: number } { return {b:1}; }\n}",
        "const C = {\n  m<T extends { a: 1 }>(x: T): { b: number } { return {b:1}; }\n};",
    ]
    for source in cases:
        target = "m" if " m<" in source else "f"
        result = extract_block(source, target, ".ts")
        assert "return" in result and "}" in result
        assert "function other" not in result


def test_existing_typescript_signature_controls_remain_supported():
    for source in (
        "function f(x: unknown): x is string { return typeof x === 'string'; }",
        "function f<T>(x: T): T extends string ? { value: T } : T { return x as any; }",
        "function f(): () => void { return () => {}; }",
        "const f = <T>(x: T): { value: T } => ({value:x});",
        "function f() { return 1; }",
    ):
        assert _block(source)


def test_multiline_if_call_before_definition_is_skipped_for_ts_java_cpp():
    sources = [
        ("function g() {\n  if (a &&\n      f(x)) {\n    run();\n  }\n}\nfunction f(y) {\n  return y;\n}", ".ts"),
        ("class A { void g() { if (a &&\n f(x)) { run(); } } int f(int y) { return y; } }", ".java"),
        ("class A { void g() { if (a &&\n f(x)) { run(); } } int f(int y) { return y; } };", ".cpp"),
    ]
    for source, ext in sources:
        result = extract_block(source, "f", ext)
        assert "return y" in result
        assert "run();" not in result


def test_call_like_head_continued_from_operator_is_skipped():
    for continuation in ("&&", "||", "+"):
        source = (
            "function g() {\n"
            f"  ready {continuation}\n"
            "    f(x) { run(); }\n"
            "}\n"
            "function f(y) { return y; }"
        )
        result = _block(source)
        assert result == "function f(y) { return y; }"

    source = (
        "function g() {\n"
        "  ready && // continued\n"
        "    f(x) { run(); }\n"
        "}\n"
        "function f(y) { return y; }"
    )
    assert _block(source) == "function f(y) { return y; }"


def test_object_method_after_comma_in_a_call_or_array_is_preserved():
    definition = "  f() { return 1 }"
    obj = "{\n  a() {},\n" + definition + "\n}"
    for source in (
        "register(" + obj + ");",
        "const items = [" + obj + "];",
        "register(0, /* options */ " + obj + ");",
        "const item = " + obj + ";",
    ):
        assert _block(source) == definition


def test_continuation_respects_comment_and_regex_boundaries():
    definition = "function f() { return true; }"
    for condition in (
        "ready /* explanatory note */ &&",
        "/[//]/.test('x') &&",
        "ready && /* trailing\n    note */",
        "ready &&\n    // note on its own line",
        "ready && // " + "note " * 100,
    ):
        source = (
            "function g(ready: boolean) {\n"
            f"  return {condition}\n"
            "    f() && (() => { return true; })();\n"
            "}\n" + definition
        )
        assert _block(source) == definition


def test_string_with_comment_markers_ends_the_class_field_before_a_method():
    source = 'class C {\n  label = "/* text */ // text" // comment\n  f() { return 1 }\n}'
    assert _block(source) == "  f() { return 1 }"


def test_cpp_digit_separator_and_spliced_line_comment_do_not_hide_body_end():
    digit_sep = "int f() {\n  int n = 1'000;\n  if (n) { return 1; }\n  return 2;\n}\nint other() { return 3; }\n"
    comment = "int f() {\n  // note \\\n  }\n  return 1;\n}\nint other() { return 3; }\n"
    for source in (digit_sep, comment):
        result = extract_block(source, "f", ".cpp")
        assert "other()" not in result
        assert result.rstrip().endswith("}")
    assert "1'000" in extract_block(digit_sep, "f", ".cpp")


def test_typescript_generic_head_and_body_stop_before_the_next_statement():
    for definition, following in (
        ("function f<T>(x: T): T { return x }", "function other<U>(x: U): U { return x }"),
        ("function f(): void {}", "{ console.log('next'); }"),
        ("function f(): number { return 1 }", "[1].forEach(console.log)"),
    ):
        assert _block(definition + "\n" + following) == definition


def test_typescript_comments_are_not_control_conditions():
    assert _block("// if ( text only\nfunction f() { return 1 }") == "function f() { return 1 }"
    assert _block("class C {\n  // reason:\n  f() { return 1 }\n}") == "  f() { return 1 }"


def test_a_lambda_after_a_call_does_not_end_the_multiline_condition():
    for ext, caller, definition in (
        (".cpp", "bool f();\nint main() {\n  if (true &&\n    f() && [] { return true; }()) {}\n}",
         "bool f() { return true; }"),
        (".ts", "function main() {\n  if (true &&\n    f() && (() => { return true; })()) {}\n}",
         "function f() { return true; }"),
    ):
        assert _block(caller + "\n" + definition, ext) == definition


def test_typescript_balances_object_constraints_and_return_types():
    definition = "function f<T extends { id: number; name: string }>(x: T): { id: number } { return x }"
    for ext in (".ts", ".tsx"):
        assert _block(definition, ext) == definition
    for name in ("is", "asserts"):
        named_type = f"function f(): {name} {{ return 1 }}"
        assert _block(f"type {name} = number;\n" + named_type) == named_type


def test_cpp_character_literal_and_crlf_splice_preserve_exact_body():
    for definition in (
        "int f() { auto c = u8'a'; return c; }",
        "int f() {\r\n  // note \\\r\n  }\r\n  return 1;\r\n}",
    ):
        assert _block(definition + "\nint other() { return 2; }", ".cpp") == definition


# ── AUDIT-9 follow-up (357): a C++ definition is not a call continuation ─────────────────────────

CPP_NEIGHBOUR = "int other(int v)\n{\n    return v + 1;\n}\n"
CPP_BEFORE = "int before(int v)\n{\n    return v - 1;\n}\n"

#: (symbol, the definition exactly as it stands in the source, the lines before it)
SPLIT_RETURN_TYPES = [
    ("duplicate", "const char *\nduplicate(const char *s)\n{\n    return s;\n}", ""),
    ("assign", "Item &\nassign(Item &value)\n{\n    return value;\n}", "struct Item {};\n\n"),
    ("move_it", "Item &&\nmove_it(Item &&value)\n{\n    return static_cast<Item &&>(value);\n}", "struct Item {};\n\n"),
    ("make_vec", "std::vector<int> *\nmake_vec(int n)\n{\n    return new std::vector<int>(n);\n}", ""),
    ("make_map", "std::map<int, int> *\nmake_map()\n{\n    return nullptr;\n}", ""),
    ("split", "char **\nsplit(const char *s)\n{\n    return nullptr;\n}", ""),
    ("lookup", "static const Item &\nlookup(int k)\n{\n    static Item it;\n    return it;\n}", "struct Item {};\n\n"),
    ("get", "Foo::Bar *\nFoo::get(int k)\n{\n    return nullptr;\n}",
     "struct Foo { struct Bar {}; Bar *get(int); };\n\n"),
]


def test_cpp_definition_whose_return_type_line_ends_in_star_or_ampersand_is_extracted_exactly():
    for symbol, definition, preamble in SPLIT_RETURN_TYPES:
        for ext in (".cpp", ".hpp", ".cc", ".c"):
            alone = preamble + definition + "\n"
            between = preamble + CPP_BEFORE + "\n" + definition + "\n\n" + CPP_NEIGHBOUR
            assert extract_block(alone, symbol, ext).strip() == definition, (symbol, ext)
            # A neighbour on each side: returning too much source fails as well as returning none.
            assert extract_block(between, symbol, ext).strip() == definition, (symbol, ext)


def test_cpp_split_return_type_definition_survives_crlf_line_ends():
    definition = SPLIT_RETURN_TYPES[0][1]
    source = (definition + "\n\n" + CPP_NEIGHBOUR).replace("\n", "\r\n")
    assert extract_block(source, "duplicate", ".cpp").replace("\r\n", "\n").strip() == definition


CTOR = "explicit Item(int n) : n_(n) {}"


def _class_with(label_lines: str, ctor: str = CTOR, tail: str = "    int n_ = 0;\n") -> str:
    return f"namespace N {{ struct Item {{\n{label_lines}    {ctor}\n{tail}}}; }}\n"


def test_cpp_constructor_after_an_access_label_returns_its_own_block():
    for labels in (
        "public:\n", "protected:\n", "private:\n", "public: // the api\n", "public:\n\n", "private:  \n",
        "public slots:\n", "Q_SIGNALS:\n", "public:\n    // the constructor follows\n",
    ):
        for tail in ("    int n_ = 0;\n", "private:\n    int n_;\n"):
            source = _class_with(labels, tail=tail)
            assert extract_block(source, "Item", ".cpp").strip() == CTOR, (labels, tail)


def test_cpp_constructor_with_an_initializer_list_after_an_access_label():
    ctor = "Item(int a, int b) : a_(a), b_(b) {}"
    assert extract_block(_class_with("public:\n", ctor, "    int a_, b_;\n"), "Item", ".cpp").strip() == ctor


def test_cpp_method_after_an_access_label_is_still_found():
    method = "int size() const { return n_; }"
    source = f"class Item {{\npublic:\n    {method}\nprivate:\n    int n_ = 0;\n}};\n"
    assert extract_block(source, "size", ".cpp").strip() == method


def _caller(body: str, callee: str = "scale") -> tuple[str, str]:
    definition = f"int {callee}(int v)\n{{\n    return v * 2;\n}}"
    return f"int run(int a, int z, bool ready)\n{{\n{body}\n}}\n\n{definition}\n", definition


def test_cpp_call_after_a_line_that_ends_in_an_operator_stays_a_call():
    lam = "scale(z, [](int q) { return q; })"
    bodies = [
        "    bool ok = ready &&\n        scale(z);\n    return ok;",
        "    int y = a *\n        scale(z);\n    return y;",
        "    int y = a &\n        scale(z);\n    return y;",
        "    return a *\n        scale(z);",
        "    return a &\n        scale(z);",
        "    return ready &&\n        scale(z);",
        "    int y = ready ? a :\n        scale(z);\n    return y;",
        "    switch (a) {\n    case 1:\n        scale(z);\n        break;\n    }\n    return 0;",
        "    if (a) goto done;\ndone:\n    scale(z);\n    return 0;",
        "    int y = a +\n        // the scale of z\n        scale(z);\n    return y;",
        "    bool ok = ready &&\n        " + lam + ";\n    return ok;",
        "    int y = a *\n        " + lam + ";\n    return y;",
        "    return a &\n        " + lam + ";",
        "    switch (a) {\n    case 1:\n        " + lam + ";\n    }\n    return 0;",
        "    if (a > 1 &&\n        scale(z) > 3) {\n        return 1;\n    }\n    return 0;",
    ]
    for ext in (".cpp", ".c"):
        for body in bodies:
            source, definition = _caller(body)
            assert extract_block(source, "scale", ext).strip() == definition, (ext, body)


def test_cpp_expression_line_that_looks_like_a_type_is_not_a_return_type():
    # `ready &&` and `a *` read like `Item &&` and `Item *`; the parameter list that is not followed by a
    # body is what tells a call from a definition.
    for line in ("ready &&", "a *", "ready & flag &", "x * y *", "a * 2 *"):
        source, definition = _caller(f"    bool ok = {line}\n        scale(z);\n    return ok;")
        assert extract_block(source, "scale", ".cpp").strip() == definition, line
    source, definition = _caller("    ready &&\n        scale(z);\n    return 0;")
    assert extract_block(source, "scale", ".cpp").strip() == definition


def test_cpp_initializer_list_members_are_not_definitions():
    assert extract_block("struct Item {\n    int n_;\n    Item(int n) :\n        n_(n) {}\n};\n", "n_", ".cpp") == ""
    assert extract_block("Item::Item(int n) :\n    base_(n)\n{\n}\n", "base_", ".cpp") == ""
    assert extract_block("Item::Item(int a, int b) :\n    a_(a),\n    b_(b)\n{\n}\n", "b_", ".cpp") == ""


def test_other_languages_keep_treating_a_trailing_star_or_colon_as_a_continuation():
    ts = ("function run(a, z) {\n  return a *\n    scale(z);\n}\n\n"
          "function scale(v) {\n  return v * 2;\n}\n")
    assert extract_block(ts, "scale", ".ts").strip() == "function scale(v) {\n  return v * 2;\n}"
    java = ("class A {\n  int run(boolean r, int z) {\n    boolean ok = r &&\n      scale(z) > 0;\n"
            "    return ok ? 1 : 0;\n  }\n\n  int scale(int v) {\n    return v * 2;\n  }\n}\n")
    assert extract_block(java, "scale", ".java").strip() == "int scale(int v) {\n    return v * 2;\n  }"
    keyed = "const o = {\n  public:\n    first() { return 1; },\n};\n"
    assert extract_block(keyed, "first", ".ts") == ""


def _gate1_existence(tmp_path, source: str, symbol: str, filename: str = "sample.cpp"):
    """The real Gate1Filter.filter() with skip_llm: only the existence stage runs, no model is called."""
    import configparser

    from tools.auto.architect import CandidateTask, CitedLocation
    from tools.auto.gate1_filter import Gate1Filter

    (tmp_path / filename).write_text(source, encoding="utf-8")
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:1/v1", "api_key": "t", "model": "m", "api_format": "openai"},
        "gate1": {"skip_llm": "true"},
        "loop": {"timeout_seconds": "5"},
    })
    gate = Gate1Filter(config=cfg, base_url="http://localhost:1/v1", api_key="t", model="m", verify_ssl=False)
    candidate = CandidateTask(
        title=f"fix {symbol}", instruction=f"fix {symbol}", target_files=[filename], acceptance_check="true",
        cited_location=CitedLocation(file=filename, symbol=symbol))
    return gate.filter([candidate], tmp_path)


def test_gate1_existence_stage_accepts_the_recovered_cpp_symbols(tmp_path):
    cases = [
        ("duplicate", SPLIT_RETURN_TYPES[0][1] + "\n\n" + CPP_NEIGHBOUR),
        ("assign", "struct Item {};\n\n" + SPLIT_RETURN_TYPES[1][1] + "\n"),
        ("Item", "namespace N { struct Item {\npublic:\n    " + CTOR + "\nprivate:\n    int n_;\n}; }\n"),
    ]
    for symbol, source in cases:
        accepted, rejected = _gate1_existence(tmp_path, source, symbol)
        assert [c.cited_location.symbol for c in accepted] == [symbol], [r.reason for r in rejected]
        assert not rejected


def test_gate1_existence_stage_still_rejects_a_symbol_that_is_not_there(tmp_path):
    accepted, rejected = _gate1_existence(tmp_path, SPLIT_RETURN_TYPES[0][1] + "\n", "no_such_symbol")
    assert not accepted and len(rejected) == 1


@pytest.mark.parametrize(("extension", "statement"), [
    (".cpp", "    a *\n        scale(n) + int{1};"),
    (".cpp", "    a &\n        scale(n) + int{1};"),
    (".cpp", "    a *\n        scale(n) && int{1};"),
    (".cpp", "    if (a) goto signals;\nsignals:\n    scale(n) + int{1};"),
    (".c", "    if (a) goto public;\npublic:\n    scale(n) + (struct Box){1}.value;"),
])
def test_cpp_expression_brace_does_not_supply_a_definition(tmp_path, extension, statement):
    (tmp_path / "scale.h").write_text("int scale(int n);\n", encoding="utf-8")
    caller = ('#include "scale.h"\nstruct Box { int value; };\n'
              f"int run(int a, int n) {{\n{statement}\n    return n;\n}}\n")
    definition = "int scale(int n) { return 2 * n; }"
    marker = "// Immediately before the definition.\n"
    source = f"{caller}\n{marker}{definition}\n"
    assert extract_block(source, "scale", extension) == definition
    assert get_context_lines(source, "scale", before=1, file_ext=extension) == marker
    assert extract_block(caller, "scale", extension) == ""
    accepted, rejected = _gate1_existence(tmp_path, caller, "scale", f"sample{extension}")
    assert not accepted and len(rejected) == 1
    assert "symbol 'scale' not found" in rejected[0].reason


def test_cpp_split_return_type_keeps_member_qualifiers():
    definition = ("    Item &\n"
                  "    assign(Item &value) & noexcept(noexcept(Item{}))\n"
                  "    { return value; }")
    source = f"struct Item {{}};\nstruct Owner {{\n{definition}\n}};\n"
    assert extract_block(source, "assign", ".hpp") == definition


@pytest.mark.parametrize("class_head", ["struct [[nodiscard]] Item", "struct alignas(8) Item"])
def test_cpp_access_label_inside_class_with_attribute(class_head):
    source = _class_with("public:\n").replace("struct Item", class_head)
    assert extract_block(source, "Item", ".hpp").strip() == CTOR


def test_cpp_trailing_struct_return_type_does_not_open_a_class_scope():
    source = ('#include "scale.h"\n'
              "struct Item {};\n"
              "auto run(int n) -> struct Item {\n"
              "    if (n) goto signals;\n"
              "signals:\n"
              "    scale(n) + int{1};\n"
              "    return {};\n"
              "}\n")
    assert extract_block(source, "scale", ".cpp") == ""
