"""tests/test_auto_parsing_o_review.py — parsing of model and operator text, after review batch o."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.auto.backlog_prioritiser import BacklogPrioritiser as BP  # noqa: E402
from tools.auto.plan_validator import _without_section  # noqa: E402
from tools.collect import verifier  # noqa: E402


@pytest.mark.parametrize("text, dep", [
    ("Make the retry loop run (when enabled) only once.", False),
    ("Write the results to the run.log file.", False),
    ("Call run() after setup.", True),
    ("Then self.run is replaced.", True),
])
def test_a_plain_word_is_a_dependency_only_as_code(text, dep):
    a = NS(target_files=["tools/loop.py"], cited_location=NS(symbol="run"))
    assert BP._cross_file_symbol_ref(a, NS(target_files=["x.py"], instruction=text)) is dep


def test_extract_claims_is_linear_on_a_long_run():
    started = time.perf_counter()
    for run in ("a" * 20000, "_" * 20000, "a" + "." * 20000, ("pkg/" * 5000)):
        verifier.extract_claims(run, "pkg/mod.py")
    assert time.perf_counter() - started < 5


def test_a_letters_only_id_is_an_entry_of_its_own():
    text = "### task_a: first\n\nbody a\n\n---\n\n### task_b: second\n\nbody b\n\n---\n\n## Manual Suggestions\n"
    assert "task_b" in _without_section(text, "task_a")


def test_a_verdict_without_a_finding_settles_nothing(tmp_path):
    for adj in ("a", "b"):
        (tmp_path / f"truth-{adj}.csv").write_text("finding,truth,how\n,REAL,x\nf1,FALSE,y\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "truth_consensus.py"),
                           str(tmp_path / "truth-a.csv"), str(tmp_path / "truth-b.csv"),
                           "--truth", str(tmp_path / "truth.csv"), "--report", str(tmp_path / "adj.md")],
                          capture_output=True, text=True, timeout=60)
    rows = (tmp_path / "truth.csv").read_text(encoding="utf-8").splitlines()
    assert not any(r.startswith(",") for r in rows) and any(r.startswith("f1,") for r in rows)
    assert "a verdict with no finding" in proc.stderr


# ── Gate 1's empty-reply retry and its deadline ─────────────────────────────────────────────

def _empty_reply_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gate1_empty_reply_o", ROOT / "tests" / "test_gate1_empty_reply.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_empty_reply_retry_stops_at_the_presence_deadline(tmp_path):
    er = _empty_reply_module()

    def slow_empty(payload):
        end = time.monotonic() + 0.05            # time.sleep is patched by the harness
        while time.monotonic() < end:
            pass
        return er.transport()

    prov = er.Provider({}, default=[slow_empty])
    er._run(tmp_path, prov, er._config({"presence_empty_retries": "5", "presence_deadline_sec": "0.01"}))
    assert sum(len(v) for v in prov.calls.values()) <= 2


# ── arena: what the operator types and what git hands back ──────────────────────────────────

def test_a_ticket_number_of_5000_digits_is_refused(tmp_path, capsys):
    import argparse
    from tools.arena import tickets
    huge = "9" * 5000
    assert tickets.issue_status(tmp_path, argparse.Namespace(number=huge, output="table", reason=None), {}, "open") == 2
    assert tickets.issue_edit(tmp_path, argparse.Namespace(number=huge, output="table"), {}) == 2
    with pytest.raises(ValueError, match="not a positive integer"):
        tickets._chosen_number(tmp_path, None, "main", huge)


def test_a_crlf_patch_keeps_its_body():
    from tools.arena.merge import body_of
    lf = "From abc Mon Sep 17 00:00:00 2001\nSubject: [PATCH] first commit\n\nthe body\nsecond line\n---\n a.py | 1 +\n"
    assert body_of(lf.replace("\n", "\r\n")) == body_of(lf) == "the body\nsecond line"


def test_run_list_keeps_a_round_with_one_odd_agent_entry(tmp_path):
    import json as _json
    from types import SimpleNamespace
    from tools.arena import rounds
    (tmp_path / "out" / "7").mkdir(parents=True)
    (tmp_path / "out" / "7" / "state.json").write_text(_json.dumps({"agents": ["x", {"state": "READY"}]}),
                                                       encoding="utf-8")
    row = rounds.list_rows(tmp_path, SimpleNamespace(out_dir="out"), proc_root=str(tmp_path / "proc"))[0]
    assert (row["STATE"], row["READY/TOTAL"]) == ("done", "1/2")


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX modes, not root")
def test_run_list_of_an_unreadable_rounds_folder_is_empty(tmp_path):
    from types import SimpleNamespace
    from tools.arena import rounds
    (tmp_path / "out").mkdir()
    os.chmod(tmp_path / "out", 0o300)
    try:
        assert rounds.list_rows(tmp_path, SimpleNamespace(out_dir="out")) == []
    finally:
        os.chmod(tmp_path / "out", 0o700)


def test_an_explicit_empty_ref_is_refused(tmp_path, capsys):
    import argparse
    from tools.arena import basecheck
    assert basecheck.run_base_check(tmp_path, argparse.Namespace(ref="", output="table"), {"branch": "main"}) == 2
    assert "--ref is empty" in capsys.readouterr().err


def test_an_indented_heading_is_a_heading():
    from tools.arena import tickets
    assert tickets._headings(["   ## AR-7 the item", "text"]) == [(0, 2)]


# ── fenced replies ──────────────────────────────────────────────────────────────────────────

def test_a_json_body_with_a_code_fence_in_a_string_is_whole():
    import json as _json
    from tools.llm_stream import strip_json_fence
    raw = '```json\n{"quote": "use ```py fences``` here"}\n```'
    assert _json.loads(strip_json_fence(raw)) == {"quote": "use ```py fences``` here"}


@pytest.mark.parametrize("label", ["text", "jsonc", "JSON5"])
def test_the_reference_filter_reads_any_fence_label(label, monkeypatch):
    from tools import search_agent as sa
    monkeypatch.setattr(sa, "_request_completion", lambda *a, **kw: f'```{label}\n["a"]\n```')
    agent = sa.SearchAgent(model="m", base_url="http://127.0.0.1:1/v1")
    assert agent._evaluate_with_llm({"a": {"code": "def a(): pass"}, "b": {"code": "def b(): pass"}}) == ["a"]


# ── small displays ──────────────────────────────────────────────────────────────────────────

def test_a_prompt_version_without_a_score_is_summarised(tmp_path):
    import json as _json
    from tools.prompt_store import PromptStore
    store = tmp_path / "prompts.json"
    store.write_text(_json.dumps({"validator_agent": {"stack": [{"version": 1, "prompt": "p", "score": None}],
                                                       "current_version": 1}}), encoding="utf-8")
    assert "v1" in PromptStore(store_path=store).get_store_summary(["validator_agent"])


def test_a_function_without_a_name_is_shown_as_imports_only(capsys):
    from tools.formatter import OutputFormatter
    from tools.prompt_parser import ParsedPrompt
    OutputFormatter.render(parsed=ParsedPrompt(file_path="app.py", target_name="", target_type="function",
                                               intent="show_imports", raw="imports of app.py"),
                           imports=[], block="", search_result={}, improvement={}, elapsed_time=1.0, iteration=1,
                           output_config={})
    out = capsys.readouterr().out
    assert "File Imports Only" in out


def test_a_prompt_store_write_interrupted_leaves_no_temp_file(tmp_path, monkeypatch):
    from tools import prompt_store as ps
    store = ps.PromptStore(store_path=tmp_path / "prompts.json")

    def interrupted(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(ps.os, "fsync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        store.push("validator_agent", "a prompt", 0.5)
    assert not list(tmp_path.glob("*.tmp"))
