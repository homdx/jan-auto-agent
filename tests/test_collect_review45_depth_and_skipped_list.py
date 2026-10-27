"""tests/test_collect_review45_depth_and_skipped_list.py — CC review fixes 45: the collect walk of HARDEN-24 B.

Three things HARDEN-24 B left behind, each reproduced on 357d55a:

* every shipped profile sets `[search] max_depth = 8`, which `--collect` read until it got a
  `[collect]` default of its own; at 6, a module seven or eight folders deep left the model of
  every shipped profile (the Architect's limit may widen the collect walk, never narrow it);
* the skipped list reached the manifest on the first `--collect` only: the second one (and the
  refresh between `--auto` tasks) scanned with plain `scan_repo`, handed the modules on, and the
  manifest was rewritten with no skipped file and nothing logged;
* a deep README, a JSON fixture or a stylesheet was called skipped too, though the scan never
  reads such a file.
"""

from __future__ import annotations

import configparser
import json
import logging
import subprocess
from pathlib import Path

import pytest

from tools.auto.collect_bridge import make_collect_bridge
from tools.collect import cli as cli_mod
from tools.collect.scanner import (
    DEFAULT_COLLECT_MAX_DEPTH,
    DEFAULT_COLLECT_MAX_FILE_KB,
    collect_walk_limits,
    scan_repo,
    scan_repo_ex,
)

ROOT = Path(__file__).resolve().parent.parent


def _cfg(**sections) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    for name, values in sections.items():
        cfg[name] = {k: str(v) for k, v in values.items()}
    return cfg


def _profile(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"), interpolation=None)
    cfg.read(path, encoding="utf-8")
    return cfg


def _ladder(root: Path, top: int) -> None:
    """One module at every depth from 0 to *top*: `m0.py`, `d0/m1.py`, `d0/d1/m2.py`, …"""
    for depth in range(top + 1):
        path = root.joinpath(*[f"d{i}" for i in range(depth)], f"m{depth}.py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"DEPTH = {depth}\n", encoding="utf-8")


def _depths(modules) -> list:
    return sorted(len(Path(m.path).parts) - 1 for m in modules)


# ── the depth of the shipped profiles ────────────────────────────────────────


@pytest.mark.parametrize("profile", sorted(p.name for p in ROOT.glob("agents*.ini")))
def test_every_shipped_profile_collects_at_least_as_deep_as_its_search_limit(profile):
    cfg = _profile(ROOT / profile)
    search = cfg.getint("search", "max_depth", fallback=None)
    if search is None:
        pytest.skip(f"{profile} names no [search] max_depth")
    assert collect_walk_limits(cfg)[0] >= search


def test_a_module_eight_folders_deep_is_collected_under_agents_ini(tmp_path):
    _ladder(tmp_path, 9)
    assert _depths(scan_repo(tmp_path, config=_profile(ROOT / "agents.ini"))) == list(range(9))


def test_a_search_limit_of_zero_is_no_limit_for_collect_either(tmp_path):
    """`RepoIngestor` reads 0 as "no limit"; before HARDEN-24 B the collect walk did too."""
    _ladder(tmp_path, 9)
    cfg = _cfg(search={"max_depth": 0, "max_file_kb": 0})
    assert collect_walk_limits(cfg) == (0, 0)
    assert _depths(scan_repo(tmp_path, config=cfg)) == list(range(10))


def test_a_wider_search_file_size_widens_collect_too():
    assert collect_walk_limits(_cfg(search={"max_file_kb": 8000}))[1] == 8000


def test_architect_wins_over_search_as_in_the_architects_walk():
    cfg = _cfg(architect={"max_depth": 10}, search={"max_depth": 7})
    assert collect_walk_limits(cfg)[0] == 10


def test_the_collect_key_still_wins_over_a_wider_search():
    cfg = _cfg(collect={"max_depth": 1, "max_file_kb": 1}, search={"max_depth": 8, "max_file_kb": 900})
    assert collect_walk_limits(cfg) == (1, 1)


def test_a_narrower_or_malformed_search_leaves_the_collect_defaults():
    for value in (1, 2, "banana", ""):
        cfg = _cfg(search={"max_depth": value, "max_file_kb": value})
        assert collect_walk_limits(cfg) == (DEFAULT_COLLECT_MAX_DEPTH, DEFAULT_COLLECT_MAX_FILE_KB)


# ── the skipped list survives the next collect ───────────────────────────────


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), capture_output=True, check=True)


def _repo_with_a_deep_module(root: Path) -> None:
    (root / "a.py").write_text("A = 1\n", encoding="utf-8")
    deep = root / "p" / "q" / "r"
    deep.mkdir(parents=True)
    (deep / "deep.py").write_text("DEEP = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text(".agent/\n.collect/\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")


def _skipped_on_the_manifest(root: Path) -> list:
    manifest = json.loads((root / ".collect" / cli_mod.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    return manifest["skipped_files"]


@pytest.fixture
def _no_seeds(monkeypatch):
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


def test_the_second_collect_keeps_the_skipped_list_and_logs_it(tmp_path, caplog, _no_seeds):
    _repo_with_a_deep_module(tmp_path)
    cfg = _cfg(collect={"max_depth": 2, "dir": ".collect"})
    cli_mod.action_collect(tmp_path, config=cfg)
    assert _skipped_on_the_manifest(tmp_path) == [["p/q/r/deep.py", "too deep"]]

    (tmp_path / "a.py").write_text("A = 2\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="tools.collect.cli"):
        result = cli_mod.action_collect(tmp_path, config=cfg)

    assert result.wrote is True
    assert _skipped_on_the_manifest(tmp_path) == [["p/q/r/deep.py", "too deep"]]
    assert "collect: skipping p/q/r/deep.py (too deep)" in [r.getMessage() for r in caplog.records]


def test_a_refresh_between_auto_tasks_keeps_the_skipped_list(tmp_path, _no_seeds):
    _repo_with_a_deep_module(tmp_path)
    ini = tmp_path / "agents.ini"
    ini.write_text(
        "[collect]\ndir = .collect\nuse_in_auto = true\nuse_in_doc = false\nstaleness = warn\n"
        "llm_summaries = false\nauto_refresh_between_tasks = true\nmax_depth = 2\n",
        encoding="utf-8",
    )
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(ini, encoding="utf-8")
    cli_mod.action_collect(tmp_path, config=cfg)
    (tmp_path / "a.py").write_text("A = 2\n\n\ndef added():\n    return 3\n", encoding="utf-8")

    bridge = make_collect_bridge(tmp_path, cfg, str(ini), task_mode="code")

    assert bridge is not None and bridge.usable is True
    assert _skipped_on_the_manifest(tmp_path) == [["p/q/r/deep.py", "too deep"]]


def test_an_explicit_refresh_with_modules_records_the_skipped_files_it_is_given(tmp_path, _no_seeds):
    _repo_with_a_deep_module(tmp_path)
    cfg = _cfg(collect={"max_depth": 2, "dir": ".collect"})
    cli_mod.action_collect(tmp_path, config=cfg)
    (tmp_path / "a.py").write_text("A = 3\n", encoding="utf-8")
    modules, skipped = scan_repo_ex(tmp_path, config=cfg)

    cli_mod.action_refresh(tmp_path, config=cfg, modules=modules, skipped_files=skipped)

    assert _skipped_on_the_manifest(tmp_path) == [["p/q/r/deep.py", "too deep"]]


# ── only a file the scan would read is called skipped ────────────────────────


def test_a_file_the_scan_never_reads_is_not_called_skipped(tmp_path):
    deep = tmp_path.joinpath(*"abc")
    deep.mkdir(parents=True)
    for name in ("deep.py", "README.md", "data.json", "notes.txt", "style.css"):
        (deep / name).write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "big.py").write_bytes(b"# " + b"x" * 4096 + b"\n")
    (tmp_path / "big.json").write_bytes(b"0" * 4096)

    _, skipped = scan_repo_ex(tmp_path, config=_cfg(collect={"max_depth": 2, "max_file_kb": 1}))

    assert skipped == [("a/b/c/deep.py", "too deep"), ("big.py", "too large")]
