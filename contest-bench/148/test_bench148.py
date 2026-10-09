"""contest-bench/148 — ticket 148's acceptance, run from inside an entry's checkout (cwd).

Behaviour only, through the runner's public helpers and the fake Kilo; no
entry's own tests are trusted. Run:  cd <entry> && python3 -m pytest -p no:cacheprovider -n 0 <this file>
"""

from __future__ import annotations

import math
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path.cwd()
for _p in (str(ROOT), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_contest_context_memory as ctm  # noqa: E402
import test_contest_runner as tr  # noqa: E402
from tools.contest import backend as backend_mod  # noqa: E402
from tools.contest import context_memory as cm  # noqa: E402
from tools.contest import runner as rm  # noqa: E402

DECLARED = 131_072
WALL = 98_777
ZAI = {"name": "APIError", "data": {"message": "Prompt exceeds max length",
                                    "statusCode": 400, "isRetryable": False}}
OLD = {"name": "ContextOverflowError", "data": {"message": "context overflow"}}
BARE_400 = {"name": "APIError", "data": {"message": "Bad Request", "statusCode": 400,
                                         "isRetryable": False}}


def _api(message, status=400, **extra):
    data = {"message": message, "isRetryable": False, **extra}
    if status is not None:
        data["statusCode"] = status
    return {"name": "APIError", "data": data}


A1 = [
    ("Request too large for gpt-4o on tokens per min (TPM): Limit 30000, Requested 45000", 429),
    ("Request too large for model llama in organization org on tokens per minute (TPM)", 413),
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

A2 = {
    "aborted": {"name": "MessageAbortedError", "data": {"message": "The operation was aborted."}},
    "econnreset": {"name": "APIError", "data": {"message": "read ECONNRESET", "statusCode": 400,
                                                "isRetryable": False,
                                                "metadata": {"code": "ECONNRESET"}}},
    "fetch-failed": _api("fetch failed", status=None),
    "auth": {"name": "ProviderAuthError", "data": {"message": "invalid token", "statusCode": 400}},
    "schema-422": _api("Invalid schema for function 'read'", status=422),
    "nostatus": {"data": {"message": "x"}},
}


def _config(tmp_path, memory, limit=DECLARED, **over):
    config = ctm._config(tmp_path, memory=memory, **over)
    return replace(config, agents=tuple(replace(a, context_limit=limit) for a in config.agents))


def _run(tmp_path, scenario, memory, prepare=None, **over):
    return tr._run_one(tmp_path, scenario, _config(tmp_path, memory, **over), prepare=prepare)


def _records(memory):
    return cm.load(memory) if memory.exists() else []


def _summarizes(fake):
    return [r for r in fake.calls("POST") if r["path"].endswith("/summarize")]


def _patches(fake):
    return list(fake.calls("PATCH"))


def _reply(tokens, grew_tokens=0):
    turn = {"events": ["busy", "idle"], "message_info": {"tokens": {"input": tokens, "output": 0}}}
    if grew_tokens:
        turn["tool_parts"] = [{"tool": "read", "status": "completed", "input": {},
                               "output": "x" * (grew_tokens * rm.SUMMARY_CHARS_PER_TOKEN)}]
    return turn


# ── A1 ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message,status", A1, ids=[m[:30] for m, _ in A1])
def test_a1_not_a_size_refusal(message, status):
    assert not rm._is_overflow(_api(message, status))


@pytest.mark.parametrize("message,status", A1, ids=[m[:30] for m, _ in A1])
def test_a1_run_writes_nothing_and_compacts_nothing(tmp_path, message, status):
    memory = tmp_path / "m.json"
    _sb, fake, _h, run, _ = _run(tmp_path, ctm._overflow_scenario(_api(message, status), 50_000),
                                 memory, max_continues_per_attempt=3)
    assert _records(memory) == []
    assert not _summarizes(fake)
    assert not run.last_error.startswith("context overflow")


@pytest.mark.parametrize("error", [_api("prompt is too long", 429), _api("prompt is too long", 503),
                                   {"name": "APIError", "data": {"message": "prompt is too long",
                                                                 "statusCode": 400,
                                                                 "isRetryable": True}}],
                         ids=["429", "503", "retryable"])
def test_a1_status_and_retry_gate_the_words(tmp_path, error):
    memory = tmp_path / "m.json"
    _sb, fake, _h, _run_, _ = _run(tmp_path, ctm._overflow_scenario(error, 90_000), memory,
                                   max_continues_per_attempt=0)
    assert _records(memory) == []


@pytest.mark.parametrize("message", ["Prompt exceeds max length",
                                     "prompt is too long: the file pushed it past 131072 tokens",
                                     "This model's input exceeds the context window",
                                     "Input is too long for requested model"])
def test_a1_positives_still_overflow(message):
    assert rm._is_overflow(_api(message))


def test_a1_wallet_left_to_quota_patterns():
    assert rm._NOT_SIZE_RE.search("your wallet balance, recharge now") is None or \
        "wallet" not in rm._NOT_SIZE_RE.pattern


# ── A2 ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", list(A2))
def test_a2_not_a_full_refusal(key):
    assert not rm._is_full_refusal(A2[key], 80_000, WALL)


def test_a2_bare_400_and_413_still_full_refusals():
    assert rm._is_full_refusal(BARE_400, 80_000, WALL)
    assert rm._is_full_refusal(_api("x", 413), 80_000, WALL)


def test_a2_inferred_reading_compacts_but_teaches_nothing(tmp_path):
    memory = tmp_path / "m.json"
    scenario = {"summary_tokens": 3_000, "turns": [
        _reply(92_000), {"events": ["busy"], "error": BARE_400},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb, fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=3)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert _summarizes(fake)
    assert _records(memory) == []
    assert _patches(fake) == []


def test_a2_worded_refusal_at_70_is_remembered(tmp_path):
    memory = tmp_path / "m.json"
    scenario = {"summary_tokens": 3_000, "turns": [
        _reply(92_000), {"events": ["busy"], "error": ZAI},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=3)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert [r.last_ok for r in _records(memory)] == [92_000]


# ── A3 ──────────────────────────────────────────────────────────────────────

def test_a3_old_spelling_counts_for_the_repeat_guard(tmp_path):
    memory = tmp_path / "m.json"
    scenario = {"summary_tokens": 3_000, "turns": [
        _reply(WALL), {"events": ["busy"], "error": OLD},
        _reply(40_000), {"events": ["busy"], "error": ZAI}]}
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=5)
    assert 40_000 not in [r.last_ok for r in _records(memory)]
    assert run.state is tr.AgentState.ERROR


def test_a3_fresh_session_batch_read_is_an_overflow(tmp_path):
    memory = tmp_path / "m.json"
    scenario = {"turns": [_reply(20_000, grew_tokens=60_000), {"events": ["busy"], "error": ZAI}]}
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=0,
                                  context_min_window=32_000)
    assert run.last_error.startswith("context overflow")
    assert [r.last_ok for r in _records(memory)] == [20_000]


def test_a3_legitimate_second_overflow(tmp_path):
    memory = tmp_path / "m.json"
    scenario = {"summary_tokens": 3_000, "turns": [
        _reply(WALL), {"events": ["busy"], "error": ZAI},
        _reply(40_000, grew_tokens=60_000), {"events": ["busy"], "error": ZAI},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb, fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=6)
    assert run.state is not tr.AgentState.ERROR, run.last_error
    assert len(_summarizes(fake)) >= 2


def test_a3_first_request_is_never_an_overflow(tmp_path):
    memory = tmp_path / "m.json"
    scenario = {"turns": [{"events": ["busy"], "error": ZAI}]}
    _sb, fake, _h, run, _ = _run(tmp_path, scenario, memory, max_continues_per_attempt=3,
                                 context_min_window=0)
    assert _records(memory) == []
    assert not _summarizes(fake)


# ── B ───────────────────────────────────────────────────────────────────────

def _remembered(tmp_path):
    return ctm._memory(tmp_path, limit=None, last_ok=WALL, prompt=None)


def test_b_push_without_watch(tmp_path):
    scenario = {"turns": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb, fake, _h, run, _ = _run(tmp_path, scenario, _remembered(tmp_path), context_watch_sec=0)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert len(_patches(fake)) == 1


def test_b1_no_watch_after_a_successful_push(tmp_path):
    scenario = {"turns": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"], "delay": 1.0,
                           "message_info": {"tokens": {"input": 90_000, "output": 0}}}]}
    sb, fake, _h, run, aborted = _run(tmp_path, scenario, _remembered(tmp_path),
                                      context_watch_sec=0.2)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert len(_patches(fake)) == 1
    assert not aborted and "context_watch_stop" not in run.turns[0]


def _track_kilo(path):
    p = Path(path) / ".kilo"
    p.mkdir()
    (p / "kilo.jsonc").write_text("{}\n")
    subprocess.run(["git", "-C", path, "add", "-f", ".kilo/kilo.jsonc"], check=True)
    subprocess.run(["git", "-C", path, "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "track kilo"], check=True)


def test_b4_tracked_kilo_file_skips_the_push(tmp_path):
    scenario = {"turns": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb, fake, _h, _run_, _ = _run(tmp_path, scenario, _remembered(tmp_path),
                                  prepare=_track_kilo, context_watch_sec=5)
    assert _patches(fake) == []
    assert (Path(sb.ws("agent-a").path) / ".kilo/kilo.jsonc").read_text() == "{}\n"


def _untracked_kilo(path):
    p = Path(path) / ".kilo"
    p.mkdir()
    (p / "kilo.jsonc").write_text("{}\n")


def test_b4_untracked_kilo_file_is_gone_after_ready(tmp_path):
    scenario = {"turns": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb, _fake, _h, run, _ = _run(tmp_path, scenario, _remembered(tmp_path),
                                 prepare=_untracked_kilo)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert not (Path(sb.ws("agent-a").path) / ".kilo/kilo.jsonc").exists()


def test_b5_inf_turns_the_watch_off():
    class C:
        context_watch_sec = math.inf
    assert rm._context_watch_sec(C()) == 0


def test_b3_exclude_on_a_non_checkout(tmp_path):
    backend_mod._exclude_kilo_dir(str(tmp_path))
    assert list(tmp_path.iterdir()) == []
