"""V8 — `--no-llm` preserves existing summaries.

The bug: a build without Pass B wrote `summary: null` for every module it
re-scanned. `--rebuild --no-llm` nulled all of them; `--collect --no-llm`
after an edit nulled the changed module's; `--module --no-llm` nulled the
patched one. 469 summaries on the real tree, destroyed by a flag that is
documented as "a purely structural build" — nothing said it was also a
purely destructive one.

Now: summaries are carried forward. A module whose source changed keeps
its prose tagged `provenance: llm-stale`; an unchanged one keeps it as it
was. `--drop-summaries` is the explicit way to get the old behaviour, and
the result message says which happened. Pass C still runs over what was
carried, so `verification_report.json` is written and the `.collect/`
file count does not depend on whether Pass B ran.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.collect import cli as cli_mod
from tools.collect.cli import (
    ARTIFACT_FILENAME,
    VERIFICATION_REPORT_FILENAME,
    action_collect,
    action_module,
    action_rebuild,
    action_refresh,
)
from tools.collect.model import LLMSummary, Provenance, ProvenanceViolation


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def _modules(collect_dir: Path) -> dict:
    payload = json.loads((collect_dir / ARTIFACT_FILENAME).read_text(encoding="utf-8"))
    return {m["path"]: m for m in payload["modules"]}


def _summaries(collect_dir: Path) -> dict:
    return {p: m["summary"] for p, m in _modules(collect_dir).items()}


def _llm(purpose_prefix: str = "purpose of"):
    calls: list = []

    def _call(system: str, user: str) -> str:
        calls.append(user)
        # The module path is quoted in the prompt; keep the purpose per-module
        # so a carried-forward summary can be told from a re-derived one.
        for name in ("pkg/a.py", "pkg/b.py", "pkg/c.py"):
            if name in user:
                return json.dumps({"purpose": f"{purpose_prefix} {name}", "notes": ""})
        return json.dumps({"purpose": f"{purpose_prefix} ?", "notes": ""})

    _call.calls = calls
    return _call


def _edit_a(root: Path) -> None:
    (root / "pkg" / "a.py").write_text("def a():\n    return 42\n\ndef extra():\n    pass\n")


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "pkg" / "b.py").write_text("def b():\n    return 2\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


@pytest.fixture
def summarized(repo: Path) -> Path:
    """A normal build with Pass B: every module carries `provenance: llm`."""
    result = action_rebuild(repo, llm_call=_llm())
    before = _summaries(result.collect_dir)
    assert all(s is not None and s["provenance"] == "llm" for s in before.values()), before
    return repo


# ── the reproduced bug: --collect --no-llm after an edit ─────────────────────

def test_collect_no_llm_after_edit_keeps_the_changed_modules_summary(summarized):
    before = _summaries(summarized / ".collect")
    _edit_a(summarized)

    result = action_collect(summarized, llm_call=None)

    after = _summaries(result.collect_dir)
    assert after["pkg/a.py"] is not None, "the bug: the edited module lost its summary"
    assert after["pkg/a.py"]["purpose"] == before["pkg/a.py"]["purpose"]
    assert after["pkg/a.py"]["provenance"] == "llm-stale"
    # The unchanged module is carried verbatim, still `llm`.
    assert after["pkg/b.py"] == before["pkg/b.py"]
    assert "Pass B skipped; 1 summaries carried forward, 1 marked llm-stale" in result.message


def test_collect_no_llm_drop_summaries_is_the_explicit_destructive_path(summarized):
    _edit_a(summarized)

    result = action_collect(summarized, llm_call=None, drop_summaries=True)

    after = _summaries(result.collect_dir)
    assert after["pkg/a.py"] is None
    assert after["pkg/b.py"] is not None, "unchanged modules are still reused verbatim"
    assert "1 summaries dropped (--drop-summaries)" in result.message


# ── --rebuild --no-llm ───────────────────────────────────────────────────────

def test_rebuild_no_llm_carries_every_summary_and_marks_only_changed_files_stale(summarized):
    before = _summaries(summarized / ".collect")
    _edit_a(summarized)

    result = action_rebuild(summarized, llm_call=None)

    after = _summaries(result.collect_dir)
    assert after["pkg/a.py"]["purpose"] == before["pkg/a.py"]["purpose"]
    assert after["pkg/a.py"]["provenance"] == "llm-stale"
    assert after["pkg/b.py"]["purpose"] == before["pkg/b.py"]["purpose"]
    assert after["pkg/b.py"]["provenance"] == "llm"
    assert "re-scanned (Pass B skipped; 3 summaries carried forward, 1 marked llm-stale)" in result.message


def test_rebuild_no_llm_drop_summaries_nulls_all_and_says_so(summarized):
    result = action_rebuild(summarized, llm_call=None, drop_summaries=True)

    after = _summaries(result.collect_dir)
    assert all(s is None for s in after.values())
    assert "3 summaries dropped (--drop-summaries)" in result.message
    assert VERIFICATION_REPORT_FILENAME in result.message
    assert not (result.collect_dir / VERIFICATION_REPORT_FILENAME).exists()


def test_file_count_is_stable_across_build_modes(summarized):
    with_llm = set(action_rebuild(summarized, llm_call=_llm()).written_files)
    no_llm = set(action_rebuild(summarized, llm_call=None).written_files)
    assert with_llm == no_llm, "a --no-llm build with carried summaries writes the same 12 files"
    assert VERIFICATION_REPORT_FILENAME in no_llm
    report = json.loads((summarized / ".collect" / VERIFICATION_REPORT_FILENAME).read_text())
    assert isinstance(report, dict)


# ── --module --no-llm ────────────────────────────────────────────────────────

def test_module_no_llm_keeps_the_patched_modules_summary_as_stale(summarized):
    before = _summaries(summarized / ".collect")
    _edit_a(summarized)

    result = action_module(summarized, "pkg/a.py", llm_call=None)

    after = _summaries(result.collect_dir)
    assert after["pkg/a.py"]["purpose"] == before["pkg/a.py"]["purpose"]
    assert after["pkg/a.py"]["provenance"] == "llm-stale"
    assert "summary carried forward, marked llm-stale" in result.message


def test_module_no_llm_drop_summaries(summarized):
    _edit_a(summarized)
    result = action_module(summarized, "pkg/a.py", llm_call=None, drop_summaries=True)
    assert _summaries(result.collect_dir)["pkg/a.py"] is None


# ── a stale summary is re-derived by the next Pass B run ─────────────────────

def test_next_llm_refresh_re_summarizes_stale_modules_even_when_unchanged(summarized):
    _edit_a(summarized)
    action_collect(summarized, llm_call=None)
    assert _summaries(summarized / ".collect")["pkg/a.py"]["provenance"] == "llm-stale"

    # Nothing changed since the --no-llm refresh, but a.py's summary is
    # stale: a Pass B run owes it a real one — and only it (b.py is fresh).
    llm = _llm("fresh")
    result = action_refresh(summarized, llm_call=llm)

    after = _summaries(result.collect_dir)
    assert len(llm.calls) == 1 and "pkg/a.py" in llm.calls[0]
    assert after["pkg/a.py"] == {"purpose": "fresh pkg/a.py", "notes": "", "provenance": "llm"}
    assert after["pkg/b.py"]["purpose"] == "purpose of pkg/b.py"


# ── the provenance value is part of the model, not invented locally ──────────

def test_llm_stale_is_in_the_enum_and_field_provenance_reports_it(summarized):
    assert Provenance.LLM_STALE == "llm-stale"
    assert Provenance.LLM_STALE in Provenance.ALL
    stale = LLMSummary(purpose="p", notes="n").as_stale()
    assert stale.provenance == "llm-stale"
    assert stale.as_stale() is stale

    from tools.collect.model import ModuleRecord

    _edit_a(summarized)
    action_collect(summarized, llm_call=None)
    rec = ModuleRecord.from_dict(_modules(summarized / ".collect")["pkg/a.py"])
    prov = rec.field_provenance()
    assert prov["purpose"] == prov["notes"] == "llm-stale"
    assert prov["path"] == "static"


def test_structural_records_still_cannot_carry_llm_stale():
    """Never widen provenance: the new tag is for `LLMSummary` only."""
    from tools.collect.model import ConfigRead

    with pytest.raises(ProvenanceViolation):
        ConfigRead(section="s", key="k", provenance="llm-stale")


# ── first-ever --no-llm build: nothing to carry, nothing to explain ───────────

def test_first_build_no_llm_has_nothing_to_carry(repo):
    result = action_rebuild(repo, llm_call=None)
    assert all(s is None for s in _summaries(result.collect_dir).values())
    assert result.message.endswith(f"wrote {len(result.written_files)} file(s) in {result.collect_dir}")
    assert "(Pass B skipped)" in result.message
    assert "carried" not in result.message and "dropped" not in result.message
