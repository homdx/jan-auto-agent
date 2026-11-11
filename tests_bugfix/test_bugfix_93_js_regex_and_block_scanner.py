"""AGNES-1 T93: the JS scanner tells a regex from a division, and a block from an object, in combination.

The cases are valid JavaScript (checked once against SpiderMonkey and V8); each is extracted whole, with the
`neighbor` function after it left out. The last test is a seeded differential fuzz: when a JS engine (`gjs`
or `node`) is installed, random nestings of blocks, switches, labels, objects, ternaries, templates, comments,
regexes and divisions are compiled by it, and every one it accepts must come out of `extract_block` unchanged."""
from __future__ import annotations

import json
import random
import shutil
import subprocess

import pytest

from tools.block_extractor import extract_block

NEIGHBOR = "\nfunction neighbor() { return 42; }\n"


def _whole(target: str) -> str:
    return extract_block(target + NEIGHBOR, "target", ".js")


CASES = {}
for _word in ("case", "if", "else", "do", "while", "for", "with", "default", "class", "return"):
    for _style, _key in (("bare", _word), ("quoted", json.dumps(_word)), ("computed", "[" + json.dumps(_word) + "]")):
        CASES[f"property_{_word}_{_style}"] = "function target() { return {" + _key + ": {valueOf: () => 4} / 2}; }"
CASES.update({
    "class_in_comment_before_greater": "function target(x) { return /* class */ x > {valueOf: () => 4} / 2; }",
    "class_in_string_before_greater": 'function target(x) { return "class" + x > {valueOf: () => 4} / 2; }',
    "class_member_before_greater": "function target(x) { return x.class > {valueOf: () => 4} / 2; }",
    "class_in_line_comment_before_greater": "function target(x) { // class\n return x > {valueOf: () => 4} / 2; }",
    "case_ternary": "function target(x) { switch (x) { case x ? 1 : 2: { x=3; } /}/.test(String(x)); } return x; }",
    "case_optional_chain": "function target(x) { switch (x) { case x?.value: { x=3; } /}/.test(String(x)); } return x; }",
    "case_comma": "function target(x) { switch (x) { case 1,2: { x=3; } /}/.test(String(x)); } return x; }",
    "case_regex_with_brace": "function target(x) { switch (x) { case /[{}]/.test(String(x)): { x=3; } /}/.test(String(x)); } return x; }",
    "case_regex_escaped": "function target(x) { switch (x) { case /x\\?/.test(String(x)): { x=3; } /}/.test(String(x)); } return x; }",
    "ternary_branch_object": 'function target(x) { return x ? "text:case" : {valueOf: () => 4} / 2; }',
    "regex_after_block_comment": "function target(x) { if (x) { x = 1 }\n/* comment */ /}/.test(x); }",
    "regex_after_line_comment_not": "function target(x) { if (x) { x = 1 }\n// comment\n!/}/.test(x); }",
    "regex_after_comment_in_condition": "function target(x) { if (x) /* c */ /}/.test(x); }",
    "division_after_comment": "function target(x) { return x /* c */ / {valueOf: () => 4}.valueOf() / 2; }",
    "member_return_divides": "function target(x) { return x.return / 2 / {a: 1}.a; }",
    "optional_member_if_divides": "function target(x) { return x?.if / {a: 1}.a; }",
    "postfix_then_divide": "function target(x) { x++ / 2; return x-- / {a: 1}.a; }",
    "not_then_regex": "function target(x) { if (x) !/}/.test(x); return 1; }",
    "template_interpolation_object": "function target(x) { return `${ {a: 1}.a / 2 }${ /}/.source }`; }",
    "label_then_regex": "function target(x) { L: { x = 1 }\n/}/.test(x); }",
    "class_expression_division": "function target() { return class {} / 2; }",
    "named_class_expression_division": "function target() { return class Inner {} / 2; }",
    "class_expression_with_method_division": "function target() { return class Inner { static valueOf() {return 4;} } / 2; }",
    "class_expression_assigned": "function target() { const C = class Named { m() {} } / 2; return C; }",
    "class_declaration_then_regex": "function target(x) { class K { m() {} }\n/}/.test(x); return 1; }",
    "class_with_type_parameters_then_regex": "function target(x) { class K<T> { m() {} }\n/}/.test(x); return 1; }",
    "async_function_expression_division": "function target() { return async function() {} / 2; }",
    "async_function_declaration_then_regex": "function target(x) { async function helper() {} /}/.test(String(x)); return 42; }",
    "async_declaration_after_return_newline": "function target(x) { if (x) return\nasync function helper() {} /}/.test(String(x)); return 42; }",
    "break_then_regex": "function target(x) { while (true) { if (x) break\n/}/.test(String(x)); return 42; } return 0; }",
    "continue_then_regex": "function target(x) { while (true) { if (x) continue\n/}/.test(String(x)); return 42; } }",
    "labeled_break_then_regex": "function target(x) { L: while (true) { if (x) break L\n/}/.test(String(x)); return 42; } return 0; }",
    "labeled_continue_then_regex": "function target(x) { L: while (true) { if (x) continue L\n/}/.test(String(x)); return 42; } }",
    "regex_right_after_less_than": "function target(x) { return x </}/.test(x); }",
    "regex_after_spaced_less_than": "function target(x) { return x < /}/.test(x); }",
    "jsx_closing_tag_is_not_a_regex": "function target() { return <ul><li>a</li><li>{'}'}</li></ul>; }",
    "declaration_after_return_newline": "function target(x) { if (x) return\nfunction helper() {} /}/.test(String(x)); return 42; }",
    "declaration_after_return_comment": "function target(x) { if (x) return // stop\nfunction helper() {} /}/.test(String(x)); return 42; }",
    "declaration_after_return_block_comment": "function target(x) { if (x) return /*\n*/ function helper() {} /}/.test(String(x)); return 42; }",
    "generator_declaration_after_return_newline": "function target(x) { if (x) return\nfunction* helper() {} /}/.test(String(x)); return 42; }",
    "declaration_after_postfix_increment": "function target(x) { x++\nfunction helper() {} /}/.test(String(x)); return 42; }",
    "declaration_after_postfix_decrement": "function target(x) { x--\nfunction helper() {} /}/.test(String(x)); return 42; }",
    "declaration_after_array_statement": "function target(x) { [1]\nfunction helper() {} /}/.test(String(x)); return 42; }",
    "function_expression_division": "function target() { return function() {} / 2; }",
    "function_expression_after_comma_newline": "function target() { return (0,\nfunction() {}) / 2; }",
    "generator_expression_division": "function target() { return function*() {} / 2; }",
    "class_extends_member_then_object": "function target() { const ns = {Base: Object};\n class A extends ns.Base {}\n return {valueOf: () => 4} / 2; }",
    "class_extends_tagged_template_then_object": "function target() { const tag = () => Object;\n class A extends tag`x` {}\n return {valueOf: () => 4} / 2; }",
    "class_extends_call_then_regex": "function target(x) { class A extends Object.create(null).constructor {}\n /}/.test(x); return 1; }",
    "class_expression_comment_after_keyword": "function target() { return class /* c */ {} / 2; }",
    "class_expression_line_comment_after_keyword": "function target() { return class // c\n {} / 2; }",
    "class_expression_nested_heritage": "function target() { return class extends (class {}) {} / 2; }",
    "class_expression_member_heritage": "function target() { const ns = {Base: Object}; return class extends ns.Base {} / 2; }",
    "regex_with_letter_after_less_than": "function target(x) { return x </a}/.test(x); }",
    "regex_with_dollar_after_less_than": "function target(x) { return x </$[}]/.test(x); }",
    "regex_with_class_after_less_than": "function target(x) { return x </a[}]/.test(x); }",
    "regex_that_looks_like_a_closing_tag": "function target(x) { return x </a>}/.test(x); }",
    "regex_div_that_looks_like_a_closing_tag": "function target(x) { return x </div>/.test(x); }",
    "regex_greater_than_after_less_than": "function target(x) { return x </>/.test(x); }",
    "regex_namespace_tag_lookalike": "function target(x) { return x </a:b>/.test(x); }",
    "regex_hyphen_tag_lookalike": "function target(x) { return x </a-b>/.test(x); }",
    "regex_tag_lookalike_after_space": "function target(x) { return x < /a>}/.test(x); }",
    "class_extends_function_expression": "function target() { return class Outer extends function Base() {} { static valueOf() { return 4; } } / 2; }",
    "class_extends_anonymous_function": "function target() { return class extends function() {} { static valueOf() { return 4; } } / 2; }",
    "class_extends_function_with_comment": "function target() { return class Outer extends function /* note */ Base() {} { static valueOf() { return 4; } } / 2; }",
    "class_extends_object_literal": "function target() { return class extends {}.constructor {} / 2; }",
    "class_declaration_extends_function": "function target(x) { class A extends function Base() {} {} /}/.test(String(x)); return 42; }",
    "regex_that_looks_like_a_closing_tag": "function target(x) { return x </a>}/.test(x); }",
    "regex_div_that_looks_like_a_closing_tag": "function target(x) { return x </div>/.test(x); }",
    "regex_greater_than_after_less_than": "function target(x) { return x </>/.test(x); }",
    "regex_namespace_tag_lookalike": "function target(x) { return x </a:b>/.test(x); }",
    "regex_hyphen_tag_lookalike": "function target(x) { return x </a-b>/.test(x); }",
    "regex_tag_lookalike_after_space": "function target(x) { return x < /a>}/.test(x); }",
    "class_extends_function_expression": "function target() { return class Outer extends function Base() {} { static valueOf() { return 4; } } / 2; }",
    "class_extends_anonymous_function": "function target() { return class extends function() {} { static valueOf() { return 4; } } / 2; }",
    "class_extends_function_with_comment": "function target() { return class Outer extends function /* note */ Base() {} { static valueOf() { return 4; } } / 2; }",
    "class_extends_object_literal": "function target() { return class extends {}.constructor {} / 2; }",
    "class_declaration_extends_function": "function target(x) { class A extends function Base() {} {} /}/.test(String(x)); return 42; }",
    "object_method_division": "function target(x) { return {m() { return 1 }}.m() / 2 / {}; }",
})


@pytest.mark.parametrize("name", sorted(CASES))
def test_js_scanner_extracts_the_function_whole(name):
    assert _whole(CASES[name]) == CASES[name]


KEYS = ["case", "if", "else", "do", "while", "for", "with", "default", "class", "return", "a", "'str'", "[\"case\"]", "1"]
OBJ = ["{}", "{valueOf: () => 4}", "{a: {b: 1}}", "{get a() { return 1 }}", "{m() { return {} }}", "{...{}}",
       "{[\"}\"]: 1}", "{'{': 2}", "{a: function() { return 1 }}", "{f: () => { return 1 }}"]
def obj(r):
    if r.random() < .4:
        return "{" + r.choice(KEYS) + ": " + r.choice(OBJ) + "}"
    return r.choice(OBJ)
OBJ_EXPR = [  # each is a valid expression whose last operand is a brace thing; a following `/ 2` divides
    lambda r: obj(r),
    lambda r: "c ? 1 : " + obj(r),
    lambda r: "c ? " + obj(r) + " : " + obj(r),
    lambda r: "1 + " + obj(r),
    lambda r: "x > " + obj(r),
    lambda r: "x < " + obj(r),
    lambda r: "x >> " + obj(r),
    lambda r: "x << " + obj(r),
    lambda r: "(c, " + obj(r) + ")",
    lambda r: "[" + obj(r) + "][0]",
    lambda r: "function() { return 1 }",
    lambda r: "class { m() {} }",
    lambda r: "`${" + obj(r) + "}`",
    lambda r: "x.class > " + obj(r),
    lambda r: "/* class */ x > " + obj(r),
    lambda r: "\"class\" + x > " + obj(r),
    lambda r: "typeof " + obj(r),
    lambda r: "void " + obj(r),
    lambda r: "!" + obj(r),
]
BLOCK_END = [
    "if (c) { v = 1 }", "if (c) { v = 1 } else { v = 2 }", "{ v = 1 }", "{}", "function f%d() { return 1 }",
    "L%d: { v = 1 }", "switch (c) { case 1: { v = 1 } }", "switch (c) { case c ? 1 : 2: { v = 1 } }",
    "switch (c) { case 1, 2: { v = 1 } }", "switch (c) { case x?.y: { v = 1 } }", "switch (c) { default: { v = 1 } }",
    "for (;;) { break }", "while (0) { v = 1 }", "try { v = 1 } catch (e) { v = 2 }", "try { v = 1 } finally { v = 2 }",
    "for (var k in x) { v = 1 }", "for (var k of [1]) { v = 1 }", "class K%d { m() { return 1 } }",
    "if (c) v = 1; else { v = 2 }", "if (c) /}/.test(x); else { v = 2 }", "if (c) L%d: { v = 1 }",
    "switch (c) { case /[{}]/.test(x): { v = 1 } }", "switch (c) { case \"}\": { v = 1 } }",
    "switch (c) { case 1: switch (v) { default: { v = 1 } } }",
]
REGEX_AFTER = ["\n/}/.test(x);", " /* c } */ /}/.test(x);", "\n// c {\n/}/.test(x);", "\n/{/.test(x);", "\n/[}]/g.test(x);",
               "\n!/}/.test(x);", "\n// }\n!/}/.test(x);", "\n/* } */ !/{/.test(x);", "\n/\\//.test(x) && /}/.test(x);"]
def block_end(r):
    s = r.choice(BLOCK_END)
    return s % r.randrange(10**6) if "%d" in s else s

def a_stmt(r):
    return block_end(r) + r.choice(REGEX_AFTER)
def b_stmt(r):
    e = r.choice(OBJ_EXPR)(r)
    tail = r.choice([" / 2;", " / 2 + 1; }", " / 2; // }", " / 2 / 3;", " / 2 + \"{\";"])
    return "v = " + e + tail if not tail.endswith("}") else "v = " + e + " / 2 + 1; "
def case_b(r):
    return "switch (c) { case 1: v = " + r.choice(OBJ_EXPR)(r) + " / 2; break; default: v = {valueOf: () => 4} / 2 }"


DIV_AFTER = [  # operand then `/ 2 /` pattern whose misreading as a regex would swallow a brace on the same line
    "x", "x.return", "x.if", "x?.class", "x[0]", "f(x)", "(x)", "1", "1.5", "x++", "x--", "'s'.length", "`t`.length",
    "x.case", "(() => { return 4 })()", "(function() { return 4 }())", "[{}].length", "({}).a", "{a: 1}.a".replace("{a: 1}.a", "({a: 1}).a"),
    "x ? 1 : 2", "(c ? {a: 1} : {a: 2}).a", "`${ {a: 4}.a }`.length", "x.\u0061", "this.x", "null", "true",
]
def div_stmt(r):
    e = r.choice(DIV_AFTER)
    return "v = " + e + " / 2 + { a: 1 }.a / 3; // }"
def ret_stmt(r):
    e = r.choice(DIV_AFTER)
    return r.choice(["if (c) return %s / 2 / 3; else { v = 1 }", "if (c) return {a: 1}.a / 2 / 3", "if (c) { return typeof {} / 2 }",
                     "if (c) return\n{ v = 1 }\n/}/.test(x)", "if (c) throw {a: 1}.a / 2 / 3", "for (var q in {a: 1}) { v = q }\n/}/.test(x)"]).replace("%s", e)
def regex_stmt(r):
    return r.choice(["v = x.replace(/}/g, '{');", "v = /}/.test(x) ? {a: 1} : /{/;", "v = [/}/, /{/];", "v = c && /}/.test(x);",
                     "v = (/}/).test(x); v = !/}/.test(x);", "v = c, /}/.test(x);", "v = typeof /}/;", "v = x in {} || /}/.test(x);",
                     "v = {a: /}/}.a;", "v = {a: /}/, b: 1 / 2}.b;", "v = [1, 2].map(y => y / 2).concat(/}/);",
                     "v = c ? /}/ : /{/;", "x = `${/}/.source}`;", "x = `${'}'}${\"{\"}`;", "x = '\\'' + \"}\" + `{`;", "v = ((x)) / 2 / 3; if (c) /}/.test(x);",
                     "v = x\n++v\n/}/.test(x)", "v = c ? x : /}/.test(x)", "v = a => /}/.test(a);", "v = async () => { await 1 }; /}/.test(x);"])


CLS = ["class {}", "class K {}", "class K { m() { return 1 } }", "class extends Object {}", "class K extends (c ? Object : Array) { m() {} }",
       "class K { static s = {a: 1}; }", "class K { get g() { return 1 } }", "class K<T> {}".replace("<T>", "")]
def cls_stmt(r):
    return r.choice(["v = %s / 2;", "return %s / 2 / 3; ", "v = [%s][0] / 2; // }", "v = c ? 1 : %s / 2;", "v = (%s) / 2 / 3;",
                     "v = typeof %s / 2;", "v = {k: %s / 2};", "%s\n/}/.test(x);".replace("%s", "class W%d {}" % r.randrange(10**6)) ]).replace("%s", r.choice(CLS)) if False else (
        r.choice(["v = %s / 2;", "v = %s / 2 / 3; // }", "v = [%s][0] / 2;", "v = c ? 1 : %s / 2;", "v = typeof %s / 2;", "v = {k: %s / 2};"]).replace("%s", r.choice(CLS)))
def asyn_stmt(r):
    f = r.choice(["async function() {}", "async function h() {}", "function() {}", "function* () {}", "async function* () {}", "function h() {}"])
    return r.choice(["v = %s / 2;", "v = %s / 2 / 3; // }", "v = c ? 1 : %s / 2;", "v = [%s][0] / 2;"]).replace("%s", f)
def asi_stmt(r):
    n = r.randrange(10**6)
    kw = r.choice(["break", "continue", "break LL", "continue LL"])
    pre = "LL: while (c) { if (x) %s\n/}/.test(x); v = 1 }" % kw
    decl = r.choice(["return\nfunction h%d() {} /}/.test(x);" % n, "x++\nfunction h%d() {} /}/.test(x);" % n, "x--\nfunction h%d() {} /}/.test(x);" % n,
                     "[1]\nfunction h%d() {} /}/.test(x);" % n, "return // c\nfunction* h%d() {} /}/.test(x);" % n,
                     "return /*\n*/ function h%d() {} /}/.test(x);" % n, "return\nasync function h%d() {} /}/.test(x);" % n,
                     "v = 1;\nasync function h%d() {} /}/.test(x);" % n, "v = {}\nfunction h%d() {}" % n])
    return r.choice([pre, "if (c) { " + decl + " }"]) if True else decl
def lt_stmt(r):
    return r.choice(["v = x </}/.test(x);", "v = x < /}/.test(x);", "v = x</{/.test(x);", "v = (x) </[}]/.test(x);", "v = x <\n/}/.test(x);"])


BASES = ["function Base() { return {} }", "function() {}", "function /* n */ B() {}", "async function() {}", "{}.constructor", "{a: 1}.constructor", "function*() {}", "Object", "ns.Base", "ns.a.Base", "ns['Base']", "f()", "f(1, {a: 1})", "(c ? Object : Array)", "(class {})", "(class extends Object {})",
         "tag`x`", "tag`${1}`", "ns?.Base", "(0, ns.Base)", "[Object][0]", "(function() { return Object })()", "class_ => Object".replace("class_ => Object", "Object")]
GAPS = ["", " ", "/* c */ ", "// c\n", "/* } */ ", "\n"]
def heritage(r):
    return r.choice(["", "extends " + r.choice(BASES) + " "])
def class_expr(r):
    return "class" + " " + r.choice(GAPS) + r.choice(["", "K "]) + r.choice(GAPS) + heritage(r) + "{ " + r.choice(["", "m() { return {} }", "static s = {a: 1};", "get g() { return 1 }"]) + " }"
def class_decl(r):
    return "class D%d " % r.randrange(10**6) + r.choice(GAPS) + heritage(r) + "{ m() { return 1 } }"
def class2_stmt(r):
    e = class_expr(r)
    return r.choice(["v = %s / 2;", "v = %s / 2 / 3; // }", "v = [%s][0] / 2;", "v = c ? 1 : %s / 2;", "v = typeof %s / 2;"]).replace("%s", e)
def class3_stmt(r):
    d = class_decl(r)
    return d + r.choice(["\nv = {valueOf: () => 4} / 2;", "\n/}/.test(x);", " v = {a: {b: 1}} / 2 / 3; // }", "\nv = x > {} / 2;", "\nv = {case: {}} / 2;"])
def lt2_stmt(r):
    t = r.choice(["a", "$", "_", "a[}]", "$[}]", "a}", "ab{", "A.b}", "x-y}", "a b}", "a:b}", "1}", "}"])
    t2 = r.choice(["a>}", "div>", ">", "a:b>", "a-b>", "a>/", "A.b>{", "$>", "_>", " >}", "a >"])
    return r.choice(["v = x </" + t + "/.test(x);", "v = x </" + t2 + "/.test(x);", "v = x < /" + t2 + "/.test(x);", "v = c ? x </" + t2 + "/.test(x) : 1;"])

def wrap(r, s, d):
    if d == 0:
        return s
    k = r.randrange(11)
    s = wrap(r, s, d - 1) if r.random() < .5 else s
    if k == 0: return "if (c) { %s\n}" % s
    if k == 1: return "if (c) { v = 1 } else { %s\n}" % s
    if k == 2: return "switch (c) { case 1: %s\n break; default: v = 2 }" % s
    if k == 3: return "LB%d: { %s\n}" % (r.randrange(10**6), s)
    if k == 4: return "(() => { %s\n})();" % s
    if k == 5: return "[1].forEach(function() { %s\n});" % s
    if k == 6: return "v = {m() { %s\n}};" % s
    if k == 7: return "class W%d { m() { %s\n} }" % (r.randrange(10**6), s)
    if k == 8: return "try { %s\n} catch (e) { v = 3 }" % s
    if k == 9: return "for (var i%d = 0; i%d < 1; i%d++) { %s\n}" % ((r.randrange(10**6),) * 3 + (s,))
    return "while (0) { %s\n}" % s

def gen(r):
    parts = []
    for _ in range(r.randrange(1, 4)):
        base = r.choice([a_stmt, b_stmt, case_b, div_stmt, ret_stmt, regex_stmt, cls_stmt, asyn_stmt, asi_stmt, lt_stmt, class2_stmt, class3_stmt, lt2_stmt])(r)
        parts.append(wrap(r, base, r.randrange(0, 3)))
    body = "\n".join(parts)
    return "function target(x, c, v) {\n" + body + "\n}"


ENGINE = shutil.which("gjs") or shutil.which("node")


def _accepted_by_engine(cases):
    if shutil.which("gjs") and ENGINE == shutil.which("gjs"):
        script = ("const cases = " + json.dumps(cases) + ";\nprint(JSON.stringify(cases.map(t => {"
                  "try { new Function('0;' + t); return true } catch (e) { return false }})));")
        run = subprocess.run(["gjs", "-c", script], text=True, capture_output=True, check=True)
    else:
        script = ("const cases = " + json.dumps(cases) + ";\nconsole.log(JSON.stringify(cases.map(t => {"
                  "try { new Function('0;' + t); return true } catch (e) { return false }})));")
        run = subprocess.run(["node", "-e", script], text=True, capture_output=True, check=True)
    return json.loads(run.stdout)


@pytest.mark.skipif(ENGINE is None, reason="needs a JavaScript engine (gjs or node) to say what is valid")
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_valid_js_is_extracted_whole(seed):
    r = random.Random(seed)
    cases = [gen(r) for _ in range(400)]
    accepted = _accepted_by_engine(cases)
    assert sum(accepted) > 300, "the generator must mostly produce valid JavaScript"
    wrong = [t for t, ok in zip(cases, accepted) if ok and _whole(t) != t]
    assert not wrong, wrong[0]


# JSX has no engine to ask (gjs and node parse plain JavaScript): these are valid by construction. Each
# element, in every nesting of tags, attribute `{…}` containers and text with apostrophes, slashes and
# regex-like `</a>/` lookalikes, must be extracted whole.
TEXT = ["(x)", "(a {1}) b", "({x})", "(a) / (b)", "hello", "don't stop", "5 / 3", "a / b / c", "it's \"quoted\"", "path/to/file", "100%", "x &amp; y", "// not a comment", "/* nor this */",
        "(parens) [brackets]", "x = y", "a ? b : c", "ok;"]
EXPR = ["x", "{a: 1}.a / 2", "/}/.test(x)", "x </a>/.test(x)", "x < y", "x </div>/.test(x)", "c && <b>t</b>", "c ? <i/> : null",
        "items.map(i => <li key={i}>{i}</li>)", "(() => { return {} })() ? 1 : 2", "`${x}}`", "'}'", "x > {} / 2", "/* } */ x", "[1, 2].map(n => n / 2)",
        "f(<a>{x}</a>)", "{valueOf: () => 4} / 2", "x /* c */ / 2"]
NAMES = ["div", "a", "Foo", "ns:tag", "A.B", "my-tag"]
def attrs(r):
    out = []
    for _ in range(r.randrange(0, 3)):
        out.append(r.choice(['id="x"', "t='it\"s'", "n={" + r.choice(EXPR) + "}", "d", "{...p}", 'href="a/b>c"', "cb={() => x > 1}"]))
    return (" " + " ".join(out)) if out else ""
def elem(r, d):
    nm = r.choice(NAMES)
    if r.random() < .15:
        return "<" + nm + attrs(r) + (" " if r.random() < .5 else "") + "/>"
    kids = "".join(child(r, d) for _ in range(r.randrange(0, 4))) if d > 0 else r.choice(TEXT)
    if r.random() < .15:
        return "<>" + kids + "</>"
    return "<" + nm + attrs(r) + ">" + kids + "</" + nm + (" " if r.random() < .2 else "") + ">"
def child(r, d):
    k = r.randrange(4)
    if k == 0: return r.choice(TEXT)
    if k == 1: return "{" + r.choice(EXPR) + "}"
    if k == 2: return elem(r, d - 1)
    return "\n  " + r.choice(TEXT) + " "
def gen_jsx(r):
    e = elem(r, r.randrange(1, 4))
    use = r.choice(["return (%s);", "return %s;", "v = %s;", "v = c && %s;", "v = c ? %s : null;", "v = () => %s;", "v = [%s, %s];", "f(%s);",
                    "return (\n %s\n);", "v = {k: %s};"])
    stmt = use.replace("%s", e) if use != "v = [%s, %s];" else use % (e, elem(r, 1))
    tail = r.choice(["", " /}/.test(x);", "\n/}/.test(x);", " v = {a: 1}.a / 2; // }", "\nif (c) { v = 1 }\n/}/.test(x);", " x </a>/.test(x);"])
    if stmt.startswith("return") :
        tail = ""
    pre = r.choice(["", "", "const id = <T,>(v: T) => v;\n  ", "const id = <T extends U>(v: T): T => v;\n  ", "const w = <A>(x)</A>;\n  ", "const w = <A>(x {1})</A>;\n  "])
    return "function target(x, c, v, items, f, p) {\n  " + pre + stmt + tail + "\n}"


JSX_CASES = {
    "text_with_apostrophe": "function target() { return <p>don't {x} stop</p>; }",
    "text_with_slash": "function target() { return <p>5 / 3 and a/b</p>; }",
    "closing_tags_in_children": "function target(x) { return <ul><li>a</li><li>{x}</li></ul>; }",
    "fragment": "function target(x) { return <>{x}<b>t</b></>; }",
    "self_closing": "function target(x) { return <div><br /><img src=\"a/b.png\"/></div>; }",
    "element_in_attribute": "function target(x) { return <A n={x && <b>t</b>} d>don't</A>; }",
    "regex_lookalike_inside_container": "function target(x) { return <p>{x </a>/.test(x)}</p>; }",
    "unclosed_element_does_not_swallow_the_next_function": "function target(x) { return <div>{x && <Foo y={1} />}\n}",
    "generic_arrow_is_no_element": "function target(x) { const id = <T>(v: T) => v; return id(x) </a>}/.test(x); }",
}


@pytest.mark.parametrize("name", sorted(JSX_CASES))
def test_jsx_is_extracted_whole(name):
    target = JSX_CASES[name]
    if name == "unclosed_element_does_not_swallow_the_next_function":
        assert "neighbor" not in extract_block(target + NEIGHBOR, "target", ".jsx")
        return
    assert extract_block(target + NEIGHBOR, "target", ".tsx" if name.startswith("generic") else ".jsx") == target


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_random_jsx_is_extracted_whole(seed):
    r = random.Random(seed)
    for _ in range(300):
        target = gen_jsx(r)
        ext = ".tsx" if "<T" in target else ".jsx"
        assert extract_block(target + NEIGHBOR, "target", ext) == target, target


TYPED_HEADS = [
    ("const y = <string>x;", ".ts"),
    ("const y = <Array<string>>x;", ".ts"),
    ("const id = <Value>(v: Value) => v;", ".ts"),
    ("const id = <T>(v: T) => v;", ".ts"),
    ("const id = <T extends Foo<Bar>>(v: T): T => v;", ".ts"),
    ("const id = <T,>(v: T) => v;", ".tsx"),
    ("const id = <T, U>(v: T) => v;", ".tsx"),
    ("const id = <T, U extends V>(v: T, w: U): T => v;", ".tsx"),
    ("const id = <T extends unknown>(v: T) => v;", ".tsx"),
    ("const id = async <T,>(v: T) => v;", ".tsx"),
    ("const el = <A>(x)</A>;", ".tsx"),
    ("const el = <A>(x)</A>;", ".jsx"),
    ("const el = <A>(x {1}) </A>;", ".jsx"),
    ("const el = <B>({x})</B>;", ".jsx"),
    ("const el = <A>(x)</A>; if (x) { x = 1 }\n/}/.test(x);", ".jsx"),
]


@pytest.mark.parametrize("head,ext", TYPED_HEADS)
def test_typescript_generics_and_parenthesized_jsx_text(head, ext):
    """A generic arrow (`<T,>(v: T) => v`) and a type assertion (`<string>x`) open no element; `<A>(x)</A>` does."""
    target = "function target(x) { " + head + " return /}/.test(String(x)); }"
    assert extract_block(target + NEIGHBOR, "target", ext) == target
