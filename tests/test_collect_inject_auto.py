"""tests/test_collect_inject_auto.py — COLLECT-23.

`AutoController.collect_context_for(target_file)` is the opt-in hook a
future architect/coder call site uses to fold a `collect` module record
into a file's context. Per the ticket's AC:

* `use_in_auto=true` + a fresh artifact -> the target file's record shows
  up in the returned block.
* `use_in_auto=false` (default) or an absent artifact -> `""`, i.e.
  "context as today" — a byte-for-byte regression check, since disabled is
  the out-of-the-box state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.auto.context_assembler import build_collect_context_block
from tools.auto.controller import AutoController
from tools.collect import cli as cli_mod
from tools.collect import loader as loader_mod


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


def _write_agents_ini(root: Path, *, use_in_auto: bool) -> Path:
    ini = root / "agents.ini"
    ini.write_text(
        "[collect]\n"
        "dir = .collect\n"
        f"use_in_auto = {'true' if use_in_auto else 'false'}\n"
        "use_in_doc = false\n"
        "staleness = warn\n"
    )
    return ini


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text(
        "def a():\n"
        "    return 1\n"
        "\n"
        "def b():\n"
        "    return 2\n"
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


# ── unit level: build_collect_context_block ─────────────────────────────


def test_build_collect_context_block_absent_model_returns_empty():
    assert build_collect_context_block(None, "pkg/a.py") == ""

    absent_model = loader_mod.CollectModel(status=loader_mod.STATUS_ABSENT)
    assert build_collect_context_block(absent_model, "pkg/a.py") == ""


def test_build_collect_context_block_unknown_file_returns_empty(mini_repo):
    cli_mod.action_collect(mini_repo)
    model = loader_mod.load(mini_repo)
    assert build_collect_context_block(model, "pkg/does_not_exist.py") == ""


def test_build_collect_context_block_includes_module_record(mini_repo):
    cli_mod.action_collect(mini_repo)
    model = loader_mod.load(mini_repo)

    block = build_collect_context_block(model, "pkg/a.py")
    assert "pkg/a.py" in block
    assert "a" in block and "b" in block


# ── controller-level: use_in_auto opt-in + regression when off ─────────


def test_use_in_auto_false_is_a_no_op_regression(mini_repo):
    cli_mod.action_collect(mini_repo)  # artifact exists and is fresh
    config_path = _write_agents_ini(mini_repo, use_in_auto=False)

    controller = AutoController(goal="test goal", base_dir=mini_repo, config_path=str(config_path))
    assert controller.collect_context_for("pkg/a.py") == ""


def test_use_in_auto_true_injects_target_file_record(mini_repo):
    cli_mod.action_collect(mini_repo)
    config_path = _write_agents_ini(mini_repo, use_in_auto=True)

    controller = AutoController(goal="test goal", base_dir=mini_repo, config_path=str(config_path))
    block = controller.collect_context_for("pkg/a.py")
    assert "pkg/a.py" in block


def test_use_in_auto_true_but_absent_artifact_is_still_a_no_op(mini_repo):
    # collect never ran -- no .collect/ at all -- even with the flag on.
    config_path = _write_agents_ini(mini_repo, use_in_auto=True)

    controller = AutoController(goal="test goal", base_dir=mini_repo, config_path=str(config_path))
    assert controller.collect_context_for("pkg/a.py") == ""


def test_default_agents_ini_has_flag_off(mini_repo):
    # No [collect] section at all -- today's behaviour, unaffected.
    cli_mod.action_collect(mini_repo)
    controller = AutoController(goal="test goal", base_dir=mini_repo, config_path="does-not-exist.ini")
    assert controller.collect_context_for("pkg/a.py") == ""
