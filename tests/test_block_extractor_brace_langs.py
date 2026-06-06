"""tests/test_block_extractor_brace_langs.py

Regression guard: gate1's existence check grounds a cited symbol by calling
``block_extractor.extract_block``.  The C-like brace pattern previously required
the method name to follow the modifiers directly, so any method WITH a return
type (i.e. virtually every Java/C/C++/Kotlin method) was never found — which
made gate1 reject every code-mode candidate on a Java repo and the plan phase
produce nothing.  These tests lock in return-type-aware method extraction plus
class/interface/struct/enum extraction.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.block_extractor import extract_block


JAVA = """\
package ru.sbp.uniqr;

import java.util.List;

public class App {

    public String run(String in) {
        return in.trim().toUpperCase();
    }

    public static void main(String[] args) {
        new App().run(null);
    }

    private List<String> names(int n) {
        return List.of();
    }

    public App() {
        // constructor
    }
}
"""


def _found(name, src=JAVA, ext=".java"):
    return bool(extract_block(src, name, ext).strip())


def test_java_method_with_return_type():
    blk = extract_block(JAVA, "run", ".java")
    assert "public String run(String in)" in blk


def test_java_static_method():
    assert _found("main")


def test_java_method_with_generic_return_type():
    assert _found("names")


def test_java_constructor():
    assert _found("App")  # class declaration or constructor — either is fine


def test_java_class_declaration():
    blk = extract_block(JAVA, "App", ".java")
    assert "class App" in blk


def test_unknown_symbol_still_empty():
    assert extract_block(JAVA, "does_not_exist", ".java") == ""


def test_c_function_with_return_type():
    c = "int add(int a, int b) {\n    return a + b;\n}\n"
    assert "int add(int a, int b)" in extract_block(c, "add", ".c")


def test_kotlin_fun():
    kt = "class S {\n    fun greet(name: String): String {\n        return name\n    }\n}\n"
    assert _found("greet", kt, ".kt")


def test_rust_fn():
    rs = "pub fn compute(x: i32) -> i32 {\n    x * 2\n}\n"
    assert _found("compute", rs, ".rs")


def test_no_false_positive_on_call_site():
    # A bare call `foo(...)` on its own indented line could be matched by the
    # loose `^\s*name(` pattern; that's acceptable (it still yields a block),
    # but a method *definition* must be preferred when both exist.
    src = "class X {\n    void caller() {\n        helper(1);\n    }\n    int helper(int n) {\n        return n;\n    }\n}\n"
    blk = extract_block(src, "helper", ".java")
    assert "int helper(int n)" in blk


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
