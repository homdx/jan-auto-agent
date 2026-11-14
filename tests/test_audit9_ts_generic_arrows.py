"""AUDIT-9 follow-up (361): TypeScript generic arrows and wrapper assignments are extracted whole, and an arrow with a parenthesized body is not cut short.

`_angle_end` read the `>` of a nested `=>` as the closer and refused any `{`, so `const f = <T extends { id: number }>(x: T) => …`
and `const f = memo<{ id: number }>(…)` came back empty; the head scan took the `{` of `=> ({ … })` for the function body and
ended the block at the object's `}`. Only the real extractor, `get_context_lines` and the real Gate 1 existence stage run here.
"""

import configparser

import pytest

from tools.block_extractor import extract_block, get_context_lines

NEIGHBOUR = "function neighbor() { return 3; }"
MARKER = "// Immediately before the definition.\n"

#: The five definitions of the ticket, each a statement of its own.
GENERIC_OBJECT = "const lookup = <T extends { id: number }>(x: T) => { return x.id; };"
GENERIC_FUNCTION = "const lookup = <T extends (x: number) => number>(f: T) => { return f(1); };"
WRAPPER_OBJECT = "const lookup = memo<{ id: number }>(props => { return props.id; });"
WRAPPER_FUNCTION = "const lookup = useCallback<(x: number) => number>(x => x + 1);"
PARENTHESIZED_OBJECT = "const lookup = (s: { n: number }, n: number) => ({ ...s, n });"
FIVE = [GENERIC_OBJECT, GENERIC_FUNCTION, WRAPPER_OBJECT, WRAPPER_FUNCTION, PARENTHESIZED_OBJECT]


def _norm(text: str) -> str:
    """The block without its trailing `;`: a brace body ends at `}`, an expression body keeps its `;`."""
    text = text.strip()
    return text[:-1].rstrip() if text.endswith(";") else text


def _assert_whole(definition: str, ext: str = ".ts", symbol: str = "lookup") -> None:
    block = extract_block(f"{definition}\n{NEIGHBOUR}\n", symbol, ext)
    assert "neighbor" not in block, block
    assert _norm(block) == _norm(definition), block


# ── the five definitions of the ticket ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ext", [".ts", ".tsx", ".mts", ".cts"])
@pytest.mark.parametrize("definition", FIVE)
def test_the_five_definitions_come_back_whole(definition, ext):
    _assert_whole(definition, ext)


@pytest.mark.parametrize("definition", FIVE)
def test_the_definition_is_found_with_code_before_and_after_it(definition):
    source = f"import {{ memo }} from 'react';\n\nexport const before = 1;\n\n{definition}\n\n{NEIGHBOUR}\n"
    block = extract_block(source, "lookup", ".ts")
    assert _norm(block) == _norm(definition) and "neighbor" not in block and "before" not in block


@pytest.mark.parametrize("definition", FIVE)
def test_get_context_lines_anchors_on_the_definition_line(definition):
    source = f"{MARKER}{definition}\n{NEIGHBOUR}\n"
    assert get_context_lines(source, "lookup", before=1, file_ext=".ts") == MARKER


# ── shapes of the same two faults ────────────────────────────────────────────────────────────────────

GENERIC_SHAPES = {
    "default type": "const lookup = <T = { a: number }>(x: T) => { return x; };",
    "arrow type in the default": "const lookup = <T = () => void>(x: T) => { return x; };",
    "nested lists": "const lookup = <M extends Map<string, Array<{ id: number }>>>(m: M) => { return m.size; };",
    "wrapped over lines": "const lookup = <\n  T extends { id: number },\n>(x: T) => {\n  return x.id;\n};",
    "return type": "const lookup = <T extends { id: number }>(x: T): T => { return x; };",
    "expression body": "const lookup = <T extends { id: number }>(x: T) => x.id;",
    "async": "const lookup = async <T extends { id: number }>(x: T) => { return x.id; };",
    "exported": "export const lookup = <T extends { id: number }>(x: T) => { return x.id; };",
    "no declaration keyword": "lookup = <T extends { id: number }>(x: T) => { return x; };",
    "template literal type": "const lookup = <T extends `a${string}`>(x: T) => { return x; };",
    "comment holding a bracket": "const lookup = <T /* > */ extends { id: number }>(x: T) => { return x; };",
    "string holding a bracket": 'const lookup = <T extends "a>b">(x: T) => { return x; };',
    "parenthesized body": "const lookup = <T extends { id: number }>(x: T) => ({ ...x, seen: true });",
    "wrapper, nested lists": "const lookup = memo<Map<string, Array<{ id: number }>>>(m => { return m.size; });",
    "wrapper, wrapped over lines": "const lookup = memo<{\n  id: number;\n}>(props => {\n  return props.id;\n});",
    "wrapper, arrow with an object body": "const lookup = memo<{ id: number }>(props => ({ id: props.id }));",
    "wrapper, two type arguments": "const lookup = forwardRef<HTMLDivElement, { id: number }>((props, ref) => { return props.id; });",
}


@pytest.mark.parametrize("name", sorted(GENERIC_SHAPES))
def test_a_generic_shape_comes_back_whole(name):
    _assert_whole(GENERIC_SHAPES[name])


def test_a_class_field_generic_arrow_is_found():
    source = f"class Box {{\n  lookup = <T extends {{ id: number }}>(x: T) => {{ return x.id; }};\n}}\n{NEIGHBOUR}\n"
    block = extract_block(source, "lookup", ".ts")
    assert "return x.id" in block and "neighbor" not in block


PAREN_SHAPES = {
    "small": "const lookup = (n: number) => ({ n });",
    "over lines": "const lookup = (n: number) => ({\n  n,\n  m: n + 1,\n});",
    "conditional of objects": "const lookup = (n: number) => (n > 1 ? { a: n } : { b: n });",
    "string of closers": 'const lookup = (n: number) => ({ s: "a)}", t: \'(\' });',
    "unbalanced closer in a string": 'const lookup = (n: number) => ({ s: "a)" });',
    "template and comment": "const lookup = (n: number) => ({\n  s: `x${n})`, // ) not the end\n  t: 1,\n});",
    "regex of closers": "const lookup = (n: number) => ({ r: /\\)}/, n });",
    "call of object and array": "const lookup = (n: number) => (make({ a: n }, [n, { b: 1 }]));",
    "return type": "const lookup = (n: number): { id: number } => ({ id: n });",
    "async": "const lookup = async (n: number) => ({ n, rows: await load(n) });",
    "arrow in the object": "const lookup = (f: number) => ({ g: (x: number) => x + 1, h: f });",
    "member after the group": "const lookup = (n: number) => ({ n }).n;",
    "comma expression": "const lookup = (n: number) => (n, { n });",
    "as const": "const lookup = (n: number) => ({ n } as const);",
}


@pytest.mark.parametrize("name", sorted(PAREN_SHAPES))
def test_a_parenthesized_body_comes_back_whole(name):
    _assert_whole(PAREN_SHAPES[name])


def test_a_parenthesized_body_with_jsx_in_tsx_is_found():
    _assert_whole("const lookup = (n: number) => ({ n, el: <b>{n}</b> });", ".tsx")
    _assert_whole("const lookup = (n: number) => (<div>{n}</div>);", ".tsx")


def test_a_generic_arrow_with_a_trailing_comma_in_tsx_is_found():
    _assert_whole("const lookup = <T,>(x: T) => { return x; };", ".tsx")


def test_crlf_line_endings_do_not_change_the_block():
    source = f"const lookup = <T extends {{ id: number }}>(x: T) => {{\r\n  return x.id;\r\n}};\r\n{NEIGHBOUR}\r\n"
    block = extract_block(source, "lookup", ".ts")
    assert "return x.id" in block and "neighbor" not in block and _norm(block).endswith("}")


def test_two_definitions_each_come_back_whole():
    first = "const first = <T extends { id: number }>(x: T) => { return x.id; };"
    second = "const second = (s: { n: number }) => ({ ...s });"
    source = f"{first}\n{second}\n{NEIGHBOUR}\n"
    assert _norm(extract_block(source, "first", ".ts")) == _norm(first)
    assert _norm(extract_block(source, "second", ".ts")) == _norm(second)


# ── what is not a definition, and is not made one by scanning into a neighbour ──────────────────────

NOT_DEFINITIONS = {
    "comparison": "const lookup = a < b;",
    "comparison with a call after the closer": "const lookup = a < b || c > (d);",
    "shift": "const lookup = a << 2 > (b);",
    "conditional": "const lookup = a ? b < c : d > (e);",
    "call site": "function run() {\n  lookup<T>(x);\n}",
    "unclosed list": "const lookup = memo<{ id: number }(props => 1);",
    "unclosed generic arrow": "const lookup = <T extends { id: number }(x: T) => { return x; };",
    "unclosed parenthesized body": "const lookup = (n: number) => ({ n };",
    "type alias": "type lookup = <T extends { id: number }>(x: T) => number;",
}


@pytest.mark.parametrize("ext", [".ts", ".tsx"])
@pytest.mark.parametrize("name", sorted(NOT_DEFINITIONS))
def test_a_non_definition_does_not_take_the_neighbour(name, ext):
    block = extract_block(f"{NOT_DEFINITIONS[name]}\n{NEIGHBOUR}\n", "lookup", ext)
    assert "neighbor" not in block, block


def test_a_call_site_and_a_comparison_give_no_block_at_all():
    assert extract_block(f"function run() {{\n  lookup<T>(x);\n}}\n{NEIGHBOUR}\n", "lookup", ".ts") == ""
    assert extract_block(f"const lookup = a < b;\n{NEIGHBOUR}\n", "lookup", ".ts") == ""


FOLLOWING_STATEMENTS = [
    "const z = c>(d);",                       # a `>` and a `(` with no space between them
    "const ok = n>(1);",
    "const q = x>(y => y);",
    "if (a>(b)) { run(); }",
    "const other = <T,>(x: T) => { return x; };",
]


@pytest.mark.parametrize("ext", [".ts", ".tsx"])
@pytest.mark.parametrize("following", FOLLOWING_STATEMENTS)
@pytest.mark.parametrize("head", [
    "const lookup = memo<Foo(props => 1);",
    "const lookup = memo<{ id: number }(props => 1);",
    "const lookup = forwardRef<A, B(props => 1);",
    "const lookup = memo<Foo(props => 1)",     # no `;`: the next line starts a statement
])
def test_an_unclosed_type_list_does_not_borrow_the_closer_of_a_later_statement(head, following, ext):
    source = f"{head}\n{following}\n{NEIGHBOUR}\n"
    block = extract_block(source, "lookup", ext)
    assert block == "", block


def test_an_unclosed_type_list_before_a_statement_without_a_semicolon_gives_no_block():
    source = "const lookup = memo<{ id: number }(props => 1)\nlet z = c>(d)\n"
    assert extract_block(source, "lookup", ".ts") == ""


@pytest.mark.parametrize("word", ["object", "typeof x", "void", "readonly string[]", "keyof Shape"])
def test_a_wrapped_type_argument_may_begin_with_a_type_word(word):
    _assert_whole(f"const lookup = memo<\n  {word}\n>(props => {{ return props; }});")


def test_an_unclosed_list_in_a_long_file_is_bounded():
    lines = "".join(f"const x{i} = a < (b ? {{ c: [d] }} : {{}});\n" for i in range(3000))
    block = extract_block(lines + "const lookup = memo<{ id: number }(p => 1);\n" + NEIGHBOUR + "\n", "lookup", ".ts")
    assert "neighbor" not in block


def test_a_very_deep_type_argument_list_is_bounded():
    source = "const lookup = memo<" + "{ a: Array<" * 300 + "x" + ">; }" * 300 + f">(p => 1);\n{NEIGHBOUR}\n"
    assert "neighbor" not in extract_block(source, "lookup", ".ts")


# ── what still works ─────────────────────────────────────────────────────────────────────────────────

CONTROLS = {
    "simple generic arrow": "const lookup = <T>(x: T) => { return x; };",
    "simple arrow": "const lookup = (x: number) => { return x; };",
    "expression arrow": "const lookup = (x: number) => x + 1;",
    "parenthesized expression": "const lookup = (n: number) => (n + 1);",
    "async generic with a return type": "export const lookup = async <T,>(x: T): Promise<T> => x;",
    "typed function declaration": "function lookup<T extends { id: number }>(x: T): number {\n  return x.id;\n}",
    "annotated declaration": "const lookup: Fn<{ id: number }> = (x) => { return x; };",
    "selector with two arguments": "const lookup = createSelector<State, number>(s => s.a, a => a);",
    "wrapper call with an arrow argument": "const lookup = wrap((n: number) => ({ n }), 1);",
}


@pytest.mark.parametrize("name", sorted(CONTROLS))
def test_an_established_shape_is_unchanged(name):
    _assert_whole(CONTROLS[name])


def test_a_tsx_arrow_returning_jsx_is_still_found():
    _assert_whole("const lookup = (n: number) => { return <div>{n}</div>; };", ".tsx")
    _assert_whole("const lookup: React.FC<{ id: number }> = ({ id }) => { return <div>{id}</div>; };", ".tsx")


def test_a_javascript_arrow_with_a_parenthesized_object_is_whole():
    _assert_whole("const lookup = (s, n) => ({ ...s, n });", ".js")
    _assert_whole("const lookup = (n) => ({\n  n,\n});", ".jsx")


# ── Gate 1, through the real filter with its model stage skipped ─────────────────────────────────────

def _gate(tmp_path):
    from tools.auto.gate1_filter import Gate1Filter

    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:1/v1", "api_key": "t", "model": "m", "api_format": "openai"},
        "gate1": {"skip_llm": "true"},
        "loop": {"timeout_seconds": "5"},
    })
    return Gate1Filter(config=cfg, base_url="http://localhost:1/v1", api_key="t", model="m", verify_ssl=False)


def _candidate(filename: str, symbol: str = "lookup"):
    from tools.auto.architect import CandidateTask, CitedLocation

    return CandidateTask(title=f"fix {symbol}", instruction=f"fix {symbol}", target_files=[filename],
                         acceptance_check="true", cited_location=CitedLocation(file=filename, symbol=symbol))


@pytest.mark.parametrize("ext", [".ts", ".tsx", ".mts", ".cts"])
@pytest.mark.parametrize("definition", FIVE)
def test_gate1_accepts_the_definition(tmp_path, definition, ext):
    filename = f"sample{ext}"
    (tmp_path / filename).write_text(f"{definition}\n{NEIGHBOUR}\n", encoding="utf-8")
    accepted, rejected = _gate(tmp_path).filter([_candidate(filename)], tmp_path)
    assert len(accepted) == 1 and not rejected, [r.reason for r in rejected]


@pytest.mark.parametrize("definition", FIVE)
def test_gate1_evidence_is_the_whole_definition(tmp_path, definition):
    (tmp_path / "sample.ts").write_text(f"{definition}\n{NEIGHBOUR}\n", encoding="utf-8")
    ok, _reason, block = _gate(tmp_path)._check_existence(_candidate("sample.ts"), tmp_path)
    assert ok and _norm(block) == _norm(definition), block


@pytest.mark.parametrize("source", [
    f"function run() {{\n  lookup<T>(x);\n}}\n{NEIGHBOUR}\n",
    f"const lookup = a < b;\n{NEIGHBOUR}\n",
    f"const lookup = memo<Foo(props => 1);\nconst z = c>(d);\n{NEIGHBOUR}\n",
])
def test_gate1_still_rejects_what_is_not_a_definition(tmp_path, source):
    (tmp_path / "sample.ts").write_text(source, encoding="utf-8")
    accepted, rejected = _gate(tmp_path).filter([_candidate("sample.ts")], tmp_path)
    assert not accepted and len(rejected) == 1
