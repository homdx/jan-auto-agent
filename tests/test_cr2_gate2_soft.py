"""tests/test_cr2_gate2_soft.py — AUTO-CR-2: Soft verdict for Gate-2 validator (creative).

Tests cover _parse_verdict_soft() and verify that the creative mode of
LLMGate2Validator uses the soft path while code/docs modes are unchanged.
"""

import configparser
import json
from types import SimpleNamespace
from unittest.mock import patch


from tools.auto.inner_loop import (
    LLMGate2Validator,
    _GATE2_SYSTEM_CREATIVE,
    _parse_verdict_soft,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_validator(task_mode: str = "creative") -> LLMGate2Validator:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":              {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url":    "http://localhost:11434",
            "api_key":     "test",
            "model":       "llama3.1:8b",
            "api_format":  "ollama",
            "num_ctx":     "4096",
        },
        "validator_agent":  {"temperature": "0.1", "max_hints": "3", "max_tokens": "256"},
        "coder":            {"context_probe": "false"},
        "loop":             {"timeout_seconds": "30"},
    })
    return LLMGate2Validator(
        base_url="http://localhost:11434",
        model="llama3.1:8b",
        api_key="test",
        api_format="ollama",
        temperature=0.1,
        timeout=30,
        max_hints=3,
        task_mode=task_mode,
        config=cfg,
    )


_DUMMY_TASK        = {"id": "cr2-test", "instruction": "Write chapter 3.", "target_files": ["chapter_03.md"]}
_DUMMY_EXEC_RESULT = SimpleNamespace(passed=True, exit_code=0, stdout="", stderr="")
_DUMMY_CODER_RESULT = SimpleNamespace(succeeded=True, files_written=["chapter_03.md"])


# ─────────────────────────────────────────────────────────────────────────────
# _parse_verdict_soft unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestParseVerdictSoft:

    def test_approved_token(self):
        approved, reason, unparseable = _parse_verdict_soft("APPROVED")
        assert approved is True
        assert unparseable is False

    def test_approved_lowercase(self):
        approved, _, unparseable = _parse_verdict_soft("approved")
        assert approved is True
        assert unparseable is False

    def test_approved_with_trailing_text(self):
        approved, _, unparseable = _parse_verdict_soft("APPROVED — chapter reads well")
        assert approved is True
        assert unparseable is False

    def test_ok_token(self):
        approved, _, unparseable = _parse_verdict_soft("OK the chapter is fine")
        assert approved is True
        assert unparseable is False

    def test_ok_lowercase(self):
        approved, _, _ = _parse_verdict_soft("ok")
        assert approved is True

    def test_revise_with_reason(self):
        raw = "REVISE: the duel in para 3 contradicts the setup"
        approved, reason, unparseable = _parse_verdict_soft(raw)
        assert approved is False
        assert unparseable is False
        assert "duel in para 3" in reason
        assert "contradicts" in reason

    def test_reject_with_reason(self):
        approved, reason, unparseable = _parse_verdict_soft("REJECT: Elena's name changes mid-scene")
        assert approved is False
        assert unparseable is False
        assert "Elena" in reason

    def test_no_with_reason(self):
        approved, reason, unparseable = _parse_verdict_soft("NO: missing the confrontation scene")
        assert approved is False
        assert unparseable is False
        assert "confrontation" in reason

    def test_revise_no_colon_reason(self):
        """REVISE with no reason still rejected, default reason supplied."""
        approved, reason, unparseable = _parse_verdict_soft("REVISE")
        assert approved is False
        assert unparseable is False
        assert reason  # non-empty fallback

    def test_unparseable_fail_open_approves(self):
        """A rambling reply → approved=True (fail-open), unparseable=True."""
        rambling = (
            "I think the chapter is quite good but the pacing in the second "
            "act feels rushed and you might want to slow down the reveal."
        )
        approved, reason, unparseable = _parse_verdict_soft(rambling)
        assert approved is True
        assert unparseable is True
        # reason should indicate the fail-open behaviour
        assert "unparseable" in reason.lower() or "fail-open" in reason.lower()

    def test_unparseable_json_blob(self):
        """A JSON blob (old creative validator output) → fail-open."""
        blob = '{"approved": false, "feedback": "needs work"}'
        approved, _, unparseable = _parse_verdict_soft(blob)
        assert approved is True
        assert unparseable is True

    def test_empty_input_fail_open(self):
        approved, reason, unparseable = _parse_verdict_soft("")
        assert approved is True
        assert unparseable is True

    def test_leading_blank_lines_skipped(self):
        """Blank lines before the verdict line are ignored."""
        approved, _, unparseable = _parse_verdict_soft("\n\n  \nAPPROVED\n")
        assert approved is True
        assert unparseable is False

    def test_revise_case_insensitive(self):
        approved, reason, unparseable = _parse_verdict_soft("revise: plot hole in act 2")
        assert approved is False
        assert unparseable is False
        assert "plot hole" in reason


# ─────────────────────────────────────────────────────────────────────────────
# LLMGate2Validator integration tests (mock LLM call)
# ─────────────────────────────────────────────────────────────────────────────

class TestLLMGate2ValidatorCreative:

    def _approve_with_raw(self, raw_llm_reply: str, task_mode: str = "creative"):
        """Run validator.approve() with a mocked LLM that returns raw_llm_reply."""
        validator = _make_validator(task_mode)
        with (
            patch("tools.llm_stream.request_completion", return_value=raw_llm_reply),
            patch.object(validator, "_read_changed_content", return_value="(prose content)"),
        ):
            return validator.approve(
                _DUMMY_TASK, _DUMMY_EXEC_RESULT, _DUMMY_CODER_RESULT,
                base_dir="/tmp",
            )

    def test_approved_token_returns_true(self):
        ok, fb = self._approve_with_raw("APPROVED")
        assert ok is True
        assert fb == ""

    def test_revise_with_reason_returns_false_with_feedback(self):
        ok, fb = self._approve_with_raw(
            "REVISE: the duel in para 3 contradicts the setup"
        )
        assert ok is False
        assert "duel in para 3" in fb

    def test_unparseable_fail_open_approves_not_unavailable(self):
        """Rambling reply → approved (fail-open), NOT 'validator unavailable'."""
        ok, fb = self._approve_with_raw(
            "I really think the chapter could use more tension in the middle section."
        )
        assert ok is True
        # Must NOT surface the old 'validator unavailable' error message
        assert "unavailable" not in fb.lower()

    def test_last_missing_context_cleared_on_creative_approve(self):
        """last_missing_context is reset each call (no stale state)."""
        validator = _make_validator("creative")
        with (
            patch("tools.llm_stream.request_completion", return_value="APPROVED"),
            patch.object(validator, "_read_changed_content", return_value=""),
        ):
            validator.approve(_DUMMY_TASK, _DUMMY_EXEC_RESULT, _DUMMY_CODER_RESULT)
        assert validator.last_missing_context == []


# ─────────────────────────────────────────────────────────────────────────────
# Regression: code mode JSON path unchanged
# ─────────────────────────────────────────────────────────────────────────────

class TestCodeModeJsonVerdictUnchanged:

    def _approve_code(self, raw_llm_reply: str):
        validator = _make_validator("code")
        with (
            patch("tools.llm_stream.request_completion", return_value=raw_llm_reply),
            patch.object(validator, "_read_changed_content", return_value="(code content)"),
        ):
            return validator.approve(
                _DUMMY_TASK, _DUMMY_EXEC_RESULT, _DUMMY_CODER_RESULT,
                base_dir="/tmp",
            )

    def test_valid_json_approved(self):
        payload = json.dumps({"approved": True, "feedback": "looks good"})
        ok, fb = self._approve_code(payload)
        assert ok is True

    def test_valid_json_rejected_with_feedback(self):
        payload = json.dumps({
            "approved": False,
            "feedback": "missing error handling",
            "hints": ["add try/except around line 42"],
        })
        ok, fb = self._approve_code(payload)
        assert ok is False
        assert "missing error handling" in fb

    def test_plain_prose_fails_closed_in_code_mode(self):
        """Plain APPROVED text is not valid JSON → fail-closed in code mode."""
        ok, fb = self._approve_code("APPROVED")
        assert ok is False
        assert "unavailable" in fb.lower() or "decode" in fb.lower() or "json" in fb.lower()


# ─────────────────────────────────────────────────────────────────────────────
# System prompt tests
# ─────────────────────────────────────────────────────────────────────────────

def test_creative_system_prompt_no_json_contract():
    """Creative Gate-2 system prompt must NOT require JSON output."""
    assert '"approved"' not in _GATE2_SYSTEM_CREATIVE
    assert "JSON object" not in _GATE2_SYSTEM_CREATIVE

def test_creative_system_prompt_instructs_one_line_verdict():
    """Creative system prompt must keep the APPROVED/REVISE verdict protocol and
    (AUTO-CR-29) ask for a numbered, multi-point critique on REVISE."""
    assert "APPROVED" in _GATE2_SYSTEM_CREATIVE
    assert "REVISE" in _GATE2_SYSTEM_CREATIVE
    # first token still pinned for the parser
    assert "FIRST token" in _GATE2_SYSTEM_CREATIVE or "first token" in _GATE2_SYSTEM_CREATIVE.lower()
    # now expects a list of every problem, not a single sentence
    assert "NUMBERED LIST" in _GATE2_SYSTEM_CREATIVE
    assert "EVERY problem" in _GATE2_SYSTEM_CREATIVE

def test_creative_validator_uses_creative_system_prompt():
    """LLMGate2Validator in creative mode loads the creative (non-JSON) prompt."""
    v = _make_validator("creative")
    assert '"approved"' not in v._system
    assert "APPROVED" in v._system

def test_code_validator_uses_json_system_prompt():
    """LLMGate2Validator in code mode still loads the JSON-contract prompt."""
    v = _make_validator("code")
    assert '"approved"' in v._system
