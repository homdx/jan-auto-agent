"""tests/test_gate1_stage_a0_unindexed.py — L1: Gate-1 Stage A0.

Stage B's presence check spent 74 of 834 live calls judging claims against
files that are not code (.md/.ini/.yaml/.json/.sh) — 23 of those 74 were
CONFIRMED, so a doc that contradicts the code is a real finding and the
stage must never reject non-code outright. What it may do is skip the LLM
presence check for a location the collect model does not index, and reject
that candidate at Stage A0 instead.

The test is membership in the collect model, not an extension allowlist:
the artifact indexes .py AND .java, so a .java candidate must survive, and
a language the collector learns tomorrow needs no allowlist change here.

This file pins the ticket's acceptance criteria one for one.
"""

from __future__ import annotations

import configparser
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.collect_bridge import CollectBridge
from tools.auto.gate1_filter import Gate1Filter, filter_candidates


# ── shared harness ───────────────────────────────────────────────────────


def _symbol(qualname: str, signature: str = ""):
    return SimpleNamespace(qualname=qualname, signature=signature)


def _module(path: str, symbols=()):
    return SimpleNamespace(path=path, public_symbols=list(symbols))


def _fresh_model(modules=()):
    return SimpleNamespace(status="fresh", modules=list(modules), test_map={})


def _base_cfg(**gate1: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["api"] = {"active": "local", "verify_ssl": "true"}
    cfg["api_local"] = {
        "base_url": "http://localhost:1337/v1", "api_key": "k",
        "model": "m", "api_format": "openai",
    }
    # skip_llm is OFF: Stage B must be reachable, and the tests below prove
    # a rejected candidate never reaches it by counting presence calls.
    cfg["gate1"] = {"skip_llm": "false", **gate1}
    cfg["loop"] = {"timeout_seconds": "60"}
    return cfg


def _filt(cfg=None, collect_bridge=None, task_mode="code") -> Gate1Filter:
    cfg = cfg or _base_cfg()
    return Gate1Filter(
        config=cfg, base_url="http://x", api_key="k", model="m",
        api_format="openai", task_mode=task_mode, collect_bridge=collect_bridge,
    )


def _recording_filt(cfg=None, collect_bridge=None, task_mode="code"):
    """A filter whose Stage B records every presence call instead of
    talking to a provider. Returns (filt, calls)."""
    filt = _filt(cfg, collect_bridge=collect_bridge, task_mode=task_mode)
    calls: list[str] = []

    def _record(candidate, code_block, module_docstring="", base_dir=None):
        calls.append(candidate.cited_location.file)
        return True, "confirmed by stub", "confirmed"

    filt._check_presence = _record
    return filt, calls


@pytest.fixture
def code_repo(tmp_path: Path) -> Path:
    """A tree with one indexed .py, one indexed .java, and the config file
    the architect is handed verbatim — real on disk, so Stage A passes for
    all three."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "pkg" / "Bot.java").write_text(
        "public class Bot {\n    int run() { return 1; }\n}\n"
    )
    (tmp_path / "agents.ini").write_text(
        "[api]\nactive = local\n\n[gate1]\nskip_llm = false\n"
    )
    return tmp_path


def _candidate(file: str, *, symbol=None, line=None, new_file=False) -> CandidateTask:
    return CandidateTask(
        title=f"task on {file}", instruction="a claimed problem",
        target_files=[file], acceptance_check="true",
        cited_location=CitedLocation(
            file=file, symbol=symbol,
            line_start=line, line_end=line, new_file=new_file,
        ),
    )


# ── 1. an unindexed location is rejected at Stage A0 with no LLM call ─────


def test_ini_candidate_rejected_with_no_presence_call(code_repo):
    bridge = CollectBridge(_fresh_model([
        _module("pkg/a.py", [_symbol("pkg.a.a")]),
        _module("pkg/Bot.java"),
    ]))
    filt, calls = _recording_filt(collect_bridge=bridge)

    accepted, rejected = filt.filter([_candidate("agents.ini", line=1)], code_repo)

    assert accepted == []
    assert len(rejected) == 1
    r = rejected[0]
    assert r.stage == "existence"
    assert r.reason.startswith("location is not an indexed source file")
    assert "agents.ini" in r.reason
    # The whole point of the stage: the presence check never ran.
    assert calls == [], "an unindexed location must cost no LLM call"


def test_stage_a0_counts_in_the_existence_bucket(code_repo):
    """Counted: the removal shows up in the run summary's existing Stage-A
    bucket via stage="existence", and the distinct reason prefix keeps the
    two separable for a log grep."""
    bridge = CollectBridge(_fresh_model([_module("pkg/a.py", [_symbol("pkg.a.a")])]))
    filt, _ = _recording_filt(collect_bridge=bridge)
    counters: dict = {}

    accepted, rejected = filt.filter(
        [_candidate("agents.ini", line=1), _candidate("pkg/missing.py", symbol="nope")],
        code_repo, counters=counters,
    )
    assert accepted == []
    # One Stage-A0 removal (unindexed) + one genuine Stage-A failure.
    assert counters["existence"] == 2
    reasons = {r.candidate.cited_location.file: r.reason for r in rejected}
    # The two stay separable: only the A0 removal's reason carries the
    # stage's own prefix, so a log grep can tell them apart.
    assert reasons["agents.ini"].startswith("location is not an indexed source file")
    assert reasons["pkg/missing.py"].startswith("cited file not found")


def test_a_py_candidate_still_gets_its_presence_call(code_repo):
    """The regression to watch: an indexed .py must sail through A0 and
    reach Stage B exactly as before."""
    bridge = CollectBridge(_fresh_model([_module("pkg/a.py", [_symbol("pkg.a.a")])]))
    filt, calls = _recording_filt(collect_bridge=bridge)

    accepted, rejected = filt.filter([_candidate("pkg/a.py", symbol="a")], code_repo)

    assert len(accepted) == 1 and calls == ["pkg/a.py"]
    assert rejected == []


# ── 2. docs mode keeps today's behaviour — the LLM call is made ───────────


def test_docs_mode_reaches_stage_b(code_repo):
    """testtext7's goal is about docstrings and comments; a claim about a
    .md or .ini file is not automatically wrong there. The stage is code
    mode only."""
    bridge = CollectBridge(_fresh_model([_module("pkg/a.py", [_symbol("pkg.a.a")])]))
    filt, calls = _recording_filt(
        collect_bridge=bridge, task_mode="docs",
    )

    accepted, rejected = filt.filter([_candidate("agents.ini", line=1)], code_repo)

    assert len(accepted) == 1
    assert calls == ["agents.ini"], "docs mode must still check the claim"
    assert rejected == []


# ── 3. no usable model → byte-identical behaviour, every candidate checked ─


def test_no_bridge_checks_every_candidate(code_repo):
    filt, calls = _recording_filt(collect_bridge=None)

    accepted, rejected = filt.filter([_candidate("agents.ini", line=1)], code_repo)

    assert len(accepted) == 1 and calls == ["agents.ini"]
    assert rejected == []


@pytest.mark.parametrize("status", ["stale", "absent"])
def test_unusable_model_checks_every_candidate(code_repo, status):
    bridge = CollectBridge(SimpleNamespace(status=status, modules=[], test_map={}))
    filt, calls = _recording_filt(collect_bridge=bridge)

    accepted, rejected = filt.filter([_candidate("agents.ini", line=1)], code_repo)

    assert len(accepted) == 1 and calls == ["agents.ini"]
    assert rejected == []


# ── 4. the config key disables the stage ──────────────────────────────────


def test_skip_llm_for_unindexed_false_disables_the_stage(code_repo):
    bridge = CollectBridge(_fresh_model([_module("pkg/a.py", [_symbol("pkg.a.a")])]))
    filt, calls = _recording_filt(
        cfg=_base_cfg(skip_llm_for_unindexed="false"), collect_bridge=bridge,
    )
    assert filt._skip_llm_for_unindexed is False

    accepted, rejected = filt.filter([_candidate("agents.ini", line=1)], code_repo)

    assert len(accepted) == 1 and calls == ["agents.ini"]
    assert rejected == []


def test_malformed_key_falls_back_to_the_default(code_repo):
    """House rule 6: a broken config value degrades to the default and never
    raises into a run."""
    filt = _filt(cfg=_base_cfg(skip_llm_for_unindexed="not-a-bool"))
    assert filt._skip_llm_for_unindexed is True


def test_key_absent_defaults_to_true(code_repo):
    """The default config has no key at all; the stage is on."""
    cfg = _base_cfg()
    assert "skip_llm_for_unindexed" not in cfg["gate1"]
    filt = _filt(cfg=cfg)
    assert filt._skip_llm_for_unindexed is True


# ── 5. membership, not an extension allowlist ─────────────────────────────


def test_java_candidate_is_not_rejected(code_repo):
    """The artifact indexes Java; the right question is 'does the model know
    this path', so a .java citation survives A0 and gets its check."""
    bridge = CollectBridge(_fresh_model([
        _module("pkg/a.py", [_symbol("pkg.a.a")]),
        _module("pkg/Bot.java"),
    ]))
    filt, calls = _recording_filt(collect_bridge=bridge)

    accepted, rejected = filt.filter([_candidate("pkg/Bot.java", line=2)], code_repo)

    assert len(accepted) == 1 and calls == ["pkg/Bot.java"]
    assert rejected == []


def test_a_py_file_the_model_does_not_know_is_rejected(code_repo):
    """The honest consequence of a membership test: a .py that exists on
    disk but is not in the artifact (a scan exclusion, a file the collector
    was never pointed at) is still unindexed, and its claim still costs no
    call. The artifact is the index, not the filesystem."""
    bridge = CollectBridge(_fresh_model([_module("pkg/a.py", [_symbol("pkg.a.a")])]))
    filt, calls = _recording_filt(collect_bridge=bridge)

    # pkg/Bot.java exists on disk (Stage A passes) but is not indexed here.
    accepted, rejected = filt.filter([_candidate("pkg/Bot.java", line=2)], code_repo)

    assert accepted == []
    assert rejected and rejected[0].reason.startswith("location is not an indexed source file")
    assert calls == []


# ── 6. the V9 wrinkle and the fail-open contract ─────────────────────────


def test_a_path_dirtied_mid_run_is_treated_as_indexed(code_repo):
    """A path a task of this run already edited is indexed, just stale —
    `module_symbols` withholds it, and reading that as 'unindexed' would
    start rejecting .py files mid-run."""
    bridge = CollectBridge(_fresh_model([_module("pkg/a.py", [_symbol("pkg.a.a")])]))
    bridge.invalidate(["pkg/a.py"])
    assert "pkg/a.py" in bridge.dirty_paths
    filt, calls = _recording_filt(collect_bridge=bridge)

    accepted, rejected = filt.filter([_candidate("pkg/a.py", symbol="a")], code_repo)

    assert len(accepted) == 1 and calls == ["pkg/a.py"]
    assert rejected == []


def test_new_file_candidate_is_exempt(code_repo):
    """A new_file citation is a path that does not exist yet by design —
    nothing indexes it, and the stage would reject every legitimate
    new-file task."""
    bridge = CollectBridge(_fresh_model([_module("pkg/a.py", [_symbol("pkg.a.a")])]))
    filt, calls = _recording_filt(collect_bridge=bridge)

    c = _candidate("pkg/new_module.py", new_file=True)
    accepted, rejected = filt.filter([c], code_repo)

    # Stage B exempts new_file too, so no presence call is expected — but
    # the candidate must be ACCEPTED, not rejected at A0.
    assert len(accepted) == 1 and rejected == []
    assert calls == []


def test_a_raising_bridge_never_rejects_a_candidate(code_repo):
    class _BrokenBridge:
        usable = True
        dirty_paths = frozenset()

        def module_symbols(self, path):
            raise RuntimeError("bridge is down")

    filt, calls = _recording_filt(collect_bridge=_BrokenBridge())

    accepted, rejected = filt.filter([_candidate("agents.ini", line=1)], code_repo)

    assert len(accepted) == 1 and calls == ["agents.ini"]
    assert rejected == [], "a broken bridge must fail open, not drop candidates"


# ── 7. wiring: filter_candidates() threads the bridge the stage needs ─────


def test_filter_candidates_threads_collect_bridge(code_repo, monkeypatch):
    """pipeline.py's entry point must still deliver the bridge — the stage
    is a no-op without it (acceptance: no model → today's behaviour)."""
    bridge = CollectBridge(_fresh_model([_module("pkg/a.py", [_symbol("pkg.a.a")])]))
    cfg = _base_cfg()

    captured = {}
    real_init = Gate1Filter.__init__

    def _spy(self, *a, **kw):
        captured["collect_bridge"] = kw.get("collect_bridge")
        return real_init(self, *a, **kw)

    monkeypatch.setattr(Gate1Filter, "__init__", _spy)

    accepted, rejected = filter_candidates(
        [_candidate("agents.ini", line=1)], code_repo, cfg,
        collect_bridge=bridge, counters={},
    )
    assert captured["collect_bridge"] is bridge
    # The stage ran: the unindexed candidate is gone before Stage B.
    assert accepted == []
    assert rejected and rejected[0].stage == "existence"
