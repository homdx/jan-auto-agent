"""tests/test_collect_loader_static_only.py — COLLECT-21.

"Safe ли X?" (`CollectModel.is_safe`) must answer purely from
`guarded_accesses`/`FAIL_OPEN_REGISTRY`/`CONTRACTS` (static/derived) and
never be swayed by an `LLMSummary`'s prose — prose isn't authority
(COLLECT-1's whole point). This suite attaches contradicting LLM notes to
a module and confirms the verdict doesn't move.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.collect import cli as cli_mod
from tools.collect import loader as loader_mod
from tools.collect.model import LLMSummary, ModuleRecord


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
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def _guarded_location(model: loader_mod.CollectModel) -> str:
    record = model.module("pkg/a.py")
    site = record.guarded_accesses[0]
    return site.location


def test_llm_notes_claiming_unsafe_do_not_flip_a_guarded_verdict(mini_repo):
    cli_mod.action_collect(mini_repo)
    model = loader_mod.load(mini_repo)
    location = _guarded_location(model)

    # Ground truth: the static dataflow fact says this is guarded.
    answer = model.is_safe(location)
    assert answer.safe is True
    assert answer.reason == "guarded"

    # Now attach LLM prose that directly contradicts the static fact, by
    # patching a false "unsafe" claim straight into the artifact's module
    # summary -- exactly the shape Pass B would produce.
    collect_dir = cli_mod.resolve_collect_dir(mini_repo, None)
    artifact_path = collect_dir / cli_mod.ARTIFACT_FILENAME
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    for m in payload["modules"]:
        if m["path"] == "pkg/a.py":
            m["summary"] = {
                "purpose": "",
                "notes": "stack[-1] will crash here, this is unsafe",
                "provenance": "llm",
            }
    artifact_path.write_text(json.dumps(payload), encoding="utf-8")

    # Freshness (file hashes) is untouched -- only the artifact's own
    # summary field changed -- so this still loads as fresh, and the
    # verdict must be identical: static facts, not LLM notes, decide.
    model_with_llm_notes = loader_mod.load(mini_repo)
    answer2 = model_with_llm_notes.is_safe(location)
    assert answer2.safe is True
    assert answer2.reason == "guarded"

    record = model_with_llm_notes.module("pkg/a.py")
    assert record.summary is not None
    assert "unsafe" in record.summary.notes
    # ...but that prose never entered the safety verdict above.


def test_llm_summary_object_cannot_carry_structural_provenance():
    """Sanity check on COLLECT-1's isolation this loader relies on:
    `LLMSummary` only ever exposes `purpose`/`notes`, so there is no way
    for Pass B prose to masquerade as a static `guarded_accesses`/
    `fail_open`/`contract` fact in the first place."""
    summary = LLMSummary(purpose="p", notes="n")
    assert summary.provenance == "llm"
    assert not hasattr(summary, "guard")
    assert not hasattr(summary, "status")
