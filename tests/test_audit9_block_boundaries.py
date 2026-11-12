"""AUDIT-9 TypeScript, call-head and C++ lexical boundary regressions."""

from tools.block_extractor import extract_block


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
