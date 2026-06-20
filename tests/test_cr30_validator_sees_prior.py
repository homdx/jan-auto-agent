"""tests/test_cr30_validator_sees_prior.py — on a re-review the creative Gate-2
validator must see its OWN previous critique and be told to compare item-by-item
(AUTO-CR-30). First pass shows no such block. Validators without the parameter
keep working (backward-compatible)."""
from __future__ import annotations

import tools.llm_stream as ls
from tools.auto.inner_loop import LLMGate2Validator, InnerLoop


def _make_validator(capture):
    def rc(**k):
        capture["payload"] = k.get("payload")
        return "REVISE:\n1. Диалог всё ещё не по заданию.\n2. Повтор не убран."
    ls.request_completion = rc
    ls.strip_think = lambda x: x
    ls.ollama_chat_url = lambda u: u
    v = object.__new__(LLMGate2Validator)
    for k, val in dict(task_mode="creative", api_format="ollama", base_url="http://x",
                       api_key="k", model="m", ssl_context=None, temperature=0.1,
                       max_tokens=512, timeout=30, num_ctx=8192, _system="SYS",
                       system="SYS").items():
        setattr(v, k, val)
    v._read_changed_content = lambda *a, **k: "Глава: текст"
    return v


def _user_text(payload):
    return " ".join(m.get("content", "") for m in payload.get("messages", [])
                    if m.get("role") == "user")


class _R:
    exit_code = 0; stdout = ""; stderr = ""


def test_prior_critique_injected_on_revalidation():
    cap = {}
    v = _make_validator(cap)
    prior = "1. Диалог Миры не по заданию — заменить.\n2. Повтор сцены — убрать."
    approved, fb = v.approve({"instruction": "паника у проливов"}, _R(), None,
                             prior_critique=prior)
    um = _user_text(cap["payload"])
    assert "YOUR PREVIOUS REVIEW" in um
    assert "Диалог Миры не по заданию" in um           # the actual prior text
    assert "ONE BY ONE" in um and "Re-read the Task" in um
    assert approved is False
    assert "Повтор не убран" in fb                       # full new critique reaches coder


def test_first_pass_has_no_previous_review_block():
    cap = {}
    v = _make_validator(cap)
    v.approve({"instruction": "паника"}, _R(), None, prior_critique="")
    assert "YOUR PREVIOUS REVIEW" not in _user_text(cap["payload"])


def test_validator_without_param_not_broken(tmp_path):
    """InnerLoop must not pass prior_critique to a validator that doesn't accept it."""
    class _OldValidator:
        def approve(self, task, exec_result, coder_result, *, base_dir=None):
            return True, ""
    class _OkCoder:
        def generate(self, *a, **k):
            class C: target_files = ["chapter_1.txt"]; missing_context = []
            return C()
    class _OkExec:
        def run(self, *a, **k):
            class E: exit_code = 0; stdout = ""; stderr = ""; passed = True
            return E()

    loop = InnerLoop(_OkCoder(), _OkExec(), _OldValidator(),
                     max_attempts=2, task_mode="creative")
    res = loop.run_task({"id": "t1", "target_files": ["chapter_1.txt"]}, tmp_path)
    assert res.passed is True   # no TypeError from the extra kwarg
