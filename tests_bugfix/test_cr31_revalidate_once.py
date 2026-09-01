"""tests/test_cr31_revalidate_once.py — on an unparseable verdict the creative
Gate-2 validator re-asks ONCE before falling open (AUTO-CR-31).

Bug it fixes: the model sometimes buries the verdict ("Let's go through each
point: ...") → _parse_verdict_soft can't find APPROVED/REVISE → fail-open →
a real rejection is lost. One retry with a hard nudge usually gets a clean
answer. Capped at one retry (max two validator calls) — no infinite loop.
"""
from __future__ import annotations

import tools.llm_stream as ls
from tools.auto.inner_loop import LLMGate2Validator


def _validator():
    v = object.__new__(LLMGate2Validator)
    for k, val in dict(task_mode="creative", api_format="ollama", base_url="http://x",
                       api_key="k", model="m", ssl_context=None, temperature=0.1,
                       max_tokens=512, timeout=30, num_ctx=8192, _system="SYS").items():
        setattr(v, k, val)
    v._read_changed_content = lambda *a, **k: "Глава: текст"
    return v


class _R:
    exit_code = 0; stdout = ""; stderr = ""


def test_recovers_on_second_try():
    calls = {"n": 0, "msgs": []}

    def rc(**k):
        calls["n"] += 1
        calls["msgs"].append(next(m["content"] for m in k["payload"]["messages"]
                                  if m["role"] == "user"))
        if calls["n"] == 1:
            return "Let's go through each point:\n1. The bridge scene repeats chapter 1..."
        return "REVISE:\n1. Сцена на мостике повторяет главу 1 — убрать."
    ls.request_completion = rc; ls.strip_think = lambda x: x; ls.ollama_chat_url = lambda u: u

    v = _validator()
    approved, fb = v.approve({"instruction": "x"}, _R(), None)
    assert calls["n"] == 2                       # exactly one retry
    assert "VERY FIRST token" in calls["msgs"][1]  # nudge added on retry
    assert approved is False                      # recovered the real rejection
    assert "повторяет главу 1" in fb              # clear critique reaches coder


def test_capped_then_fail_open():
    calls = {"n": 0}

    def rc(**k):
        calls["n"] += 1
        return "Hmm, let me think about this..."   # never parseable
    ls.request_completion = rc; ls.strip_think = lambda x: x; ls.ollama_chat_url = lambda u: u

    v = _validator()
    approved, _fb = v.approve({"instruction": "x"}, _R(), None)
    assert calls["n"] == 2          # capped — no infinite loop
    assert approved is True         # still fail-open after the one retry


def test_clear_first_reply_no_retry():
    calls = {"n": 0}

    def rc(**k):
        calls["n"] += 1
        return "APPROVED"
    ls.request_completion = rc; ls.strip_think = lambda x: x; ls.ollama_chat_url = lambda u: u

    v = _validator()
    approved, _fb = v.approve({"instruction": "x"}, _R(), None)
    assert calls["n"] == 1          # no needless retry when first reply is clear
    assert approved is True
