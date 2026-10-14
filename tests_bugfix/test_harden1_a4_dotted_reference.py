"""tests_bugfix/test_harden1_a4_dotted_reference.py — HARDEN-1 A4: a dotted
reference resolves to its last segment.

``extract_block`` looked a target name up verbatim, so ``module.func`` never
found ``func`` — and ``find_references`` emits exactly that shape on every run:
a call through an imported module is the commonest Python reference, so the
search reported it missing every time and the validator was fed that as noise.

The whole name is still tried first, so a real dotted definition (a
``Class.method`` qualname, a JS ``exports.h = function ...``) is unchanged, and
a name that is defined nowhere still returns ``''``.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.block_extractor import extract_block  # noqa: E402

PY_SRC = (
    "import os\n"
    "\n"
    "\n"
    "def func(a):\n"
    "    return a\n"
    "\n"
    "\n"
    "def c(a):\n"
    "    return a + 1\n"
    "\n"
    "\n"
    "class C:\n"
    "    def method(self):\n"
    "        return 1\n"
)


class TestADottedReferenceResolves:
    def test_module_func_finds_func(self):
        assert extract_block(PY_SRC, "module.func", ".py") == "def func(a):\n    return a\n"

    def test_a_b_c_finds_c(self):
        assert extract_block(PY_SRC, "a.b.c", ".py") == "def c(a):\n    return a + 1\n"

    def test_the_whole_name_still_wins_when_it_is_a_real_definition(self):
        # `C.method` is the qualname of the method, so the whole name matches directly.
        assert extract_block(PY_SRC, "C.method", ".py") == "    def method(self):\n        return 1\n"

    def test_the_bare_name_is_unchanged(self):
        assert extract_block(PY_SRC, "func", ".py") == "def func(a):\n    return a\n"

    def test_a_brace_language_dotted_call_resolves_to_the_definition(self):
        js = (
            "function main() {\n"
            "    obj.method(1);\n"
            "}\n"
            "\n"
            "function method() {\n"
            "    return 2;\n"
            "}\n"
        )
        assert extract_block(js, "obj.method", ".js") == "function method() {\n    return 2;\n}"

    def test_a_real_dotted_definition_is_kept_whole(self):
        js = "exports.h = function () {\n    return 1;\n}\n"
        assert extract_block(js, "exports.h", ".js") == js.rstrip("\n")


class TestNoMatchIsStillNoMatch:
    def test_an_unknown_dotted_name_returns_empty(self):
        assert extract_block(PY_SRC, "zzz.qqq", ".py") == ""

    def test_an_unknown_bare_name_returns_empty(self):
        assert extract_block(PY_SRC, "zzz", ".py") == ""
