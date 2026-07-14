"""tests/test_collect_incremental.py — COLLECT-24: incremental refresh +
staleness-mode polish.

* Changing one file re-collects only that file's record — every other
  module's `ModuleRecord`, including its LLM `summary`, is reused verbatim
  from the previous artifact rather than re-derived.
* An unchanged tree's `--refresh` never calls the LLM at all.
* The manifest is patched/rebuilt so a follow-up `--check` reports fresh
  again.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.collect import cli as cli_mod
from tools.collect.cli import ARTIFACT_FILENAME, action_check, action_collect, action_refresh


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
    """Same neutralization as `test_collect_cli.py`: COLLECT-10/15 seed
    data cites real jan-auto-agent symbols that don't exist in these
    synthetic mini repos — this file tests refresh orchestration
    (COLLECT-24), not seed content."""
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


def _artifact_modules(collect_dir: Path) -> dict:
    payload = json.loads((collect_dir / ARTIFACT_FILENAME).read_text(encoding="utf-8"))
    return {m["path"]: m for m in payload["modules"]}


def _counting_llm_call(responses_by_snippet=None):
    """A fake `LlmCall` that records every `(system, user)` pair it was
    called with, so tests can assert exactly which modules — if any — hit
    Pass B on a given `--refresh`."""
    calls = []

    def _call(system: str, user: str) -> str:
        calls.append((system, user))
        return json.dumps({"purpose": "stub purpose", "notes": ""})

    _call.calls = calls
    return _call


# ── unchanged tree: zero LLM calls ──────────────────────────────────────────


def test_refresh_of_unchanged_tree_makes_no_llm_calls(mini_repo):
    llm = _counting_llm_call()
    action_collect(mini_repo, llm_call=llm)
    llm.calls.clear()

    result = action_refresh(mini_repo, llm_call=llm)

    assert result.wrote is True
    assert llm.calls == []


def test_refresh_of_unchanged_tree_preserves_purpose(mini_repo):
    llm = _counting_llm_call()
    first = action_collect(mini_repo, llm_call=llm)
    before = _artifact_modules(first.collect_dir)

    result = action_refresh(mini_repo, llm_call=llm)
    after = _artifact_modules(result.collect_dir)

    for path in ("pkg/a.py", "pkg/b.py"):
        assert after[path]["summary"] == before[path]["summary"]


# ── one changed file: only that record is re-collected/re-summarized ───────


def test_refresh_only_calls_llm_for_the_changed_module(mini_repo):
    llm = _counting_llm_call()
    action_collect(mini_repo, llm_call=llm)
    llm.calls.clear()

    (mini_repo / "pkg" / "a.py").write_text(
        "def a():\n    return 42\n\ndef extra():\n    pass\n"
    )

    result = action_refresh(mini_repo, llm_call=llm)

    assert result.wrote is True
    # exactly one module's source (a.py) should have been sent to the LLM
    assert len(llm.calls) == 1
    _system, user_msg = llm.calls[0]
    assert "pkg/a.py" in user_msg


def test_refresh_rescans_changed_module_structural_facts(mini_repo):
    action_collect(mini_repo)
    (mini_repo / "pkg" / "a.py").write_text(
        "def a():\n    return 42\n\ndef extra():\n    pass\n"
    )

    result = action_refresh(mini_repo)
    modules = _artifact_modules(result.collect_dir)

    qualnames = {s["qualname"] for s in modules["pkg/a.py"]["public_symbols"]}
    assert "pkg/a.py:extra" in qualnames
    # untouched module still present
    assert "pkg/b.py" in modules


def test_refresh_leaves_unchanged_module_summary_untouched(mini_repo):
    llm = _counting_llm_call()
    first = action_collect(mini_repo, llm_call=llm)
    before = _artifact_modules(first.collect_dir)

    (mini_repo / "pkg" / "a.py").write_text(
        "def a():\n    return 42\n\ndef extra():\n    pass\n"
    )
    result = action_refresh(mini_repo, llm_call=llm)
    after = _artifact_modules(result.collect_dir)

    # b.py never changed -> its summary is byte-identical to before
    assert after["pkg/b.py"]["summary"] == before["pkg/b.py"]["summary"]
    # a.py changed -> it got a fresh (stub) summary
    assert after["pkg/a.py"]["summary"]["purpose"] == "stub purpose"


# ── manifest is updated so a follow-up --check reports fresh again ─────────


def test_refresh_updates_manifest_so_check_is_fresh_again(mini_repo):
    action_collect(mini_repo)
    (mini_repo / "pkg" / "b.py").write_text("def b():\n    return 99\n")

    stale = action_check(mini_repo)
    assert stale.fresh is False

    action_refresh(mini_repo)

    fresh = action_check(mini_repo)
    assert fresh.fresh is True


# ── added / removed files ───────────────────────────────────────────────────


def test_refresh_picks_up_added_file(mini_repo):
    action_collect(mini_repo)
    (mini_repo / "pkg" / "c.py").write_text("def c():\n    return 3\n")

    result = action_refresh(mini_repo)
    modules = _artifact_modules(result.collect_dir)
    assert "pkg/c.py" in modules


def test_refresh_drops_removed_file(mini_repo):
    action_collect(mini_repo)
    (mini_repo / "pkg" / "b.py").unlink()

    result = action_refresh(mini_repo)
    modules = _artifact_modules(result.collect_dir)
    assert "pkg/b.py" not in modules


# ── no prior artifact: falls back to a full build ───────────────────────────


def test_refresh_without_prior_artifact_falls_back_to_full_build(mini_repo):
    result = action_refresh(mini_repo)
    assert result.wrote is True
    assert "no prior artifact" in result.message
    assert (result.collect_dir / ARTIFACT_FILENAME).exists()
