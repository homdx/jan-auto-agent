"""tests/test_collect_loader_query.py — COLLECT-21.

The fresh-model query surface: module record lookup, contracts-by-edge,
fail-open-by-module, and the "broken JSON -> treated as absent, not an
error" contract.
"""

from __future__ import annotations

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
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text(
        "def guarded():\n"
        "    stack = []\n"
        "    if not stack:\n"
        "        return None\n"
        "    return stack[-1]\n"
        "\n"
        "def unguarded():\n"
        "    items = []\n"
        "    return items[-1]\n"
        "\n"
        "def swallow():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception:\n"
        "        pass\n"
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def test_module_lookup(mini_repo):
    cli_mod.action_collect(mini_repo)
    model = loader_mod.load(mini_repo)
    assert model.status == loader_mod.STATUS_FRESH

    record = model.module("pkg/a.py")
    assert record is not None
    assert record.path == "pkg/a.py"
    assert model.module("pkg/does_not_exist.py") is None


def test_fail_open_for_module(mini_repo):
    cli_mod.action_collect(mini_repo)
    model = loader_mod.load(mini_repo)

    entries = model.fail_open_for("pkg/a.py")
    assert len(entries) == 1
    assert entries[0].location.startswith("pkg/a.py:")
    assert model.fail_open_for("pkg/does_not_exist.py") == []


def test_contracts_for_returns_empty_without_seeds(mini_repo):
    cli_mod.action_collect(mini_repo)
    model = loader_mod.load(mini_repo)
    # seeds are neutralized in this suite (COLLECT-10 content isn't under test)
    assert model.contracts_for("pkg/a.py") == []


def test_zero_coverage_reports_untested_modules(mini_repo):
    cli_mod.action_collect(mini_repo)
    model = loader_mod.load(mini_repo)
    assert "pkg/a.py" in model.zero_coverage()


def test_corrupt_artifact_treated_as_absent(mini_repo):
    cli_mod.action_collect(mini_repo)
    collect_dir = cli_mod.resolve_collect_dir(mini_repo, None)
    (collect_dir / cli_mod.ARTIFACT_FILENAME).write_text("{not valid json", encoding="utf-8")

    model = loader_mod.load(mini_repo)
    assert model.status == loader_mod.STATUS_ABSENT
    assert model.module("pkg/a.py") is None


def test_partial_artifact_missing_keys_treated_as_absent(mini_repo):
    cli_mod.action_collect(mini_repo)
    collect_dir = cli_mod.resolve_collect_dir(mini_repo, None)
    (collect_dir / cli_mod.ARTIFACT_FILENAME).write_text(
        '{"modules": [{"path": "pkg/a.py", "public_symbols": "not-a-list-of-dicts"}]}',
        encoding="utf-8",
    )

    model = loader_mod.load(mini_repo)
    assert model.status == loader_mod.STATUS_ABSENT
