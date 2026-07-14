"""tests/test_collect_graph.py — COLLECT-8.

* `imported_by` is the exact reverse index of `import_edges`: for every
  path `a` and `b` seen in the graph, `b in imported_by[a]` iff
  `a in edges[b]` — and every module in the input has an entry in both
  directions, even with zero edges.
* Entry-points on this real repo include `main.py`.
* An import cycle doesn't crash graph construction (no recursion, plain
  adjacency maps).
* `build_call_edges` finds a call to an unambiguous cross-module symbol,
  and does not attribute an ambiguous (same-name-in-two-modules) call to
  either owner.
* AC: `imported_by` has an entry for every scanned module (blast-radius is
  always available, never requires a defensive `.get`).
"""

from pathlib import Path

import pytest

from tools.collect.graph import (
    build_call_edges,
    entry_points,
    imported_by,
    import_edges,
    resolve_import,
)
from tools.collect.model import FunctionRecord, ModuleRecord
from tools.collect.scanner import scan_module, scan_repo

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo"
REPO_ROOT = Path(__file__).parent.parent


def _record(path, imports=(), symbols=()):
    return ModuleRecord(path=path, imports=tuple(imports), public_symbols=tuple(symbols))


def _symbol(module_path, name):
    return FunctionRecord(
        qualname=f"{module_path}:{name}",
        module=module_path,
        lineno=1,
        signature=f"{name}(...)",
    )


# ── import_edges / imported_by: symmetry + totality ────────────────────────


def test_resolve_import_exact_and_package_prefix():
    index = {"tools.prompt_parser": "tools/prompt_parser.py", "tools.collect": "tools/collect/__init__.py"}
    assert resolve_import("tools.prompt_parser", index) == "tools/prompt_parser.py"
    # coarser from-import form (extract_imports records only the source
    # module) resolves to the package's __init__.
    assert resolve_import("tools.collect.model", index) == "tools/collect/__init__.py"
    assert resolve_import("os.path", index) is None


def test_import_edges_drops_external_imports():
    modules = [
        _record("pkg/a.py", imports=("pkg.b", "logging", "os")),
        _record("pkg/b.py"),
    ]
    edges = import_edges(modules)
    assert edges["pkg/a.py"] == frozenset({"pkg/b.py"})
    assert edges["pkg/b.py"] == frozenset()


def test_imported_by_is_exact_reverse_of_import_edges():
    modules = [
        _record("pkg/a.py", imports=("pkg.b",)),
        _record("pkg/b.py", imports=("pkg.c",)),
        _record("pkg/c.py"),
    ]
    edges = import_edges(modules)
    reverse = imported_by(edges)

    all_paths = set(edges)
    assert set(reverse) == all_paths  # totality: every module has an entry
    for a in all_paths:
        for b in all_paths:
            assert (b in reverse[a]) == (a in edges.get(b, frozenset()))

    assert reverse["pkg/b.py"] == frozenset({"pkg/a.py"})
    assert reverse["pkg/c.py"] == frozenset({"pkg/b.py"})
    assert reverse["pkg/a.py"] == frozenset()  # nothing imports the root


def test_imported_by_available_for_every_scanned_module_zero_importers_included():
    # COLLECT-8 AC: blast-radius (imported_by) is available for every
    # module, including ones nobody imports — no defensive .get needed.
    modules = scan_repo(FIXTURE_ROOT)
    edges = import_edges(modules)
    reverse = imported_by(edges)
    assert set(reverse) == {m.path for m in modules}
    for m in modules:
        assert isinstance(reverse[m.path], frozenset)


def test_import_cycle_does_not_crash_graph_construction():
    modules = [
        _record("pkg/a.py", imports=("pkg.b",)),
        _record("pkg/b.py", imports=("pkg.a",)),
    ]
    edges = import_edges(modules)
    reverse = imported_by(edges)
    assert edges["pkg/a.py"] == frozenset({"pkg/b.py"})
    assert edges["pkg/b.py"] == frozenset({"pkg/a.py"})
    assert reverse["pkg/a.py"] == frozenset({"pkg/b.py"})
    assert reverse["pkg/b.py"] == frozenset({"pkg/a.py"})
    # A cycle means neither module is a "nothing imports this" entry point.
    assert entry_points(edges, reverse) == []


# ── entry_points ─────────────────────────────────────────────────────────


def test_entry_points_include_main_py_on_real_repo():
    # Restrict to production modules: a test importing `main` (e.g. to
    # drive its CLI) is not itself part of the call graph `main.py` is the
    # root of, so it shouldn't disqualify `main.py` as an entry point.
    modules = [m for m in scan_repo(REPO_ROOT) if not m.path.startswith("tests/")]
    edges = import_edges(modules)
    eps = entry_points(edges)
    assert "main.py" in eps
    assert eps == sorted(eps)  # deterministic order (COLLECT-3)


def test_entry_points_defaults_to_computing_reverse_index():
    modules = [_record("pkg/root.py", imports=("pkg.leaf",)), _record("pkg/leaf.py")]
    edges = import_edges(modules)
    assert entry_points(edges) == ["pkg/root.py"]


# ── build_call_edges: heuristic call graph ──────────────────────────────


def test_build_call_edges_finds_unambiguous_cross_module_call(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "helper.py").write_text("def do_thing():\n    return 1\n", encoding="utf-8")
    (pkg / "caller.py").write_text(
        "from pkg.helper import do_thing\n\n\ndef run():\n    return do_thing()\n",
        encoding="utf-8",
    )
    modules = scan_repo(tmp_path)
    edges = build_call_edges(tmp_path, modules)
    assert edges["pkg/caller.py"] == frozenset({"pkg/helper.py"})
    assert edges["pkg/helper.py"] == frozenset()


def test_build_call_edges_skips_ambiguous_symbol_names(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "one.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (pkg / "two.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    (pkg / "caller.py").write_text("def wrapper():\n    return run()\n", encoding="utf-8")
    modules = scan_repo(tmp_path)
    edges = build_call_edges(tmp_path, modules)
    # `run` is ambiguous (owned by both one.py and two.py) -> not attributed
    # to either, rather than guessed.
    assert edges["pkg/caller.py"] == frozenset()


def test_build_call_edges_skips_parse_error_modules_without_crashing(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    (pkg / "good.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    modules = scan_repo(tmp_path)
    edges = build_call_edges(tmp_path, modules)
    assert edges["pkg/broken.py"] == frozenset()
    assert set(edges) == {"pkg/__init__.py", "pkg/broken.py", "pkg/good.py"}


def test_build_call_edges_java_branch_skips_undecodable_file_without_crashing(tmp_path):
    """BUGFIX regression: the Java branch of `build_call_edges` used to
    catch only `OSError` around its re-read of the source, so a `.java`
    file that isn't valid UTF-8 raised a bare `UnicodeDecodeError`
    straight out of `build_call_edges` — contradicting that branch's own
    comment ("one broken/unavailable file can't take down the scan") and
    the Python branch right below it, which already caught both. A
    `ModuleRecord` with no `parse_error` is used here (rather than going
    through `scan_repo`, which now itself catches this at read time) to
    exercise `build_call_edges`'s own defense-in-depth directly.
    """
    from tools.collect.lang import Language
    (tmp_path / "Bad.java").write_bytes(b"class Bad { void m() { \xff\xfe } }")
    modules = [ModuleRecord(path="Bad.java", language=Language.JAVA)]
    edges = build_call_edges(tmp_path, modules)
    assert edges == {"Bad.java": frozenset()}


# ── COLLECT-26: mixed Python + Java repo, imported_by symmetric for both ───


def test_mixed_python_java_repo_imported_by_is_symmetric(tmp_path):
    import configparser

    from tools.collect.java_parser import is_available

    if not is_available():
        pytest.skip("tree-sitter-java not installed")

    java_pkg = tmp_path / "com" / "example"
    java_pkg.mkdir(parents=True)
    (java_pkg / "Point.java").write_text(
        "package com.example;\npublic class Point { int x; }\n", encoding="utf-8"
    )
    other_pkg = tmp_path / "other"
    other_pkg.mkdir()
    (other_pkg / "Consumer.java").write_text(
        "package other;\n"
        "import com.example.Point;\n"
        "public class Consumer { Point p; }\n",
        encoding="utf-8",
    )
    (tmp_path / "helper.py").write_text("def f():\n    pass\n", encoding="utf-8")

    config = configparser.ConfigParser()
    config["collect"] = {"languages": "python,java"}
    modules = scan_repo(tmp_path, config=config)
    assert {m.path for m in modules} == {
        "com/example/Point.java",
        "other/Consumer.java",
        "helper.py",
    }

    edges = import_edges(modules)
    reverse = imported_by(edges)

    # The real cross-package Java import resolves to a genuine edge...
    assert edges["other/Consumer.java"] == frozenset({"com/example/Point.java"})
    assert reverse["com/example/Point.java"] == frozenset({"other/Consumer.java"})

    # ...and the unrelated Python file neither imports nor is imported by
    # either Java file — languages don't bleed into each other's edges.
    assert edges["helper.py"] == frozenset()
    assert reverse["helper.py"] == frozenset()

    # Full symmetry, both directions, across all three modules at once —
    # the COLLECT-26 AC, verbatim ("imported_by is symmetric for both
    # languages at once"), not just spot-checked on the one real edge.
    for a in edges:
        for b in edges:
            assert (b in reverse[a]) == (a in edges.get(b, frozenset()))


def test_java_cannot_import_python_module_no_crash_no_edge(tmp_path):
    # "Java can't import .py here or vice versa" (COLLECT-26 AC): a
    # same-named module in the other language must not accidentally
    # resolve — resolve_import works purely off each language's own
    # dotted-name index, so there's no shared namespace for a collision
    # to even occur in, but this pins that explicitly rather than by
    # inference.
    import configparser

    from tools.collect.java_parser import is_available

    if not is_available():
        pytest.skip("tree-sitter-java not installed")

    (tmp_path / "Shared.java").write_text(
        "public class Shared { void f() {} }\n", encoding="utf-8"
    )
    (tmp_path / "shared.py").write_text("def f():\n    pass\n", encoding="utf-8")
    config = configparser.ConfigParser()
    config["collect"] = {"languages": "python,java"}
    modules = scan_repo(tmp_path, config=config)
    edges = import_edges(modules)
    assert edges == {"Shared.java": frozenset(), "shared.py": frozenset()}
