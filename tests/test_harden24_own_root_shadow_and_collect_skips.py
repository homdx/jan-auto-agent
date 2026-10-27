"""tests/test_harden24_own_root_shadow_and_collect_skips.py — HARDEN-24 A and B.

Part A: a folder without its own `__init__.py` that sits *inside* a package used
to be read as the layout's source root. `src/c/tests/test_c.py` therefore had
the layout-relative name `test_c`, its import `c.core` was rewritten to
`c.tests.c.core`, and the longest-prefix fallback crawled that miss up to `c` —
the package's `__init__.py` instead of `src/c/core.py`. The edge the graph drew
named the wrong file, so a change to `core.py` never selected the test that
imports it.

Part B: the collect stage read the Architect's `[search] max_depth = 2` and
`[search] max_file_kb = 500`, so a source three folders deep or a 600 KB
generated file was missing from the structural model, the log said nothing, and
the manifest did not name the file.
"""

from __future__ import annotations

import configparser
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.auto.repo_ingest import RepoIngestor  # noqa: E402
from tools.collect import cli as collect_cli  # noqa: E402
from tools.collect import manifest as manifest_mod  # noqa: E402
from tools.collect.graph import build_module_index, import_edges, resolve_import  # noqa: E402
from tools.collect.model import ModuleRecord  # noqa: E402
from tools.collect.scanner import (  # noqa: E402
    DEFAULT_COLLECT_MAX_DEPTH,
    DEFAULT_COLLECT_MAX_FILE_KB,
    collect_walk_limits,
    scan_repo,
    scan_repo_ex,
)


def _module(path: str, imports=()) -> ModuleRecord:
    return ModuleRecord(path=path, imports=tuple(imports))


# ── Part A ───────────────────────────────────────────────────────────────────


class TestOwnRootOnlyOnAPackageRoot:
    """A `tests/` folder without its own `__init__.py` inside a package is not
    the layout's source root, so it must not shadow the package's own imports."""

    @staticmethod
    def _src_package():
        return [
            _module("src/c/__init__.py"),
            _module("src/c/core.py"),
            _module("src/c/tests/test_c.py", ("c.core",)),
        ]

    def test_a_folder_without_init_does_not_shadow_the_package_import(self):
        index = build_module_index(self._src_package())
        # The repro: this used to return `src/c/__init__.py`.
        assert resolve_import(
            "c.core", index, "src/c/tests/test_c.py"
        ) == "src/c/core.py"

    def test_importing_the_package_still_names_its_init(self):
        index = build_module_index(self._src_package())
        assert resolve_import("c", index, "src/c/tests/test_c.py") == "src/c/__init__.py"

    def test_the_importer_keeps_its_own_source_root(self):
        index = build_module_index(self._src_package())
        # `src` is the layout's root for every file in the tree, not `c.tests`.
        assert index.roots_by_path["src/c/tests/test_c.py"] == ("src",)

    def test_the_graph_draws_the_edge_to_the_right_file(self):
        edges = import_edges(self._src_package())
        assert edges["src/c/tests/test_c.py"] == frozenset({"src/c/core.py"})

    def test_the_package_init_is_not_the_target_of_a_core_import(self):
        edges = import_edges(self._src_package())
        assert "src/c/__init__.py" not in edges["src/c/tests/test_c.py"]


class TestExistingLayoutsAreUnchanged:
    """A flat layout, a plain `src/` layout and a folder with its own
    `__init__.py` keep every edge they had."""

    def test_flat_layout_keeps_its_edges(self):
        modules = [_module("pkg/a.py", ("pkg.b",)), _module("pkg/b.py")]
        assert import_edges(modules)["pkg/a.py"] == frozenset({"pkg/b.py"})
        index = build_module_index(modules)
        assert resolve_import("pkg.b", index, "pkg/a.py") == "pkg/b.py"

    def test_src_layout_keeps_its_edges(self):
        modules = [
            _module("src/pkg/__init__.py"),
            _module("src/pkg/core.py"),
            _module("src/pkg/app.py", ("pkg.core",)),
        ]
        assert import_edges(modules)["src/pkg/app.py"] == frozenset({"src/pkg/core.py"})
        index = build_module_index(modules)
        assert resolve_import("pkg.core", index, "src/pkg/app.py") == "src/pkg/core.py"

    def test_a_folder_with_its_own_init_is_unchanged(self):
        modules = [
            _module("src/c/__init__.py"),
            _module("src/c/core.py"),
            _module("src/c/tests/__init__.py"),
            _module("src/c/tests/test_c.py", ("c.core",)),
        ]
        index = build_module_index(modules)
        assert resolve_import("c.core", index, "src/c/tests/test_c.py") == "src/c/core.py"
        # The source root is still `src`, and the sub-package keeps its own alias.
        assert index.roots_by_path["src/c/tests/test_c.py"] == ("src",)
        assert index["c.tests.test_c"] == "src/c/tests/test_c.py"

    def test_a_two_service_monorepo_keeps_its_edges(self):
        modules = [
            _module("svc_a/app/__init__.py"),
            _module("svc_a/app/models.py"),
            _module("svc_b/app/__init__.py"),
            _module("svc_b/app/models.py"),
            _module("svc_b/app/views.py", ("app.models",)),
        ]
        assert import_edges(modules)["svc_b/app/views.py"] == frozenset({"svc_b/app/models.py"})


# ── Part B ───────────────────────────────────────────────────────────────────


def _cfg(**sections) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    for name, values in sections.items():
        cfg[name] = {k: str(v) for k, v in values.items()}
    return cfg


def _deep_repo(tmp_path: Path) -> Path:
    """A repo whose sources sit three folders deep, with a shallow file too."""
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "a" / "b" / "c" / "deep.py").write_text("DEEP = 1\n", encoding="utf-8")
    (tmp_path / "a" / "top.py").write_text("TOP = 1\n", encoding="utf-8")
    (tmp_path / "shallow.py").write_text("SHALLOW = 1\n", encoding="utf-8")
    return tmp_path


class TestCollectReadsItsOwnLimits:
    """`--collect` no longer shares the Architect's `[search]` limits."""

    def test_the_collect_defaults_are_at_least_as_wide_as_the_search_ones(self):
        assert DEFAULT_COLLECT_MAX_DEPTH >= 2
        assert DEFAULT_COLLECT_MAX_FILE_KB >= 500

    def test_a_three_folder_deep_tree_is_collected_with_the_collect_default(
        self, tmp_path
    ):
        root = _deep_repo(tmp_path)
        cfg = _cfg(search={"max_depth": 2, "max_file_kb": 500})
        paths = {m.path for m in scan_repo(root, config=cfg)}
        assert "a/b/c/deep.py" in paths
        assert "a/top.py" in paths
        assert "shallow.py" in paths

    def test_the_architect_still_limits_on_search(self, tmp_path):
        """The same tree under `[search] max_depth = 2` still limits the Architect."""
        root = _deep_repo(tmp_path)
        cfg = _cfg(search={"max_depth": 2, "max_file_kb": 500})
        assert set(RepoIngestor(root, cfg).walk()) == {"a/top.py", "shallow.py"}

    def test_collect_max_depth_narrows_the_collect_walk(self, tmp_path):
        root = _deep_repo(tmp_path)
        cfg = _cfg(collect={"max_depth": 1})
        assert collect_walk_limits(cfg) == (1, DEFAULT_COLLECT_MAX_FILE_KB)
        paths = {m.path for m in scan_repo(root, config=cfg)}
        assert paths == {"a/top.py", "shallow.py"}

    def test_collect_max_file_kb_narrows_the_collect_walk(self, tmp_path):
        (tmp_path / "big.py").write_bytes(b"# " + b"x" * 4096 + b"\n")
        (tmp_path / "small.py").write_text("SMALL = 1\n", encoding="utf-8")
        cfg = _cfg(collect={"max_file_kb": 1})
        paths = {m.path for m in scan_repo(tmp_path, config=cfg)}
        assert paths == {"small.py"}

    def test_a_malformed_collect_limit_degrades_to_the_default(self, tmp_path):
        """Fail-open: a malformed key means "no collect data" about the limit, never a raise."""
        root = _deep_repo(tmp_path)
        for bad in ("banana", "", "-1", "0"):
            cfg = _cfg(collect={"max_depth": bad, "max_file_kb": bad})
            assert collect_walk_limits(cfg) == (
                DEFAULT_COLLECT_MAX_DEPTH,
                DEFAULT_COLLECT_MAX_FILE_KB,
            )
        paths = {m.path for m in scan_repo(root, config=cfg)}
        assert "a/b/c/deep.py" in paths

    def test_no_config_at_all_keeps_the_collect_defaults(self, tmp_path):
        root = _deep_repo(tmp_path)
        assert collect_walk_limits(None) == (
            DEFAULT_COLLECT_MAX_DEPTH,
            DEFAULT_COLLECT_MAX_FILE_KB,
        )
        assert "a/b/c/deep.py" in {m.path for m in scan_repo(root)}


class TestSkippedFilesAreNamed:
    """A skipped file is logged once with its reason and listed in the manifest."""

    def test_scan_repo_ex_reports_the_reason(self, tmp_path):
        root = _deep_repo(tmp_path)
        cfg = _cfg(collect={"max_depth": 1})
        modules, skipped = scan_repo_ex(root, config=cfg)
        assert {m.path for m in modules} == {"a/top.py", "shallow.py"}
        assert skipped == [("a/b/c/deep.py", "too deep")]

    def test_a_large_file_is_reported_too_large(self, tmp_path):
        (tmp_path / "big.py").write_bytes(b"# " + b"x" * 4096 + b"\n")
        cfg = _cfg(collect={"max_file_kb": 1})
        modules, skipped = scan_repo_ex(tmp_path, config=cfg)
        assert modules == []
        assert skipped == [("big.py", "too large")]

    def test_nothing_skipped_gives_nothing(self, tmp_path):
        root = _deep_repo(tmp_path)
        modules, skipped = scan_repo_ex(root, config=_cfg(search={"max_depth": 2}))
        assert len(modules) == 3 and skipped == []

    def test_each_skipped_file_is_logged_once(self, tmp_path, caplog):
        root = _deep_repo(tmp_path)
        cfg = _cfg(collect={"max_depth": 1})
        _, skipped = scan_repo_ex(root, config=cfg)
        with caplog.at_level("WARNING"):
            collect_cli._log_skipped_files(skipped)
        lines = [r.getMessage() for r in caplog.records]
        assert lines == ["collect: skipping a/b/c/deep.py (too deep)"]
        assert len(lines) == len(skipped)

    def test_logging_nothing_says_nothing(self, caplog):
        with caplog.at_level("WARNING"):
            collect_cli._log_skipped_files(())
        assert caplog.records == []

    def test_a_build_records_the_skipped_files_on_its_manifest(self, tmp_path):
        root = _deep_repo(tmp_path)
        cfg = _cfg(collect={"max_depth": 1, "dir": ".collect"})
        modules, skipped = scan_repo_ex(root, config=cfg)
        written = collect_cli._full_build(
            root, config=cfg, config_path=None, llm_call=None, modules=modules,
            skipped_files=tuple(skipped),
        )
        manifest = manifest_mod.read_manifest(
            written[0] / collect_cli.MANIFEST_FILENAME
        )
        assert manifest.skipped_files == (("a/b/c/deep.py", "too deep"),)
        assert {p for p, _ in manifest.skipped_files}.isdisjoint(manifest.file_hashes)

    def test_the_manifest_round_trips_skipped_files(self):
        manifest = manifest_mod.Manifest(
            collector_version="t", generated_at="t", git_sha=None, dirty=False,
            skipped_files=[("a/b/c/deep.py", "too deep"), ("big.py", "too large")],
        )
        round_tripped = manifest_mod.Manifest.from_json(manifest.to_json())
        assert sorted(round_tripped.skipped_files) == sorted(manifest.skipped_files)
        payload = json.loads(manifest.to_json())
        assert "a/b/c/deep.py" in json.dumps(payload["skipped_files"])

    def test_an_old_manifest_without_the_key_reads_empty(self):
        """Fail-open: a manifest written before this change is still readable."""
        payload = {
            "collector_version": "t",
            "generated_at": "t",
            "git_sha": None,
            "dirty": False,
            "file_hashes": {},
            "skip_dirs": [],
            "unsummarized": [],
            "profile_hashes": {},
        }
        assert manifest_mod.Manifest.from_dict(payload).skipped_files == ()

    def test_a_malformed_skipped_files_key_reads_empty(self):
        for bad in ("nope", {"a": 1}, [["only-one"]], [[1, 2]], [["ok", "ok", "extra"]]):
            payload = {
                "collector_version": "t", "generated_at": "t", "git_sha": None,
                "dirty": False, "file_hashes": {}, "skip_dirs": [],
                "unsummarized": [], "profile_hashes": {}, "skipped_files": bad,
            }
            assert manifest_mod.Manifest.from_dict(payload).skipped_files == (), bad
