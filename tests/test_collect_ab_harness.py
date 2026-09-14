"""tests/test_collect_ab_harness.py — M5: the A/B harness.

Covers the pieces that make the two arms comparable:
* the config copy is patched, the template is never written, every base_url
  goes to the stub, every api_key (commented too) is scrubbed, a stub off
  loopback is refused;
* the replay key ignores exactly the COLLECT block, so a coder reply recorded
  with the pack off serves the pack-on arm;
* the plan seed drops non-.py tasks and cuts the dependency edges to them;
* the counters read every trace of a tree, the gate-1 split prefers M4's
  ``stage`` param, M4's ``collect_block`` events win over the header grep;
* the verdict: pass / the three fail conditions / no_data / gate-1 sanity;
* the stub answers openai (stream + plain) and ollama shapes, replays from a
  recordings file, and is deterministic.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import urllib.request
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "collect_ab.py"
_spec = importlib.util.spec_from_file_location("collect_ab", _SCRIPT)
ab = importlib.util.module_from_spec(_spec)
sys.modules["collect_ab"] = ab
_spec.loader.exec_module(ab)  # type: ignore[union-attr]

STUB = "http://127.0.0.1:14141/v1"

_TEMPLATE = """\
[api]
active = remote
verify_ssl = true

[api_local]
base_url   = http://localhost:11434
api_key    = ollama
model      = qwen

[api_remote]
#base_url   = https://openapi.web.url/v1
#api_key    = sk-old-commented-secret
api_format = openai
base_url = https://token.provider.example/v1
api_key = sk-live-secret-value
model = some-model

[gate1_llm]
base_url = https://other.example/v1
api_key = sk-other-secret

[trace]
enabled              = true
max_field_chars      = 12000

[collect]
enabled         = true
use_in_auto     = true
max_context_chars_auto = 1200
"""

_BLOCK_ON = (
    "COLLECT MODEL (static facts, do not contradict):\n"
    "module: pkg/calc.py\n"
    "callers: entry point — nothing imports this\n"
    "calls_into: pkg/util.py\n"
    "tests: 1 file: tests/test_calc.py\n"
    "public_symbols: pkg/calc.py:add, pkg/calc.py:sub\n"
    "\n"
)
_BLOCK_OFF = (
    "COLLECT MODEL (static facts, do not contradict):\n"
    "module: pkg/calc.py\n"
    "public_symbols: pkg/calc.py:add, pkg/calc.py:sub\n"
    "\n"
)
_PROMPT_TAIL = "### pkg/calc.py\ndef add(a, b):\n    return a + b\n"


# ── config ────────────────────────────────────────────────────────────────────

def test_patch_never_writes_the_template_and_scrubs_every_key(tmp_path):
    src = tmp_path / "agents.ini"
    src.write_text(_TEMPLATE, encoding="utf-8")
    before = src.read_bytes()
    summary = ab.patch_config(src, tmp_path / "off.ini", pack_enabled=False, stub_url=STUB)
    assert src.read_bytes() == before
    copy = (tmp_path / "off.ini").read_text(encoding="utf-8")
    assert "sk-live-secret-value" not in copy
    assert "sk-old-commented-secret" not in copy
    assert "sk-other-secret" not in copy
    assert "https://token.provider.example" not in copy
    assert "https://other.example" not in copy
    assert len(re.findall(rf"^base_url\s*= {re.escape(STUB)}$", copy, re.M)) == 3   # local, remote, gate1_llm
    assert summary["base_url"] == 3 and summary["api_key"] == 4
    assert "pack_enabled = false" in copy
    assert "max_field_chars      = 400000" in copy       # value patched, layout kept


def test_arm_copies_differ_by_exactly_the_switch(tmp_path):
    src = tmp_path / "agents.ini"
    src.write_text(_TEMPLATE, encoding="utf-8")
    ab.patch_config(src, tmp_path / "off.ini", pack_enabled=False, stub_url=STUB)
    ab.patch_config(src, tmp_path / "on.ini", pack_enabled=True, stub_url=STUB)
    off = (tmp_path / "off.ini").read_text().splitlines()
    on = (tmp_path / "on.ini").read_text().splitlines()
    diff = [(a, b) for a, b in zip(off, on) if a != b]
    assert len(off) == len(on)
    assert diff == [("pack_enabled = false", "pack_enabled = true")]


def test_active_section_with_commented_base_url_gets_the_stub(tmp_path):
    src = tmp_path / "agents.ini"
    src.write_text(_TEMPLATE.replace("base_url = https://token.provider.example/v1\n", ""),
                   encoding="utf-8")
    summary = ab.patch_config(src, tmp_path / "off.ini", pack_enabled=False, stub_url=STUB)
    copy = (tmp_path / "off.ini").read_text(encoding="utf-8")
    assert "[api_remote] base_url" in summary["inserted"]
    remote = copy.split("[api_remote]", 1)[1].split("[gate1_llm]", 1)[0]
    assert f"base_url = {STUB}" in remote


def test_absent_collect_section_is_created(tmp_path):
    src = tmp_path / "agents.ini"
    src.write_text(_TEMPLATE.split("[collect]")[0], encoding="utf-8")
    ab.patch_config(src, tmp_path / "on.ini", pack_enabled=True, stub_url=STUB)
    copy = (tmp_path / "on.ini").read_text(encoding="utf-8")
    assert "[collect]\npack_enabled = true\nuse_in_auto = true\n" in copy


@pytest.mark.parametrize("url", ["https://api.provider.example/v1", "http://10.0.0.5:8080", ""])
def test_stub_off_loopback_is_refused_before_anything_is_written(tmp_path, url):
    src = tmp_path / "agents.ini"
    src.write_text(_TEMPLATE, encoding="utf-8")
    with pytest.raises(ab.LiveProviderRefused):
        ab.patch_config(src, tmp_path / "off.ini", pack_enabled=False, stub_url=url)
    assert not (tmp_path / "off.ini").exists()


def test_patch_in_place_is_refused(tmp_path):
    src = tmp_path / "agents.ini"
    src.write_text(_TEMPLATE, encoding="utf-8")
    with pytest.raises(ValueError):
        ab.patch_config(src, src, pack_enabled=False, stub_url=STUB)


def test_pack_budget_read_from_the_template(tmp_path):
    src = tmp_path / "agents.ini"
    src.write_text(_TEMPLATE, encoding="utf-8")
    assert ab.read_pack_budget(src) == 1200
    src.write_text(_TEMPLATE.replace("max_context_chars_auto = 1200", "max_context_chars_auto = oops"))
    assert ab.read_pack_budget(src) == ab.DEFAULT_BUDGET


# ── the replay key and the block ──────────────────────────────────────────────

def test_replay_key_ignores_exactly_the_collect_block():
    system = "You are a senior software engineer implementing a targeted code improvement."
    head = "TASK ID:    AUTO-T1\nCURRENT FILE CONTENTS:\n"
    assert ab.replay_key(system, head + _BLOCK_ON + _PROMPT_TAIL) \
        == ab.replay_key(system, head + _BLOCK_OFF + _PROMPT_TAIL) \
        == ab.replay_key(system, head + _PROMPT_TAIL)
    assert ab.replay_key(system, head + _PROMPT_TAIL) != ab.replay_key(system, head + _PROMPT_TAIL + "x")
    assert ab.cut_collect_block(head + _BLOCK_ON + _PROMPT_TAIL) == head + _PROMPT_TAIL


def test_count_collect_blocks_splits_pack_rows_from_the_v2_rows():
    assert ab.count_collect_blocks(_BLOCK_ON + _PROMPT_TAIL) == (1, 3)
    assert ab.count_collect_blocks(_BLOCK_OFF + _PROMPT_TAIL) == (1, 0)
    assert ab.count_collect_blocks(_PROMPT_TAIL) == (0, 0)
    assert ab.count_collect_blocks("") == (0, 0)


# ── the seeded plan ───────────────────────────────────────────────────────────

def test_drop_non_py_tasks_cuts_the_dangling_dependency_edges():
    plan = {"goal": "g", "tasks": [
        {"id": "AUTO-T1", "target_files": ["skills/a.skill.ini"], "dependencies": []},
        {"id": "AUTO-T2", "target_files": ["pkg/calc.py", "README.md"], "dependencies": ["AUTO-T1"]},
        {"id": "AUTO-T3", "target_files": ["docs/x.md"], "dependencies": []},
        {"id": "AUTO-T4", "target_files": [], "dependencies": ["AUTO-T3", "AUTO-T2"]},
    ]}
    out, dropped, dangling = ab.drop_non_py_tasks(plan)
    assert dropped == ["AUTO-T1", "AUTO-T3"]
    assert [t["id"] for t in out["tasks"]] == ["AUTO-T2", "AUTO-T4"]
    assert out["tasks"][0]["dependencies"] == []
    assert out["tasks"][1]["dependencies"] == ["AUTO-T2"]
    assert dangling == ["AUTO-T2 -> AUTO-T1", "AUTO-T4 -> AUTO-T3"]


def test_seed_copies_plan_progress_tickets_and_plan_trace_but_no_run_log(tmp_path):
    plan_tree, arm = tmp_path / "plan", tmp_path / "arm"
    agent = plan_tree / ".agent"
    (agent / "tickets").mkdir(parents=True)
    (agent / "plan.json").write_text(json.dumps({"goal": "g", "base_dir": str(plan_tree), "tasks": []}))
    (agent / "progress.json").write_text('{"status": "idle"}')
    (agent / "tickets" / "TICKET-AUTO-T1.json").write_text("{}")
    (agent / "trace_plan.jsonl").write_text('{"kind": "run_start"}\n')
    (agent / "run.log").write_text("plan phase: done\n")
    ab.seed_agent_dir(plan_tree, arm)
    seeded = arm / ".agent"
    assert json.loads((seeded / "plan.json").read_text())["base_dir"] == str(arm.resolve())
    assert (seeded / "progress.json").is_file()
    assert (seeded / "tickets" / "TICKET-AUTO-T1.json").is_file()
    assert (seeded / "trace_plan.jsonl").is_file()
    assert not (seeded / "run.log").exists()


# ── counters ──────────────────────────────────────────────────────────────────

def _ev(kind, source="x", target="y", content="", **params):
    return {"kind": kind, "source": source, "target": target, "content": content, "params": params}


def _write_trace(path: Path, events) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def test_counters_read_every_trace_of_the_tree(tmp_path):
    tree = tmp_path / "tree"
    _write_trace(tree / ".agent" / "trace_aaa.jsonl", [
        _ev("probe_request", "architect", "probe"),
        _ev("probe_result", "probe", "architect", misses=2),
        _ev("probe_declined", "architect", "probe", reason="unresolved after 2 rounds", ops=3),
        _ev("rejected", "controller", "gate1_filter", reason="cited file not found: 'x.py'"),
        _ev("rejected", "controller", "gate1_filter", reason="claim not present in code"),
        _ev("rejected", "controller", "gate1_filter", stage="existence", reason="whatever"),
        _ev("rejected", "controller", "gate1_filter", reason="duplicate of an earlier candidate with fingerprint 'a'"),
        _ev("llm_request", "architect", "llm", "plan me"),
    ])
    _write_trace(tree / ".agent" / "trace_bbb.jsonl", [
        _ev("call", "controller", "outer_loop", task_id="AUTO-T1"),
        _ev("llm_request", "coder", "llm", _BLOCK_OFF + _PROMPT_TAIL, task_id="AUTO-T1"),
        _ev("llm_response", "llm", "coder", '{"files": [], "context_request": ["Config"]}'),
        _ev("decision", "gate2", "inner_loop", "REJECTED", task="AUTO-T1", attempt="1", stage="gate2"),
        _ev("llm_request", "coder", "llm", _BLOCK_ON + _PROMPT_TAIL, task_id="AUTO-T1"),
        _ev("llm_response", "llm", "coder", '{"files": []}'),
        _ev("decision", "overall", "inner_loop", "APPROVED", task="AUTO-T1", attempt="2", stage="overall"),
        _ev("result", "outer_loop", "controller", passed="True", task="AUTO-T1"),
        _ev("call", "controller", "outer_loop", task_id="AUTO-T2"),
        _ev("llm_request", "coder", "llm", _PROMPT_TAIL, task_id="AUTO-T2"),
        _ev("decision", "outer_loop", "controller", "BLOCKED", task_id="AUTO-T2"),
        "not json at all",
    ])
    (tree / ".agent" / "trace_bbb.jsonl").write_text(
        (tree / ".agent" / "trace_bbb.jsonl").read_text().replace('"not json at all"', "{broken"))
    c = ab.counters_for_tree(tree)
    assert c["traces"] == ["trace_aaa.jsonl", "trace_bbb.jsonl"]
    assert c["probe_requests"] == 1 and c["probe_misses"] == 5
    assert c["gate1_rejected_existence"] == 2 and c["gate1_rejected_presence"] == 1
    assert c["gate1_rejected_duplicate"] == 1
    assert c["llm_calls"] == 4 and c["coder_calls"] == 3
    assert c["context_re_requests"] == 1
    assert c["tasks_executed"] == 2 and c["tasks_done"] == 1 and c["tasks_blocked"] == 1
    assert c["gate2_attempts"] == 2 and c["gate2_attempts_per_task"] == 1.0
    assert c["coder_rounds_per_task"] == 1.5
    assert c["collect_blocks"] == 2 and c["collect_pack_rows"] == 3
    assert c["collect_source"] == "header"
    expected = (len(_BLOCK_OFF + _PROMPT_TAIL) + len(_BLOCK_ON + _PROMPT_TAIL) + len(_PROMPT_TAIL)) / 3
    assert c["prompt_chars_per_coder_call"] == round(expected, 1)


def test_m4_collect_block_events_win_over_the_header_grep():
    events = [
        _ev("llm_request", "coder", "llm", _BLOCK_ON + _PROMPT_TAIL),
        _ev("collect_block", "collect_bridge", "coder", pack_rows=4, chars=300),
    ]
    c = ab.counters_from_events(events)
    assert (c["collect_blocks"], c["collect_pack_rows"], c["collect_source"]) == (1, 4, "events")


def test_missing_tree_is_all_zero_and_not_an_error(tmp_path):
    c = ab.counters_for_tree(tmp_path / "nowhere")
    assert c["traces"] == [] and c["coder_calls"] == 0 and c["llm_calls"] == 0


# ── the verdict ───────────────────────────────────────────────────────────────

def _arm(**over):
    base = {k: 0 for k, _, _ in ab.ROWS}
    base.update(coder_calls=10, collect_blocks=10, prompt_chars_per_coder_call=5000.0,
                gate2_attempts_per_task=1.0)
    base.update(over)
    return base


def test_verdict_pass_when_nothing_rises_and_the_pack_stays_in_budget():
    d = ab.diff_arms(_arm(collect_pack_rows=0), _arm(collect_pack_rows=30, prompt_chars_per_coder_call=5800.0),
                     budget=1200)
    assert d["verdict"] == "pass" and d["reasons"] == []
    assert d["delta"]["prompt_chars_per_coder_call"] == 800.0
    table = ab.render_table(d)
    assert "verdict: pass" in table and "A must be 0, B > 0" in table


@pytest.mark.parametrize("on_over, needle", [
    ({"probe_misses": 1}, "probe misses rose"),
    ({"gate2_attempts_per_task": 1.5}, "gate2 attempts per task rose"),
    ({"prompt_chars_per_coder_call": 6200.0}, "not under the pack budget"),
    ({"collect_pack_rows": 0}, "arm B shows no pack row"),
])
def test_verdict_fails_on_each_condition(on_over, needle):
    on = _arm(collect_pack_rows=30)
    on.update(on_over)
    d = ab.diff_arms(_arm(collect_pack_rows=0), on, budget=1200)
    assert d["verdict"] == "fail"
    assert any(needle in r for r in d["reasons"])


def test_verdict_fails_when_arm_a_shows_pack_rows():
    d = ab.diff_arms(_arm(collect_pack_rows=3), _arm(collect_pack_rows=30), budget=1200)
    assert d["verdict"] == "fail" and any("pack_enabled=false did not take" in r for r in d["reasons"])


def test_verdict_no_data_when_the_coder_was_never_reached():
    d = ab.diff_arms(_arm(coder_calls=0), _arm(collect_pack_rows=30), budget=1200)
    assert d["verdict"] == "no_data"


def test_gate1_rows_must_be_equal_between_arms():
    d = ab.diff_arms(_arm(collect_pack_rows=0, gate1_rejected_presence=2),
                     _arm(collect_pack_rows=30, gate1_rejected_presence=3), budget=1200)
    assert d["verdict"] == "fail"
    assert any("plan was not seeded identically" in r for r in d["reasons"])


# ── the stub ──────────────────────────────────────────────────────────────────

def _post(url: str, body: dict) -> bytes:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read()


def _msgs(system: str, user: str) -> list:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def test_stub_shapes_every_api_format_and_replays_across_the_block(tmp_path):
    rec = tmp_path / "rec.jsonl"
    srv = ab.start_stub(recordings=ab.Recordings(rec), log_path=tmp_path / "hits.jsonl")
    try:
        base = srv.url.removesuffix("/v1")
        system = "You are a senior software engineer implementing a targeted code improvement."
        user_off = ("TASK ID:    AUTO-T1\nTARGET FILES TO MODIFY:\n  - pkg/calc.py\n\n"
                    "CURRENT FILE CONTENTS:\n" + _BLOCK_OFF + _PROMPT_TAIL)
        user_on = user_off.replace(_BLOCK_OFF, _BLOCK_ON)

        plain = json.loads(_post(base + "/v1/chat/completions",
                                 {"messages": _msgs(system, user_off), "stream": False}))
        reply = plain["choices"][0]["message"]["content"]
        files = json.loads(reply)["files"]
        assert files == [{"path": "pkg/calc.py",
                          "content": "def add(a, b):\n    return a + b\n# collect_ab stub touch\n"}]

        sse = _post(base + "/v1/chat/completions", {"messages": _msgs(system, user_on), "stream": True})
        lines = [ln for ln in sse.decode().splitlines() if ln.startswith("data:")]
        assert lines[-1] == "data: [DONE]"
        chunk = json.loads(lines[0][len("data:"):])
        assert chunk["choices"][0]["delta"]["content"] == reply       # replayed across the block

        nd = json.loads(_post(base + "/api/chat", {"messages": _msgs(system, user_off), "stream": True}))
        assert nd["done"] is True and nd["message"]["content"] == reply

        hits = [json.loads(ln) for ln in (tmp_path / "hits.jsonl").read_text().splitlines()]
        assert [h["source"] for h in hits] == ["canned", "replay", "replay"]
        assert all(h["role"] == "coder" and h["task_id"] == "AUTO-T1" for h in hits)
        assert len(ab.Recordings(rec)) == 1
    finally:
        srv.shutdown()


def test_stub_replays_a_recording_file_verbatim(tmp_path):
    rec = tmp_path / "rec.jsonl"
    system, user = "You are a plan reviewer. Given a GOAL", "GOAL: x"
    key = ab.replay_key(system, user)
    rec.write_text(json.dumps({"key": key, "role": "plan_reviewer", "reply": "REVISE: recorded"}) + "\n")
    srv = ab.start_stub(recordings=ab.Recordings(rec))
    try:
        body = json.loads(_post(srv.url + "/chat/completions", {"messages": _msgs(system, user)}))
        assert body["choices"][0]["message"]["content"] == "REVISE: recorded"
    finally:
        srv.shutdown()


def test_canned_answers_are_schema_valid_for_every_role():
    listing = "### pkg/calc.py\ndef add(a, b):\n    return a + b\n\n### tests/test_calc.py\ndef test_add():\n    pass\n"
    arch = json.loads(ab.canned_answer("architect", "", listing))
    assert arch[0]["target_files"] == ["pkg/calc.py", "tests/test_calc.py"]
    assert arch[0]["cited_location"]["symbol"] == "add"
    assert ab.canned_answer("architect", "", "### README.md\nhello\n") == "[]"
    g1 = json.loads(ab.canned_answer("gate1", "", "```python\n\ndef add(a, b):\n```"))
    assert g1["verdict"] == "confirmed" and g1["evidence"] == "def add(a, b):"
    assert json.loads(ab.canned_answer("gate2", "", ""))["approved"] is True
    assert ab.canned_answer("plan_reviewer", "", "") == "APPROVED"
    assert ab.canned_answer("search_filter", "", "") == "[]"
    assert ab.role_of("You are a senior software architect performing a targeted code review.") == "architect"
    assert ab.role_of("You are a static code reviewer performing a false-positive check.") == "gate1"
    assert ab.role_of("You are a code-change validator. ") == "gate2"
    assert ab.role_of("You are a senior software engineer implementing a targeted code improvement.") == "coder"


# ── report: the no-subprocess path end to end ─────────────────────────────────

def test_report_command_diffs_two_trees_and_writes_json(tmp_path, capsys):
    off, on = tmp_path / "off", tmp_path / "on"
    plan_events = [_ev("rejected", "controller", "gate1_filter", reason="cited file not found: 'x'")]
    _write_trace(off / ".agent" / "trace_plan.jsonl", plan_events)
    _write_trace(on / ".agent" / "trace_plan.jsonl", plan_events)
    _write_trace(off / ".agent" / "trace_run.jsonl", [
        _ev("call", "controller", "outer_loop", task_id="AUTO-T1"),
        _ev("llm_request", "coder", "llm", _BLOCK_OFF + _PROMPT_TAIL),
        _ev("decision", "overall", "inner_loop", "APPROVED", stage="overall"),
    ])
    _write_trace(on / ".agent" / "trace_run.jsonl", [
        _ev("call", "controller", "outer_loop", task_id="AUTO-T1"),
        _ev("llm_request", "coder", "llm", _BLOCK_ON + _PROMPT_TAIL),
        _ev("decision", "overall", "inner_loop", "APPROVED", stage="overall"),
    ])
    out = tmp_path / "ab.json"
    rc = ab.main(["report", "--a", str(off), "--b", str(on), "--out", str(out)])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "verdict: pass" in printed
    payload = json.loads(out.read_text())
    assert payload["mode"] == "report" and payload["verdict"] == "pass"
    assert payload["off"]["gate1_rejected_existence"] == payload["on"]["gate1_rejected_existence"] == 1
    assert payload["delta"]["collect_pack_rows"] == 3


def test_main_refuses_a_live_stub_url_with_exit_3(tmp_path, capsys):
    src = tmp_path / "agents.ini"
    src.write_text(_TEMPLATE, encoding="utf-8")
    rc = ab.main(["configs", "--config", str(src), "--out-dir", str(tmp_path / "cfg"),
                  "--stub-url", "https://api.provider.example/v1"])
    assert rc == 3
    assert "refused" in capsys.readouterr().err
    assert not (tmp_path / "cfg").exists()
