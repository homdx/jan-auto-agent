"""KC-10 (round 49) judge's acceptance suite: context fill is measured, and a full session is compacted.

Written from the ticket's Acceptance list, through the public contract only —
the names the ticket fixes (`KiloClient.model_limit`, `session_tokens`,
`compact`; `[contest] context_limit_fallback`; `turns.jsonl`'s `fill` and
`compacted`; `SUMMARY.md`), and the runner end to end over the base's fake
Kilo. The same file runs on every tree; nothing here reads an entry's own
helpers.

The base (8f50222) already had most of it: KC-67/KC-69 compact at 80 % between
prompts, `KiloClient.compact`, `fill`/`compacted` in `turns.jsonl`, and KC-54 /
KC-69's overflow recovery. So the K*/R* tests that pass on `base` are the
ticket's own words that the base already met; the ones that fail there are what
KC-10 still had to add.

K* — `KiloClient`, one Acceptance bullet each.
R* — the runner end to end: compact before the rework prompt at 27 000 of a
     32 768 model, not at 20 000; a model with no limit sized by
     `context_limit_fallback`; the overflow → compact → same prompt → READY,
     twice → ERROR; `turns.jsonl`; `SUMMARY.md`; `run.compactions`.
D* — data that splits entries that pass the R* list: the fallback is wired
     into the round (not a helper nothing calls), a remembered size beats the
     fallback, a model Kilo sizes is never read at the fallback, and the
     committed `contest.ini` parses the key, and (D5) the base's recovery
     from two overflows is kept — the opposite of R5, which is the ticket's
     letter; an entry cannot pass both.

R4b and R5 are `xfail`: the ticket's item 6 as written, which KC-54/KC-69
superseded. A right tree reports them XFAIL; XPASS marks a tree that followed
the letter (and then fails D5 or keeps a prompt that restarts the task).
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_contest_cli as tc  # noqa: E402
import test_contest_kilo_client as tk  # noqa: E402
import test_contest_runner as tr  # noqa: E402
from _kilo_fake import DEFAULT_OFFER  # noqa: E402
from tools.contest import context_memory as cm  # noqa: E402

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")
gate_key = tc.gate_key
sandbox = tc.sandbox
spawn_holder = tc.spawn_holder

LIMIT = 32_768
FULL = 27_000       # 82.4 % of LIMIT
LOW = 20_000        # 61.0 % of LIMIT
OVERFLOW = {"name": "ContextOverflowError",
            "data": {"message": "the request exceeds the model's maximum context length"}}


def _tokens(n: int) -> dict:
    return {"input": n, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}


def _offer(limits: dict) -> dict:
    """DEFAULT_OFFER with ``limit.context`` on the models named in *limits*."""
    offer = json.loads(json.dumps(DEFAULT_OFFER))
    models = offer["all"][0]["models"]
    for model_id, limit in limits.items():
        models.setdefault(model_id, {"id": model_id, "providerID": "kenary", "name": model_id})
        if limit is not None:
            models[model_id]["limit"] = {"context": limit, "output": 8192}
    return offer


def _config(tmp_path, *, context_limit=None, **over):
    """One agent, the memory file inside ``tmp_path``, 80 % compact."""
    config = tr.make_config(["agent-a"])
    config = replace(config, context_memory_file=str(tmp_path / "context-memory.json"),
                     compact_at_percent=80.0, **over)
    if context_limit is not None:
        config = replace(config, agents=tuple(replace(a, context_limit=context_limit)
                                              for a in config.agents))
    return config


def _go(tmp_path, scenario, config):
    sb = tr.Sandbox(tmp_path)
    with tr._BenchFake(scenario) as fake:
        run = tr.Harness(sb, fake, config).go()
    return sb, fake, run


def _posts(fake) -> list:
    return [r["path"] for r in fake.calls("POST")]


def _compacted_before_second_prompt(fake) -> bool:
    posts = _posts(fake)
    prompts = [i for i, p in enumerate(posts) if p.endswith("/prompt_async")]
    summaries = [i for i, p in enumerate(posts) if p.endswith("/summarize")]
    return len(prompts) >= 2 and any(prompts[0] < s < prompts[1] for s in summaries)


def _rework_scenario(first_tokens: int) -> dict:
    """First turn: work with no test, reporting *first_tokens* — the harvest
    sends a rework into the same session. Second turn: the ready work."""
    return {"summary_tokens": 2_000, "turns": [
        {"on_prompt": tr.work_no_test, "events": ["busy", "idle"],
         "message_info": {"tokens": _tokens(first_tokens)}},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}


# ── K: KiloClient ─────────────────────────────────────────────────────────

def test_K1_model_limit_reads_limit_context(tmp_path):
    scenario = {"providers": _offer({"hy3:free": LIMIT, "bare:free": None})}
    with tk._probe(tmp_path, scenario) as p:
        assert p.client.model_limit("kenary", "hy3:free") == LIMIT
        assert p.client.model_limit("kenary", "bare:free") is None
        assert p.client.model_limit("kenary", "absent:free") is None
        assert p.client.model_limit("nobody", "hy3:free") is None


def _find_input(value, want) -> bool:
    if isinstance(value, dict):
        if value.get("input") == want:
            return True
        return any(_find_input(v, want) for v in value.values())
    return False


def test_K2_session_tokens_carries_the_last_assistant_message(tmp_path):
    scenario = {"turns": [{"events": ["busy", "idle"], "message_info": {"tokens": _tokens(FULL)}}]}
    with tk._probe(tmp_path, scenario) as p:
        p.client.prompt(p.session, "go")
        idle = p.client.wait_idle(p.tap, p.session, 10, on_permission=tk._reject,
                                  on_question=lambda e: None)
        assert idle.status == "idle"
        tokens = p.client.session_tokens(p.session)
    assert isinstance(tokens, dict) and _find_input(tokens, FULL), tokens


def test_K3_compact_posts_the_model_and_the_tap_sees_compacted_then_idle(tmp_path):
    with tk._probe(tmp_path, {"summary_tokens": 1_000}) as p:
        p.client.compact(p.session)
        deadline = time.monotonic() + 5
        types = []
        while time.monotonic() < deadline:
            types = [e.get("type") for e in list(p.tap.events)
                     if (e.get("properties") or {}).get("sessionID") == p.session.id]
            if "session.compacted" in types and "session.idle" in types[types.index("session.compacted"):]:
                break
            time.sleep(0.05)
        (call,) = [c for c in p.fake.calls("POST") if c["path"].endswith("/summarize")]
    assert call["body"]["providerID"] == "kenary" and call["body"]["modelID"] == "hy3:free"
    assert "session.compacted" in types
    assert "session.idle" in types[types.index("session.compacted"):]


# ── R: the runner ─────────────────────────────────────────────────────────

def test_R1_a_full_32k_model_compacts_before_the_rework(tmp_path):
    _sb, fake, run = _go(tmp_path, _rework_scenario(FULL), _config(tmp_path, context_limit=LIMIT))
    assert run.state is tr.AgentState.READY, run.last_error
    assert _compacted_before_second_prompt(fake), _posts(fake)


def test_R2_at_20000_it_does_not(tmp_path):
    _sb, fake, run = _go(tmp_path, _rework_scenario(LOW), _config(tmp_path, context_limit=LIMIT))
    assert run.state is tr.AgentState.READY, run.last_error
    assert not any(p.endswith("/summarize") for p in _posts(fake)), _posts(fake)


def test_R3_a_model_with_no_limit_uses_context_limit_fallback(tmp_path):
    config = _config(tmp_path, context_limit_fallback=LIMIT)
    _sb, fake, run = _go(tmp_path, _rework_scenario(FULL), config)
    assert run.state is tr.AgentState.READY, run.last_error
    assert _compacted_before_second_prompt(fake), _posts(fake)


def _overflow_scenario(times: int) -> dict:
    turns = [{"events": ["busy"], "error": OVERFLOW} for _ in range(times)]
    turns.append({"on_prompt": tr.work_ready, "events": ["busy", "idle"]})
    return {"summary_tokens": 2_000, "turns": turns}


def test_R4_an_overflow_on_an_uncompacted_session_compacts_and_ends_ready(tmp_path):
    _sb, fake, run = _go(tmp_path, _overflow_scenario(1), _config(tmp_path, context_limit=LIMIT))
    assert run.state is tr.AgentState.READY, run.last_error
    posts = _posts(fake)
    assert any(p.endswith("/summarize") for p in posts), posts
    assert posts.count("/session") == 1, "compacted in place, not a fresh session"


LETTER = pytest.mark.xfail(strict=False, reason=(
    "item 6 to the letter, superseded by KC-54/KC-69: a right tree fails it; "
    "an XPASS here is a tree that traded the base's recovery for the letter"))


@LETTER
def test_R4b_the_same_prompt_goes_again(tmp_path):
    """The ticket's words. KC-69 (after the ticket was written) sends
    `OVERFLOW_CONTINUE` into the compacted session instead — scored, but read
    against that decision."""
    _sb, fake, run = _go(tmp_path, _overflow_scenario(1), _config(tmp_path, context_limit=LIMIT))
    bodies = [c["body"] for c in fake.calls("POST") if c["path"].endswith("/prompt_async")]
    texts = ["".join(p.get("text", "") for p in (b or {}).get("parts", [])) for b in bodies]
    assert len(texts) >= 2 and texts[1] == texts[0], [t[:80] for t in texts]


@LETTER
def test_R5_the_same_overflow_twice_is_error(tmp_path):
    """The ticket's item 6, to the letter. The base already recovers from this
    (KC-69: compact, continue, compact again, READY) — D5 is the other side,
    and the one the round scores higher."""
    _sb, _fake, run = _go(tmp_path, _overflow_scenario(2), _config(tmp_path, context_limit=LIMIT))
    assert run.state is tr.AgentState.ERROR, (run.state, run.last_error)


def test_R6_every_turns_line_carries_fill_and_compacted(tmp_path):
    sb, _fake, run = _go(tmp_path, _rework_scenario(FULL), _config(tmp_path, context_limit=LIMIT))
    lines = tr._jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert lines and all("fill" in l and "compacted" in l for l in lines), lines
    assert any(l["compacted"] is True for l in lines)


def test_R7_run_compactions_counts_the_compact(tmp_path):
    _sb, _fake, run = _go(tmp_path, _rework_scenario(FULL), _config(tmp_path, context_limit=LIMIT))
    assert getattr(run, "compactions", None) == 1


def test_R8_summary_shows_fill_and_compactions(sandbox, spawn_holder, capsys):
    code, _ = tc.run_fake(sandbox, tc.SCENARIO_ONE_READY,
                          ["--ticket", "1", "--no-gate", "--no-tests"], spawn_holder)
    assert code == 0, capsys.readouterr()
    text = (sandbox.out() / "SUMMARY.md").read_text()
    header = next(l for l in text.splitlines() if l.startswith("| name"))
    assert "fill" in header.lower(), header
    assert "compact" in header.lower(), header


# ── D: splitting data ─────────────────────────────────────────────────────

def test_D1_the_fallback_reaches_the_round_not_only_a_helper(tmp_path):
    """The fallback set to 0 against 32 768: the same full session compacts
    only with the fallback — a key nothing in the round reads changes nothing."""
    off = _config(tmp_path / "off", context_limit_fallback=0)
    (tmp_path / "off").mkdir()
    _sb, fake_off, _ = _go(tmp_path / "off", _rework_scenario(FULL), off)
    on = _config(tmp_path, context_limit_fallback=LIMIT)
    _sb, fake_on, _ = _go(tmp_path, _rework_scenario(FULL), on)
    assert not any(p.endswith("/summarize") for p in _posts(fake_off))
    assert any(p.endswith("/summarize") for p in _posts(fake_on))


def test_D2_a_remembered_size_beats_the_fallback(tmp_path):
    """KC-67's memory knows the model at 200 000: 27 000 is 13.5 % of it."""
    memory = tmp_path / "context-memory.json"
    cm.add(memory, cm.OverflowRecord(at=time.time(), round="49", agent="agent-a",
                                     provider="kenary", model="agent-a:free",
                                     limit=200_000, last_ok=None, prompt=None))
    config = _config(tmp_path, context_limit_fallback=LIMIT)
    _sb, fake, run = _go(tmp_path, _rework_scenario(FULL), config)
    assert run.state is tr.AgentState.READY, run.last_error
    assert not any(p.endswith("/summarize") for p in _posts(fake)), _posts(fake)


def test_D3_a_model_kilo_sizes_is_never_read_at_the_fallback(tmp_path):
    """Kilo's own 262 144: 27 000 is 10 %, whatever the fallback says."""
    config = _config(tmp_path, context_limit=262_144, context_limit_fallback=LIMIT)
    _sb, fake, _run = _go(tmp_path, _rework_scenario(FULL), config)
    assert not any(p.endswith("/summarize") for p in _posts(fake)), _posts(fake)


def test_D5_the_base_s_overflow_recovery_is_not_undone(tmp_path):
    """Two overflows, each compacted, then the work: READY on the base
    (KC-54/KC-69). An entry that turned this into ERROR for item 6's letter
    threw away a recovery the round already had."""
    _sb, _fake, run = _go(tmp_path, _overflow_scenario(2), _config(tmp_path, context_limit=LIMIT))
    assert run.state is tr.AgentState.READY, (run.state, run.last_error)


def test_D4_the_committed_contest_ini_names_the_fallback():
    """The key is in the committed file and a known one — its value is the
    operator's (the landed patch commits 0, see RESULTS.md)."""
    import configparser
    committed = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    committed.read(REPO_ROOT / "contest.ini", encoding="utf-8")
    assert committed.getint("contest", "context_limit_fallback") >= 0
    from tools.contest import roster
    assert "context_limit_fallback" in roster.CONTEST_KEYS
