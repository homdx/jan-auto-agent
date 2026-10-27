"""tests/test_collect_layouts.py — HARDEN-4 D3.

A repository that keeps its packages under `src/` (or `lib/`) got no import
graph and no test map at all: `_module_dotted_name("src/pkg/core.py")` was
`src.pkg.core`, while the source says `import pkg.core`, so every module
resolved to nothing, looked like an entry point, had blast radius 0 and no
tests — the risk index could not rank anything and the callers/tests rows of
the collect block came out empty. A flat copy of the same four files resolved
every edge.

The fix keys a module's layout-relative dotted name alongside its full path
name, so both layouts answer the same, and keeps the full path as a key so
nothing that resolves today stops resolving (a repo with a real top-level
package called `src` still resolves `import src.pkg.core`).
"""

from __future__ import annotations

from pathlib import Path

from tools.collect.graph import (
    _module_dotted_name,
    build_module_index,
    import_edges,
    imported_by,
    resolve_import,
)
from tools.collect.model import ModuleRecord
from tools.collect.test_map import build_test_map

#: The files both layouts are built from: a package with one importee, plus the test file that
#: covers it. Only the package moves under `src/`/`lib/`; the test root stays at the repo root, the
#: layout pytest itself uses (and the one `test_paths.is_test_module` recognises).
PACKAGE_FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": "import pkg.util\n\n\ndef run():\n    return util.helper()\n",
    "pkg/util.py": "def helper():\n    return 1\n",
}
TEST_FILES = {
    "tests/test_core.py": "from pkg import core\n\n\ndef test_run():\n    assert core.run()\n",
}


def _write_repo(root: Path, source_dir: str = "") -> Path:
    """Write the package under `root/source_dir` ("" = a flat layout, "src"/"lib" = a source-root
    layout) and the test tree at the repo root."""
    for rel, source in {**PACKAGE_FILES, **TEST_FILES}.items():
        under = rel.startswith("pkg/") and bool(source_dir)
        path = root / source_dir / rel if under else root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


def _graph_and_test_map(root: Path):
    from tools.collect.scanner import scan_repo

    modules = scan_repo(root)
    edges = import_edges(modules)
    return (
        {m.path: sorted(edges[m.path]) for m in modules},
        {path: list(tests) for path, tests in build_test_map(root, modules).items()},
        imported_by(edges),
    )


def _strip_source_dir(path: str) -> str:
    return path.split("/", 1)[1] if path.startswith(("src/", "lib/")) else path


def _layout_relative(table: dict) -> dict:
    """A src/lib-layout table with the leading `src/`/`lib/` taken off every path — keys and edge
    targets alike — so it can be compared against the flat layout's own table key for key."""
    return {
        _strip_source_dir(path): sorted(_strip_source_dir(t) for t in targets)
        for path, targets in table.items()
    }


def _build(tmp_path: Path, source_dir: str = ""):
    return _graph_and_test_map(_write_repo(tmp_path / (source_dir or "flat"), source_dir))


# ── both layouts answer the same ─────────────────────────────────────────────


def test_src_layout_and_flat_layout_yield_the_same_edges(tmp_path):
    flat_edges, _flat_tests, _flat_rev = _build(tmp_path, "")
    src_edges, _src_tests, _src_rev = _build(tmp_path, "src")

    assert src_edges["src/pkg/core.py"] == ["src/pkg/util.py"]
    assert _layout_relative(src_edges) == flat_edges


def test_src_layout_and_flat_layout_yield_the_same_test_map(tmp_path):
    _flat_edges, flat_tests, _rev = _build(tmp_path, "")
    _src_edges, src_tests, _rev2 = _build(tmp_path, "src")

    assert src_tests["src/pkg/core.py"] == ["tests/test_core.py"]
    assert _layout_relative(src_tests) == flat_tests


def test_src_layout_modules_are_not_all_entry_points(tmp_path):
    _edges, _tests, reverse = _build(tmp_path, "src")
    # core.py is imported by its test file; util.py is imported by core.py.
    assert reverse["src/pkg/core.py"] == frozenset({"tests/test_core.py"})
    assert reverse["src/pkg/util.py"] == frozenset({"src/pkg/core.py"})
    assert reverse["src/pkg/__init__.py"] == frozenset({"tests/test_core.py"})


def test_a_lib_layout_gets_the_same_treatment(tmp_path):
    flat_edges, flat_tests, _rev = _build(tmp_path, "")
    lib_edges, lib_tests, _rev2 = _build(tmp_path, "lib")

    assert _layout_relative(lib_edges) == flat_edges
    assert _layout_relative(lib_tests) == flat_tests


def test_the_src_layout_repo_has_no_module_left_unresolved(tmp_path):
    edges, _tests, _rev = _build(tmp_path, "src")
    # Every module that imports something in-repo has at least one edge: none of
    # them reads as an entry point it should not be.
    assert edges["src/pkg/core.py"]
    assert edges["tests/test_core.py"]


# ── the dotted name itself ───────────────────────────────────────────────────


def test_dotted_name_of_a_src_layout_file_keeps_its_full_form():
    # The full path name is still what _module_dotted_name reports: the alias is added on top of it.
    assert _module_dotted_name("src/pkg/core.py") == "src.pkg.core"
    assert _module_dotted_name("lib/pkg/core.py") == "lib.pkg.core"
    assert _module_dotted_name("pkg/core.py") == "pkg.core"
    assert _module_dotted_name("tools/collect/model.py") == "tools.collect.model"


def test_module_index_keys_both_names_of_a_src_layout_file():
    index = build_module_index([ModuleRecord(path="src/pkg/core.py")])
    assert index["src.pkg.core"] == "src/pkg/core.py"
    assert index["pkg.core"] == "src/pkg/core.py"


def test_a_flat_layout_module_gets_no_extra_key():
    # Nothing to strip: the repo root is already the source root.
    index = build_module_index([ModuleRecord(path="pkg/core.py")])
    assert index == {"pkg.core": "pkg/core.py"}


# ── resolution ───────────────────────────────────────────────────────────────


def test_import_pkg_core_resolves_in_a_src_layout():
    modules = [ModuleRecord(path=p) for p in ("src/pkg/__init__.py", "src/pkg/core.py", "src/pkg/util.py")]
    index = build_module_index(modules)
    assert resolve_import("pkg.core", index) == "src/pkg/core.py"
    assert resolve_import("pkg.util", index) == "src/pkg/util.py"
    assert resolve_import("pkg", index) == "src/pkg/__init__.py"
    assert resolve_import("pkg.core.MISSING", index) == "src/pkg/core.py"


def test_a_real_top_level_package_called_src_still_resolves():
    modules = [
        ModuleRecord(path="src/pkg/__init__.py"),
        ModuleRecord(path="src/pkg/core.py", imports=("src.pkg.util",)),
        ModuleRecord(path="src/pkg/util.py"),
    ]
    index = build_module_index(modules)
    assert resolve_import("src.pkg.util", index) == "src/pkg/util.py"
    assert resolve_import("src.pkg", index) == "src/pkg/__init__.py"
    # The short alias is still there for code that spells it that way.
    assert resolve_import("pkg.util", index) == "src/pkg/util.py"


def test_a_full_path_key_is_never_displaced_by_another_layout():
    # pkg/core.py and src/pkg/core.py both claim `pkg.core`: the one whose own
    # dotted name it is keeps it, the other is simply left unaliased (fail-open,
    # no wrong edge) rather than stealing the import.
    modules = [ModuleRecord(path=p) for p in ("pkg/core.py", "src/pkg/core.py")]
    index = build_module_index(modules)
    assert index["pkg.core"] == "pkg/core.py"
    assert index["src.pkg.core"] == "src/pkg/core.py"


def test_index_order_is_stable_for_the_same_input():
    modules = [ModuleRecord(path=p) for p in ("src/pkg/util.py", "src/pkg/core.py", "src/pkg/__init__.py")]
    assert build_module_index(modules) == build_module_index(list(reversed(modules)))


def test_a_relative_import_inside_a_src_layout_still_resolves():
    modules = [
        ModuleRecord(path="src/pkg/__init__.py"),
        ModuleRecord(path="src/pkg/core.py", imports=(".util",)),
        ModuleRecord(path="src/pkg/util.py"),
    ]
    edges = import_edges(modules)
    assert edges["src/pkg/core.py"] == frozenset({"src/pkg/util.py"})


def test_external_imports_still_add_no_edge_in_a_src_layout():
    modules = [
        ModuleRecord(path="src/pkg/__init__.py"),
        ModuleRecord(path="src/pkg/core.py", imports=("os.path", "requests", "pkg.util")),
        ModuleRecord(path="src/pkg/util.py"),
    ]
    edges = import_edges(modules)
    assert edges["src/pkg/core.py"] == frozenset({"src/pkg/util.py"})
