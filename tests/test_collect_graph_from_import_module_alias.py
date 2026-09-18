"""L4 — `from <pkg> import <module> as <alias>` reaches the module, not
only the package.

`extract_imports` records `from tools.collect import test_map as
test_map_mod` as `"tools.collect"`, which `resolve_import` can only land on
`tools/collect/__init__.py`. The module actually named never got an edge, so
for every sibling `cli.py` imports in that house style the V3 `callers:` row
was missing its main shipped consumer.

The fix records the finer `"pkg.name"` spelling in a separate field,
`ModuleRecord.from_imports`, and `import_edges` resolves both lists. Kept
separate on purpose: the Pass A facts block and the rendered `Imports:` line
keep the coarse list, so the fix costs the producer nothing — the last two
tests here pin that.

Fixture cases run against a `tmp_path` package; the real-repo cases scan this
tree (`.collect/` is gitignored, so an artifact can't be relied on) and are
what puts this file in `tests/SLOW_TESTS.txt`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tools.collect._determinism import canonical_dumps
from tools.collect.ast_facts import extract_from_import_names, extract_imports
from tools.collect.graph import build_module_index, import_edges, imported_by, resolve_import
from tools.collect.loader import STATUS_FRESH, CollectModel
from tools.collect.model import ModuleRecord
from tools.collect.render import render_module_map
from tools.collect.scanner import scan_module, scan_repo
from tools.collect.summarizer import _facts_block

PKG_FILES = {
    "pkg/__init__.py": "SYMBOL = 1\nb = 'a symbol that shadows nothing'\n",
    "pkg/b.py": "def b_fn():\n    return 1\n",
    "pkg/sub/__init__.py": "def sub_fn():\n    return 2\n",
    "pkg/inner/__init__.py": "",
    "pkg/inner/leaf.py": "LEAF = 1\n",
}


def _write(root: Path, extra: dict) -> None:
    for rel, source in {**PKG_FILES, **extra}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def _graph(root: Path):
    modules = scan_repo(root)
    edges = import_edges(modules)
    return modules, edges, imported_by(edges)


# ── acceptance 1: `from pkg import b as bb` edges to pkg/b.py ────────────────


@pytest.mark.parametrize("statement", [
    "from pkg import b",
    "from pkg import b as bb",
    "from pkg import b as _b_mod",
    "from pkg import (b as bb, SYMBOL)",
])
def test_from_pkg_import_module_alias_edges_to_the_module(tmp_path, statement):
    _write(tmp_path, {"pkg/a.py": statement + "\n"})
    _, edges, reverse = _graph(tmp_path)
    assert "pkg/b.py" in edges["pkg/a.py"]
    assert "pkg/a.py" in reverse["pkg/b.py"]
    # The package edge is kept, not replaced: `from pkg import b` really
    # does import `pkg/__init__.py` first.
    assert edges["pkg/a.py"] == frozenset({"pkg/__init__.py", "pkg/b.py"})


def test_from_pkg_import_subpackage_edges_to_its_init(tmp_path):
    _write(tmp_path, {"pkg/a.py": "from pkg import sub as sub_mod\n"})
    _, edges, _ = _graph(tmp_path)
    assert edges["pkg/a.py"] == frozenset({"pkg/__init__.py", "pkg/sub/__init__.py"})


def test_from_nested_pkg_import_module(tmp_path):
    _write(tmp_path, {"pkg/a.py": "from pkg.inner import leaf as leaf_mod\n"})
    _, edges, _ = _graph(tmp_path)
    assert edges["pkg/a.py"] == frozenset({"pkg/inner/__init__.py", "pkg/inner/leaf.py"})


def test_relative_from_pkg_import_module_alias(tmp_path):
    _write(tmp_path, {"pkg/a.py": "from .inner import leaf as leaf_mod\n"})
    _, edges, _ = _graph(tmp_path)
    assert edges["pkg/a.py"] == frozenset({"pkg/inner/__init__.py", "pkg/inner/leaf.py"})


# ── acceptance 2: a symbol in pkg/__init__.py resolves there and nowhere else


@pytest.mark.parametrize("statement", [
    "from pkg import SYMBOL",
    "from pkg import SYMBOL as S",
    "from pkg import SYMBOL, b as bb",  # mixed: symbol adds nothing beyond __init__
])
def test_from_pkg_import_symbol_resolves_to_init_only(tmp_path, statement):
    _write(tmp_path, {"pkg/a.py": statement + "\n"})
    _, edges, _ = _graph(tmp_path)
    expected = {"pkg/__init__.py"} | ({"pkg/b.py"} if " b " in statement else set())
    assert edges["pkg/a.py"] == frozenset(expected)


def test_relative_symbol_import_falls_back_to_the_package(tmp_path):
    _write(tmp_path, {"pkg/inner/x.py": "from . import LEAF\nfrom .. import SYMBOL as S\n"})
    _, edges, _ = _graph(tmp_path)
    assert edges["pkg/inner/x.py"] == frozenset({"pkg/inner/__init__.py", "pkg/__init__.py"})


def test_star_and_external_imports_add_no_edge(tmp_path):
    _write(tmp_path, {"pkg/a.py": "from pkg import *\nfrom os import path\nfrom typing import Any\n"})
    modules, edges, _ = _graph(tmp_path)
    assert edges["pkg/a.py"] == frozenset({"pkg/__init__.py"})
    a = next(m for m in modules if m.path == "pkg/a.py")
    assert a.from_imports == ("os.path", "typing.Any")  # candidates, unresolved, harmless


# ── the extractor and the field ──────────────────────────────────────────────


@pytest.mark.parametrize("source, expected", [
    ("from pkg import b as bb", ["pkg.b"]),
    ("from pkg import b, c as cc, D", ["pkg.D", "pkg.b", "pkg.c"]),
    ("from pkg import b\nfrom pkg import b as b2", ["pkg.b"]),
    ("from .sub import mod as m", [".sub.mod"]),
    ("from . import x", []),          # already precise in `imports` (".x")
    ("from pkg import *", []),
    ("import pkg.b as bb", []),       # plain `import` is precise already
])
def test_extract_from_import_names(source, expected):
    assert extract_from_import_names(ast.parse(source)) == expected


def test_alias_never_enters_any_recorded_name():
    tree = ast.parse("from pkg import b as totally_local_name")
    assert "totally_local_name" not in " ".join(extract_imports(tree) + extract_from_import_names(tree))


def test_imports_field_is_unchanged_by_l4():
    tree = ast.parse("from tools.collect import test_map as test_map_mod\nfrom . import util\n")
    assert extract_imports(tree) == [".util", "tools.collect"]


def test_scan_module_round_trips_from_imports_through_the_artifact():
    rec = scan_module("from pkg import b as bb\n", "pkg/a.py")
    assert rec.from_imports == ("pkg.b",)
    again = ModuleRecord.from_dict(rec.to_dict())
    assert again == rec
    assert again.field_provenance()["from_imports"] == "static"


def test_pre_l4_artifact_loads_with_empty_from_imports_and_the_old_graph():
    d = scan_module("from pkg import b as bb\n", "pkg/a.py").to_dict()
    del d["from_imports"]
    old = ModuleRecord.from_dict(d)
    assert old.from_imports == ()
    index = build_module_index([old, ModuleRecord(path="pkg/__init__.py"), ModuleRecord(path="pkg/b.py")])
    assert [resolve_import(n, index, importer_path="pkg/a.py") for n in old.imports] == ["pkg/__init__.py"]


def test_resolve_import_needs_no_new_rule():
    index = build_module_index([ModuleRecord(path=p) for p in ("pkg/__init__.py", "pkg/b.py")])
    assert resolve_import("pkg.b", index, importer_path="pkg/a.py") == "pkg/b.py"
    assert resolve_import("pkg.SYMBOL", index, importer_path="pkg/a.py") == "pkg/__init__.py"
    assert resolve_import("other.thing", index, importer_path="pkg/a.py") is None


# ── determinism ──────────────────────────────────────────────────────────────


def test_import_edges_stay_sorted_deduplicated_and_stable(tmp_path):
    _write(tmp_path, {
        "pkg/a.py": "from pkg import b as bb\nfrom pkg import b\nimport pkg.b\nfrom pkg import SYMBOL\n",
    })
    first = {k: sorted(v) for k, v in _graph(tmp_path)[1].items()}
    second = {k: sorted(v) for k, v in _graph(tmp_path)[1].items()}
    assert canonical_dumps(first) == canonical_dumps(second)
    assert first["pkg/a.py"] == ["pkg/__init__.py", "pkg/b.py"]


# ── the producer pays nothing: prompt and page keep the coarse list ─────────


def test_pass_a_facts_block_does_not_carry_the_candidates():
    rec = scan_module("from typing import Any, Dict\nfrom pkg import b as bb\n", "pkg/a.py")
    block = _facts_block(rec)
    assert "imports: pkg, typing" in block
    assert "typing.Any" not in block and "pkg.b" not in block and "from_imports" not in block


def test_rendered_page_does_not_carry_the_candidates():
    rec = scan_module("from typing import Any\nfrom pkg import b as bb\n", "pkg/a.py")
    page = render_module_map([rec])
    assert "`pkg`" in page and "`typing`" in page
    assert "typing.Any" not in page and "pkg.b" not in page


# ── acceptance 3: this repo ──────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = "tools/collect/cli.py"
TEST_MAP = "tools/collect/test_map.py"


@pytest.fixture(scope="module")
def live():
    modules = scan_repo(REPO_ROOT)
    edges = import_edges(modules)
    reverse = imported_by(edges)
    model = CollectModel(
        status=STATUS_FRESH,
        modules=tuple(modules),
        import_edges={k: tuple(sorted(v)) for k, v in edges.items()},
        imported_by={k: tuple(sorted(v)) for k, v in reverse.items()},
    )
    return modules, edges, reverse, model


def _mod_targets_of_cli() -> set:
    """Every `from tools.collect import X as X_mod` in cli.py, read from the
    source so the test follows the file rather than a hard-coded nine."""
    tree = ast.parse((REPO_ROOT / CLI).read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "tools.collect":
            for alias in node.names:
                if alias.asname and alias.asname.endswith("_mod"):
                    out.add(f"tools/collect/{alias.name}.py")
    return out


def test_cli_py_edges_carry_every_sibling_mod_target(live):
    _, edges, _, _ = live
    targets = _mod_targets_of_cli()
    assert len(targets) >= 9, targets
    assert targets <= edges[CLI], sorted(targets - edges[CLI])


def test_callers_of_test_map_names_cli_py(live):
    _, _, reverse, model = live
    assert CLI in reverse[TEST_MAP]
    assert CLI in model.callers_of(TEST_MAP)
