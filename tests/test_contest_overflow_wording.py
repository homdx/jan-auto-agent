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

Round 148, the false positives of the two readings: the wording path must not
match a rate cap, a body, a timeout, an output cap, an attachment or a plan;
the inferred reading must not match an error any other path owns (Kilo shutting
down, a dropped socket, a key, a request the provider would not take); and the
watch stands down once Kilo has the window, never acts after its turn ended,
and keeps ``.kilo/kilo.jsonc`` out of the agent's tree.

The fake Kilo only — no live provider, no memory file outside ``tmp_path``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_contest_context_memory as ctm  # noqa: E402
import test_contest_policy as tcp  # noqa: E402
import test_contest_policy_file_asks as tcpf  # noqa: E402
import test_contest_runner as tr  # noqa: E402
from tools.contest import cli as contest_cli  # noqa: E402
from tools.contest import context_memory as cm  # noqa: E402
from tools.contest import policy as policy_mod  # noqa: E402
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


#: Round 148 part A1: the refusals the wording path read as an overflow that are
#: not about a size at all — a rate cap, a body, a timeout, an output cap, an
#: attachment, a plan. The message and the status it really came with.
NOT_A_SIZE_REFUSAL = [
    ("Request too large for gpt-4o on tokens per min (TPM): Limit 30000, Requested 45000", 429),
    ("Request too large for model on tokens per minute (TPM): Limit 10000, Requested 12000",
     413),
    ("Request entity too large", 413),
    ("Request body exceeds the maximum size of 32MB", 413),
    ("Request timed out: context deadline exceeded", 504),
    ("context deadline exceeded", 400),
    ("input queue exceeded", 529),
    ("Request exceeded the time limit", 400),
    ("max_tokens exceeds the model's maximum output tokens", 400),
    ("message exceeds max image size", 400),
    ("input exceeds 20 images", 400),
    ("exceeded your token budget", 400),
    ("Your error message is too large to display", 400),
]


@pytest.mark.parametrize("message,status", NOT_A_SIZE_REFUSAL)
def test_a_refusal_that_is_not_about_a_size_is_not_read_as_one(message, status):
    """Round 148 part A1: the wording path does not read a status, so a rate cap
    or a timeout that carries the size words is the error it always was, never a
    window. ``_is_overflow`` is the runner's own reading too, so both must agree."""
    assert not _is_overflow({"name": "APIError", "data": {"message": message,
                                                           "statusCode": status}},
                            _committed_quota_re())
    assert not _is_overflow(message, _committed_quota_re())


def test_a_size_refusal_that_names_a_file_is_still_an_overflow():
    """A veto must be a class, not a word: a real overflow can say the file the
    model read pushed it past the wall."""
    quota = _committed_quota_re()
    assert _is_overflow({"name": "APIError", "data": {
        "message": "prompt is too long: the file pushed it past 131072 tokens",
        "statusCode": 400}}, quota)
    # an attachment cap is vetoed on purpose: a compact cannot fix it
    assert not _is_overflow({"name": "APIError", "data": {
        "message": "input exceeds 20 images", "statusCode": 400}}, quota)
    # and the rate cap above stays a rate cap with the file in it too
    assert not _is_overflow({"name": "APIError", "data": {
        "message": "the file you sent put the request over tokens per minute (TPM)",
        "statusCode": 413}}, quota)


def test_a_size_word_the_wording_path_declined_vetoes_the_inference_too():
    """A1's last row reaches the inferred reading, which never asks about words:
    a message the wording path found size words in and declined is the response's
    size, not the session's. The inference stays for the message that says
    nothing about one at all."""
    quota = _committed_quota_re()
    full = int(FULL_REFUSAL_SHARE * DECLARED) + 1
    display = {"name": "APIError", "data": {"message": "Your error message is too large to display",
                                            "statusCode": 400}}
    assert not _is_overflow(display, quota)
    assert not _is_full_refusal(display, REFUSED_AT, DECLARED, quota)
    # the wordless refusal this reading exists for is unaffected
    assert _is_full_refusal(BARE_400, REFUSED_AT, DECLARED, quota)
    assert _is_full_refusal(BARE_400, full, DECLARED, quota)


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
    # 413 is literally a size status, and an API error is the caller of it
    assert _is_full_refusal(
        {"name": "APIError", "data": {"message": "x", "statusCode": 413}}, full, DECLARED)


@pytest.mark.parametrize("error", [
    {"data": {"message": "x"}},                                    # no status at all
    {"data": {"message": "x", "statusCode": 413}},                 # no name: not an API error
    {"name": "APIError", "data": {"message": "x", "statusCode": 422}},   # a schema
    {"name": "APIError", "data": {"message": "x", "statusCode": 500}},
    {"name": "Error", "data": {"message": "x", "statusCode": 400}},       # not an API error
])
def test_a_refusal_without_a_status_or_an_api_error_name_is_not_one(error):
    """Round 148: the reading is an inference, so it needs the provider's own
    refusal — a status it answers a size with, and a name that says the
    provider refused it. A payload with no status at all, or one that is not an
    API error, is the request or its shape, not its size."""
    assert not _is_full_refusal(error, int(FULL_REFUSAL_SHARE * DECLARED) + 1, DECLARED)


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


#: Round 148: the payloads a refusal at 81 % of the window must not be read as an
#: overflow with — Kilo shutting down, a dropped socket, a key, a request the
#: provider would not take at all.
ROUND_148_REFUSALS = [
    {"name": "MessageAbortedError", "data": {"message": "The message was aborted"}},
    {"name": "APIError", "data": {"message": "read ECONNRESET", "statusCode": 400,
                                  "metadata": {"code": "ECONNRESET"}}},
    {"name": "APIError", "data": {"message": "read ECONNRESET", "statusCode": 400,
                                  "isRetryable": True}},
    {"name": "Error", "data": {"message": "fetch failed"}},
    {"name": "Error", "data": {"message": "socket hang up"}},
    {"name": "ProviderAuthError", "data": {"message": "invalid token", "statusCode": 401}},
    {"name": "APIError", "data": {"message": "Unexpected end of JSON input"}},
    {"name": "APIError", "data": {"message": "Invalid tool call arguments"}},
    {"name": "APIError", "data": {"message": "blocked by content policy"}},
    {"name": "APIError", "data": {"message": "Invalid schema for function 'bash'",
                                  "statusCode": 422}},
]


@pytest.mark.parametrize("error", ROUND_148_REFUSALS)
def test_a_refusal_the_runner_does_not_own_is_not_inferred_as_a_full_one(error):
    """Round 148: 80 000 of a 98 777 window is a session full enough to be
    refused for its size, but only by a provider that refused its *size*. An
    inferred reading that took any of these would record one, PATCH a smaller
    window into Kilo, and compact a session that is not full at all."""
    assert not _is_full_refusal(error, 80_000, REFUSED_AT, _committed_quota_re())


@pytest.mark.parametrize("message,status", NOT_A_SIZE_REFUSAL)
def test_the_a1_refusals_are_not_overflows_in_a_run(tmp_path, message, status):
    """Item 1's second half, through the run: the same words land on a session
    at 75 % of a window, and the run ends on its own path — here ERROR, since
    none of them is a quota, a rate or a retry — with nothing remembered."""
    memory = tmp_path / "context-memory.json"
    error = {"name": "APIError", "data": {"message": message, "statusCode": status}}
    scenario = ctm._overflow_scenario(error, REFUSED_AT)
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory,
                                  max_continues_per_attempt=0, max_error_retries=0)
    assert run.state is tr.AgentState.ERROR
    assert "context overflow" not in run.last_error
    assert cm.load(memory) == []


# A wording the overflow path does match, so only the run can keep it out: a
# 429 and a 5xx are round 146's rate path, and an ``isRetryable`` refusal is
# round 63's retry. Neither sizes the model.
@pytest.mark.parametrize("status,retryable", [(429, False), (503, False), (400, True)],
                         ids=["429", "5xx", "retryable"])
def test_a_status_a_rate_or_a_retry_with_size_words_is_not_an_overflow(
        tmp_path, status, retryable):
    """Item 2: the wording reads as a size refusal, and the run must not either,
    no matter how full the session is."""
    error = {"name": "APIError", "data": {"message": "prompt exceeds max length",
                                          "statusCode": status, "isRetryable": retryable}}
    assert _is_overflow(error["data"]) is True, error["data"]
    memory = tmp_path / "context-memory.json"
    scenario = ctm._overflow_scenario(error, REFUSED_AT)
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory,
                                  max_continues_per_attempt=0, max_error_retries=0)
    assert run.state is tr.AgentState.ERROR
    assert "context overflow" not in run.last_error
    assert cm.load(memory) == []


def test_a_runner_unsent_abort_at_a_full_session_is_resumable(tmp_path):
    """Item 4's second half: at 81 % of its window a ``MessageAbortedError`` is
    Kilo's own interruption, round 87's path — the run resumes and the session
    was not the wall."""
    memory = tmp_path / "context-memory.json"
    aborted = {"name": "MessageAbortedError", "data": {"message": "The message was aborted"}}
    scenario = ctm._overflow_scenario(aborted, int(0.81 * DECLARED))
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=3)
    assert run.state is tr.AgentState.ERROR
    assert run.resumable is True
    assert cm.load(memory) == []


def test_a_worded_refusal_at_seventy_percent_is_remembered(tmp_path):
    """Item 5's worded half: at 70 % of the window, past the share the inferred
    reading waits for, a worded 400 is the window and it is remembered."""
    memory = tmp_path / "context-memory.json"
    scenario = ctm._overflow_scenario(ZAI, int(0.70 * DECLARED))
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=0)
    assert run.state is tr.AgentState.STALLED
    (record,) = cm.load(memory)
    assert record.last_ok == int(0.70 * DECLARED)


# ─────────────────────────────────────────────────────────────────────────────
# 3. the run: remembered and compacted, not ERROR
# ─────────────────────────────────────────────────────────────────────────────

def _declared(config, limit=DECLARED):
    """*config* whose agent carries Kilo's declared window, as intake puts it."""
    return replace(config, agents=tuple(replace(a, context_limit=limit) for a in config.agents))


def _run(tmp_path, scenario, memory, **over):
    config = _declared(ctm._config(tmp_path, memory=memory, **over))
    return tr._run_one(tmp_path, scenario, config)


def test_a_refusal_with_words_is_remembered_as_an_overflow(tmp_path):
    """Round 145 replayed: 98 777 went through, the next request was refused in
    words about a size. The run ends STALLED `context overflow` (no continue
    left), not ERROR, and the memory holds `last_ok = 98 777` for the model."""
    memory = tmp_path / "context-memory.json"
    scenario = ctm._overflow_scenario(ZAI, REFUSED_AT)
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=0)
    assert run.state is tr.AgentState.STALLED
    assert run.last_error == "context overflow"
    (record,) = cm.load(memory)
    assert (record.provider, record.model) == ("kenary", "agent-a:free")
    assert record.limit is None and record.last_ok == REFUSED_AT
    assert cm.smallest_size([record], "kenary", "agent-a:free") == REFUSED_AT


def test_a_refusal_without_words_is_compacted_but_remembers_nothing(tmp_path):
    """Round 148: the same refusal with no words about a size is still an
    overflow of the session that holds 98 777 — the work is compacted and the
    run goes on — but an inferred reading names no size, so nothing is written
    to the memory and no window is handed to Kilo. The record would have sized
    every model of the next rounds by a number the provider never said."""
    memory = tmp_path / "context-memory.json"
    scenario = {"summary_tokens": 3_000, "turns": [
        {"events": ["busy", "idle"], "message_info": {"tokens": {"input": REFUSED_AT,
                                                                   "output": 0}}},
        {"events": ["busy"], "error": BARE_400},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=3)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert any(p.endswith("/summarize") for p, _ in ctm._posts(fake))
    assert cm.load(memory) == []
    assert fake.calls("PATCH") == []


def _big_read_turn(last_ok_input, grew_tokens, dirty=False):
    """A reply that went through at *last_ok_input* whose tool results added
    *grew_tokens* more — glm reading a batch in one step (round 145).

    ``dirty`` leaves the read on the worktree uncommitted, so the run keeps
    going after the read instead of harvesting it: a batch that overflows does
    not say "I am done", it only fills the context.
    """
    turn = {"events": ["busy", "idle"],
            "message_info": {"tokens": {"input": last_ok_input, "output": 0}},
            "tool_parts": [{"tool": "read", "status": "completed",
                            "output": "x" * (grew_tokens * runner_mod.SUMMARY_CHARS_PER_TOKEN)}]}
    if dirty:
        turn["on_prompt"] = tr.work_edit_no_commit
    return turn


def test_a_batch_read_in_one_step_is_an_overflow_and_keeps_the_last_ok(tmp_path):
    """Round 148 part A3: a fresh session at 20 000 that reads 60 000 in one step
    was refused while the last reply was still under the floor, so nothing was
    remembered for it and the next session read the same batch into the same
    wall. The floor is measured on the request — the reply plus what its tools
    added — and the record keeps the reply, which is the only number the
    provider proved."""
    memory = tmp_path / "context-memory.json"
    scenario = {"turns": [
        _big_read_turn(20_000, 60_000),
        {"events": ["busy"], "error": ZAI},
    ]}
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=0)
    assert run.state is tr.AgentState.STALLED
    assert run.last_error.startswith("context overflow")
    (record,) = cm.load(memory)
    assert record.last_ok == 20_000
    assert record.grew == 60_000
    assert cm.size_of(record, 32_000) is None, "a loose record sizes nothing"


def test_a_loop_of_the_same_refusal_ends_the_attempt(tmp_path):
    """The cost, stated: nothing sizes the model between two such refusals, so
    the run keeps meeting the wall until the attempt's own budgets run out. It
    ends there, it does not loop, and every record is the loose one — each
    holds `last_ok` 20 000, which sizes nothing for the next rounds."""
    memory = tmp_path / "context-memory.json"
    scenario = {"summary_tokens": 3_000, "turns": [
        _big_read_turn(20_000, 60_000, dirty=True),
        {"events": ["busy"], "error": ZAI},
        _big_read_turn(20_000, 60_000, dirty=True),
        {"events": ["busy"], "error": ZAI},
        _big_read_turn(20_000, 60_000, dirty=True),
        {"events": ["busy"], "error": ZAI},
    ]}
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=5,
                                  max_sessions_per_attempt=5, max_rework=0)
    assert run.state is tr.AgentState.STALLED
    assert run.last_error == "context overflow"
    records = cm.load(memory)
    assert [record.last_ok for record in records] == [20_000, 20_000, 20_000]
    assert all(record.grew == 60_000 for record in records)
    assert all(cm.size_of(record, 32_000) is None for record in records)


def test_an_old_spelling_counts_for_the_repeat_guard(tmp_path):
    """Round 148 part A3: a `ContextOverflowError` at 98 777, then zai's wording
    at 40 000 after the compact. On HEAD the second was accepted and written,
    and the remembered window dropped from 98 777 to 40 000."""
    memory = tmp_path / "context-memory.json"
    scenario = {"summary_tokens": 3_000, "turns": [
        {"events": ["busy", "idle"], "message_info": {"tokens": {"input": REFUSED_AT,
                                                                   "output": 0}}},
        {"events": ["busy"], "error": ctm.KENARY_OVERFLOW},
        {"events": ["busy", "idle"], "message_info": {"tokens": {"input": 40_000, "output": 0}}},
        {"events": ["busy"], "error": ZAI},
    ]}
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=3,
                                  max_error_retries=0)
    assert run.state is tr.AgentState.ERROR
    (record,) = cm.load(memory)
    assert record.last_ok == REFUSED_AT


def test_a_legitimate_second_overflow_is_not_a_repeat(tmp_path):
    """Round 148 part A3: the compacted session at 40 000 reads 60 000 and is
    refused — 100 000 against a 98 777 wall, past half of the first one. On HEAD
    the guard compared the reply alone (40 000) and rejected it."""
    memory = tmp_path / "context-memory.json"
    scenario = {"summary_tokens": 3_000, "turns": [
        {"events": ["busy", "idle"], "message_info": {"tokens": {"input": REFUSED_AT,
                                                                   "output": 0}}},
        {"events": ["busy"], "error": ctm.KENARY_OVERFLOW},
        _big_read_turn(40_000, 60_000),
        {"events": ["busy"], "error": ZAI},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=3)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert [record.last_ok for record in cm.load(memory)] == [REFUSED_AT, 40_000]


def test_a_refusal_of_the_first_request_is_not_an_overflow(tmp_path):
    """Round 148 part A3: nothing went through yet, whatever the floor — a
    refusal of the session's first request cannot have filled a context."""
    memory = tmp_path / "context-memory.json"
    scenario = {"turns": [{"events": ["busy"], "error": ZAI}]}
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=0,
                                  context_min_window=0)
    assert run.state is tr.AgentState.ERROR
    assert cm.load(memory) == []


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


@pytest.mark.parametrize("last_ok,grew,floor,declared,want", [
    (81_311, 42_588, 32_000, 131_072, 81_311),  # live glm: loose, but near the wall
    (17_382, 269_086, 32_000, 131_072, None),   # KC-73's agnes: far from the wall
    (14_179, 134_000, 32_000, 131_072, None),   # KC-73's glm-4-7
    (25_000, 100, 32_000, 131_072, None),       # tight, but under the floor
    (17_382, 269_086, 0, None, None),           # no window at all: KC-73 as it was
    (247_828, 3_000, 0, None, 247_828),         # floor off, tight: sized as before
    (33_000, 200_000, 32_000, 262_144, None),   # 149's bug: loose, far below the wall
    (81_311, 42_588, 32_000, None, None),       # loose with no window: nothing
])
def test_the_window_floor_decides_which_last_ok_sizes_a_model(
        last_ok, grew, floor, declared, want):
    record = ctm._record(limit=None, last_ok=last_ok, grew=grew, prompt=None)
    assert cm.size_of(record, floor, declared=declared) == want


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

def test_the_watch_stops_a_turn_at_80_percent_of_a_remembered_size(tmp_path, monkeypatch):
    """Kilo was told 131 072 and compacts only there; the memory says 98 777.
    A turn that grows to 90 000 (91 %) without asking anything is stopped by
    the watch, and the next prompt is compacted first — round 145's 45-minute
    turn of reads, caught before the wall.

    Round 148: the watch is the fallback for a window Kilo was not given, so
    the push is not there to stand down on — a backend without
    `set_model_limit` is the shape."""
    monkeypatch.delattr("tools.contest.backend.KiloBackend.set_model_limit")
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


def test_the_watch_is_off_when_the_window_went_to_kilo(tmp_path):
    """Round 148: Kilo now takes the remembered window at the first prompt, and
    it compacts by it at the same fill the watch would fire at — `kilo_limit`'s
    `input - reserved` and `compact_at_percent` are the same 80 %. Both firing
    at once would let the watch's abort win and throw the step away."""
    memory = _remembered(tmp_path)
    scenario = {"turns": [
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"],
         "message_info": {"tokens": {"input": 90_000, "output": 0}}},
    ]}
    sb, fake, _h, run, aborted = _run(tmp_path, scenario, memory, context_watch_sec=0.2)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert len(_patches(fake)) == 1, "the window was handed to Kilo"
    assert not aborted and "context_watch_stop" not in run.turns[0]


def test_a_push_that_fails_leaves_the_watch_armed(tmp_path, monkeypatch):
    """Round 148: a `PATCH /config` that fails is the window not being handed
    over, so the watch still stands and is the fallback."""
    def no_config(self, provider_id, model_id, limit):
        raise RuntimeError("no config for this workspace")
    monkeypatch.setattr("tools.contest.backend.KiloBackend.set_model_limit", no_config)
    memory = _remembered(tmp_path)
    scenario = {"abort_idles": True, "summary_tokens": 3_000, "turns": [
        {"events": ["busy"], "idle": False,
         "message_info": {"tokens": {"input": 90_000, "output": 0}}},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, aborted = _run(tmp_path, scenario, memory, context_watch_sec=0.2)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert fake.calls("PATCH") == []
    assert aborted and run.turns[0]["context_watch_stop"] == 91.1


def test_a_watch_that_reads_after_the_turn_ended_does_nothing(tmp_path, monkeypatch):
    """Round 148: the read is an HTTP call and can return after the turn is
    over. Its actions must not land on the next turn — no flags, no abort, no
    `context_watch_stop` written into a turn that is already recorded."""
    late = threading.Event()
    real_messages = tr.KiloBackend.messages

    def slow_messages(self, session):
        # only the watch's own read is late: it is the one that outlives the turn
        if threading.current_thread().name.startswith("context-watch-"):
            late.wait(timeout=10)
        return real_messages(self, session)
    monkeypatch.delattr("tools.contest.backend.KiloBackend.set_model_limit")
    monkeypatch.setattr(tr.KiloBackend, "messages", slow_messages)
    memory = _remembered(tmp_path)
    scenario = {"turns": [
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"],
         "message_info": {"tokens": {"input": 90_000, "output": 0}}},
    ]}
    sb, fake, _h, run, aborted = _run(tmp_path, scenario, memory, context_watch_sec=0.2)
    late.set()
    tr._assert_ready(run, sb.ws("agent-a"))
    assert not aborted
    assert all("context_watch_stop" not in turn for turn in run.turns)


# ─────────────────────────────────────────────────────────────────────────────
# 7. the remembered window goes to the running Kilo at once
# ─────────────────────────────────────────────────────────────────────────────

def _patches(fake) -> list:
    return [(r["query"].get("directory"), r["body"]) for r in fake.calls("PATCH")]


def test_an_overflow_below_kilo_s_window_is_handed_to_kilo_before_the_next_prompt(tmp_path):
    """Live, 7.6.2: `PATCH /config` sent with the session's directory resizes
    the model on the running server, and Kilo compacts by it inside the turn.
    It also reloads the workspace's instance and ends its `/event` stream — so
    it goes between turns, right before the next prompt, and the backend
    reconnects the stream: the continue after it is heard and the run ends
    READY well inside the silence window (a dead stream would sit it out).
    Sent once, for agent-a's worktree, with `kilo_limit`'s numbers, and
    `.kilo/` kept out of that worktree's git."""
    import subprocess
    import time as _time
    memory = tmp_path / "context-memory.json"
    scenario = {"summary_tokens": 3_000, "turns": [
        {"events": ["busy", "idle"], "message_info": {"tokens": {"input": REFUSED_AT,
                                                                  "output": 0}}},
        {"events": ["busy"], "error": ZAI},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    started = _time.monotonic()
    sb, fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=3)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert _time.monotonic() - started < 40, "the stream after the reload was heard"
    ((directory, body),) = _patches(fake)
    wt = sb.ws("agent-a").path
    assert Path(directory).resolve() == Path(wt).resolve()
    limit = body["provider"]["kenary"]["models"]["agent-a:free"]["limit"]
    assert limit == cm.kilo_limit(REFUSED_AT, None, cm.DEFAULT_COMPACT_AT_PERCENT)
    # the patch is between turns: after the overflow's compact, before the continue
    calls = [(r["method"], r["path"]) for r in fake.calls()]
    patch_at = calls.index(("PATCH", "/config"))
    summarize_at = max(i for i, c in enumerate(calls) if c[1].endswith("/summarize"))
    continue_at = max(i for i, c in enumerate(calls) if c[1].endswith("/prompt_async"))
    assert summarize_at < patch_at < continue_at
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


def test_the_window_is_handed_over_when_the_watch_is_off(tmp_path):
    """Round 148 part B1: the push runs at PROMPTED, independent of the watch —
    `context_watch_sec = 0` stands the fallback down and the window still goes
    to Kilo at the first prompt."""
    memory = _remembered(tmp_path)
    scenario = {"turns": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb, fake, _h, run, _ = _run(tmp_path, scenario, memory, context_watch_sec=0)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert len(_patches(fake)) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 8. `.kilo/` does not outlive a turn
# ─────────────────────────────────────────────────────────────────────────────

def test_a_tracked_kilo_jsonc_keeps_the_window_out_of_the_agent_s_tree(tmp_path, caplog):
    """Round 148 part B4: `info/exclude` does nothing when the checkout already
    **tracks** `.kilo/kilo.jsonc` — the patch would rewrite a tracked file and
    land in the agent's diff. The push is skipped with a warning, and B1's rule
    leaves the watch armed because the push never happened."""
    import logging
    caplog.set_level(logging.WARNING, logger="tools.contest.backend")
    caplog.set_level(logging.INFO, logger="tools.contest.runner")
    memory = _remembered(tmp_path)
    scenario = {"abort_idles": True, "summary_tokens": 3_000, "turns": [
        {"events": ["busy"], "idle": False,
         "message_info": {"tokens": {"input": 90_000, "output": 0}}},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}

    def track_it(path):
        wt = Path(path)
        (wt / ".kilo").mkdir(parents=True, exist_ok=True)
        (wt / ".kilo" / "kilo.jsonc").write_text('{"provider": {}}\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(wt), "add", ".kilo/kilo.jsonc"], check=True)
        subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", "tracked"], check=True)

    sb, fake, _h, run, aborted = tr._run_one(
        tmp_path, scenario,
        _declared(ctm._config(tmp_path, memory=memory, context_watch_sec=0.2)),
        prepare=track_it)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert fake.calls("PATCH") == []
    assert (sb.ws("agent-a").path / ".kilo" / "kilo.jsonc").read_text(encoding="utf-8") \
        == '{"provider": {}}\n'
    assert any("tracks .kilo" in record.getMessage() for record in caplog.records)
    # the push never happened, so the watch stands and is what stops the turn
    assert aborted
    assert any("inside the turn" in record.getMessage() and "91.1%" in record.getMessage()
               for record in caplog.records)


def test_an_edit_of_kilos_own_project_file_goes_to_the_gate(tmp_path):
    """Round 148 part B4: the agent's own edit of `.kilo/kilo.jsonc` is not
    geometry's call. That file is where `PATCH /config` writes the window, and
    an edit to it is an edit to what Kilo allows for the rest of the round —
    `policy._kilo_config` sends it to the gate, never `once`."""
    worktree = tcpf._worktree(tmp_path)
    gate = tcp.StubGate(json.dumps(tcp.ALLOW))
    policy = policy_mod.Policy(tcp.make_config(), completion_fn=gate,
                               clock=tcp.FakeClock())

    decision = tcp.decide(policy, tcpf.file_event(worktree, ".kilo/kilo.jsonc"), worktree)

    assert decision.layer == "gate"
    assert len(gate.calls) == 1


def test_a_stale_project_file_is_gone_before_the_first_prompt(tmp_path):
    """Item 15's second half: the drop is not only on the way out. A `.kilo/`
    left in the worktree by an earlier attempt of the same model is gone before
    the resumed agent's server is spawned, so its very first prompt is not
    carrying a window the model no longer has."""
    seen = []

    def first_prompt(directory, text):
        seen.append((Path(directory) / ".kilo" / "kilo.jsonc").exists())
        tr.work_ready(directory, text)

    def seed(worktree):
        (Path(worktree) / ".kilo").mkdir(parents=True, exist_ok=True)
        (Path(worktree) / ".kilo" / "kilo.jsonc").write_text(
            '{"provider": {"timeout": {"request": 600}}}\n', encoding="utf-8")

    scenario = {"turns": [{"on_prompt": first_prompt, "events": ["busy", "idle"]}]}
    sb, _fake, _h, run, _ = tr._run_one(tmp_path, scenario, _declared(ctm._config(tmp_path)),
                                        prepare=seed)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert seen == [False]


def test_the_project_file_a_patch_wrote_does_not_outlive_the_run(tmp_path):
    """Round 148 part B4: Kilo keeps the patch in the workspace, so a resumed
    agent's server finds a window the model no longer has. The run deletes its
    own on the way out."""
    memory = tmp_path / "context-memory.json"
    scenario = {"summary_tokens": 3_000, "turns": [
        {"events": ["busy", "idle"], "message_info": {"tokens": {"input": REFUSED_AT,
                                                                   "output": 0}}},
        {"events": ["busy"], "error": ZAI},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=3)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert len(_patches(fake)) == 1
    assert not (sb.ws("agent-a").path / ".kilo" / "kilo.jsonc").exists()


def test_an_untracked_project_file_is_dropped_and_a_tracked_one_is_kept(tmp_path):
    """`drop_stale_kilo_file` deletes only what the checkout does not track: Kilo
    marks the file `configProtected`, so a tracked one is the agent's."""
    import tools.contest.backend as backend_mod
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "t"], check=True)
    (wt / ".kilo").mkdir()
    (wt / ".kilo" / "kilo.jsonc").write_text("{}\n", encoding="utf-8")
    assert backend_mod.tracked_kilo_files(wt) == []
    assert backend_mod.drop_stale_kilo_file(wt) is True
    assert not (wt / ".kilo" / "kilo.jsonc").exists()
    (wt / ".kilo" / "kilo.jsonc").write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", ".kilo/kilo.jsonc"], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", "keep"], check=True)
    assert backend_mod.tracked_kilo_files(wt) == [".kilo/kilo.jsonc"]
    assert backend_mod.drop_stale_kilo_file(wt) is False
    assert (wt / ".kilo" / "kilo.jsonc").exists()


def test_a_directory_that_is_not_a_checkout_is_left_alone(tmp_path):
    """No `git` at all: nothing is written, nothing is deleted, nothing raises."""
    import tools.contest.backend as backend_mod
    d = tmp_path / "not-a-repo"
    (d / ".kilo").mkdir(parents=True)
    (d / ".kilo" / "kilo.jsonc").write_text("{}\n", encoding="utf-8")
    backend_mod._exclude_kilo_dir(d)
    assert backend_mod.tracked_kilo_files(d) == []
    assert backend_mod.drop_stale_kilo_file(d) is False
    assert (d / ".kilo" / "kilo.jsonc").exists()


# ─────────────────────────────────────────────────────────────────────────────
# 9. the watch interval
# ─────────────────────────────────────────────────────────────────────────────

def test_the_watch_interval_is_read_fail_open():
    def cfg(value):
        return replace(tr.make_config(["agent-a"]), context_watch_sec=value)
    assert runner_mod._context_watch_sec(cfg(10)) == 10.0
    assert runner_mod._context_watch_sec(cfg(0)) == 0.0
    assert runner_mod._context_watch_sec(cfg(-1)) == 0.0
    assert runner_mod._context_watch_sec(cfg(float("inf"))) == 0.0
    assert runner_mod._context_watch_sec(cfg(float("nan"))) == 0.0
    assert runner_mod._context_watch_sec(cfg("soon")) == 0.0
    assert runner_mod._context_watch_sec(cfg(None)) == 0.0


def test_the_ini_watches_every_ten_seconds_and_a_code_config_does_not(tmp_path):
    """Round 148 part B5: the ini default is 10, the dataclass default is off —
    a config built in code is today's, a loaded round arms the watch."""
    from tools.contest.roster import load_roster
    ini = tmp_path / "contest.ini"
    ini.write_text("[contest]\n\n[contest.agent.a]\nmodel = p/m\n", encoding="utf-8")
    config = load_roster(ini)
    assert config.context_watch_sec == 10.0
    assert runner_mod._context_watch_sec(config) == 10.0
    assert tr.make_config(["agent-a"]).context_watch_sec == 0.0
    assert "context_watch_sec            = 10" in (REPO_ROOT / "contest.ini").read_text(
        encoding="utf-8")
