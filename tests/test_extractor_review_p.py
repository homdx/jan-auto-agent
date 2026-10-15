"""Review p: a call of a name defined elsewhere is not its declaration, a named function expression is."""

from __future__ import annotations

import pytest

from tools.block_extractor import extract_block

_IMPORT = "import { compute } from './m';\n"


@pytest.mark.parametrize("ext,source", [
    ("c", "void run(struct S *x) {\n    x->compute(1);\n}\n"),
    ("cpp", "void run() {\n    Engine::compute(1);\n}\n"),
    ("cpp", "void run() {\n    std::chrono::compute(1);\n}\n"),
    ("php", "<?php\nclass A {\n  function run() {\n    $this->compute(1);\n  }\n}\n"),
    ("rs", "fn run() {\n    Self::compute(1);\n}\n"),
    ("js", _IMPORT + "const config = {\n  timeout: compute(5),\n  retries: 3,\n};\n"),
    ("js", _IMPORT + "ready && compute(1);\n"),
    ("ts", _IMPORT + "function run(): void {\n  ready || compute(1);\n}\n"),
    ("js", _IMPORT + "outer: compute(1);\n"),
    ("ts", _IMPORT + "const ops = [\n  compute(),\n  moveTo(0, 0),\n  options.on ? x : y,\n];\n"),
    ("c", "int ok(const char *a) {\n    return compute(a, \"x\") ||\n        other(a);\n}\n"),
    ("c", "void walk(struct node *n) {\n    compute(n->labels, l)\n        visit(l);\n}\n"),
    ("js", _IMPORT + "const total = compute(1) + 2;\n"),
])
def test_a_call_of_a_name_defined_elsewhere_is_not_found(ext, source):
    assert extract_block(source, "compute", ext) == ""


@pytest.mark.parametrize("ext,source,first_line", [
    ("c", "int compute(int a);\n", "int compute(int a);"),
    ("c", "char *compute(void);\n", "char *compute(void);"),
    ("c", "extern int compute(void);\n", "extern int compute(void);"),
    ("c", "int compute(int a)\n    __attribute__((noreturn));\n", "int compute(int a)"),
    ("c", "void compute(int a) __attribute__((noreturn));\n", "void compute(int a) __attribute__((noreturn));"),
    ("ts", "declare function compute(x: number): void;\n", "declare function compute(x: number): void;"),
    ("ts", "interface Store {\n  compute(): void;\n}\n", "compute(): void;"),
    ("ts", "abstract class A {\n  abstract compute(): void;\n}\n", "abstract compute(): void;"),
    ("java", "interface Job {\n    void compute() throws IOException;\n}\n", "void compute() throws IOException;"),
])
def test_a_declaration_without_a_body_is_still_found(ext, source, first_line):
    assert extract_block(source, "compute", ext).splitlines()[0].strip() == first_line


@pytest.mark.parametrize("head", ["return function logger(req, res, next) {",
                                  "return async function logger(req, res, next) {"])
def test_a_named_function_expression_after_return_is_its_definition(head):
    source = f"function morgan(format) {{\n  {head}\n    next()\n  }}\n}}\nmodule.exports = morgan\n"
    assert extract_block(source, "logger", "js") == f"  {head}\n    next()\n  }}"
