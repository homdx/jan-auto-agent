"""tests/test_collect_loader_stale.py — COLLECT-21.

A changed file after a collect run makes the artifact stale; the loader's
behavior then depends entirely on `[collect] staleness`:

* `warn`    (default) -> data is returned with `status="stale"`.
* `refresh` -> incrementally/fully rebuilt before returning, `status="fresh"`.
* `ignore`  -> treated exactly like `absent`.
"""

from __future__ import annotations

import configparser
import subprocess
from pathlib import Path

import pytest

from tools.collect import cli as cli_mod
from tools.collect import loader as loader_mod


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "a.py").write_text("def a():\n    return 1\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def _config(staleness: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["collect"] = {"staleness": staleness}
    return cfg


def test_warn_returns_stale_data_with_flag(mini_repo):
    cli_mod.action_collect(mini_repo)
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 2\n")

    model = loader_mod.load(mini_repo, config=_config("warn"))
    assert model.status == loader_mod.STATUS_STALE
    assert model.is_stale is True
    assert model.available is True
    # stale data is still real data, not wiped
    assert model.module("pkg/a.py") is not None


def test_default_staleness_is_warn(mini_repo):
    cli_mod.action_collect(mini_repo)
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 2\n")

    model = loader_mod.load(mini_repo, config=None)
    assert model.status == loader_mod.STATUS_STALE


def test_refresh_rebuilds_incrementally(mini_repo):
    cli_mod.action_collect(mini_repo)
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 2\n")
    (mini_repo / "pkg" / "c.py").write_text("def c():\n    return 3\n")

    model = loader_mod.load(mini_repo, config=_config("refresh"))
    assert model.status == loader_mod.STATUS_FRESH
    assert model.module("pkg/c.py") is not None

    # the on-disk manifest is now fresh too
    collect_dir = cli_mod.resolve_collect_dir(mini_repo, None)
    from tools.collect import manifest as manifest_mod

    rebuilt = manifest_mod.read_manifest(collect_dir / cli_mod.MANIFEST_FILENAME)
    assert manifest_mod.is_fresh(rebuilt, mini_repo)


def test_ignore_treats_stale_as_absent(mini_repo):
    cli_mod.action_collect(mini_repo)
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 2\n")

    model = loader_mod.load(mini_repo, config=_config("ignore"))
    assert model.status == loader_mod.STATUS_ABSENT
    assert model.available is False


def test_unknown_staleness_value_falls_back_to_warn(mini_repo):
    cli_mod.action_collect(mini_repo)
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 2\n")

    model = loader_mod.load(mini_repo, config=_config("bogus-value"))
    assert model.status == loader_mod.STATUS_STALE
