"""tests/test_collect_test_map.py — COLLECT-12.

* A module with a dedicated (import-referencing) test file is matched to
  it in `build_test_map`.
* A module nothing test-side imports lands on the zero-list.
* The `from pkg import submodule` import form (what real callers in this
  repo actually use to reach e.g. `tools/backoff.py`) is matched too, not
  just the coarser `from pkg.submodule import name` form.
* AC reference case: on this real repo, `tools/backoff.py` and
  `tools/llm_stream.py` are *not* on today's zero-list (both are imported
  by at least one test file post-fixes) — `build_test_map` must be able to
  reproduce that against the real tree.
* thin-list and zero-list are disjoint: nothing appears on both.
"""

from pathlib import Path

from tools.collect.scanner import scan_repo
from tools.collect.test_map import (
    build_test_map,
    is_test_module,
    thin_coverage,
    zero_coverage,
)

REPO_ROOT = Path(__file__).parent.parent


def _write(root, rel, content):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ── is_test_module ──────────────────────────────────────────────────────


def test_is_test_module_requires_tests_dir_and_test_prefix():
    assert is_test_module("tests/test_collect_graph.py")
    assert not is_test_module("tools/collect/graph.py")
    # Scaffolding/fixtures under tests/ are not themselves "a test".
    assert not is_test_module("tests/_pass_a_stub.py")
    assert not is_test_module("tests/fixtures/collect_mini_repo/pkg/a.py")


# ── build_test_map: dedicated test matched, module without tests zeroed ──


def test_module_with_dedicated_test_is_matched(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/target.py", "def run():\n    return 1\n")
    _write(
        tmp_path,
        "tests/test_target.py",
        "from pkg.target import run\n\n\ndef test_run():\n    assert run() == 1\n",
    )
    modules = scan_repo(tmp_path)
    test_map = build_test_map(tmp_path, modules)
    assert test_map["pkg/target.py"] == ("tests/test_target.py",)


def test_module_without_any_importing_test_is_on_zero_list(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/target.py", "def run():\n    return 1\n")
    _write(tmp_path, "pkg/orphan.py", "def lonely():\n    return 2\n")
    _write(
        tmp_path,
        "tests/test_target.py",
        "from pkg.target import run\n\n\ndef test_run():\n    assert run() == 1\n",
    )
    modules = scan_repo(tmp_path)
    test_map = build_test_map(tmp_path, modules)
    assert test_map["pkg/orphan.py"] == ()
    assert zero_coverage(test_map) == ["pkg/__init__.py", "pkg/orphan.py"]


def test_test_modules_are_never_keys_in_the_map(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/target.py", "def run():\n    return 1\n")
    _write(tmp_path, "tests/test_target.py", "from pkg.target import run\n")
    modules = scan_repo(tmp_path)
    test_map = build_test_map(tmp_path, modules)
    assert "tests/test_target.py" not in test_map


def test_map_is_total_over_source_modules_even_with_zero_coverage(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/lonely.py", "x = 1\n")
    modules = scan_repo(tmp_path)
    test_map = build_test_map(tmp_path, modules)
    assert test_map == {"pkg/__init__.py": (), "pkg/lonely.py": ()}


def test_from_package_import_submodule_style_is_matched(tmp_path):
    # AC: the real repo reaches tools/backoff.py via `from tools import
    # backoff`, not `import tools.backoff` — ModuleRecord.imports alone
    # would collapse this to just "tools" and silently zero-list the
    # submodule. build_test_map must still match it.
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/sub.py", "def run():\n    return 1\n")
    _write(tmp_path, "tests/test_sub.py", "from pkg import sub\n")
    modules = scan_repo(tmp_path)
    test_map = build_test_map(tmp_path, modules)
    assert test_map["pkg/sub.py"] == ("tests/test_sub.py",)


# ── thin-list / zero-list: disjoint worklists ────────────────────────────


def test_thin_list_excludes_zero_covered_modules(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/well_covered.py", "def a():\n    return 1\n")
    _write(tmp_path, "pkg/thin.py", "def b():\n    return 2\n")
    _write(tmp_path, "pkg/zero.py", "def c():\n    return 3\n")
    _write(
        tmp_path,
        "tests/test_a.py",
        "from pkg.well_covered import a\nfrom pkg.thin import b\n",
    )
    _write(tmp_path, "tests/test_b.py", "from pkg.well_covered import a\n")
    _write(tmp_path, "tests/test_c.py", "from pkg.well_covered import a\n")

    modules = scan_repo(tmp_path)
    test_map = build_test_map(tmp_path, modules)
    zeros = zero_coverage(test_map)
    thins = thin_coverage(test_map, thin_threshold=1)

    assert zeros == ["pkg/__init__.py", "pkg/zero.py"]
    assert thins == ["pkg/thin.py"]  # exactly one covering test file
    assert "pkg/well_covered.py" not in thins  # covered by three test files
    assert set(zeros).isdisjoint(thins)


# ── AC: reproduces the manual zero-list check on real modules ───────────


def test_backoff_and_llm_stream_are_not_on_the_zero_list_post_fixes():
    modules = scan_repo(REPO_ROOT)
    test_map = build_test_map(REPO_ROOT, modules)
    zeros = set(zero_coverage(test_map))
    assert "tools/backoff.py" not in zeros
    assert "tools/llm_stream.py" not in zeros


def test_zero_and_thin_lists_are_sorted_and_deterministic():
    modules = scan_repo(REPO_ROOT)
    test_map = build_test_map(REPO_ROOT, modules)
    zeros = zero_coverage(test_map)
    thins = thin_coverage(test_map)
    assert zeros == sorted(zeros)
    assert thins == sorted(thins)
    assert set(zeros).isdisjoint(thins)
