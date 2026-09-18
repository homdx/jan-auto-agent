"""tests/test_run4_coder_budget_ladder.py — RUN-4: a cut-off reply climbs the coder's budget.

epic-tasks/32: ``LLM response was cut off before the JSON was complete`` was
the single most frequent coder failure string in the live runs (807
occurrences in testtext, 572 in testtext6), and it always came back with the
SAME ``[coder] max_tokens``. A whole-file rewrite of a 7 kB module needs
~2500–3500 output tokens, so at a 1200 budget the model was cut at the same
place five times, the task went EXHAUSTED, the outer loop opened the next
round with the same budget, and after ten rounds the task was BLOCKED — six
of the 29 BLOCKED tasks ended on that string. Gate 1 already solved exactly
this for itself (GATE1-LEARN-1/2, a re-ask ladder that raises the budget on a
truncated reply and learns the tier that worked); the coder had no ladder.

Acceptance (from the ticket):
  * cut off at 3 000, complete at 6 000 → the second call goes out at
    ``max_tokens=6000``, the task proceeds, no third call;
  * always cut off → 3 000, 6 000, 12 000, 12 000, 12 000 (cap = 4×) and
    then EXHAUSTED, exactly as today;
  * a NO-JSON reply (and a malformed-JSON reply) → next attempt at the same
    budget;
  * the next task on the same Coder starts at the learned 6 000;
  * ``max_tokens_cap`` malformed → warning + default, the style every other
    key in ``Coder.__init__`` already uses;
  * the raised budget is on the coder decision event as ``max_tokens=…``.
"""

from __future__ import annotations

import configparser
import importlib.util
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.agent_trace import tracer
from tools.auto.coder import (
    Coder,
    _TRUNCATION_ADVICE,
    _TRUNCATION_HEAD,
    _TRUNCATION_RAISED_ADVICE,
)
from tools.auto.inner_loop import InnerLoop


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── the reply shapes the live runs produced ───────────────────────────────────

PATH = "pkg/mod.py"
# Unterminated string, no closing brace: the decode error is
# "Unterminated string" and the text does not end with "}", so
# _parse_response classifies it as a truncation — the shape AUTO-T30 actually
# produced at 1200 tokens.
CUT_OFF_JSON = '{"files": [{"path": "pkg/mod.py", "content": "def f():\\n    return '


def _complete(path: str = PATH) -> str:
    return json.dumps({"files": [{"path": path, "content": "def f():\n    return 1\n"}]})


COMPLETE_JSON = _complete()
NO_JSON = "Sorry, I cannot produce that file — could you clarify the acceptance check?"
# Has a "{" but the object is closed: "Expecting property name" yet the text
# ends with "}", so this is the malformed-JSON case, not a truncation.
MALFORMED_JSON = '{"files": [}'

TASK = {"id": "AUTO-T30", "title": "module", "instruction": "rewrite pkg/mod.py",
        "target_files": [PATH]}
TASK_B = {"id": "AUTO-T31", "title": "other", "instruction": "touch pkg/other.py",
          "target_files": ["pkg/other.py"]}


def _config(max_tokens: int = 3000, extra_coder: dict[str, str] | None = None) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict(
        {
            "api": {"active": "local", "verify_ssl": "false"},
            "api_local": {
                "base_url": "http://localhost:1337/v1",
                "api_key": "test",
                "model": "test-model",
                "api_format": "openai",
            },
            "loop": {"timeout_seconds": "300"},
            "coder": {
                "temperature": "0.2",
                "max_tokens": str(max_tokens),
                **(extra_coder or {}),
            },
        }
    )
    return cfg


def _make_coder(task_mode: str = "code", **kwargs) -> Coder:
    return Coder(
        config=_config(**kwargs),
        base_url="http://localhost:1337/v1",
        api_key="test",
        model="test-model",
        api_format="openai",
        verify_ssl=False,
        task_mode=task_mode,
    )


# ── provider stand-ins: they answer by the budget they were asked with ───────

class BudgetStub:
    """Cut off below *cut_off_below* tokens, complete above. Records the budget."""

    def __init__(self, cut_off_below: int, complete: str = COMPLETE_JSON):
        self.cut_off_below = cut_off_below
        self.complete = complete
        self.budgets: list[int] = []

    def __call__(self, **kwargs) -> str:
        budget = int(kwargs["payload"]["max_tokens"])
        self.budgets.append(budget)
        return CUT_OFF_JSON if budget < self.cut_off_below else self.complete


class FixedStub:
    """Always the same reply, whatever the budget."""

    def __init__(self, reply: str):
        self.reply = reply
        self.budgets: list[int] = []

    def __call__(self, **kwargs) -> str:
        self.budgets.append(int(kwargs["payload"]["max_tokens"]))
        return self.reply


class PassingExecutor:
    def run(self, task):
        return SimpleNamespace(passed=True, exit_code=0, stdout="", stderr="",
                               traceback="", command="python3 -m pytest", timed_out=False)


class ApprovingValidator:
    def approve(self, task, exec_result, coder_result, *, base_dir=None):
        return True, ""


# ── 1. the ladder ────────────────────────────────────────────────────────────

def test_cut_off_then_complete_climbs_once_and_proceeds(tmp_path):
    stub = BudgetStub(cut_off_below=6000)          # complete once the budget is 6 000
    coder = _make_coder()                            # cap defaults to 4 × 3 000
    loop = InnerLoop(coder, PassingExecutor(), ApprovingValidator(), max_attempts=5)
    with patch("tools.llm_stream.request_completion", side_effect=stub):
        result = loop.run_task(TASK, tmp_path)

    assert stub.budgets == [3000, 6000], "one climb, then the file lands — no third call"
    assert result.passed is True
    assert result.attempts_used == 2


def test_always_cut_off_climbs_to_the_cap_then_exhausts(tmp_path):
    stub = BudgetStub(cut_off_below=10**9)          # never complete
    coder = _make_coder()                            # cap defaults to 12 000
    loop = InnerLoop(coder, PassingExecutor(), ApprovingValidator(), max_attempts=5)
    with patch("tools.llm_stream.request_completion", side_effect=stub):
        result = loop.run_task(TASK, tmp_path)

    assert stub.budgets == [3000, 6000, 12000, 12000, 12000]
    assert result.passed is False
    assert result.attempts_used == 5
    # At the cap there is no more room, so this is the real "shorten it" case
    # and the feedback keeps the existing sentence.
    assert "or raise [coder] max_tokens" in result.last_feedback
    assert "output budget was raised" not in result.last_feedback


def test_no_json_reply_keeps_the_budget(tmp_path):
    stub = FixedStub(NO_JSON)
    coder = _make_coder()
    with patch("tools.llm_stream.request_completion", side_effect=stub):
        results = [coder.generate(TASK, tmp_path) for _ in range(3)]

    assert stub.budgets == [3000, 3000, 3000], "more tokens do not fix prose"
    assert all(not r.succeeded for r in results)
    assert all(r.error.startswith("LLM response contained NO JSON at all") for r in results)
    assert all(r.budget_raised is False for r in results)


def test_malformed_json_keeps_the_budget(tmp_path):
    stub = FixedStub(MALFORMED_JSON)
    coder = _make_coder()
    with patch("tools.llm_stream.request_completion", side_effect=stub):
        results = [coder.generate(TASK, tmp_path) for _ in range(2)]

    assert stub.budgets == [3000, 3000], "more tokens do not fix a broken object"
    assert all(r.error.startswith("JSON decode failed") for r in results)
    assert all(r.budget_raised is False for r in results)


def test_explicit_cap_stops_the_ladder(tmp_path):
    stub = FixedStub(CUT_OFF_JSON)
    coder = _make_coder(extra_coder={"max_tokens_cap": "6000"})
    with patch("tools.llm_stream.request_completion", side_effect=stub):
        coder.generate(TASK, tmp_path)
        coder.generate(TASK, tmp_path)
        coder.generate(TASK, tmp_path)

    assert stub.budgets == [3000, 6000, 6000], "the cap is where it stops"


def test_truncation_feedback_shape_is_unchanged_at_the_cap(tmp_path):
    # The message the model sees today, byte for byte: the ticket keeps it for
    # the at-cap case because that IS the real "shorten it" case.
    coder = _make_coder(extra_coder={"max_tokens_cap": "3000"})
    _files, err = coder._parse_response(CUT_OFF_JSON, "t", [PATH])
    assert err.startswith(_TRUNCATION_HEAD)
    assert " — the revised file was too long for the output token budget. " in err
    assert _TRUNCATION_ADVICE in err
    assert "(decode error: Unterminated string starting at:" in err


# ── 2. the feedback the next attempt reads ───────────────────────────────────

def test_feedback_says_the_budget_was_raised(tmp_path):
    stub = FixedStub(CUT_OFF_JSON)
    coder = _make_coder()
    with patch("tools.llm_stream.request_completion", side_effect=stub):
        r = coder.generate(TASK, tmp_path)

    assert r.budget_raised is True
    assert r.error.startswith(_TRUNCATION_HEAD)
    assert "output budget was raised to 6000 tokens for this attempt" in r.error
    assert "do NOT shorten the file" in r.error
    # ...and it must not tell the model to shorten a file that must be complete.
    assert "keep the response minimal" not in r.error
    assert "or raise [coder] max_tokens" not in r.error


def test_raised_advice_names_the_budget_it_will_get(tmp_path):
    _files, err = _make_coder()._parse_response(CUT_OFF_JSON, "t", [PATH])
    raised = err.replace(_TRUNCATION_ADVICE, _TRUNCATION_RAISED_ADVICE.format(budget=12000))
    assert raised.startswith(f"{_TRUNCATION_HEAD} — the revised file was too long")
    assert "raised to 12000 tokens for this attempt" in raised


def test_feedback_carries_the_decode_error_from_both_branches(tmp_path):
    stub = FixedStub(CUT_OFF_JSON)
    coder = _make_coder()
    with patch("tools.llm_stream.request_completion", side_effect=stub):
        r = coder.generate(TASK, tmp_path)
    assert "(decode error: Unterminated string" in r.error


# ── 3. the learned tier survives the task ────────────────────────────────────

def test_learned_budget_survives_to_the_next_task(tmp_path):
    coder = _make_coder()
    stub = BudgetStub(cut_off_below=6000)
    with patch("tools.llm_stream.request_completion", side_effect=stub):
        coder.generate(TASK, tmp_path)          # cut off at 3 000
        first = coder.generate(TASK, tmp_path)  # complete at 6 000
    other = FixedStub(_complete("pkg/other.py"))
    with patch("tools.llm_stream.request_completion", side_effect=other):
        second = coder.generate(TASK_B, tmp_path)  # new task, same instance

    assert first.succeeded and second.succeeded
    assert stub.budgets == [3000, 6000]
    assert other.budgets == [6000], "the next task starts at the learned 6 000"
    assert coder._learned_max_tokens == 6000


def test_a_new_coder_starts_cold(tmp_path):
    stub = BudgetStub(cut_off_below=6000)
    with patch("tools.llm_stream.request_completion", side_effect=stub):
        first = _make_coder()
        first.generate(TASK, tmp_path)
        first.generate(TASK, tmp_path)
        assert first._learned_max_tokens == 6000

    fresh = _make_coder()
    assert fresh._learned_max_tokens is None
    assert fresh._start_task_budget() == 3000


def test_learned_budget_never_goes_below_config():
    coder = _make_coder()
    coder._record_parseable_budget(100)
    assert coder._start_task_budget() == 3000, "config wins over a smaller sample"


def test_same_task_keeps_the_climbed_budget_across_calls(tmp_path):
    stub = FixedStub(CUT_OFF_JSON)
    coder = _make_coder(extra_coder={"max_tokens_cap": "12000"})
    with patch("tools.llm_stream.request_completion", side_effect=stub):
        coder.generate(TASK, tmp_path)
        coder.generate(TASK, tmp_path)
        coder.generate(TASK, tmp_path)
        # A different task on the same instance starts at config, not 12 000:
        # the climb is per task, the learning is not.
        coder.generate(TASK_B, tmp_path)

    assert stub.budgets == [3000, 6000, 12000, 3000]


# ── 4. the config key ────────────────────────────────────────────────────────

def test_default_cap_is_four_times_max_tokens():
    assert _make_coder()._max_tokens_cap == 12000
    assert _make_coder(max_tokens=1200)._max_tokens_cap == 4800


def test_malformed_cap_is_a_warning_and_the_default(caplog):
    coder = _make_coder(extra_coder={"max_tokens_cap": "banana"})
    assert coder._max_tokens_cap == 12000
    assert any("max_tokens_cap is malformed" in m for m in caplog.messages)
    # And the ladder still works off that default.
    assert coder._start_task_budget() == 3000


def test_non_integer_cap_is_a_warning_and_the_default(caplog):
    coder = _make_coder(extra_coder={"max_tokens_cap": "lots"})
    assert coder._max_tokens_cap == 12000
    assert any("max_tokens_cap is malformed" in m for m in caplog.messages)


def test_empty_cap_is_unset_not_malformed(caplog):
    # _cfg_mode treats a blank value as "not set" (same as max_tokens itself):
    # the 4x default applies without a warning.
    coder = _make_coder(extra_coder={"max_tokens_cap": ""})
    assert coder._max_tokens_cap == 12000
    assert not any("max_tokens_cap" in m for m in caplog.messages)


def test_cap_is_resolved_per_mode_like_max_tokens():
    # max_tokens_cap_creative beats max_tokens_cap in creative mode; the
    # default is 4x the mode-resolved max_tokens.
    coder = _make_coder(
        extra_coder={"max_tokens_creative": "2000", "max_tokens_cap": "5000",
                     "max_tokens_cap_creative": "7000"},
        task_mode="creative",
    )
    assert coder._max_tokens == 2000
    assert coder._max_tokens_cap == 7000
    coder = _make_coder(extra_coder={"max_tokens_creative": "2000"},
                        task_mode="creative")
    assert coder._max_tokens_cap == 8000


def test_cap_below_max_tokens_is_clamped_up(caplog):
    # A cap below the base budget would cap the FIRST attempt down; the ladder
    # exists to grow, never to shrink.
    coder = _make_coder(extra_coder={"max_tokens_cap": "1000"})
    assert coder._max_tokens_cap == 3000
    assert coder._start_task_budget() == 3000
    assert any("below max_tokens" in m for m in caplog.messages)


def test_agents_ini_documents_the_cap():
    ini = (PROJECT_ROOT / "agents.ini").read_text(encoding="utf-8")
    coder_block = ini[ini.index("[coder]"):ini.index("[inner_loop]")]
    assert "max_tokens_cap" in coder_block
    assert "4 × max_tokens" in coder_block


# ── 5. the trace ─────────────────────────────────────────────────────────────

@pytest.fixture()
def traced(tmp_path):
    """Enable the global tracer for one test into a private file."""
    trace_path = tmp_path / "trace_test.jsonl"
    tracer.configure(enabled=True, path=str(trace_path))
    tracer._run_id = "run4test001"
    try:
        yield trace_path
    finally:
        tracer.configure(enabled=False)


def _decisions(trace_path: Path, stage: str = "coder") -> list[dict]:
    if not trace_path.exists():
        return []
    events = [json.loads(l) for l in trace_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [e for e in events
            if e.get("kind") == "decision" and e.get("source") == stage
            and e.get("target") == "inner_loop"]


def test_raised_budget_is_on_the_coder_decision_event(tmp_path, traced):
    stub = FixedStub(CUT_OFF_JSON)
    coder = _make_coder(extra_coder={"max_tokens_cap": "6000"})
    loop = InnerLoop(coder, PassingExecutor(), ApprovingValidator(), max_attempts=3)
    with patch("tools.llm_stream.request_completion", side_effect=stub):
        loop.run_task(TASK, tmp_path)

    decisions = _decisions(traced)
    assert len(decisions) == 3
    assert stub.budgets == [3000, 6000, 6000]
    assert decisions[0]["params"]["max_tokens"] == "3000"
    assert decisions[0]["params"]["budget_raised"] == "True"
    # At the cap the ladder stops, so the event must say so instead of "True".
    assert decisions[1]["params"]["max_tokens"] == "6000"
    assert decisions[1]["params"]["budget_raised"] == "False"
    assert decisions[1]["params"]["attempt"] == "2"


def test_non_truncation_rejection_traces_the_budget_not_a_raise(tmp_path, traced):
    stub = FixedStub(NO_JSON)
    coder = _make_coder()
    loop = InnerLoop(coder, PassingExecutor(), ApprovingValidator(), max_attempts=1)
    with patch("tools.llm_stream.request_completion", side_effect=stub):
        loop.run_task(TASK, tmp_path)

    (decision,) = _decisions(traced)
    assert decision["params"]["max_tokens"] == "3000"
    assert decision["params"]["budget_raised"] == "False"
    assert decision["params"]["stage"] == "coder"


def test_trace_has_no_budget_for_fakes_without_the_fields(tmp_path, traced):
    class LegacyCoder:                       # older fakes: no max_tokens field
        def generate(self, task, base_dir, prior_feedback=None, **kwargs):
            return SimpleNamespace(succeeded=False, error="no files written")

    loop = InnerLoop(LegacyCoder(), PassingExecutor(), ApprovingValidator(), max_attempts=1)
    loop.run_task(TASK, tmp_path)
    (decision,) = _decisions(traced)
    assert "max_tokens" not in decision["params"]
    assert "budget_raised" not in decision["params"]


# ── 6. trace_round_snapshot counts it ────────────────────────────────────────

def _load_trace_round_snapshot():
    """scripts/ has no __init__.py, so load it by path."""
    spec = importlib.util.spec_from_file_location(
        "trace_round_snapshot", PROJECT_ROOT / "scripts" / "trace_round_snapshot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_counts_ladder_escalations_and_budgets():
    module = _load_trace_round_snapshot()
    from tools.agent_trace import AgentTracer
    sanitize = AgentTracer()._sanitize

    def decision(params):
        return {"kind": "decision", "source": "coder", "target": "inner_loop",
                "params": sanitize(params)}

    events = [
        decision({"task": "AUTO-T30", "attempt": 1, "stage": "coder"}),   # pre-RUN-4 line
        decision({"task": "AUTO-T30", "attempt": 1, "stage": "coder",
                  "max_tokens": 3000, "budget_raised": True}),
        decision({"task": "AUTO-T30", "attempt": 2, "stage": "coder",
                  "max_tokens": 6000, "budget_raised": True}),
        decision({"task": "AUTO-T30", "attempt": 3, "stage": "coder",
                  "max_tokens": 12000, "budget_raised": False}),
        decision({"task": "AUTO-T30", "attempt": 4, "stage": "executor",
                  "exit_code": 1}),
    ]
    with tempfile.TemporaryDirectory() as raw:
        agent = Path(raw) / ".agent"
        agent.mkdir()
        with open(agent / "trace_run4.jsonl", "w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event) + "\n")
        run = module.read_run(Path(raw))

    assert run["coder_budget_escalations"] == 2, "two attempts had just raised the budget"
    assert run["coder_budgets"] == {"3000": 1, "6000": 1, "12000": 1}
    assert "max_tokens" not in run  # the counters are the point, not a new column of params


# ── 6. reviewer additions (round 32 acceptance) ──────────────────────────────

def test_exhausted_task_does_not_teach_the_next_one(tmp_path):
    # T30 is cut off at every rung (12 000 never parsed): nothing was learned,
    # so T31 starts at the configured 3 000 — a budget that never produced a
    # reply is not a tier that "worked".
    coder = _make_coder()
    loop = InnerLoop(coder, PassingExecutor(), ApprovingValidator(), max_attempts=5)
    never = BudgetStub(cut_off_below=10**9)
    with patch("tools.llm_stream.request_completion", side_effect=never):
        assert loop.run_task(TASK, tmp_path).passed is False
    other = FixedStub(_complete("pkg/other.py"))
    with patch("tools.llm_stream.request_completion", side_effect=other):
        assert loop.run_task(TASK_B, tmp_path).passed is True
    assert never.budgets == [3000, 6000, 12000, 12000, 12000]
    assert other.budgets == [3000]
    assert coder._learned_max_tokens == 3000


def test_next_round_of_the_same_task_keeps_the_climbed_budget(tmp_path):
    # The outer loop reopens an EXHAUSTED task with the same id: the evidence
    # that 3 000 is too small is about this task, so the ladder is not
    # re-climbed from the floor (AUTO-T30 spent 8 rounds doing exactly that).
    coder = _make_coder()
    loop = InnerLoop(coder, PassingExecutor(), ApprovingValidator(), max_attempts=2)
    stub = BudgetStub(cut_off_below=12000)
    with patch("tools.llm_stream.request_completion", side_effect=stub):
        assert loop.run_task(TASK, tmp_path).passed is False   # 3000, 6000
        assert loop.run_task(TASK, tmp_path).passed is True    # 12000
    assert stub.budgets == [3000, 6000, 12000]


def test_creative_cut_off_chapter_climbs_the_same_ladder(tmp_path):
    # A chapter that stops mid-sentence at the budget is the same cut-off as
    # a mid-JSON reply: the next attempt gets double the budget and the
    # feedback says so. At 100 tokens (~400 chars) a 450-char body that ends
    # mid-word trips the guard; at 200 the same body is a finished chapter.
    coder = _make_coder(max_tokens=100, task_mode="creative")
    chapter = {"id": "AUTO-T40", "title": "ch", "instruction": "write chapter 1",
               "target_files": ["chapter_01.md"]}
    body = ("The rain kept falling on the roof and nobody in the house dared " * 7)[:450]
    stub = FixedStub(body)
    with patch("tools.llm_stream.request_completion", side_effect=stub):
        first = coder.generate(chapter, tmp_path)
        second = coder.generate(chapter, tmp_path)
    assert not first.succeeded and first.budget_raised is True
    assert "hit the token budget" in first.error
    assert "raised to 200 tokens" in first.error
    assert second.succeeded and second.max_tokens == 200
    assert stub.budgets == [100, 200]


def test_parse_response_is_still_pure():
    # tests/test_theme_validator.py builds a Coder with __new__ and calls the
    # parser directly: the ladder lives in generate(), never in the parser.
    coder = Coder.__new__(Coder)
    coder._task_mode = "code"
    files, msg = coder._parse_response(CUT_OFF_JSON, "t")
    assert files == [] and msg.startswith("LLM response was cut off")
    files, msg = coder._parse_response(COMPLETE_JSON, "t")
    assert msg == "" and files[0]["path"] == PATH


def test_ok_with_skips_decision_carries_the_budget(tmp_path, traced):
    # The budget is on every coder decision, not only the rejected ones.
    extra = json.dumps({"files": [
        {"path": PATH, "content": "def f():\n    return 1\n"},
        {"path": "pkg/not_mine.py", "content": "x = 1\n"},
    ]})
    coder = _make_coder()
    loop = InnerLoop(coder, PassingExecutor(), ApprovingValidator(), max_attempts=2)
    with patch("tools.llm_stream.request_completion", side_effect=FixedStub(extra)):
        assert loop.run_task(TASK, tmp_path).passed is True
    skips = [d for d in _decisions(traced) if d["content"] == "OK_WITH_SKIPS"]
    assert len(skips) == 1
    assert skips[0]["params"]["max_tokens"] == "3000"
    assert skips[0]["params"]["budget_raised"] == "False"
