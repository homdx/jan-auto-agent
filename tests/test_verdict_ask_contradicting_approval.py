"""An approval that goes on to name a problem, or no verdict at all, is put back to the model once (VERDICT-ASK)."""
from __future__ import annotations

import configparser

import pytest

import tools.llm_stream as ls
from tools.auto import verdict_followup as vf
from tools.auto.architect import _ARCH_PLAN_SYSTEM, ClusterReviewer
from tools.auto.continuity_validator import _CONTINUITY_SYSTEM, ContinuityValidator
from tools.auto.fact_validator import _GATE3_SYSTEM_FACTS, FactValidator
from tools.auto.inner_loop import _GATE2_SYSTEM_CREATIVE, LLMGate2Validator
from tools.auto.theme_validator import _THEME_SYSTEM, ThemeValidator

CONTRARY = "No errors found: the chapter ignores the bible."


@pytest.mark.parametrize("reply,clause", [
    (CONTRARY, "the chapter ignores the bible"),
    ("No errors found. The chapter ignores the bible.", "The chapter ignores the bible"),
    ("APPROVED\nAnna's age changes from 12 to 14.", "Anna's age changes from 12 to 14"),
    ("Нет противоречий: глава игнорирует библию.", "глава игнорирует библию"),
    ("Согласна. Возраст Анны не соответствует библии.", "Возраст Анны не соответствует библии"),
])
def test_a_contrary_clause_after_an_approval_is_found(reply, clause):
    assert vf.contrary_clause(reply) == clause


@pytest.mark.parametrize("reply", [
    "APPROVED", "No errors found.", "APPROVED: the chapter follows the bible.",
    "APPROVED. Nothing contradicts the bible.", "APPROVED — the chapter does not contradict the bible.",
    "No issues; it never ignores the outline.", "OK, the chapter doesn't break canon.",
    "No contradictions found. Missing nothing.", "Нет противоречий.",
    "Противоречий нет, всё соответствует.", "Согласна, ничто не противоречит библии.",
    "Одобрено, ничего не нарушено.",
])
def test_praise_and_a_negated_problem_ask_nothing(reply):
    assert vf.contrary_clause(reply) is None


class _Ask:
    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def __call__(self, extra):
        self.asked.append(extra)
        return self.answers.pop(0)


def test_the_follow_up_quotes_the_reply_and_the_contrary_clause():
    ask = _Ask("REVISE: bring chapter 3 back in line with the bible.")
    approved, reason, unparseable, reply = vf.settle_verdict(CONTRARY, ask, who="t")
    assert (approved, unparseable) == (False, False)                    # was approved
    assert reason == "bring chapter 3 back in line with the bible."
    assert reply.startswith("REVISE")
    [extra] = ask.asked
    assert CONTRARY in extra and '"the chapter ignores the bible"' in extra
    assert "APPROVED" in extra and "REVISE" in extra


def test_the_model_may_say_the_clause_was_no_problem():
    approved, _, unparseable, _ = vf.settle_verdict(CONTRARY, _Ask("APPROVED"), who="t")
    assert (approved, unparseable) == (True, False)


@pytest.mark.parametrize("answer", ["Hmm, hard to say.", "", CONTRARY])
def test_an_answer_no_clearer_passes_on_fail_open_flagged(answer):
    approved, reason, unparseable, reply = vf.settle_verdict(CONTRARY, _Ask(answer), who="t")
    assert (approved, unparseable, reply) == (True, True, CONTRARY)
    assert "contradicts itself" in reason


def test_a_failed_follow_up_leaves_the_first_reading():
    def boom(_):
        raise OSError("down")
    assert vf.settle_verdict(CONTRARY, boom, who="t")[:3][0] is True


def test_a_clear_reply_is_not_asked_about():
    ask = _Ask()
    assert vf.settle_verdict("REVISE: fix the date.", ask, who="t")[:3] == (False, "fix the date.", False)
    assert vf.settle_verdict("APPROVED", ask, who="t")[:3] == (True, "", False)
    assert ask.asked == []


def test_an_unparseable_reply_is_asked_about_with_the_reply_quoted():
    ask = _Ask("REVISE: chapter 2 contradicts the bible.")
    raw = "Looks good. Chapter 2 contradicts the bible."
    approved, reason, _, _ = vf.settle_verdict(raw, ask, who="t")
    assert approved is False and "contradicts the bible" in reason
    assert raw in ask.asked[0] and "VERY FIRST token" in ask.asked[0]


# ── the five callers ─────────────────────────────────────────────────────────

class _Llm:
    def __init__(self, *replies):
        self.replies, self.users = list(replies), []

    def __call__(self, system, user):
        self.users.append(user)
        return self.replies.pop(0)


def _reviewer(llm):
    cfg = configparser.ConfigParser()
    cfg.read_dict({"api": {"active": "local"},
                   "api_local": {"base_url": "http://x/v1", "api_key": "x", "model": "m"},
                   "architect": {}, "loop": {"timeout_seconds": "30"}})
    r = ClusterReviewer(cfg, "http://x/v1", "x", "m", task_mode="creative")
    r._llm_call = llm
    return r


_CHECKS = {
    "continuity": lambda llm: ContinuityValidator(llm).check("bible", "chapter"),
    "fact": lambda llm: FactValidator(llm).check({"instruction": "x"}, "chapter"),
    "theme": lambda llm: ThemeValidator(llm, "no gore").check("chapter"),
    "plan": lambda llm: _reviewer(llm).validate_plan("goal", [{"title": "t", "instruction": "i"}]),
}


def _approved(result):
    return result[0] if isinstance(result, tuple) else result.approved


@pytest.mark.parametrize("name", sorted(_CHECKS))
def test_each_validator_asks_about_a_contradicting_approval(name):
    llm = _Llm(CONTRARY, "REVISE: follow the bible.")
    assert _approved(_CHECKS[name](llm)) is False                        # was approved
    assert len(llm.users) == 2 and CONTRARY in llm.users[1]
    assert llm.users[1].startswith(llm.users[0])                         # the same request, asked again


@pytest.mark.parametrize("name", sorted(_CHECKS))
def test_each_validator_asks_once_about_an_unparseable_reply(name):
    llm = _Llm("Let me think about the chapter...", "REVISE: follow the bible.")
    assert _approved(_CHECKS[name](llm)) is False                        # was approved on fail-open
    assert len(llm.users) == 2


@pytest.mark.parametrize("name", sorted(_CHECKS))
def test_each_validator_takes_a_clean_approval_in_one_call(name):
    llm = _Llm("APPROVED")
    assert _approved(_CHECKS[name](llm)) is True and len(llm.users) == 1


def test_gate2_creative_asks_about_a_contradicting_approval(monkeypatch):
    users = []

    def rc(**k):
        users.append(next(m["content"] for m in k["payload"]["messages"] if m["role"] == "user"))
        return CONTRARY if len(users) == 1 else "REVISE:\n1. Chapter 3 ignores the bible — follow it."
    monkeypatch.setattr(ls, "request_completion", rc)
    monkeypatch.setattr(ls, "strip_think", lambda x: x)
    monkeypatch.setattr(ls, "ollama_chat_url", lambda u: u)
    v = object.__new__(LLMGate2Validator)
    for k, val in dict(task_mode="creative", api_format="ollama", base_url="http://x", api_key="k",
                       model="m", ssl_context=None, temperature=0.1, max_tokens=512, timeout=30,
                       num_ctx=8192, _system="SYS").items():
        setattr(v, k, val)
    v._read_changed_content = lambda *a, **k: "chapter"

    class _R:
        exit_code = 0; stdout = ""; stderr = ""
    approved, feedback = v.approve({"instruction": "x"}, _R(), None)
    assert approved is False and "ignores the bible" in feedback          # was approved
    assert len(users) == 2 and CONTRARY in users[1]


@pytest.mark.parametrize("prompt", [_GATE2_SYSTEM_CREATIVE, _CONTINUITY_SYSTEM, _GATE3_SYSTEM_FACTS,
                                    _THEME_SYSTEM, _ARCH_PLAN_SYSTEM])
def test_every_creative_verdict_prompt_states_the_strict_format(prompt):
    assert prompt.endswith(vf.STRICT_VERDICT_RULE)
