"""Judge's acceptance suite for round 152, written from the ticket alone.

The overflow reading's wording gaps (xAI, images, TGI, 422/500), the repeat
guard's one unit, and a small window remembered through the round's
``context_limit_fallback``. Checked by behaviour only: `runner._is_overflow`,
`context_memory.size_of` and whole runs through
`tests/test_contest_runner._run_one`. No private helper of any entry is named.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/152/acceptance_152.py -n 0 -q
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_contest_context_memory as ctm  # noqa: E402
import test_contest_runner as tr  # noqa: E402
from tools.contest import context_memory as cm  # noqa: E402
from tools.contest import runner as runner_mod  # noqa: E402
from tools.contest.runner import _is_overflow  # noqa: E402

DECLARED = 131_072
REFUSED_AT = 98_777

XAI = ("This model's maximum prompt length is 131072 but the request contains "
       "150000 tokens.")
IMAGES = "Your prompt with 3 images exceeds the context window"
TGI = ("Input validation error: `inputs` tokens + `max_new_tokens` must be <= 4096. "
       "Given: 4000 `inputs` tokens and 500 `max_new_tokens`")
ZAI = {"name": "APIError", "data": {"message": "Prompt exceeds max length",
                                    "statusCode": 400, "isRetryable": False}}


def _err(message, status=400):
    return {"name": "APIError", "data": {"message": message, "statusCode": status,
                                         "isRetryable": False}}


def _run(tmp_path, scenario, declared=DECLARED, **over):
    memory = tmp_path / "context-memory.json"
    config = ctm._config(tmp_path, memory=memory, **over)
    config = replace(config, agents=tuple(replace(a, context_limit=declared)
                                          for a in config.agents))
    sb, fake, h, run, _ = tr._run_one(tmp_path, scenario, config)
    return run, memory


def _turn(last_ok, grew=0, dirty=True):
    turn = {"events": ["busy", "idle"],
            "message_info": {"tokens": {"input": last_ok, "output": 0}}}
    if grew:
        turn["tool_parts"] = [{"tool": "read", "status": "completed",
                               "output": "x" * (grew * runner_mod.SUMMARY_CHARS_PER_TOKEN)}]
    if dirty:
        turn["on_prompt"] = tr.work_edit_no_commit
    return turn


def _refused(error):
    return {"events": ["busy"], "error": error}


# ── W1: xAI ─────────────────────────────────────────────────────────────────

def test_w1_xai_wording_is_an_overflow():
    assert _is_overflow(_err(XAI)) is True


def test_w1b_maximum_input_length_with_a_number_is_an_overflow():
    assert _is_overflow(_err("maximum input length is 32768 tokens, got 40000")) is True


def test_w1n_maximum_prompt_length_setting_without_a_size_is_not():
    assert _is_overflow(_err("maximum prompt length must be a positive integer")) is False


@pytest.mark.parametrize("message,status", [
    ("maximum input size is 10 MB", 413),
    ("maximum input size is 10485760 bytes", 413),
    ("maximum input size is 10485760 bytes", 400),
])
def test_w1m_a_byte_limit_named_with_maximum_size_is_not_a_context(message, status):
    """xAI's pattern counts tokens; an upload cap counts bytes — a body, not
    the session, and remembered it would size the model by a byte count."""
    assert _is_overflow(_err(message, status)) is False


def test_w1r_xai_in_a_run_is_remembered(tmp_path):
    run, memory = _run(tmp_path, ctm._overflow_scenario(_err(XAI), REFUSED_AT),
                       max_continues_per_attempt=0)
    assert run.state is tr.AgentState.STALLED
    assert run.last_error.startswith("context overflow")
    (record,) = cm.load(memory)
    assert record.last_ok == REFUSED_AT


def test_w1g_a_second_xai_far_below_is_the_guard_s(tmp_path):
    scenario = {"summary_tokens": 3_000, "turns": [
        _turn(REFUSED_AT, dirty=False), _refused(_err(XAI)),
        _turn(40_000, dirty=False), _refused(_err(XAI)),
    ]}
    run, memory = _run(tmp_path, scenario, max_continues_per_attempt=5)
    assert run.state is tr.AgentState.ERROR
    assert len(cm.load(memory)) == 1


# ── W2: the images veto ─────────────────────────────────────────────────────

def test_w2_images_that_exceed_the_context_window_is_an_overflow():
    assert _is_overflow(_err(IMAGES)) is True


@pytest.mark.parametrize("message", [
    "image attachment too large",
    "token budget exceeded, retry later",
    "Too many images: at most 5 images exceed the allowed count",
    "monthly allowance exceeded",
])
def test_w2n_images_and_budgets_without_the_wall_stay_vetoed(message):
    assert _is_overflow(_err(message)) is False


def test_w2q_the_wall_does_not_lift_a_rate_or_quota_veto():
    assert _is_overflow(_err(
        "Rate limit exceeded: too many tokens per minute for this context window")) is False
    assert _is_overflow(_err("quota exceeded for the context window of this plan")) is False


def test_w2r_images_in_a_run_is_remembered(tmp_path):
    run, memory = _run(tmp_path, ctm._overflow_scenario(_err(IMAGES), REFUSED_AT),
                       max_continues_per_attempt=0)
    assert run.state is tr.AgentState.STALLED
    assert len(cm.load(memory)) == 1


# ── W3 / W4: TGI and the statuses ──────────────────────────────────────────

def test_w3_tgi_at_422_is_not_an_overflow():
    assert _is_overflow(_err(TGI, 422)) is False


def test_w3b_tgi_at_400_is_decided_the_same_in_both_readings(tmp_path):
    """Either decision is the ticket's; `_is_overflow` and the run must agree."""
    worded = _is_overflow(_err(TGI, 400))
    run, memory = _run(tmp_path, ctm._overflow_scenario(_err(TGI, 400), REFUSED_AT),
                       max_continues_per_attempt=0, max_error_retries=0)
    if worded:
        assert run.state is tr.AgentState.STALLED
        assert len(cm.load(memory)) == 1
    else:
        assert run.state is tr.AgentState.ERROR
        assert cm.load(memory) == []


@pytest.mark.parametrize("status", [422, 500])
def test_w4_prompt_too_long_at_422_and_500_is_not(status):
    assert _is_overflow(_err("prompt too long", status)) is False


@pytest.mark.parametrize("status", [422, 500])
def test_w4r_prompt_too_long_at_422_and_500_in_a_run(tmp_path, status):
    run, memory = _run(tmp_path, ctm._overflow_scenario(_err("prompt too long", status),
                                                         REFUSED_AT),
                       max_continues_per_attempt=0, max_error_retries=0)
    assert run.state is tr.AgentState.ERROR
    assert cm.load(memory) == []


def test_w4s_the_statuses_stay_400_and_413():
    assert tuple(runner_mod._SIZE_REFUSAL_STATUSES) == (400, 413)


# ── W5: the repeat guard compares one unit ─────────────────────────────────

def test_w5a_a_big_read_request_then_a_small_one_is_a_repeat(tmp_path):
    """First refusal: 20 000 went through, its tools added 60 000 — the request
    was 80 000. Second: 35 000, nothing added. 35 000 is under half of 80 000
    (a repeat), though over half of the 20 000 the old guard stored."""
    scenario = {"summary_tokens": 3_000, "turns": [
        _turn(20_000, 60_000, dirty=False), _refused(ZAI),
        _turn(35_000, dirty=False), _refused(ZAI),
    ]}
    run, memory = _run(tmp_path, scenario, max_continues_per_attempt=5,
                       max_sessions_per_attempt=5)
    assert run.state is tr.AgentState.ERROR
    assert len(cm.load(memory)) == 1


def test_w5b_the_old_spelling_records_the_request_too(tmp_path):
    scenario = {"summary_tokens": 3_000, "turns": [
        _turn(20_000, 60_000, dirty=False), _refused(ctm.KENARY_OVERFLOW),
        _turn(35_000, dirty=False), _refused(ZAI),
    ]}
    run, memory = _run(tmp_path, scenario, max_continues_per_attempt=5,
                       max_sessions_per_attempt=5)
    assert run.state is tr.AgentState.ERROR
    assert len(cm.load(memory)) == 1


def test_w5c_a_small_reply_with_a_big_read_after_a_full_one_is_legitimate(tmp_path):
    scenario = {"summary_tokens": 3_000, "turns": [
        _turn(REFUSED_AT, dirty=False), _refused(ctm.KENARY_OVERFLOW),
        _turn(40_000, 60_000, dirty=False), _refused(ZAI),
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    run, memory = _run(tmp_path, scenario, max_continues_per_attempt=3)
    assert run.state is tr.AgentState.READY
    assert [r.last_ok for r in cm.load(memory)] == [REFUSED_AT, 40_000]


def test_w5d_the_record_still_keeps_last_ok(tmp_path):
    scenario = {"turns": [_turn(40_000, 10_000), _refused(ZAI)]}
    run, memory = _run(tmp_path, scenario, max_continues_per_attempt=0)
    (record,) = cm.load(memory)
    assert record.last_ok == 40_000


# ── W6: a small window through the fallback ────────────────────────────────

def _tight(last_ok):
    return ctm._record(limit=None, last_ok=last_ok, grew=None, prompt=None)


def test_w6_fallback_lowers_the_floor_with_no_declared_window():
    assert cm.size_of(_tight(28_000), 32_000, declared=None, fallback=32_768) == 28_000


def test_w6n_neither_declared_nor_fallback_keeps_the_floor():
    assert cm.size_of(_tight(28_000), 32_000, declared=None, fallback=None) is None
    assert cm.size_of(_tight(28_000), 32_000, declared=None, fallback=0) is None


def test_w6d_declared_wins_over_the_fallback():
    assert cm.size_of(_tight(28_000), 32_000, declared=131_072, fallback=32_768) is None


def test_w6s_the_share_applies_to_the_fallback():
    assert cm.size_of(_tight(28_000), 32_000, fallback=32_768, share_pct=90) is None
    assert cm.size_of(_tight(28_000), 32_000, fallback=32_768, share_pct=0) is None


def test_w6r_a_run_with_only_a_fallback_remembers_the_small_window(tmp_path):
    run, memory = _run(tmp_path, ctm._overflow_scenario(ZAI, 28_000), declared=None,
                       max_continues_per_attempt=0, context_limit_fallback=32_768)
    assert run.state is tr.AgentState.STALLED
    (record,) = cm.load(memory)
    assert record.last_ok == 28_000
    assert cm.smallest_size([record], record.provider, record.model, 32_000,
                            fallback=32_768) == 28_000


def test_w6rn_a_run_with_no_window_at_all_stays_an_error(tmp_path):
    run, memory = _run(tmp_path, ctm._overflow_scenario(ZAI, 28_000), declared=None,
                       max_continues_per_attempt=0, max_error_retries=0)
    assert run.state is tr.AgentState.ERROR
    assert cm.load(memory) == []


# ── W7: the tests read the share from the config ───────────────────────────

def test_w7_the_wording_tests_do_not_read_runner_full_refusal_share():
    """Item 7: no test reads the runner's constant — prose may still name it."""
    import re
    text = (ROOT / "tests" / "test_contest_overflow_wording.py").read_text(encoding="utf-8")
    code = [line for line in text.splitlines()
            if not line.lstrip().startswith(("#", "``", "*")) and "``" not in line]
    used = [line for line in code
            if re.search(r"runner(?:_mod)?\.FULL_REFUSAL_SHARE|^\s*FULL_REFUSAL_SHARE,\s*$", line)]
    assert used == []
