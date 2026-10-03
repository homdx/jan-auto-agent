"""tests/test_contest_overflow_wording.py — round 145: a context overflow in any provider's words, and a remembered size below Kilo's.

Round 145's glm-4.5-flash (zai) worked 45 minutes, grew step by step to
98 777 tokens and was refused with ``Prompt exceeds max length`` — a 400 with
no name, a wording KC-54's three fixed spellings did not know. The runner ended
it ERROR: no record in the KC-67 memory, no compact, the run lost. Kilo had
been told 131 072 for the model, so neither Kilo's compact nor the runner's 80 %
gate came before the provider's real wall.

The cases:

  1. the wording: a size refusal in any provider's words is an overflow; money,
     a plan, a key or a rate never is (deepseek-free's ``longer than the free
     tier allows``, TeamoRouter's empty wallet, a rate limit);
  2. a refusal with no words about a size, of a session at or past
     ``FULL_REFUSAL_SHARE`` of its window, is an overflow too — and of a small
     session it is the error it always was;
  3. the overflow is remembered (``last_ok``) and compacted, so the run goes on;
  4. the next session sizes the model by the remembered 98 777, not Kilo's
     131 072, and compacts in time; the next round hands Kilo the smaller one;
  5. deepseek-free's refusal stays ``ERROR provider_quota`` and writes nothing.

The fake Kilo only — no live provider, no memory file outside ``tmp_path``.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_contest_context_memory as ctm  # noqa: E402
import test_contest_runner as tr  # noqa: E402
from tools.contest import cli as contest_cli  # noqa: E402
from tools.contest import context_memory as cm  # noqa: E402
from tools.contest import runner as runner_mod  # noqa: E402
from tools.contest.runner import (  # noqa: E402
    FULL_REFUSAL_SHARE,
    OVERFLOW_CONTINUE,
    _context_budget,
    _is_full_refusal,
    _is_overflow,
    _quota_re,
)

#: What zai declares for glm-4.5-flash, and where it refused (round 145).
DECLARED = 131_072
REFUSED_AT = 98_777

#: The payloads round 144/145 got back, as `session.error` carried them.
ZAI = {"name": "APIError", "data": {"message": "Prompt exceeds max length",
                                    "statusCode": 400, "isRetryable": False}}
DEEPSEEK_FREE = {"name": "APIError", "data": {
    "message": ("This prompt is longer than the free tier allows for a single request. "
                "Shorten it, or add credits to use this model without the free-tier cap: "
                "https://example.invalid/console/billing"),
    "statusCode": 400, "isRetryable": False}}
WALLET = {"name": "APIError", "data": {
    "message": "Your wallet balance is insufficient. Recharge at https://example.invalid to continue.",
    "statusCode": 400, "isRetryable": False}}
#: A refusal that says nothing about why.
BARE_400 = {"name": "APIError", "data": {"message": "Bad Request", "statusCode": 400,
                                         "isRetryable": False}}


def _committed_quota_text() -> str:
    """The committed `contest.ini`'s `quota_patterns` (no roster load: that
    wants the gate's key in the environment)."""
    import configparser
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"), interpolation=None)
    parser.read(REPO_ROOT / "contest.ini", encoding="utf-8")
    return parser.get("contest", "quota_patterns")


def _committed_quota_re():
    """`_committed_quota_text` as the round compiles it."""
    from types import SimpleNamespace
    return _quota_re(SimpleNamespace(quota_patterns=_committed_quota_text()))


# ─────────────────────────────────────────────────────────────────────────────
# 1. the wording
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "Prompt exceeds max length",                                          # zai
    "the request exceeds the model's maximum context length",             # kenary
    "prompt is too long: 210000 tokens > 200000 maximum",
    "The input token count (1200000) exceeds the maximum number of tokens allowed (1048576).",
    "Request too large for model",
    "This model's maximum context length is 262144 tokens.",              # sensenova
    "context_length_exceeded",
    "Input is longer than the maximum context of this model",
    "too many input tokens",
])
def test_a_size_refusal_in_any_words_is_an_overflow(message):
    quota = _committed_quota_re()
    assert _is_overflow({"name": "APIError", "data": {"message": message, "statusCode": 400}},
                        quota)
    assert _is_overflow(message, quota)


@pytest.mark.parametrize("message", [
    DEEPSEEK_FREE["data"]["message"],
    WALLET["data"]["message"],
    "Rate limit exceeded: too many tokens per minute",
    "You exceeded your current quota, please check your plan and billing details.",
    "Invalid API key",
    "Bad Request",
    "Invalid schema for function 'bash'",
    "ECONNRESET",
])
def test_money_a_plan_a_key_a_rate_or_anything_else_is_not(message):
    assert not _is_overflow({"name": "APIError", "data": {"message": message}},
                            _committed_quota_re())


def test_the_round_s_quota_patterns_veto_a_size_refusal():
    """An operator phrase in `quota_patterns` wins over the size words."""
    quota = re.compile("shared pool", re.IGNORECASE)
    error = {"data": {"message": "prompt exceeds the shared pool size"}}
    assert _is_overflow(error) and not _is_overflow(error, quota)


# ─────────────────────────────────────────────────────────────────────────────
# 2. a refusal with no words, of a session that is already full
# ─────────────────────────────────────────────────────────────────────────────

def test_a_bare_refusal_of_a_full_session_is_an_overflow():
    full = int(FULL_REFUSAL_SHARE * DECLARED) + 1
    assert _is_full_refusal(BARE_400, REFUSED_AT, DECLARED)
    assert _is_full_refusal(BARE_400, full, DECLARED)
    assert _is_full_refusal({"data": {"message": "x", "statusCode": 413}}, full, DECLARED)
    assert _is_full_refusal({"data": {"message": "x"}}, full, DECLARED), "no status: a refusal"


@pytest.mark.parametrize("error,last_ok,size", [
    (BARE_400, 20_000, DECLARED),                    # a small session: not about the size
    (BARE_400, REFUSED_AT, None),                    # no window to measure against
    (BARE_400, 0, DECLARED),                         # nothing went through yet
    ({"data": {"message": "x", "statusCode": 429}}, REFUSED_AT, DECLARED),
    ({"data": {"message": "x", "statusCode": 401}}, REFUSED_AT, DECLARED),
    ({"data": {"message": "x", "statusCode": 503}}, REFUSED_AT, DECLARED),
    ({"data": {"message": "x", "isRetryable": True}}, REFUSED_AT, DECLARED),
    ({"name": "ProviderQuota"}, REFUSED_AT, DECLARED),
    ({"name": "ProviderUnavailable"}, REFUSED_AT, DECLARED),
    (DEEPSEEK_FREE, REFUSED_AT, DECLARED),
    (WALLET, REFUSED_AT, DECLARED),
    ("Bad Request", REFUSED_AT, DECLARED),
])
def test_a_bare_refusal_that_is_not_about_the_size_stays_an_error(error, last_ok, size):
    assert not _is_full_refusal(error, last_ok, size, _committed_quota_re())


# ─────────────────────────────────────────────────────────────────────────────
# 3. the run: remembered and compacted, not ERROR
# ─────────────────────────────────────────────────────────────────────────────

def _declared(config, limit=DECLARED):
    """*config* whose agent carries Kilo's declared window, as intake puts it."""
    return replace(config, agents=tuple(replace(a, context_limit=limit) for a in config.agents))


def _run(tmp_path, scenario, memory, **over):
    config = _declared(ctm._config(tmp_path, memory=memory, **over))
    return tr._run_one(tmp_path, scenario, config)


@pytest.mark.parametrize("error", [ZAI, BARE_400], ids=["zai-words", "no-words"])
def test_glm_s_refusal_is_remembered_as_an_overflow(tmp_path, error):
    """Round 145 replayed: 98 777 went through, the next request was refused.
    The run ends STALLED `context overflow` (no continue left), not ERROR, and
    the memory holds `last_ok = 98 777` for the model."""
    memory = tmp_path / "context-memory.json"
    scenario = ctm._overflow_scenario(error, REFUSED_AT)
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=0)
    assert run.state is tr.AgentState.STALLED
    assert run.last_error == "context overflow"
    (record,) = cm.load(memory)
    assert (record.provider, record.model) == ("kenary", "agent-a:free")
    assert record.limit is None and record.last_ok == REFUSED_AT
    assert cm.smallest_size([record], "kenary", "agent-a:free") == REFUSED_AT


def test_glm_s_refusal_is_compacted_and_the_work_goes_on(tmp_path, caplog):
    """With a continue left the overflow is compacted in the same session and
    the run ends READY — what round 145's glm-4.5-flash should have done."""
    import logging
    caplog.set_level(logging.INFO, logger="tools.contest.runner")
    memory = tmp_path / "context-memory.json"
    scenario = {"summary_tokens": 3_000, "turns": [
        {"events": ["busy", "idle"], "message_info": {"tokens": {"input": REFUSED_AT,
                                                                  "output": 0}}},
        {"events": ["busy"], "error": ZAI},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, _ = _run(tmp_path, scenario, memory,
                                max_continues_per_attempt=3)
    tr._assert_ready(run, sb.ws("agent-a"))
    posts = [r["path"] for r in fake.calls("POST")]
    assert f"/session/{run.session_id}/summarize" in posts
    assert any(text == OVERFLOW_CONTINUE for _sid, text in tr._prompts(fake))
    assert cm.load(memory)[0].last_ok == REFUSED_AT
    assert any("read as a context overflow" in r.getMessage() for r in caplog.records)


def test_a_bare_refusal_of_a_small_session_is_error_as_before(tmp_path):
    memory = tmp_path / "context-memory.json"
    scenario = ctm._overflow_scenario(BARE_400, 20_000)
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=0)
    assert run.state is tr.AgentState.ERROR
    assert "Bad Request" in run.last_error
    assert not memory.exists()


@pytest.mark.parametrize("error", [DEEPSEEK_FREE, WALLET], ids=["deepseek-free", "wallet"])
def test_a_plan_s_cap_is_a_quota_and_nothing_is_remembered(tmp_path, error):
    """Round 144's deepseek-free and round 145's empty wallet: right to stop,
    and nothing about the model's window goes into the memory."""
    memory = tmp_path / "context-memory.json"
    scenario = ctm._overflow_scenario(error, REFUSED_AT)
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=3,
                                  quota_patterns=_committed_quota_text())
    assert run.state is tr.AgentState.ERROR
    assert "longer than the free tier" in run.last_error or "wallet" in run.last_error
    # both are named in the committed quota_patterns (the wallet since round 145)
    assert run.last_error.startswith("provider_quota:"), run.last_error
    assert not memory.exists() or cm.load(memory) == []


# ─────────────────────────────────────────────────────────────────────────────
# 4. the next session and the next round use the smaller size
# ─────────────────────────────────────────────────────────────────────────────

def _remembered(tmp_path, last_ok=REFUSED_AT) -> Path:
    return ctm._memory(tmp_path, limit=None, last_ok=last_ok, prompt=None)


def test_a_remembered_size_below_kilo_s_is_the_window():
    spec = replace(tr.make_config(["agent-a"]).agents[0], context_limit=DECLARED)
    below = [ctm._record(limit=None, last_ok=REFUSED_AT, prompt=None).to_dict()]
    above = [ctm._record(limit=None, last_ok=200_000, prompt=None).to_dict()]
    assert _context_budget(spec, below) == (REFUSED_AT, "remembered")
    assert _context_budget(spec, above) == (DECLARED, "kilo")
    assert _context_budget(spec, []) == (DECLARED, "kilo")


def test_the_next_session_compacts_at_80_percent_of_the_refused_size(tmp_path):
    """82 000 is 62.6 % of Kilo's 131 072 — no compact by Kilo's number — and
    83 % of the remembered 98 777: the runner compacts before the rework."""
    memory = _remembered(tmp_path)
    sb, fake, _h, run, _ = _run(tmp_path, ctm._rework_scenario(82_000), memory)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert ctm._summarize_path(run.session_id) in [p for p, _ in ctm._posts(fake)]
    second = run.turns[1]
    assert second["context_source"] == "remembered" and second["context_size"] == REFUSED_AT
    assert second["compacted"] is True and second["fill"] == 83.0


def test_the_next_round_hands_kilo_the_smaller_size(tmp_path, monkeypatch):
    monkeypatch.delenv("KILO_CONFIG_CONTENT", raising=False)
    config = _declared(ctm._config(tmp_path, memory=_remembered(tmp_path)))
    out, content = contest_cli._with_remembered_limits(config, tmp_path / "out" / "146", None)
    (agent,) = out.agents
    assert agent.context_limit == REFUSED_AT
    limit = json.loads(content)["provider"][agent.provider_id]["models"][agent.model_id]["limit"]
    assert limit == cm.kilo_limit(REFUSED_AT, None, cm.DEFAULT_COMPACT_AT_PERCENT)
    # Kilo compacts after the step that crosses `input - reserve`: below the wall
    assert limit["input"] - cm.KILO_COMPACT_RESERVE < REFUSED_AT


def test_a_remembered_size_above_kilo_s_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("KILO_CONFIG_CONTENT", raising=False)
    config = _declared(ctm._config(tmp_path, memory=_remembered(tmp_path, last_ok=200_000)))
    out, content = contest_cli._with_remembered_limits(config, tmp_path / "out" / "146", None)
    assert out is config and content is None


def test_full_refusal_share_is_a_share():
    assert 0 < runner_mod.FULL_REFUSAL_SHARE < 1


# ─────────────────────────────────────────────────────────────────────────────
# 5. a plan's cap and a loop never get into the memory
# ─────────────────────────────────────────────────────────────────────────────

def test_a_size_refusal_of_a_small_session_is_an_error_and_writes_nothing(tmp_path):
    """A provider that turns a 20 000-token session away in size words is a
    plan's cap, not a window: under `context_min_window` it is the error it was
    — no memory, no compact, no loop of compacts on the next session."""
    memory = tmp_path / "context-memory.json"
    scenario = ctm._overflow_scenario(ZAI, 20_000)
    _sb, fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=3,
                                 context_min_window=32_000)
    assert run.state is tr.AgentState.ERROR
    assert "Prompt exceeds max length" in run.last_error
    assert not memory.exists()
    assert not [r for r in fake.calls("POST") if r["path"].endswith("/summarize")]


def test_the_floor_off_lets_a_small_size_refusal_through(tmp_path):
    memory = tmp_path / "context-memory.json"
    scenario = ctm._overflow_scenario(ZAI, 20_000)
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=0,
                                  context_min_window=0)
    assert run.state is tr.AgentState.STALLED and run.last_error == "context overflow"
    assert cm.load(memory)[0].last_ok == 20_000


def test_a_second_refusal_far_below_the_first_is_not_the_window_again(tmp_path, caplog):
    """The first refusal at 98 777 is compacted; the provider refuses again at
    10 000 — far under the wall it named before. That is not the window: the
    run ends ERROR instead of compacting a session that has nothing to give."""
    import logging
    caplog.set_level(logging.INFO, logger="tools.contest.runner")
    memory = tmp_path / "context-memory.json"
    scenario = {"summary_tokens": 3_000, "turns": [
        {"events": ["busy", "idle"], "message_info": {"tokens": {"input": REFUSED_AT,
                                                                  "output": 0}}},
        {"events": ["busy"], "error": ZAI},
        {"events": ["busy", "idle"], "message_info": {"tokens": {"input": 40_000,
                                                                  "output": 0}}},
        {"events": ["busy"], "error": ZAI},
    ]}
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=5,
                                  context_min_window=32_000)
    assert run.state is tr.AgentState.ERROR
    assert len(cm.load(memory)) == 1, "the second refusal is not remembered"
    assert any("not the window again" in r.getMessage() for r in caplog.records)


def test_the_full_refusal_percent_comes_from_the_config(tmp_path):
    memory = tmp_path / "context-memory.json"
    scenario = ctm._overflow_scenario(BARE_400, REFUSED_AT)
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=0,
                                  context_full_refusal_percent=0)
    assert run.state is tr.AgentState.ERROR and not memory.exists()


@pytest.mark.parametrize("last_ok,grew,floor,want", [
    (81_311, 42_588, 32_000, 81_311),   # live glm: loose, but a reply the provider took
    (17_382, 269_086, 32_000, None),    # KC-73's agnes: under the floor
    (14_179, 134_000, 32_000, None),    # KC-73's glm-4-7
    (25_000, 100, 32_000, None),        # tight, but under the floor: a plan's cap
    (17_382, 269_086, 0, None),         # floor off: KC-73 exactly as it was
    (247_828, 3_000, 0, 247_828),
])
def test_the_window_floor_decides_which_last_ok_sizes_a_model(last_ok, grew, floor, want):
    record = ctm._record(limit=None, last_ok=last_ok, grew=grew, prompt=None)
    assert cm.size_of(record, floor) == want


def test_the_three_keys_are_read_from_the_ini(tmp_path):
    from tools.contest.roster import load_roster
    ini = tmp_path / "contest.ini"
    ini.write_text("[contest]\ncontext_min_window = 48000\n"
                   "context_full_refusal_percent = 70\ncontext_watch_sec = 4\n\n"
                   "[contest.agent.a]\nmodel = p/m\n", encoding="utf-8")
    config = load_roster(ini)
    assert cm.min_window(config) == 48_000
    assert cm.full_refusal_percent(config) == 70.0
    assert runner_mod._context_watch_sec(config) == 4.0
    text = (REPO_ROOT / "contest.ini").read_text(encoding="utf-8")
    for key in ("context_min_window", "context_full_refusal_percent", "context_watch_sec"):
        assert f"\n{key}" in text, key


# ─────────────────────────────────────────────────────────────────────────────
# 6. inside a turn: the watch stops a session Kilo cannot size
# ─────────────────────────────────────────────────────────────────────────────

def test_the_watch_stops_a_turn_at_80_percent_of_a_remembered_size(tmp_path):
    """Kilo was told 131 072 and compacts only there; the memory says 98 777.
    A turn that grows to 90 000 (91 %) without asking anything is stopped by
    the watch, and the next prompt is compacted first — round 145's 45-minute
    turn of reads, caught before the wall."""
    memory = _remembered(tmp_path)
    scenario = {"abort_idles": True, "summary_tokens": 3_000, "turns": [
        {"events": ["busy"], "idle": False,
         "message_info": {"tokens": {"input": 90_000, "output": 0}}},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, aborted = _run(tmp_path, scenario, memory, context_watch_sec=0.2)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert aborted
    first = run.turns[0]
    assert first["context_watch_stop"] == 91.1
    assert ctm._summarize_path(run.session_id) in [p for p, _ in ctm._posts(fake)]


def test_no_watch_for_a_size_kilo_knows_itself(tmp_path):
    """Nothing remembered: the size is Kilo's own and Kilo compacts by it — the
    watch is not armed, and a turn at 91 % of it is left to Kilo."""
    scenario = {"turns": [
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"],
         "message_info": {"tokens": {"input": 120_000, "output": 0}}},
    ]}
    sb, fake, _h, run, aborted = _run(tmp_path, scenario, tmp_path / "none.json",
                                      context_watch_sec=0.2)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert not aborted and "context_watch_stop" not in run.turns[0]


def test_the_watch_is_off_at_zero(tmp_path):
    memory = _remembered(tmp_path)
    scenario = {"turns": [
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"],
         "message_info": {"tokens": {"input": 90_000, "output": 0}}},
    ]}
    sb, _fake, _h, run, aborted = _run(tmp_path, scenario, memory, context_watch_sec=0)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert not aborted and "context_watch_stop" not in run.turns[0]


# ─────────────────────────────────────────────────────────────────────────────
# 7. the remembered window goes to the running Kilo at once
# ─────────────────────────────────────────────────────────────────────────────

def _patches(fake) -> list:
    return [(r["query"].get("directory"), r["body"]) for r in fake.calls("PATCH")]


def test_an_overflow_below_kilo_s_window_is_handed_to_kilo_for_this_workspace(tmp_path):
    """Live, 7.6.2: `PATCH /config` sent with the session's directory resizes
    the model on the running server, and Kilo compacts by it inside the turn.
    The overflow at 98 777 is sent once, for agent-a's worktree, with the same
    limit the next round's spawn overlay would carry — and `.kilo/` is kept
    out of that worktree's git."""
    memory = tmp_path / "context-memory.json"
    scenario = ctm._overflow_scenario(ZAI, REFUSED_AT)
    sb, fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=0)
    assert run.state is tr.AgentState.STALLED
    ((directory, body),) = _patches(fake)
    wt = sb.ws("agent-a").path
    assert Path(directory).resolve() == Path(wt).resolve()
    limit = body["provider"]["kenary"]["models"]["agent-a:free"]["limit"]
    assert limit == cm.kilo_limit(REFUSED_AT, None, cm.DEFAULT_COMPACT_AT_PERCENT)
    import subprocess
    exclude = subprocess.run(["git", "-C", str(wt), "rev-parse", "--git-path", "info/exclude"],
                             capture_output=True, text=True, check=True).stdout.strip()
    path = Path(exclude) if Path(exclude).is_absolute() else Path(wt) / exclude
    assert ".kilo/" in path.read_text(encoding="utf-8").splitlines()


def test_a_remembered_window_is_handed_over_when_the_turn_starts(tmp_path):
    """A run that starts with the memory already below Kilo's window sends it
    before the first wait — the resumed glm of round 145 is sized at once."""
    memory = _remembered(tmp_path)
    scenario = {"turns": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb, fake, _h, run, _ = _run(tmp_path, scenario, memory, context_watch_sec=5)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert len(_patches(fake)) == 1


def test_nothing_is_handed_over_when_kilo_s_window_is_the_smaller(tmp_path):
    memory = _remembered(tmp_path, last_ok=200_000)
    scenario = ctm._overflow_scenario(ZAI, 120_000)
    _sb, fake, _h, _run_, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=0)
    assert _patches(fake) == []


def test_a_plan_s_cap_hands_nothing_over(tmp_path):
    memory = tmp_path / "context-memory.json"
    scenario = ctm._overflow_scenario(DEEPSEEK_FREE, REFUSED_AT)
    _sb, fake, _h, _run_, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=3,
                                    quota_patterns=_committed_quota_text())
    assert _patches(fake) == []
