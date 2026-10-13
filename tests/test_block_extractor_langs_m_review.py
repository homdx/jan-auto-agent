"""tests/test_block_extractor_langs_m_review.py — blocks and imports of non-Python targets, and a BOM, after review
batch m (№89–94, №103): generic, typed and qualified signatures are found, a Rust lifetime, a C preprocessor line
and a JS regex do not move a block's end, a form feed does not shift a Python block, a BOM'd module is read,
imports are one statement each, and a lambda's parameters are one parameter.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import block_extractor as B  # noqa: E402
from tools.claimcheck import anchors as cc_anchors  # noqa: E402
from tools.claimcheck.anchors import PathRepoView  # noqa: E402
from tools.collect.ast_facts import _split_params  # noqa: E402
from tools.collect.scanner import scan_repo  # noqa: E402

# ── №90: signatures of the brace languages ───────────────────────────────────────────────────

SIGNATURES = [
    (".ts", "function identity<T>(a: T): T {\n  return a;\n}\n", "identity"),
    (".ts", "const f = (x: number): number => {\n  return x;\n};\n", "f"),
    (".ts", "export const g = async <T>(x: T): Promise<T> => {\n  return x;\n};\n", "g"),
    (".rs", "fn longest<'a>(x: &'a str, y: &'a str) -> &'a str {\n    x\n}\n", "longest"),
    (".rs", "fn run<T>(x: T) where T: Copy {\n    let _ = x;\n}\n", "run"),
    (".rs", "impl<'a> P<'a> {\n    fn new(input: &'a str) -> P<'a> {\n        P { input }\n    }\n}\n", "new"),
    (".go", "func Map[T, U any](xs []T, f func(T) U) []U {\n\treturn nil\n}\n", "Map"),
    (".go", "type Server struct {\n\tport int\n}\n", "Server"),
    (".java", "class X {\n  public <T> T first(java.util.List<T> xs) {\n    return xs.get(0);\n  }\n}\n", "first"),
    (".c", "static char *dupstr(const char *s) {\n  return 0;\n}\n", "dupstr"),
    (".c", "unsigned long get(void) {\n  return 0;\n}\n", "get"),
    (".c", "static int\nfoo(void)\n{\n  return 1;\n}\n", "foo"),
    (".cpp", "int Foo::bar(int x) {\n  return x;\n}\n", "bar"),
    (".js", "function* gen() {\n  yield 1;\n}\n", "gen"),
    (".js", "exports.h = function (a, b) {\n  return a;\n};\n", "h"),
]


@pytest.mark.parametrize("ext, source, name", SIGNATURES, ids=[f"{e}:{n}" for e, _, n in SIGNATURES])
def test_a_signature_is_found_to_its_closing_brace(ext, source, name):
    block = B.extract_block(source, name, ext)
    assert name in block.split("\n", 2)[0] + block.split("\n", 2)[1] and block.rstrip().endswith("}"), block


def test_a_call_before_the_definition_is_still_no_definition():
    src = "function call() {\n  helper<number>(1, 2);\n}\nfunction helper<T>(a: T, b: T) {\n  return a;\n}\n"
    assert B.extract_block(src, "helper", ".ts").startswith("function helper<T>(")


# ── №91, №92: what moved a block's end ───────────────────────────────────────────────────────

@pytest.mark.parametrize("ext, source, name, block", [
    (".rs", "fn run() {\n    let l: &'static str = \"ok\";\n    println!(\"{}\", l);\n}\n\nfn other() {\n    // don't\n}\n",
     "run", "fn run() {\n    let l: &'static str = \"ok\";\n    println!(\"{}\", l);\n}"),
    (".rs", "fn c() -> char {\n    let q = '\\'';\n    let b = '{';\n    q\n}\nfn d() {}\n", "c",
     "fn c() -> char {\n    let q = '\\'';\n    let b = '{';\n    q\n}"),
    (".c", "int f(void) {\n#if 0\n#error don't compile\n#endif\n  return 1;\n}\nint g(void) {\n  // won't\n}\n", "f",
     "int f(void) {\n#if 0\n#error don't compile\n#endif\n  return 1;\n}"),
    (".js", "function h(s) {\n  return /}/.test(s);\n}\nfunction k() { return 1; }\n", "h",
     "function h(s) {\n  return /}/.test(s);\n}"),
    (".js", "function k(a, b) {\n  return a / b / 2;\n}\nfunction z() {}\n", "k",
     "function k(a, b) {\n  return a / b / 2;\n}"),
], ids=["rust-static-str", "rust-char-literals", "c-preprocessor-apostrophe", "js-regex-brace", "js-division"])
def test_a_block_ends_at_its_own_closing_brace(ext, source, name, block):
    assert B.extract_block(source, name, ext) == block


# ── №93: the lines ast counts ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sep", ["\x0c", "\u2028", "\x85", "\x1c"], ids=["form-feed", "U+2028", "NEL", "FS"])
def test_a_python_block_is_not_shifted_by_a_line_break_ast_does_not_count(sep):
    src = f"x = 1\n{sep}\ndef f():\n    return 2\n"
    assert B.extract_block(src, "f", ".py") == "def f():\n    return 2\n"


# ── №89: a module saved with a BOM ───────────────────────────────────────────────────────────

BOM_SRC = "\ufeff\"\"\"Rows.\"\"\"\nimport os\n\n\ndef load_rows(path):\n    return os.listdir(path)\n"


def test_a_bom_module_has_its_block_imports_and_docstring():
    assert B.extract_block(BOM_SRC, "load_rows", ".py") == "def load_rows(path):\n    return os.listdir(path)\n"
    assert B.extract_imports(BOM_SRC, ".py") == ["os"]
    assert B.extract_module_docstring(BOM_SRC, ".py") == "Rows."
    assert B.get_context_lines(BOM_SRC, "load_rows", 2, ".py").strip() == ""


def test_collect_and_the_anchors_read_a_bom_module(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "rows.py").write_bytes(BOM_SRC.encode("utf-8"))
    [mod] = [m for m in scan_repo(tmp_path) if m.path == "pkg/rows.py"]
    assert not mod.parse_error and [s.qualname for s in mod.public_symbols] == ["pkg/rows.py:load_rows"]
    claim = "`load_rows` in `pkg/rows.py` lists the directory"
    resolved = cc_anchors.resolve_claim(claim, PathRepoView(tmp_path))
    assert [(r.anchor.text, r.found) for r in resolved] == [("load_rows", True), ("pkg/rows.py", True)]


# ── №94: imports, one statement each ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("ext, source, names", [
    (".ts", "import './polyfills'\nimport React from 'react'\nimport type {Props} from './p'\n"
            "import {\n  a,\n  type B as C,\n} from 'x'\n", ["React", "Props", "a", "C"]),
    (".rs", "use foo::bar as baz;\nuse std::io::{Read as R};\n", ["foo", "baz", "std", "io", "R"]),
    (".go", 'import (\n\t"fmt" // see )\n\t"os"\n\t_ "net/http/pprof"\n\t. "math"\n\tal "a/b"\n)\n',
     ["fmt", "os", "al"]),
], ids=["ts", "rust", "go"])
def test_imports(ext, source, names):
    assert B.extract_imports(source, ext) == names


# ── №103: a lambda's parameters ──────────────────────────────────────────────────────────────

def test_a_lambda_default_is_one_parameter():
    assert _split_params("a, f=lambda x, y: x + y, b=1, c: Dict[str, int]=None, g=lambda: (1, 2), **kw") == \
        ["a", "f=lambda x, y: x + y", "b=1", "c: Dict[str, int]=None", "g=lambda: (1, 2)", "**kw"]
    assert _split_params("lambda_count=1, x=2") == ["lambda_count=1", "x=2"]
