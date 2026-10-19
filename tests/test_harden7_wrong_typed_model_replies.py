"""tests/test_harden7_wrong_typed_model_replies.py — HARDEN-7 (305).

One class of defect for parts A to C: a reply that is valid JSON whose field has
another type than the prompt asked for, which a small local model sends by
design. The repository already treats the class as a bug — ``architect.py`` has
``_to_str_or_empty`` / ``_to_bool_or`` with "BUGFIX (audit)" docstrings — these
are the places that never got it, and each one is model-dependent but costs a
lot: a change its own validator rejected is committed (A), one malformed Gate 1
reply takes the planning phase down with every other candidate's verdict (B),
the answer after every LLM call has been paid for (C).

Part D is the gap HARDEN-2 B2 left: the patch that keeps a failed task's work
was decoded as text, so a CRLF line end and a non-UTF-8 byte were both
destroyed and ``git apply`` refused the whole patch.

What this file pins down
------------------------
A. ``LLMGate2Validator.approve`` reads ``approved`` fail-closed: only an
   explicit yes approves, so ``{"approved": "false", "feedback": "REJECT: …"}``
   is a rejection that keeps its critique.
B. ``Gate1Filter._parse_presence_response`` reads its three fields as text, so a
   wrong-typed one is judged (or re-asked once) instead of raising out of
   ``filter()``.
C. The validator's ``feedback`` and the improvement agent's ``explanation`` /
   ``improved_code`` are strings by the time a caller reads them.
D. ``GitManager.diff_working_tree`` + ``AutoController._save_uncommitted_patch``
   round-trip a CRLF and a latin-1 file byte for byte.

The provider is faked at ``tools.llm_stream.request_completion`` throughout; the
Gate-1 and Gate-2 stubs follow the existing suites' shape
(``tests/test_gate1_empty_reply.py``, ``tests/test_cr2_gate2_soft.py``). Part D
drives a real git repository and the controller's own save.
"""

from __future__ import annotations

import configparser
import io
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.architect import CandidateTask, CitedLocation  # noqa: E402
from tools.auto.gate1_filter import Gate1Filter  # noqa: E402
from tools.auto.git_manager import GitError, make_git_manager  # noqa: E402
from tools.auto.inner_loop import LLMGate2Validator, _format_gate2_feedback  # noqa: E402
from tools.formatter import OutputFormatter  # noqa: E402
from tools.validator_agent import ValidatorAgent  # noqa: E402

try:
    # The shared readers the change adds; a tree without them still collects
    # this file, so every other test below fails on the defect it pins.
    from tools.llm_stream import reply_is_yes, reply_text  # noqa: E402
except ImportError:  # pragma: no cover - only on a tree without the fix
    reply_text = reply_is_yes = None


# ── the shared readers of a reply field's type ───────────────────────────────

class TestReplyText:
    """A field of another type is text, never an AttributeError."""

    pytestmark = pytest.mark.skipif(reply_text is None,
                                    reason="tools.llm_stream.reply_text is the change under test")
    def test_string_is_stripped(self):
        assert reply_text("  a reason  ") == "a reason"

    def test_none_is_empty(self):
        assert reply_text(None) == ""

    def test_list_is_one_item_per_line(self):
        assert reply_text(["too long", "no tests"]) == "too long\nno tests"

    def test_empty_list_is_empty(self):
        assert reply_text([]) == ""

    def test_number_is_its_text(self):
        assert reply_text(5) == "5"

    def test_bool_is_its_text(self):
        assert reply_text(True) == "True"

    def test_object_is_its_compact_json_and_never_raises(self):
        assert reply_text({"a": 1}) == '{"a":1}'


class TestReplyIsYes:
    """Fail-closed: only an explicit yes approves."""

    pytestmark = pytest.mark.skipif(reply_is_yes is None,
                                    reason="tools.llm_stream.reply_is_yes is the change under test")
    def test_true_approves(self):
        assert reply_is_yes(True) is True

    def test_false_does_not(self):
        assert reply_is_yes(False) is False

    def test_the_accepted_spellings_approve(self):
        for spelling in ("true", "TRUE", " yes ", "y", "1", "approved", "OK"):
            assert reply_is_yes(spelling) is True, spelling

    def test_a_negative_spelling_does_not_approve(self):
        for spelling in ("false", "no", "rejected", "0", "", "maybe", "nope"):
            assert reply_is_yes(spelling) is False, spelling

    def test_the_number_one_approves(self):
        assert reply_is_yes(1) is True
        assert reply_is_yes(2) is False

    def test_null_and_zero_do_not(self):
        assert reply_is_yes(None) is False
        assert reply_is_yes(0) is False

    def test_a_list_or_an_object_is_never_a_yes(self):
        assert reply_is_yes(["true"]) is False
        assert reply_is_yes({"approved": True}) is False


# ── Part A: Gate 2 approves a candidate its own validator rejected ───────────

_GATE2_FEEDBACK = "REJECT: breaks the public API"

# (the `approved` value in the reply, whether it approves) — the ticket's table.
_APPROVED_TABLE = [
    (True, True),
    (False, False),
    ("false", False),
    ("no", False),
    ("False", False),
    ("rejected", False),
    (0, False),
    (None, False),
]

_GATE2_TASK = {"id": "h7-a", "instruction": "Write the fix."}
_GATE2_EXEC = SimpleNamespace(passed=True, exit_code=0, stdout="", stderr="")
_GATE2_CODER = SimpleNamespace(succeeded=True, files_written=[])


def _gate2() -> LLMGate2Validator:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:11434", "api_key": "test",
                      "model": "llama3.1:8b", "api_format": "ollama",
                      "num_ctx": "4096"},
        "validator_agent": {"temperature": "0.1", "max_hints": "3",
                            "max_tokens": "256"},
        "loop": {"timeout_seconds": "30"},
    })
    return LLMGate2Validator(
        base_url="http://localhost:11434", model="llama3.1:8b", api_key="test",
        api_format="ollama", temperature=0.1, timeout=30, max_hints=3,
        task_mode="code", config=cfg,
    )


def _approve_with(approved_value):
    """Run the code-mode Gate 2 with a stubbed model that answers *approved_value*."""
    validator = _gate2()
    raw = json.dumps({"approved": approved_value, "feedback": _GATE2_FEEDBACK})
    with (
        patch("tools.llm_stream.request_completion", return_value=raw),
        patch.object(validator, "_read_changed_content", return_value="(code)"),
    ):
        return validator.approve(
            _GATE2_TASK, _GATE2_EXEC, _GATE2_CODER, base_dir="/tmp",
        )


class TestGate2ApprovedFieldType:

    def test_the_whole_table(self):
        for value, expected in _APPROVED_TABLE:
            approved, _ = _approve_with(value)
            assert approved is expected, f"approved={value!r}"

    def test_a_rejection_keeps_its_critique(self):
        for value in ("false", "no", "rejected"):
            approved, feedback = _approve_with(value)
            assert approved is False, value
            assert "breaks the public API" in feedback, value

    def test_a_list_feedback_is_still_a_rejection_with_text(self):
        validator = _gate2()
        raw = json.dumps({"approved": False,
                          "feedback": ["no tests", "no changelog"]})
        with (
            patch("tools.llm_stream.request_completion", return_value=raw),
            patch.object(validator, "_read_changed_content", return_value="(code)"),
        ):
            approved, feedback = validator.approve(
                _GATE2_TASK, _GATE2_EXEC, _GATE2_CODER, base_dir="/tmp",
            )
        assert approved is False
        assert feedback.startswith("Reason: no tests\nno changelog"), feedback

    def test_a_numeric_feedback_is_still_a_rejection_with_text(self):
        validator = _gate2()
        raw = json.dumps({"approved": "false", "feedback": 5,
                          "suggested_approach": ["try a fixture"]})
        with (
            patch("tools.llm_stream.request_completion", return_value=raw),
            patch.object(validator, "_read_changed_content", return_value="(code)"),
        ):
            approved, feedback = validator.approve(
                _GATE2_TASK, _GATE2_EXEC, _GATE2_CODER, base_dir="/tmp",
            )
        assert approved is False
        assert "Reason: 5" in feedback
        assert "Suggested approach: try a fixture" in feedback

    def test_a_missing_feedback_still_says_no_reason_given(self):
        validator = _gate2()
        raw = json.dumps({"approved": "rejected"})
        with (
            patch("tools.llm_stream.request_completion", return_value=raw),
            patch.object(validator, "_read_changed_content", return_value="(code)"),
        ):
            approved, feedback = validator.approve(
                _GATE2_TASK, _GATE2_EXEC, _GATE2_CODER, base_dir="/tmp",
            )
        assert approved is False
        assert "Reason: no reason given" in feedback

    def test_the_existing_feedback_contract_is_unchanged(self):
        """The Gate-2 feedback string a well-behaved reply produced is unchanged."""
        assert _format_gate2_feedback(
            {"feedback": "bad", "hints": ["fix it"], "suggested_approach": "try X"},
            max_hints=3,
        ) == "Reason: bad\nHints:\n  1. fix it\nSuggested approach: try X"

    def test_a_null_or_wrong_typed_hint_is_text_or_nothing(self):
        """A null hint is no hint, a number or a list is text: `[null]` printed "1. None" to the coder."""
        assert _format_gate2_feedback(
            {"feedback": "bad", "hints": [None, "", 2, ["a", "b"], "last"]}, max_hints=3,
        ) == "Reason: bad\nHints:\n  1. 2\n  2. a\nb\n  3. last"
        assert _format_gate2_feedback({"feedback": "bad", "hints": [None]}, max_hints=3) == "Reason: bad"

    def test_an_approval_is_still_an_approval(self):
        approved, feedback = _approve_with(True)
        assert approved is True and feedback == ""


# ── Part B: one malformed Gate 1 reply ends the whole filter ─────────────────

# The evidence the model quotes: a verbatim substring of the code block below.
_EVIDENCE = "return raw"


def _gate1_config(**over) -> configparser.ConfigParser:
    gate1 = {
        "temperature": "0.0", "max_tokens": "4096", "skip_llm": "false",
        "llm_call_retry_wait_sec": "0", "llm_call_retry_max": "1",
        "unparseable_retry_mode": "fast", "unparseable_learn": "false",
        "presence_workers": "1",
    }
    gate1.update({k: str(v) for k, v in over.items()})
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://stub.local:1337/v1", "api_key": "test",
                      "model": "test-model", "api_format": "openai",
                      "num_ctx": "65536"},
        "gate1": gate1,
        "loop": {"timeout_seconds": "10"},
    })
    return cfg


def _gate1_repo(tmp_path: Path, n: int) -> list[CandidateTask]:
    """*n* candidates, each over a real file whose code block holds ``return raw``."""
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        (tmp_path / "tools" / f"c{i}.py").write_text(textwrap.dedent(f"""\
            def parse_config_{i}(raw):
                # TODO: validate input
                return raw
        """), encoding="utf-8")
        out.append(CandidateTask(
            title=f"candidate {i}",
            instruction="fix the problem described in the claim",
            target_files=[f"tools/c{i}.py"],
            acceptance_check="python -m pytest tests -q",
            cited_location=CitedLocation(file=f"tools/c{i}.py",
                                         symbol=f"parse_config_{i}"),
            cluster="agents"))
    return out


def _confirmed(**over):
    body = {"verdict": "confirmed", "evidence": _EVIDENCE,
            "reason": "the claim is present"}
    body.update(over)
    return json.dumps(body)


_REJECTED = json.dumps({"verdict": "rejected", "evidence": "",
                        "reason": "already handled"})


class _Provider:
    """A per-candidate script of replies (the last one repeats); every call is
    recorded, so the number and shape of the attempts is observable."""

    def __init__(self, plans: dict[str, list]):
        self.plans = plans
        self.calls: dict[str, list[dict]] = {}

    def __call__(self, url, headers, payload, **kw):
        user_msg = payload["messages"][-1]["content"]
        key = "tools/" + user_msg.split("tools/", 1)[1].split(",", 1)[0]
        seq = self.calls.setdefault(key, [])
        plan = self.plans.get(key, [_REJECTED])
        step = plan[min(len(seq), len(plan) - 1)]
        seq.append(payload)
        return step


def _run_gate1(tmp_path, provider, cfg=None, n=5):
    cfg = cfg or _gate1_config()
    flt = Gate1Filter(
        config=cfg, base_url="http://stub.local:1337/v1", api_key="test",
        model="test-model", api_format="openai", verify_ssl=False,
    )
    counters: dict = {}
    with patch("tools.llm_stream.request_completion", side_effect=provider), \
         patch("tools.auto.gate1_filter.time.sleep", lambda s: None):
        accepted, rejected = flt.filter(
            _gate1_repo(tmp_path, n), base_dir=tmp_path, counters=counters,
        )
    return ([c.title for c in accepted],
            {r.candidate.title: r.reason for r in rejected}, counters)


class TestGate1PresenceFieldType:

    def test_a_list_evidence_that_quotes_the_code_is_read_as_text(self, tmp_path):
        """The ticket's first row: ``evidence`` a list raised AttributeError. Read
        as text it is one line, and the line it quotes is in the code block."""
        prov = _Provider({"tools/c0.py": [
            _confirmed(evidence=["return raw"]),
        ]})
        accepted, rejected, counters = _run_gate1(tmp_path, prov, n=1)
        assert accepted == ["candidate 0"]
        assert rejected == {}
        assert counters["presence_confirmed"] == 1

    def test_a_list_evidence_that_quotes_nothing_is_a_rejection(self, tmp_path):
        prov = _Provider({"tools/c0.py": [
            _confirmed(evidence=["invented quote"]),
        ]})
        accepted, rejected, counters = _run_gate1(tmp_path, prov, n=1)
        assert accepted == [] and rejected["candidate 0"].startswith(
            "verdict was 'confirmed' but no matching evidence quote")
        # A real verdict, not a parse failure: no re-ask was spent on it.
        assert counters["presence_reask"] == 0
        assert len(prov.calls["tools/c0.py"]) == 1

    def test_a_numeric_reason_does_not_stop_the_verdict(self, tmp_path):
        """The ticket's second row: ``reason`` a number raised AttributeError."""
        prov = _Provider({"tools/c0.py": [
            _confirmed(reason=5),
        ]})
        accepted, rejected, _ = _run_gate1(tmp_path, prov, n=1)
        assert accepted == ["candidate 0"]
        assert rejected == {}

    def test_a_list_verdict_is_unparseable_and_is_re_asked_once(self, tmp_path):
        """The ticket's third row: ``verdict`` a list raised AttributeError. Read
        as text its two lines are not a verdict word, so it takes the existing
        unparseable path — one re-ask, then the model's own rejection."""
        prov = _Provider({"tools/c0.py": [
            _confirmed(verdict=["confirmed", "rejected"]),
            _REJECTED,
        ]})
        accepted, rejected, counters = _run_gate1(tmp_path, prov, n=1)
        assert accepted == []
        assert rejected["candidate 0"] == "already handled"
        assert counters["presence_reask"] == 1
        assert len(prov.calls["tools/c0.py"]) == 2

    def test_one_bad_reply_costs_one_candidate_not_the_filter(self, tmp_path):
        """End to end, as the ticket's sketch: five candidates, one of them
        answering a list ``verdict``. The other four are judged, the bad one is
        re-asked once and then rejected, and ``filter()`` returns."""
        plans = {
            # two controls: a confirmation and a rejection
            "tools/c0.py": [_confirmed()],
            "tools/c1.py": [_REJECTED],
            # two more wrong-typed fields, both judged rather than fatal
            "tools/c2.py": [_confirmed(evidence=["return raw"])],
            "tools/c3.py": [_confirmed(reason=5)],
            # the bad one: an unparseable verdict, then a real rejection
            "tools/c4.py": [_confirmed(verdict=["confirmed", "rejected"]),
                            _REJECTED],
        }
        accepted, rejected, counters = _run_gate1(tmp_path, _Provider(plans), n=5)
        assert accepted == ["candidate 0", "candidate 2", "candidate 3"]
        assert rejected == {
            "candidate 1": "already handled",
            "candidate 4": "already handled",
        }
        assert counters["presence_confirmed"] == 3
        assert counters["presence_rejected"] == 2
        assert counters["presence_reask"] == 1

    def test_a_one_item_list_verdict_is_not_a_confirmation(self, tmp_path):
        """A verdict is a word: `["confirmed"]` is the unparseable reply (asked again), never a yes."""
        prov = _Provider({"tools/c0.py": [_confirmed(verdict=["confirmed"]), _REJECTED]})
        accepted, rejected, counters = _run_gate1(tmp_path, prov, n=1)
        assert accepted == []
        assert rejected == {"candidate 0": "already handled"}
        assert counters["presence_confirmed"] == 0
        assert counters["presence_reask"] == 1

    def test_the_all_string_control_is_unchanged(self, tmp_path):
        prov = _Provider({"tools/c0.py": [_confirmed()],
                          "tools/c1.py": [_REJECTED]})
        accepted, rejected, counters = _run_gate1(tmp_path, prov, n=2)
        assert accepted == ["candidate 0"]
        assert rejected == {"candidate 1": "already handled"}
        assert counters["presence_confirmed"] == 1
        assert counters["presence_rejected"] == 1

    def test_a_null_field_is_read_as_empty_not_as_a_crash(self, tmp_path):
        prov = _Provider({"tools/c0.py": [
            _confirmed(verdict=None, evidence=None, reason=None),
        ]})
        accepted, rejected, counters = _run_gate1(
            tmp_path, prov, _gate1_config(presence_unknown="reject"), n=1)
        # An absent verdict is not a word: unparseable, so it is re-asked — and
        # then, with nothing better, dropped as unknown rather than crashing.
        assert accepted == []
        assert "unrecognised verdict ''" in rejected["candidate 0"]
        assert counters["presence_unknown"] == 1
        assert len(prov.calls["tools/c0.py"]) == 6   # the first call + 5 re-asks


# ── Part C: a validator or an improvement reply of the wrong type ────────────

_VALIDATOR_PAYLOAD = {
    "task": "t", "iteration": 1, "target_block": "",
    "imports": "", "related_code": {}, "missing_refs": "None",
}


def _validate_with(feedback_value):
    agent = ValidatorAgent(model="test-model", base_url="http://fake-host",
                           api_key="x", timeout=5, max_iter=3)
    raw = json.dumps({"status": "needs_fix", "feedback": feedback_value,
                      "suggested_searches": []})
    with patch("tools.validator_agent.request_completion", return_value=raw):
        return agent.validate(_VALIDATOR_PAYLOAD)


class TestValidatorFeedbackType:
    """The three rows of the ticket's table, plus the two that must not change."""

    def test_a_list_feedback_becomes_text(self):
        result = _validate_with(["too long", "no tests"])
        assert result["feedback"] == "too long\nno tests"
        assert result["status"] == "needs_fix"

    def test_a_number_feedback_becomes_text(self):
        assert _validate_with(5)["feedback"] == "5"

    def test_an_object_feedback_becomes_compact_json(self):
        assert _validate_with({"a": 1})["feedback"] == '{"a":1}'

    def test_a_string_feedback_is_byte_identical_to_today(self):
        """The one behaviour a caller could notice: a string is left exactly as
        it arrived, whitespace included, so the caller's own ``.strip()`` and
        its ``{feedback!r}`` print are unchanged."""
        for value in ("text", "  padded  ", ""):
            assert _validate_with(value)["feedback"] == value

    def test_every_caller_can_strip_it(self):
        """The exact expression every caller used, on every wrong-typed row."""
        for value in (["too long", "no tests"], 5, {"a": 1}, None):
            feedback = (_validate_with(value).get("feedback") or "").strip()
            assert isinstance(feedback, str)

    def test_the_error_paths_still_carry_string_feedback(self):
        agent = ValidatorAgent(model="test-model", base_url="http://fake-host",
                               api_key="x", timeout=5, max_iter=3)
        with patch("tools.validator_agent.request_completion",
                   side_effect=ConnectionError("refused")):
            assert "_api_error" in agent.validate(_VALIDATOR_PAYLOAD)
        with patch("tools.validator_agent.request_completion",
                   return_value="prose, not JSON"):
            assert "_unparseable" in agent.validate(_VALIDATOR_PAYLOAD)


def _parsed(intent="improve"):
    from tools.prompt_parser import ParsedPrompt
    return ParsedPrompt(
        file_path="app.py", target_name="foo", target_type="function",
        intent=intent, raw="improve foo in app.py",
    )


def _render(improvement):
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        OutputFormatter.render(
            parsed=_parsed(), imports=[], block="def foo(): pass",
            search_result={}, improvement=improvement, elapsed_time=1.0,
            iteration=1, output_config={},
        )
    return buf.getvalue()


class TestImprovementFieldType:
    """``render`` is the last step of a run, after every LLM call has been paid
    for; it must print a list instead of raising."""

    def test_a_list_explanation_is_printed_one_item_per_line(self):
        out = _render({"explanation": ["first point", "second point"],
                       "issues": [], "improved_code": "x = 1\n", "changes": []})
        assert "# EXPLANATION" in out
        assert "first point" in out and "second point" in out

    def test_a_list_improved_code_is_printed(self):
        out = _render({"explanation": "", "issues": [],
                       "improved_code": ["def foo():", "    return 1"],
                       "changes": []})
        assert "# IMPROVED CODE" in out
        assert "def foo():" in out

    def test_a_number_explanation_is_printed_as_its_text(self):
        out = _render({"explanation": 5, "issues": [],
                       "improved_code": "", "changes": []})
        assert "# EXPLANATION" in out and "\n5\n" in out

    def test_a_string_reply_is_byte_identical_to_today(self):
        improvement = {"explanation": "This fixes the bug.", "issues": [],
                       "improved_code": "def foo():\n    return 1\n",
                       "changes": []}
        out = _render(improvement)
        assert "# EXPLANATION" in out and "This fixes the bug." in out
        assert "# IMPROVED CODE" in out
        assert "def foo():\n    return 1\n" in out
        # the section is not printed for an empty or null field, as before
        assert "# EXPLANATION" not in _render(
            {"explanation": None, "issues": [], "improved_code": "", "changes": []})
        assert "# IMPROVED CODE" not in _render(
            {"explanation": "x", "issues": [], "improved_code": None, "changes": []})


class TestEditSurvivesAListFeedback:
    """`/edit` is the interactive shell's path and nothing there catches an
    AttributeError, so a validator that lists its complaints as an array used to
    end the shell. It now prints them and goes on."""

    def _editor(self, validation, max_iterations=3):
        import tools.actions as A
        act = A.OrchestratorActions.__new__(A.OrchestratorActions)
        act.max_iterations, act.timeout_seconds, act.stream_agents = max_iterations, 60, False
        act._edit_file_content = lambda instr, fp, original, **kw: (
            original.replace("line one", "line two"))
        act._validate_edit = lambda *a, **kw: validation
        return act

    def test_a_list_feedback_is_printed_not_raised(self, tmp_path, capsys):
        f = tmp_path / "note.txt"
        f.write_text("line one\n", encoding="utf-8")
        self._editor({"status": "needs_fix",
                      "feedback": ["add tests", "fix the docstring"]}).run_edit(
            "/edit note.txt :: tweak", str(tmp_path))
        out = capsys.readouterr().out
        assert "Edit feedback" in out
        assert "add tests" in out and "fix the docstring" in out
        assert "FILE EDITED" in out

    def test_a_number_feedback_is_printed_not_raised(self, tmp_path, capsys):
        f = tmp_path / "note.txt"
        f.write_text("line one\n", encoding="utf-8")
        self._editor({"status": "needs_fix", "feedback": 5}).run_edit(
            "/edit note.txt :: tweak", str(tmp_path))
        assert "Edit feedback" in capsys.readouterr().out

    def test_an_api_error_with_a_list_feedback_is_printed_not_raised(self, tmp_path, capsys):
        f = tmp_path / "note.txt"
        f.write_text("line one\n", encoding="utf-8")
        # one iteration, so the exhausted-API-error branch is the one reached
        self._editor({"status": "needs_fix", "_api_error": True,
                      "feedback": ["HTTP 500", "try later"]},
                     max_iterations=1).run_edit(
            "/edit note.txt :: tweak", str(tmp_path))
        out = capsys.readouterr().out
        assert "validator unavailable" in out.lower()
        assert "HTTP 500" in out


class TestTextQaSurvivesAListFeedback:
    """The text-QA loop and the whole-file search read the same field, including
    inside the loop's concluding warning — the last print of a run whose every
    LLM call has already been paid for."""

    def _actions(self, validation, max_iterations=1):
        import tools.actions as A
        act = A.OrchestratorActions.__new__(A.OrchestratorActions)
        (act.max_iterations, act.timeout_seconds, act.stream_agents,
         act.search_full_file_max_chars) = max_iterations, 60, False, 100000
        act._answer_from_file = lambda *a, **kw: "an answer"
        act._validate_text_answer = lambda *a, **kw: validation
        return act

    def test_a_list_feedback_reaches_the_concluding_warning(self, capsys):
        self._actions({"status": "needs_fix", "grounded": False,
                       "feedback": ["too long", "no citation"]}).run_text_qa(
            "what?", "faq.txt", "the document", ".")
        out = capsys.readouterr().out
        assert "UNVALIDATED ANSWER" in out
        assert "too long" in out and "no citation" in out

    def test_the_search_path_prints_a_list_feedback(self, tmp_path, capsys):
        """`/search` over a real file: a rejected candidate answer prints the
        validator's list of complaints instead of raising."""
        import tools.actions as A
        act = A.OrchestratorActions.__new__(A.OrchestratorActions)
        (act.max_iterations, act.timeout_seconds, act.stream_agents,
         act.search_full_file_max_chars) = 3, 60, False, 100000
        faq = tmp_path / "faq.txt"
        faq.write_text("How do I reset it?  Hold the button.\n", encoding="utf-8")
        act._ask_over_text_retryable = lambda *a, **kw: "hold the button"
        act._validate_text_answer = lambda *a, **kw: {
            "status": "needs_fix", "grounded": False,
            "feedback": ["not in the document"]}
        act.run_search("/search reset it in faq.txt", str(tmp_path))
        out = capsys.readouterr().out
        assert "not in the document" in out

    def test_the_search_paths_unavailable_branch_prints_a_list_feedback(self, tmp_path, capsys):
        import tools.actions as A
        act = A.OrchestratorActions.__new__(A.OrchestratorActions)
        (act.max_iterations, act.timeout_seconds, act.stream_agents,
         act.search_full_file_max_chars) = 3, 60, False, 100000
        faq = tmp_path / "faq.txt"
        faq.write_text("How do I reset it?  Hold the button.\n", encoding="utf-8")
        act._ask_over_text_retryable = lambda *a, **kw: "hold the button"
        act._validate_text_answer = lambda *a, **kw: {
            "status": "needs_fix", "_unparseable": True,
            "feedback": ["reply with JSON only"]}
        act.run_search("/search reset it in faq.txt", str(tmp_path))
        out = capsys.readouterr().out
        assert "Validator unavailable" in out and "reply with JSON only" in out


class TestPipelineSurvivesAListFeedback:
    """``main.run_pipeline`` catches only KeyboardInterrupt and EOFError, so a
    validator that listed its complaints as an array used to end the whole
    pipeline — the last step of the run, after every LLM call had been paid for.
    These drive the real method with the model and the search stubbed."""

    @staticmethod
    def _app(validation, improvement=None):
        import main as M
        app = M.Orchestrator.__new__(M.Orchestrator)
        app.max_iterations = 1
        app.timeout_seconds = 60
        app.validator_agent = SimpleNamespace(
            max_hints=3, validate=lambda payload: validation)
        app.search_agent = SimpleNamespace(
            run=lambda **kw: {"found": {}, "not_found": [],
                              "searched_files": []})
        app.improvement_agent = SimpleNamespace(
            process=lambda intent, ctx: improvement
            or {"explanation": "", "issues": [], "improved_code": "",
                "changes": []})
        app.metrics_collector = SimpleNamespace(
            record=lambda rec: None, summarize_failures=lambda n: {
                "total_runs": 0, "avg_iterations": 0.0,
                "json_parse_failure_rate": 0.0})
        app.prompt_store = SimpleNamespace(
            get_version_label=lambda name: "hardcoded",
            get_current=lambda name: "")
        app.optimizer_enabled = False
        app.config = configparser.ConfigParser()
        return app

    @staticmethod
    def _parsed():
        from tools.prompt_parser import ParsedPrompt
        return ParsedPrompt(
            file_path="app.py", target_name="foo", target_type="function",
            intent="improve", raw="improve foo in app.py")

    def test_a_list_feedback_is_printed_not_raised(self, tmp_path, capsys):
        import main as M
        import tools.block_extractor as bx
        app = self._app({"status": "needs_fix",
                         "feedback": ["widen the search", "cite a line"],
                         "suggested_searches": []})
        src = tmp_path / "app.py"
        src.write_text("def foo():\n    return 1\n", encoding="utf-8")
        with patch.object(M, "parse_prompt", return_value=self._parsed()), \
             patch.object(bx, "extract_imports", return_value=[]), \
             patch.object(bx, "extract_block", return_value="def foo():\n    return 1"), \
             patch.object(bx, "find_references", return_value=[]), \
             patch.object(bx, "get_context_lines", return_value=[]), \
             patch("tools.file_reader.read_file", return_value=src.read_text()):
            app.run_pipeline("improve foo in app.py", str(tmp_path))
        out = capsys.readouterr().out
        assert "Validation – widen the search\ncite a line" in out

    def test_a_list_feedback_is_recorded_as_text_in_the_run_record(self, tmp_path):
        import main as M
        import tools.block_extractor as bx
        recorded: list = []
        app = self._app({"status": "needs_fix",
                         "feedback": ["widen the search", "cite a line"],
                         "suggested_searches": []})
        app.metrics_collector = SimpleNamespace(
            record=recorded.append, summarize_failures=lambda n: {
                "total_runs": 0, "avg_iterations": 0.0,
                "json_parse_failure_rate": 0.0})
        src = tmp_path / "app.py"
        src.write_text("def foo():\n    return 1\n", encoding="utf-8")
        with patch.object(M, "parse_prompt", return_value=self._parsed()), \
             patch.object(bx, "extract_imports", return_value=[]), \
             patch.object(bx, "extract_block", return_value="def foo():\n    return 1"), \
             patch.object(bx, "find_references", return_value=[]), \
             patch.object(bx, "get_context_lines", return_value=[]), \
             patch("tools.file_reader.read_file", return_value=src.read_text()):
            app.run_pipeline("improve foo in app.py", str(tmp_path))
        assert recorded
        assert recorded[0].validator_feedback == "widen the search\ncite a line"

    def test_the_api_error_branch_prints_a_list_feedback(self, tmp_path, capsys):
        import main as M
        import tools.block_extractor as bx
        app = self._app({"status": "needs_fix", "_api_error": True,
                         "feedback": ["HTTP 500", "try later"],
                         "suggested_searches": []})
        src = tmp_path / "app.py"
        src.write_text("def foo():\n    return 1\n", encoding="utf-8")
        with patch.object(M, "parse_prompt", return_value=self._parsed()), \
             patch.object(bx, "extract_imports", return_value=[]), \
             patch.object(bx, "extract_block", return_value="def foo():\n    return 1"), \
             patch.object(bx, "find_references", return_value=[]), \
             patch.object(bx, "get_context_lines", return_value=[]), \
             patch("tools.file_reader.read_file", return_value=src.read_text()), \
             patch("tools.backoff.api_error_pause", lambda n, chk: None):
            app.run_pipeline("improve foo in app.py", str(tmp_path))
        out = capsys.readouterr().out
        assert "HTTP 500" in out


# ── Part D: the saved patch does not apply to a CRLF or a non-UTF-8 file ─────

_AGENT_USER = "auto-agent-test"
_AGENT_EMAIL = "auto-agent-test@localhost"

#: (path, bytes at HEAD, bytes after the coder's edit). Two CRLF, two latin-1
#: and one plain UTF-8 file: the shapes a checkout of a Windows-authored or a
#: legacy file really has.
_EDITED = [
    ("w.txt", b"line1\r\nline2\r\n", b"line1\r\nCHANGED\r\n"),
    ("l.py", b"# caf\xe9 euro \xe9\nold = 1\n", b"# caf\xe9 euro \xe9\nNEW = 2\n"),
    ("u.py", "# utf8 caf\u00e9\nold = 1\n".encode(),
     "# utf8 caf\u00e9\nNEW = 2\n".encode()),
]

#: (path, bytes) — new files the coder left untracked.
_ADDED = [
    ("n_w.txt", b"new1\r\nnew2\r\n"),
    ("n_l.py", b"# new caf\xe9\nx = 3\n"),
]


def _git(cwd, *args) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=" + _AGENT_USER,
         "-c", "user.email=" + _AGENT_EMAIL, *args],
        cwd=cwd, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _repo(base: Path) -> None:
    """A one-commit repo holding every file of ``_EDITED`` at HEAD, with
    ``.agent/`` ignored before the run — as ``discard_working_changes``
    requires, and so the saved patch itself survives the clean."""
    base.mkdir(parents=True)
    (base / ".gitignore").write_text(".agent/\n")
    (base / "README.md").write_text("# repo\n")
    for rel, content, _ in _EDITED:
        (base / rel).write_bytes(content)
    _git(base, "init", "-q")
    _git(base, "add", "-A")
    _git(base, "commit", "-qm", "init")


def _controller(base: Path):
    """An ``AutoController`` with a real ``GitManager`` — the same shape as
    ``tests_bugfix/test_bugfix_harden5_c_failed_commit.py``, so the save under
    test is the controller's own."""
    from tools.auto.controller import AutoController, RunLimits
    ctrl = AutoController.__new__(AutoController)
    ctrl.goal = "t"
    ctrl.base_dir = base
    ctrl.agent_dir = base / ".agent"
    ctrl.limits = RunLimits()
    ctrl.git = make_git_manager(base, _cfg())
    return ctrl


def _cfg() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["auto"] = {"git_user": _AGENT_USER, "git_email": _AGENT_EMAIL}
    return cfg


class TestSavedPatchBytes:

    def test_a_crlf_and_a_latin1_file_round_trip_byte_for_byte(self, tmp_path):
        base = tmp_path / "repo"
        _repo(base)
        for rel, _, edited in _EDITED:
            (base / rel).write_bytes(edited)
        for rel, content in _ADDED:
            (base / rel).write_bytes(content)

        ctrl = _controller(base)
        patch_path = ctrl._save_uncommitted_patch(
            {"id": "D1"}, SimpleNamespace())

        assert patch_path and Path(patch_path).exists()
        # the patch holds git's own bytes: no carriage return was lost, and no
        # non-UTF-8 byte became U+FFFD
        raw = Path(patch_path).read_bytes()
        assert raw.count(b"\r\n") >= sum(
            c.count(b"\r\n") for _, _, c in _EDITED) + 2
        assert b"\xef\xbf\xbd" not in raw
        assert b"\xe9" in raw

        # the tree goes back to HEAD, as an exhausted task's discard would
        _git(base, "reset", "--hard", "-q")
        _git(base, "clean", "-fdq")
        assert _git(base, "status", "--porcelain") == ""

        proc = subprocess.run(["git", "apply", patch_path], cwd=base,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

        for rel, _, edited in _EDITED:
            assert (base / rel).read_bytes() == edited, rel
        for rel, content in _ADDED:
            assert (base / rel).read_bytes() == content, rel
        # the patch survived the clean: it lives in the ignored .agent/
        assert Path(patch_path).exists()

    def test_diff_working_tree_keeps_every_byte_git_printed(self, tmp_path):
        base = tmp_path / "repo"
        _repo(base)
        for rel, _, edited in _EDITED:
            (base / rel).write_bytes(edited)
        for rel, content in _ADDED:
            (base / rel).write_bytes(content)

        captured = make_git_manager(base, _cfg()).diff_working_tree()

        # CRLF and latin-1 both survive: the surrogateescape decode of the
        # bytes git printed re-encodes to exactly those bytes. `untracked_paths`
        # returns them in git's own order, which is sorted.
        expected = _git_bytes(base, "diff", "HEAD", "--binary", "--") + b"".join(
            _git_bytes(base, "diff", "--no-index", "--binary", "--",
                       "/dev/null", rel)
            for rel in sorted(p for p, _ in _ADDED)
        )
        assert captured.encode("utf-8", "surrogateescape") == expected
        assert "\r\n" in captured
        assert "\ufffd" not in captured

    def test_a_utf8_lf_patch_is_the_one_today_produced(self, tmp_path):
        """The common case is unchanged: an LF UTF-8 edit round-trips through a
        text-decoded diff exactly as it did before."""
        base = tmp_path / "repo"
        _repo(base)
        (base / "u.py").write_bytes("# utf8 caf\u00e9\nNEW = 2\n".encode())
        captured = make_git_manager(base, _cfg()).diff_working_tree()
        assert captured == _git_bytes(
            base, "diff", "HEAD", "--binary", "--").decode("utf-8")
        assert "\r" not in captured

    def test_a_failed_capture_leaves_the_work_in_the_tree(self, tmp_path):
        """Fail-open: git that cannot answer keeps the work where it is."""
        base = tmp_path / "repo"
        _repo(base)
        (base / "u.py").write_bytes(
            "# utf8 caf\u00e9\nNEW = 2\n".encode())
        ctrl = _controller(base)
        with patch.object(ctrl.git, "diff_working_tree",
                          side_effect=GitError("git exploded")):
            assert ctrl._save_uncommitted_patch({"id": "D2"},
                                                SimpleNamespace()) == ""
        assert (base / "u.py").read_bytes() == "# utf8 caf\u00e9\nNEW = 2\n".encode()


def _git_bytes(cwd, *args) -> bytes:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    assert proc.returncode in (0, 1), proc.stderr
    return proc.stdout
