"""tests_bugfix/test_collect_src_layout_import_graph_301.py

Bug: a repository that keeps its packages under `src/` (or `lib/`) produced
no import graph and no test map. `graph._module_dotted_name("src/pkg/core.py")`
was `src.pkg.core` while the source says `import pkg.core`, so every internal
import resolved to nothing: each module looked like an entry point, blast
radius was 0 everywhere, the risk index could not rank anything, and the
callers/tests rows of the collect block came out empty. The same four files
in a flat layout resolved every edge.

Field report: HARDEN-4 D3, reproduced on a four-file repo in both layouts
before the fix.
"""

from __future__ import annotations

from pathlib import Path

from tools.collect.graph import build_module_index, import_edges, resolve_import
from tools.collect.model import ModuleRecord
from tools.collect.scanner import scan_repo
from tools.collect.test_map import build_test_map

PACKAGE = {
    "pkg/__init__.py": "",
    "pkg/core.py": "import pkg.util\n\n\ndef run():\n    return util.helper()\n",
    "pkg/util.py": "def helper():\n    return 1\n",
}
TEST = "from pkg import core\n\n\ndef test_run():\n    assert core.run()\n"


def _repo(root: Path, source_dir: str = "") -> Path:
    for rel, source in PACKAGE.items():
        path = root / source_dir / rel if source_dir else root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_core.py").write_text(TEST, encoding="utf-8")
    return root


def _flat_paths(table: dict) -> dict:
    """The table with a leading `src/` taken off every key and target, so a src layout can be
    compared against the flat layout's own table key for key."""
    return {
        (p[4:] if p.startswith("src/") else p): sorted(t[4:] if t.startswith("src/") else t for t in targets)
        for p, targets in table.items()
    }


def test_src_layout_gets_the_same_edges_as_the_flat_layout(tmp_path):
    flat_modules = scan_repo(_repo(tmp_path / "flat"))
    src_modules = scan_repo(_repo(tmp_path / "src", "src"))

    flat_edges = import_edges(flat_modules)
    src_edges = import_edges(src_modules)

    assert src_edges["src/pkg/core.py"] == frozenset({"src/pkg/util.py"})
    assert _flat_paths(src_edges) == {p: sorted(t) for p, t in flat_edges.items()}


def test_src_layout_gets_the_same_test_map_as_the_flat_layout(tmp_path):
    flat_modules = scan_repo(_repo(tmp_path / "flat"))
    src_modules = scan_repo(_repo(tmp_path / "src", "src"))

    flat_map = build_test_map(tmp_path / "flat", flat_modules)
    src_map = build_test_map(tmp_path / "src", src_modules)

    assert src_map["src/pkg/core.py"] == ("tests/test_core.py",)
    assert _flat_paths(src_map) == {p: sorted(t) for p, t in flat_map.items()}


def test_a_real_top_level_package_named_src_still_resolves():
    modules = [
        ModuleRecord(path="src/pkg/__init__.py"),
        ModuleRecord(path="src/pkg/core.py", imports=("src.pkg.util",)),
        ModuleRecord(path="src/pkg/util.py"),
    ]
    index = build_module_index(modules)
    assert resolve_import("src.pkg.util", index) == "src/pkg/util.py"
    assert resolve_import("pkg.util", index) == "src/pkg/util.py"


def test_a_loose_script_directory_keeps_its_full_dotted_name():
    # `scripts/` holds no packages, so it is not a source root: nothing that
    # resolved before changes.
    assert build_module_index([ModuleRecord(path="scripts/run.py")]) == {"scripts.run": "scripts/run.py"}
