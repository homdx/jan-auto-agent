"""L5 — `test_map` scans every test root the import graph scans.

`build_test_map` used to classify a test with its own `"tests/"` prefix
while `loader.callers_of` excluded importers by V1's six roots. A
`tests_bugfix/` test was therefore a *source module* to the map (keyed,
uncovered, on the zero-list) and a test importer to the graph, and the V3
pack for `main.py` read "13 test files do" three lines above "tests: 5
files". Both now read `tools.collect.test_paths`, and a symlinked mirror
(`.smoke_tests/`, `.regression_tests/`) is credited once, under its real
path.

Real-repo cases scan the tree once (module-scoped fixture), so this file is
in `tests/SLOW_TESTS.txt`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.collect import loader as loader_mod
from tools.collect import risk as risk_mod
from tools.collect import test_map as test_map_mod
from tools.collect import test_paths
from tools.collect.graph import import_edges, imported_by
from tools.collect.loader import STATUS_FRESH, CollectModel
from tools.collect.model import ModuleRecord
from tools.collect.scanner import scan_repo
from tools.collect.test_map import build_test_map, thin_coverage, zero_coverage
from tools.auto.context_assembler import _row_callers, _row_tests

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = "main.py"


def _write(root: Path, rel: str, content: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _module(path: str) -> ModuleRecord:
    return ModuleRecord(path=path, language="python")


# ── one list, two predicates ─────────────────────────────────────────────


def test_loader_and_test_map_read_the_same_list():
    """The ticket's rule 1: import it or move it — never two lists."""
    assert not hasattr(loader_mod, "_TEST_PATH_PREFIXES")
    assert not hasattr(test_map_mod, "TEST_DIR_PREFIX")
    assert loader_mod.is_test_path is test_paths.is_test_path
    assert test_map_mod.is_test_module is test_paths.is_test_module
    assert risk_mod.is_test_module is test_paths.is_test_module


@pytest.mark.parametrize("root", test_paths.TEST_PATH_PREFIXES)
def test_every_root_the_loader_knows_is_a_test_root_for_the_map(root):
    assert test_paths.is_test_path(f"{root}test_x.py")
    assert test_paths.is_test_module(f"{root}test_x.py")
    # Location alone is not coverage: scaffolding in a test root still
    # covers nothing, and a conftest is plumbing wherever it sits.
    assert test_paths.is_test_path(f"{root}helper.py")
    assert not test_paths.is_test_module(f"{root}helper.py")
    assert not test_paths.is_test_module(f"{root}conftest.py")


@pytest.mark.parametrize(
    "path, is_path, is_module",
    [
        ("conftest.py", True, False),
        ("tests/conftest.py", True, False),
        ("tests/_pass_a_stub.py", True, False),
        ("tests/fixtures/collect_mini_repo/pkg/a.py", True, False),
        ("stub-test/stub_codeapp_server.py", True, False),
        ("tools/collect/test_map.py", False, False),   # shipped, name only
        ("test_top_level.py", False, False),           # name is not a signal
        ("tests_bugfix/test_b.py", True, True),
        ("testsuite/test_x.py", False, False),         # prefix, not substring
        ("", False, False),
        (None, False, False),                          # fail open, never raise
    ],
)
def test_the_two_predicates(path, is_path, is_module):
    assert test_paths.is_test_path(path) is is_path
    assert test_paths.is_test_module(path) is is_module


def test_loader_is_test_path_is_unchanged_in_effect():
    """V1's filter keeps exactly its old answers, now from the shared list."""
    f = CollectModel._is_test_path
    recorded = frozenset({"weird/place/test_it.py"})
    assert f("tests_bugfix/test_a.py", frozenset())
    assert f(".smoke_tests/test_a.py", frozenset())
    assert f("conftest.py", frozenset())
    assert f("weird/place/test_it.py", recorded)
    assert not f("weird/place/test_it.py", frozenset())
    assert not f("tools/collect/test_map.py", recorded)
    assert not f("", recorded)


# ── AC 1: two roots, one map ─────────────────────────────────────────────


def test_tests_and_tests_bugfix_both_cover_the_same_module(tmp_path):
    _write(tmp_path, "m.py", "def f():\n    return 1\n")
    _write(tmp_path, "tests/test_a.py", "import m\n")
    _write(tmp_path, "tests_bugfix/test_b.py", "from m import f\n")
    tmap = build_test_map(tmp_path, scan_repo(tmp_path))
    assert tmap["m.py"] == ("tests/test_a.py", "tests_bugfix/test_b.py")
    # A test from any root is a test, never a source-module key.
    assert "tests_bugfix/test_b.py" not in tmap
    assert "tests/test_a.py" not in tmap
    assert zero_coverage(tmap) == []


def test_every_root_counts_and_scaffolding_still_does_not(tmp_path):
    _write(tmp_path, "m.py", "X = 1\n")
    for root in ("tests/", "tests_bugfix/", "tests_slow/", "stub-test/"):
        _write(tmp_path, f"{root}test_here.py", "import m\n")
    _write(tmp_path, "tests/_helper.py", "import m\n")
    _write(tmp_path, "tests_bugfix/conftest.py", "import m\n")
    _write(tmp_path, "conftest.py", "import m\n")
    tmap = build_test_map(tmp_path, scan_repo(tmp_path))
    assert tmap["m.py"] == (
        "stub-test/test_here.py", "tests/test_here.py",
        "tests_bugfix/test_here.py", "tests_slow/test_here.py",
    )
    # Scaffolding is not coverage: not credited, and not a covering key
    # either — a conftest.py is test plumbing, so it is not shipped code.
    assert "tests/_helper.py" in tmap          # helper stays a source module
    assert "tests_bugfix/conftest.py" not in tmap["m.py"]
    assert "conftest.py" not in tmap["m.py"]


def test_a_test_root_that_was_a_source_module_leaves_the_zero_list(tmp_path):
    """The number that moves: `tests_bugfix/` files were on the zero-list
    as untested shipped code. They are tests; they are not keys."""
    _write(tmp_path, "m.py", "X = 1\n")
    _write(tmp_path, "tests_bugfix/test_b.py", "import m\n")
    _write(tmp_path, "tests_bugfix/test_c.py", "X = 2\n")
    tmap = build_test_map(tmp_path, scan_repo(tmp_path))
    assert zero_coverage(tmap) == []
    assert thin_coverage(tmap) == ["m.py"]
    assert set(tmap) == {"m.py"}


# ── AC 2: a symlinked mirror counts once, under its real path ────────────


def _symlink_or_skip(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(os.path.relpath(target, link.parent), link)
    except (OSError, NotImplementedError) as e:  # pragma: no cover
        pytest.skip(f"symlinks unavailable here: {e}")


def test_canonical_test_path_collapses_a_mirror_and_leaves_a_real_file(tmp_path):
    real = _write(tmp_path, "tests/test_a.py", "import m\n")
    _symlink_or_skip(tmp_path / "tests_slow/test_a.py", real)
    assert test_paths.canonical_test_path(tmp_path, "tests_slow/test_a.py") == "tests/test_a.py"
    assert test_paths.canonical_test_path(tmp_path, "tests/test_a.py") == "tests/test_a.py"
    assert test_paths.canonical_test_path(tmp_path, "tests/missing.py") == "tests/missing.py"


def test_a_mirror_scanned_beside_its_target_is_one_covering_file(tmp_path):
    """`tests_slow/` stands in for `.smoke_tests/`: the walk prunes dotted
    directories, so a dotted mirror never reaches the scanner — but a mirror
    under any visible test root would, and must collapse."""
    _write(tmp_path, "m.py", "X = 1\n")
    real = _write(tmp_path, "tests/test_a.py", "import m\n")
    _symlink_or_skip(tmp_path / "tests_slow/test_a.py", real)
    modules = scan_repo(tmp_path)
    assert {m.path for m in modules} >= {"tests/test_a.py", "tests_slow/test_a.py"}
    tmap = build_test_map(tmp_path, modules)
    assert tmap["m.py"] == ("tests/test_a.py",)
    assert thin_coverage(tmap) == ["m.py"]


def test_a_dotted_mirror_handed_in_directly_still_counts_once(tmp_path):
    """The records need not come from the walk — an artifact rebuilt by a
    collector that does not prune dotted dirs must not double the count."""
    _write(tmp_path, "m.py", "X = 1\n")
    real = _write(tmp_path, "tests/test_a.py", "import m\n")
    _symlink_or_skip(tmp_path / ".smoke_tests/test_a.py", real)
    _symlink_or_skip(tmp_path / ".regression_tests/test_a.py", real)
    modules = [
        _module("m.py"), _module(".regression_tests/test_a.py"),
        _module("tests/test_a.py"), _module(".smoke_tests/test_a.py"),
    ]
    tmap = build_test_map(tmp_path, modules)
    assert tmap["m.py"] == ("tests/test_a.py",)


def test_a_mirror_seen_alone_is_credited_under_the_real_path(tmp_path):
    _write(tmp_path, "m.py", "X = 1\n")
    real = _write(tmp_path, "tests/test_a.py", "import m\n")
    _symlink_or_skip(tmp_path / ".smoke_tests/test_a.py", real)
    tmap = build_test_map(tmp_path, [_module("m.py"), _module(".smoke_tests/test_a.py")])
    assert tmap["m.py"] == ("tests/test_a.py",)


def test_a_mirrors_relative_import_resolves_beside_the_real_file(tmp_path):
    """Nothing lives beside a link: `from . import helper` in a mirror of
    `tests/test_a.py` means `tests/helper.py`."""
    _write(tmp_path, "tests/__init__.py")
    _write(tmp_path, "tests/helper.py", "X = 1\n")
    real = _write(tmp_path, "tests/test_a.py", "from . import helper\n")
    _symlink_or_skip(tmp_path / ".smoke_tests/test_a.py", real)
    modules = [
        _module("tests/__init__.py"), _module("tests/helper.py"),
        _module(".smoke_tests/test_a.py"),
    ]
    tmap = build_test_map(tmp_path, modules)
    assert tmap["tests/helper.py"] == ("tests/test_a.py",)


def test_a_dangling_or_escaping_link_fails_open(tmp_path):
    _write(tmp_path, "m.py", "X = 1\n")
    _symlink_or_skip(tmp_path / "tests/test_gone.py", tmp_path / "tests/nowhere.py")
    outside = _write(tmp_path.parent / f"{tmp_path.name}_outside", "test_out.py", "import m\n")
    _symlink_or_skip(tmp_path / "tests/test_out.py", outside)
    assert test_paths.canonical_test_path(tmp_path, "tests/test_gone.py") == "tests/test_gone.py"
    assert test_paths.canonical_test_path(tmp_path, "tests/test_out.py") == "tests/test_out.py"
    modules = [_module("m.py"), _module("tests/test_gone.py"), _module("tests/test_out.py")]
    tmap = build_test_map(tmp_path, modules)   # never raises
    # The escaping link is still read and credited under its own name; the
    # dangling one contributes no edges.
    assert tmap["m.py"] == ("tests/test_out.py",)


def test_the_map_is_deterministic(tmp_path):
    _write(tmp_path, "m.py", "X = 1\n")
    real = _write(tmp_path, "tests/test_a.py", "import m\n")
    _write(tmp_path, "tests_bugfix/test_b.py", "import m\n")
    _symlink_or_skip(tmp_path / "tests_slow/test_a.py", real)
    modules = scan_repo(tmp_path)
    a = build_test_map(tmp_path, modules)
    b = build_test_map(tmp_path, list(reversed(modules)))
    assert a == b


# ── AC 3: on this repo, the two rows agree ───────────────────────────────


@pytest.fixture(scope="module")
def live():
    modules = scan_repo(REPO_ROOT)
    edges = import_edges(modules)
    reverse = imported_by(edges)
    tmap = build_test_map(REPO_ROOT, modules)
    model = CollectModel(
        status=STATUS_FRESH,
        modules=tuple(modules),
        import_edges={k: tuple(sorted(v)) for k, v in edges.items()},
        imported_by={k: tuple(sorted(v)) for k, v in reverse.items()},
        test_map=tmap,
        zero_coverage_list=tuple(zero_coverage(tmap)),
        thin_coverage_list=tuple(thin_coverage(tmap)),
    )
    return modules, reverse, tmap, model


def test_no_test_from_any_root_is_a_map_key(live):
    modules, _, tmap, _ = live
    assert not [p for p in tmap if test_paths.is_test_module(p)]
    bugfix_tests = [m.path for m in modules if m.path.startswith("tests_bugfix/")]
    assert bugfix_tests, "this repo has a tests_bugfix/ root"
    assert not (set(bugfix_tests) & set(tmap))


def test_the_map_is_the_test_half_of_the_graph_for_every_module(live):
    """`test_map[path]` and the test importers in `imported_by[path]` are
    the same set — the map and the graph read one list now."""
    modules, reverse, tmap, _ = live
    for m in modules:
        if test_paths.is_test_module(m.path):
            continue
        from_graph = {p for p in reverse.get(m.path, ()) if test_paths.is_test_module(p)}
        assert set(tmap[m.path]) == from_graph, m.path


def test_main_py_callers_and_tests_rows_state_one_number(live):
    _, reverse, tmap, model = live
    test_importers = [p for p in reverse[TARGET] if test_paths.is_test_path(p)]
    assert len(test_importers) == len(tmap[TARGET])
    assert any(p.startswith("tests_bugfix/") for p in tmap[TARGET])
    callers = _row_callers(model, TARGET, None)
    tests = _row_tests(model, TARGET, None)
    n = len(tmap[TARGET])
    assert callers == f"callers: entry point — nothing in shipped code imports this; {n} test files do"
    assert tests.startswith(f"tests: {n} files: ")
