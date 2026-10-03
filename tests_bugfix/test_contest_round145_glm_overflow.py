"""tests_bugfix/test_contest_round145_glm_overflow.py — round 145: zai's `Prompt exceeds max length` at 98 777 of a declared 131 072 ended glm-4.5-flash ERROR instead of an overflow.

Two bugs in one run: the runner knew only three overflow spellings, so the
refusal was a plain error (no memory, no compact); and Kilo's declared window
always beat the memory, so a remembered refusal below it would never have
moved the compact earlier either.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.contest.runner import _context_budget, _is_full_refusal, _is_overflow  # noqa: E402

ZAI = {"name": "APIError", "data": {"message": "Prompt exceeds max length",
                                    "statusCode": 400, "isRetryable": False}}
DEEPSEEK_FREE = {"name": "APIError", "data": {
    "message": "This prompt is longer than the free tier allows for a single request.",
    "statusCode": 400}}


def test_zai_s_refusal_is_an_overflow_and_the_free_tier_s_is_not():
    assert _is_overflow(ZAI)
    assert not _is_overflow(DEEPSEEK_FREE)
    assert not _is_full_refusal(DEEPSEEK_FREE, 98_777, 131_072)


def test_a_refused_size_below_the_declared_window_sizes_the_model():
    spec = SimpleNamespace(context_limit=131_072, provider_id="zai", model_id="glm-4.5-flash")
    record = {"at": 1e12, "provider": "zai", "model": "glm-4.5-flash", "last_ok": 98_777}
    assert _context_budget(spec, [record]) == (98_777, "remembered")
