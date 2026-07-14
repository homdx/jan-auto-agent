"""tests/test_collect_cli.py — COLLECT-19.

* `--check` never writes anything, anywhere.
* `--collect` creates the full artifact set in `.collect/` when missing;
  is a no-op (no write) once fresh.
* `--module <path>` updates only that module's record + patches the
  manifest, rather than doing a full rescan.
* `collect` can never modify a file outside `[collect] dir` — the whole
  source tree's hash is identical before and after every action.
"""

from __future__ import annotations

import configparser
import hashlib
import subprocess
from pathlib import Path

import pytest

from tools.collect import cli as cli_mod
from tools.collect.cli import (
    ARTIFACT_FILENAME,
    MANIFEST_FILENAME,
    CollectCliError,
    action_check,
    action_collect,
    action_module,
    action_refresh,
    parse_collect_args,
    resolve_collect_dir,
    run,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    """COLLECT-10/COLLECT-15 seed data cites symbols from the *real*
    jan-auto-agent repo (build_chat_request, _parse_verdict_soft, ...),
    which don't exist in these synthetic mini repos. This module tests
    orchestration (COLLECT-19), not seed content (COLLECT-10/15), so the
    seeds are neutralized to empty for every test here.
    """
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "pkg" / "b.py").write_text("def b():\n    return 2\n")
    _init_repo(tmp_path)
    return tmp_path


def _tree_hash(root: Path) -> str:
    """Hash of every tracked source file's content + relative path, used
    to prove `collect` never touches anything outside `[collect] dir`."""
    h = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if ".collect" in path.parts or ".git" in path.parts:
            continue
        h.update(str(path.relative_to(root)).encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()


# ── --check: never writes ───────────────────────────────────────────────────


def test_check_writes_nothing_when_never_run(mini_repo):
    before = _tree_hash(mini_repo)
    result = action_check(mini_repo)
    assert result.action == "check"
    assert result.wrote is False
    assert result.fresh is False
    assert not (mini_repo / ".collect").exists()
    assert _tree_hash(mini_repo) == before


def test_check_writes_nothing_when_stale_or_fresh(mini_repo):
    action_collect(mini_repo)
    collect_dir = resolve_collect_dir(mini_repo, None)
    manifest_before = (collect_dir / MANIFEST_FILENAME).read_bytes()
    src_before = _tree_hash(mini_repo)

    result = action_check(mini_repo)
    assert result.wrote is False
    assert result.fresh is True
    assert (collect_dir / MANIFEST_FILENAME).read_bytes() == manifest_before
    assert _tree_hash(mini_repo) == src_before

    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 2\n")
    src_before = _tree_hash(mini_repo)
    result = action_check(mini_repo)
    assert result.wrote is False
    assert result.fresh is False
    assert (collect_dir / MANIFEST_FILENAME).read_bytes() == manifest_before
    assert _tree_hash(mini_repo) == src_before


# ── --collect: builds if missing/stale, no-op once fresh ───────────────────


def test_collect_builds_full_artifact_set(mini_repo):
    before = _tree_hash(mini_repo)
    result = action_collect(mini_repo)

    assert result.wrote is True
    collect_dir = result.collect_dir
    assert collect_dir == mini_repo / ".collect"
    assert (collect_dir / ARTIFACT_FILENAME).exists()
    assert (collect_dir / MANIFEST_FILENAME).exists()
    for page in (
        "ARCHITECTURE.md", "MODULE_MAP.md", "CONTRACTS.md",
        "FAIL_OPEN_REGISTRY.md", "GATES.md", "TEST_MAP.md",
        "RISK_INDEX.md", "CONFIG_MAP.md", "GLOSSARY.md",
    ):
        assert (collect_dir / page).exists(), f"missing {page}"
        assert page in result.written_files

    # source tree untouched
    assert _tree_hash(mini_repo) == before


def test_collect_is_noop_once_fresh(mini_repo):
    first = action_collect(mini_repo)
    assert first.wrote is True
    artifact_path = first.collect_dir / ARTIFACT_FILENAME
    mtime_before = artifact_path.stat().st_mtime_ns

    second = action_collect(mini_repo)
    assert second.wrote is False
    assert second.fresh is True
    assert artifact_path.stat().st_mtime_ns == mtime_before


def test_collect_rebuilds_when_stale(mini_repo):
    action_collect(mini_repo)
    (mini_repo / "pkg" / "b.py").write_text("def b():\n    return 99\n")

    result = action_collect(mini_repo)
    assert result.wrote is True
    assert result.fresh is True


# ── --refresh: unconditional rebuild ────────────────────────────────────────


def test_refresh_rebuilds_even_when_fresh(mini_repo):
    first = action_collect(mini_repo)
    assert first.wrote is True

    result = action_refresh(mini_repo)
    assert result.wrote is True
    assert result.action == "refresh"


# ── --module: incremental ───────────────────────────────────────────────────


def test_module_patches_single_record_and_manifest(mini_repo):
    action_collect(mini_repo)

    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 42\n\ndef extra():\n    pass\n")
    before = _tree_hash(mini_repo)
    result = action_module(mini_repo, "pkg/a.py")

    assert result.action == "module"
    assert result.wrote is True
    assert _tree_hash(mini_repo) == before  # module read, never written to

    import json
    artifact = json.loads((result.collect_dir / ARTIFACT_FILENAME).read_text(encoding="utf-8"))
    by_path = {m["path"]: m for m in artifact["modules"]}
    assert "pkg/a.py" in by_path
    qualnames = {s["qualname"] for s in by_path["pkg/a.py"]["public_symbols"]}
    assert "pkg/a.py:extra" in qualnames
    # untouched module b.py still present, not re-parsed away
    assert "pkg/b.py" in by_path

    # manifest patched — now fresh again
    check = action_check(mini_repo)
    assert check.fresh is True


def test_module_falls_back_to_full_refresh_without_existing_artifact(mini_repo):
    result = action_module(mini_repo, "pkg/a.py")
    assert result.action == "module"
    assert result.wrote is True
    assert "no existing artifact" in result.message
    assert (result.collect_dir / ARTIFACT_FILENAME).exists()


def test_module_rejects_nonexistent_path(mini_repo):
    action_collect(mini_repo)
    with pytest.raises(CollectCliError):
        action_module(mini_repo, "pkg/does_not_exist.py")


# ── [collect] dir config plumbing ───────────────────────────────────────────


def test_custom_collect_dir_from_config(mini_repo):
    config = configparser.ConfigParser()
    config.read_dict({"collect": {"dir": "artifacts/model"}})
    result = action_collect(mini_repo, config=config)
    assert result.collect_dir == mini_repo / "artifacts" / "model"
    assert (mini_repo / "artifacts" / "model" / ARTIFACT_FILENAME).exists()
    assert not (mini_repo / ".collect").exists()


# ── run() dispatch + parse_collect_args ─────────────────────────────────────


def test_run_dispatches_all_actions(mini_repo):
    assert run(mini_repo, "check").action == "check"
    assert run(mini_repo, "collect").action == "collect"
    assert run(mini_repo, "refresh").action == "refresh"
    assert run(mini_repo, "module", module_path="pkg/a.py").action == "module"


def test_run_rejects_unknown_action(mini_repo):
    with pytest.raises(CollectCliError):
        run(mini_repo, "bogus")


def test_run_module_requires_module_path(mini_repo):
    with pytest.raises(CollectCliError):
        run(mini_repo, "module")


def test_parse_collect_args_defaults_to_collect():
    assert parse_collect_args([]) == {"action": "collect", "module_path": None}


def test_parse_collect_args_check():
    assert parse_collect_args(["--check"]) == {"action": "check", "module_path": None}


def test_parse_collect_args_refresh():
    assert parse_collect_args(["--refresh"]) == {"action": "refresh", "module_path": None}


def test_parse_collect_args_module():
    assert parse_collect_args(["--module", "pkg/a.py"]) == {
        "action": "module", "module_path": "pkg/a.py",
    }


def test_parse_collect_args_module_missing_path_raises():
    with pytest.raises(CollectCliError):
        parse_collect_args(["--module"])
