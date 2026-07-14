"""tests/test_collect_java_call_graph.py — COLLECT-29.

`build_call_edges`'s Java branch (`graph._java_call_names`): unambiguous
method and constructor calls across `.java` files produce edges, exactly
the way `test_collect_graph.py`'s Python call-graph tests already pin for
`ast.Call` sites; a name that's ambiguous across classes (an overloaded
or inherited-looking method, e.g. `area()` declared by two different
classes) is skipped rather than attributed to either owner; a `.java`
file that fails to parse can't crash the walk.

Java scanning is opt-in (COLLECT-28); every `scan_repo` call below passes
an explicit `languages = python,java` config so these tests exercise real
Java call-graph construction regardless of that toggle's own default.
"""

import configparser
from pathlib import Path

import pytest

from tools.collect.graph import build_call_edges
from tools.collect.java_parser import is_available
from tools.collect.scanner import scan_repo

pytestmark = pytest.mark.skipif(not is_available(), reason="tree-sitter-java not installed")

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo_java"


def _java_enabled_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["collect"] = {"languages": "python,java"}
    return cfg


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


# ── unambiguous method call across files ────────────────────────────────


def test_build_call_edges_finds_unambiguous_java_method_call(tmp_path):
    _write(
        tmp_path / "pkg" / "Helper.java",
        "package pkg;\n"
        "public class Helper {\n"
        "    public static int doThing() {\n"
        "        return 1;\n"
        "    }\n"
        "}\n",
    )
    _write(
        tmp_path / "pkg" / "Caller.java",
        "package pkg;\n"
        "public class Caller {\n"
        "    public int run() {\n"
        "        return doThing();\n"
        "    }\n"
        "}\n",
    )
    modules = scan_repo(tmp_path, config=_java_enabled_config())
    edges = build_call_edges(tmp_path, modules)
    assert edges["pkg/Caller.java"] == frozenset({"pkg/Helper.java"})
    assert edges["pkg/Helper.java"] == frozenset()


# ── unambiguous constructor call across files ───────────────────────────


def test_build_call_edges_finds_unambiguous_java_constructor_call(tmp_path):
    _write(
        tmp_path / "pkg" / "Point.java",
        "package pkg;\n"
        "public record Point(int x, int y) {\n"
        "}\n",
    )
    _write(
        tmp_path / "pkg" / "Factory.java",
        "package pkg;\n"
        "public class Factory {\n"
        "    public Point origin() {\n"
        "        return new Point(0, 0);\n"
        "    }\n"
        "}\n",
    )
    modules = scan_repo(tmp_path, config=_java_enabled_config())
    edges = build_call_edges(tmp_path, modules)
    assert edges["pkg/Factory.java"] == frozenset({"pkg/Point.java"})


# ── ambiguous method names are skipped, not misattributed ───────────────


def test_build_call_edges_skips_ambiguous_java_method_names(tmp_path):
    _write(
        tmp_path / "pkg" / "One.java",
        "package pkg;\n"
        "public class One {\n"
        "    public int run() {\n"
        "        return 1;\n"
        "    }\n"
        "}\n",
    )
    _write(
        tmp_path / "pkg" / "Two.java",
        "package pkg;\n"
        "public class Two {\n"
        "    public int run() {\n"
        "        return 2;\n"
        "    }\n"
        "}\n",
    )
    _write(
        tmp_path / "pkg" / "Caller.java",
        "package pkg;\n"
        "public class Caller {\n"
        "    public int wrapper() {\n"
        "        return run();\n"
        "    }\n"
        "}\n",
    )
    modules = scan_repo(tmp_path, config=_java_enabled_config())
    edges = build_call_edges(tmp_path, modules)
    # `run` is declared by both One and Two -> ambiguous, not attributed
    # to either, rather than guessed (same principle as the Python
    # `test_build_call_edges_skips_ambiguous_symbol_names`).
    assert edges["pkg/Caller.java"] == frozenset()


def test_mini_repo_overloaded_area_method_is_not_misattributed():
    # The real mini-repo fixture already has two classes (Circle, Square,
    # both in Shape.java) that each declare an `area()` method — the
    # overload-heavy case the COLLECT-29 AC names explicitly. No other
    # file in the fixture calls `area()` unqualified, so this mainly
    # pins "no crash, no edge fabricated out of an ambiguous name."
    modules = scan_repo(FIXTURE_ROOT, config=_java_enabled_config())
    edges = build_call_edges(FIXTURE_ROOT, modules)
    for path, targets in edges.items():
        assert "com/example/Shape.java" not in targets or path == "com/example/Shape.java"


# ── no crash on a broken .java file ──────────────────────────────────────


def test_build_call_edges_skips_broken_java_file_without_crashing(tmp_path):
    _write(
        tmp_path / "pkg" / "Broken.java",
        "package pkg;\npublic class Broken {\n",  # unterminated: real syntax error
    )
    _write(
        tmp_path / "pkg" / "Good.java",
        "package pkg;\npublic class Good {\n    public int helper() {\n        return 1;\n    }\n}\n",
    )
    modules = scan_repo(tmp_path, config=_java_enabled_config())
    edges = build_call_edges(tmp_path, modules)
    assert edges["pkg/Broken.java"] == frozenset()
    assert set(edges) == {"pkg/Broken.java", "pkg/Good.java"}


# ── mixed Python + Java repo: each language resolves independently ──────


def test_mixed_repo_java_and_python_call_edges_do_not_cross_wire(tmp_path):
    _write(
        tmp_path / "pkg" / "Helper.java",
        "package pkg;\n"
        "public class Helper {\n"
        "    public static int doJavaThing() {\n"
        "        return 1;\n"
        "    }\n"
        "}\n",
    )
    _write(
        tmp_path / "pkg" / "Caller.java",
        "package pkg;\n"
        "public class Caller {\n"
        "    public int run() {\n"
        "        return doJavaThing();\n"
        "    }\n"
        "}\n",
    )
    (tmp_path / "helper.py").write_text(
        "def do_python_thing():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "caller.py").write_text(
        "from helper import do_python_thing\n\n\ndef run():\n    return do_python_thing()\n",
        encoding="utf-8",
    )
    modules = scan_repo(tmp_path, config=_java_enabled_config())
    edges = build_call_edges(tmp_path, modules)
    assert edges["pkg/Caller.java"] == frozenset({"pkg/Helper.java"})
    assert edges["caller.py"] == frozenset({"helper.py"})
