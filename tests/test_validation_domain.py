"""tests/test_validator_domain.py — AUTO-DM-5: Validator configurable system prompt.

Verifies:

  Prompt constants:
    - _GATE2_SYSTEM_CODE, _GATE2_SYSTEM_DOCS, _GATE2_SYSTEM_CREATIVE are defined.
    - _GATE2_SYSTEM is an alias for _GATE2_SYSTEM_CODE (backward compat).
    - _GATE2_SYSTEMS maps each mode to the right constant.

  LLMGate2Validator.__init__ prompt selection:
    - task_mode="code"  with no config override → _GATE2_SYSTEM_CODE used.
    - task_mode="docs"  with no config override → _GATE2_SYSTEM_DOCS used.
    - task_mode="creative" with no config override → _GATE2_SYSTEM_CREATIVE used.
    - [validator_agent] system = <custom> in agents.ini overrides the built-in
      for task_mode="code" only.
    - Unknown task_mode falls back to _GATE2_SYSTEM_CODE (fail-safe).

  AUTO-CR-19-1 (update): the legacy bare [validator_agent] system key is
  code-specific (agents.ini ships a code-completeness prompt under that key).
  It used to be consulted as the fallback for *every* task_mode, so a
  creative/docs run with no system_creative / system_docs override silently
  inherited the code prompt, and Gate-2 judged prose against code-completeness
  criteria. The bare system key now applies to task_mode="code" only; other
  modes fall through to system_{mode} (if set) or their own built-in.

  approve() uses self._system:
    - approve() sends self._system, not the bare module constant.
    - Changing self._system after construction changes what approve() sends.

  Regression (existing tests continue to pass):
    - test_auto_loop1_validator_sees_code.py must still pass.
"""

from __future__ import annotations

import configparser
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.inner_loop import (
    LLMGate2Validator,
    _GATE2_SYSTEM,
    _GATE2_SYSTEM_CODE,
    _GATE2_SYSTEM_DOCS,
    _GATE2_SYSTEM_CREATIVE,
    _GATE2_SYSTEMS,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(system_override: str | None = None) -> configparser.ConfigParser:
    """Build a minimal ConfigParser, optionally with a [validator_agent] system key."""
    cfg = configparser.ConfigParser()
    d: dict = {
        "api":            {"active": "local", "verify_ssl": "true"},
        "api_local":      {"base_url": "http://localhost:9999", "model": "x",
                           "api_key": "", "api_format": "openai"},
        "validator_agent": {"temperature": "0.1", "max_hints": "3", "max_tokens": "512"},
        "auto":           {"exec_timeout_sec": "60"},
        "loop":           {"max_attempts": "3"},
    }
    if system_override is not None:
        d["validator_agent"]["system"] = system_override
    cfg.read_dict(d)
    return cfg


def _validator(task_mode: str = "code", config=None) -> LLMGate2Validator:
    return LLMGate2Validator(task_mode=task_mode, config=config)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level constants
# ─────────────────────────────────────────────────────────────────────────────

class TestPromptConstants:

    def test_gate2_system_code_is_defined(self) -> None:
        assert isinstance(_GATE2_SYSTEM_CODE, str) and len(_GATE2_SYSTEM_CODE) > 20

    def test_gate2_system_docs_is_defined(self) -> None:
        assert isinstance(_GATE2_SYSTEM_DOCS, str) and len(_GATE2_SYSTEM_DOCS) > 20

    def test_gate2_system_creative_is_defined(self) -> None:
        assert isinstance(_GATE2_SYSTEM_CREATIVE, str) and len(_GATE2_SYSTEM_CREATIVE) > 20

    def test_gate2_system_alias_equals_code(self) -> None:
        """_GATE2_SYSTEM must remain an alias of _GATE2_SYSTEM_CODE for backward compat."""
        assert _GATE2_SYSTEM is _GATE2_SYSTEM_CODE

    def test_gate2_systems_map_code(self) -> None:
        assert _GATE2_SYSTEMS["code"] is _GATE2_SYSTEM_CODE

    def test_gate2_systems_map_docs(self) -> None:
        assert _GATE2_SYSTEMS["docs"] is _GATE2_SYSTEM_DOCS

    def test_gate2_systems_map_creative(self) -> None:
        assert _GATE2_SYSTEMS["creative"] is _GATE2_SYSTEM_CREATIVE

    def test_docs_prompt_mentions_documentation(self) -> None:
        assert "document" in _GATE2_SYSTEM_DOCS.lower()

    def test_creative_prompt_mentions_creative(self) -> None:
        assert "creative" in _GATE2_SYSTEM_CREATIVE.lower()

    def test_code_prompt_mentions_code(self) -> None:
        assert "code" in _GATE2_SYSTEM_CODE.lower()

    def test_all_prompts_require_json_output(self) -> None:
        # code and docs modes still use JSON verdict contracts.
        # creative mode intentionally uses a line-oriented soft verdict (AUTO-CR-2).
        for name, prompt in _GATE2_SYSTEMS.items():
            if name == "creative":
                # Soft verdict: APPROVED/REVISE, no JSON contract required.
                assert "APPROVED" in prompt, f"{name} prompt missing 'APPROVED'"
                assert "REVISE" in prompt, f"{name} prompt missing 'REVISE'"
                assert '"approved"' not in prompt, f"{name} prompt must NOT have JSON 'approved' key"
            else:
                assert "approved" in prompt, f"{name} prompt missing 'approved'"
                assert "feedback" in prompt, f"{name} prompt missing 'feedback'"


# ─────────────────────────────────────────────────────────────────────────────
# LLMGate2Validator.__init__ prompt selection
# ─────────────────────────────────────────────────────────────────────────────

class TestValidatorSystemPromptSelection:

    # ── No config override — built-in selected by task_mode ──────────────────

    def test_code_mode_no_override_uses_code_prompt(self) -> None:
        v = _validator("code")
        assert v._system == _GATE2_SYSTEM_CODE

    def test_docs_mode_no_override_uses_docs_prompt(self) -> None:
        v = _validator("docs")
        assert v._system == _GATE2_SYSTEM_DOCS

    def test_creative_mode_no_override_uses_creative_prompt(self) -> None:
        v = _validator("creative")
        assert v._system == _GATE2_SYSTEM_CREATIVE

    def test_unknown_mode_falls_back_to_code_prompt(self) -> None:
        v = _validator("nonexistent_mode")
        assert v._system == _GATE2_SYSTEM_CODE

    def test_no_config_no_task_mode_defaults_to_code(self) -> None:
        """Constructing with no args at all must still work (all-defaults path)."""
        v = LLMGate2Validator()
        assert v._system == _GATE2_SYSTEM_CODE

    # ── agents.ini override wins regardless of task_mode ─────────────────────

    def test_config_override_wins_over_code_mode(self) -> None:
        cfg = _cfg(system_override="custom prompt")
        v = _validator("code", config=cfg)
        assert v._system == "custom prompt"

    def test_config_override_does_not_win_over_docs_mode(self) -> None:
        """AUTO-CR-19-1: the bare 'system' key is code-specific and must NOT
        leak into docs mode. With no system_docs override, docs mode falls
        through to its own built-in."""
        cfg = _cfg(system_override="custom prompt")
        v = _validator("docs", config=cfg)
        assert v._system == _GATE2_SYSTEM_DOCS

    def test_config_override_does_not_win_over_creative_mode(self) -> None:
        """AUTO-CR-19-1: same protection for creative mode — this is the exact
        rubber-stamp scenario the bug caused (legacy code prompt judging prose)."""
        cfg = _cfg(system_override="custom prompt")
        v = _validator("creative", config=cfg)
        assert v._system == _GATE2_SYSTEM_CREATIVE

    def test_config_without_system_key_uses_builtin(self) -> None:
        """Config present but without 'system' key → fallback to built-in."""
        cfg = _cfg(system_override=None)  # no system key in validator_agent
        v = _validator("docs", config=cfg)
        assert v._system == _GATE2_SYSTEM_DOCS

    def test_existing_live_agents_ini_override_respected(self) -> None:
        """Simulate the live agents.ini config which already has a custom
        code-completeness system prompt under the bare 'system' key.

        AUTO-CR-19-1: this legacy prompt is code-specific. It must still be
        honoured for task_mode="code" (no behavioural change there), but it
        must NOT leak into docs/creative — those fall back to their own
        built-ins instead of inheriting a code-completeness prompt.
        """
        live_prompt = (
            "You are a code completeness validator. Check: 1) Is the function body "
            "complete and not cut off? ..."
        )
        cfg = _cfg(system_override=live_prompt)

        v_code = _validator("code", config=cfg)
        assert v_code._system == live_prompt, "Live prompt must be preserved in code mode"

        v_docs = _validator("docs", config=cfg)
        assert v_docs._system == _GATE2_SYSTEM_DOCS, (
            "docs mode must NOT inherit the code-specific legacy prompt"
        )

        v_creative = _validator("creative", config=cfg)
        assert v_creative._system == _GATE2_SYSTEM_CREATIVE, (
            "creative mode must NOT inherit the code-specific legacy prompt "
            "(this was the AUTO-CR-19-1 rubber-stamp bug)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Medium #2 — mode-specific ini keys (system_docs / system_creative)
# mirrors Coder / Architect: each mode can be overridden independently
# ─────────────────────────────────────────────────────────────────────────────

def _cfg_mode_keys(
    system_docs: str | None = None,
    system_creative: str | None = None,
    system_legacy: str | None = None,
) -> configparser.ConfigParser:
    """Build a config that may have any combination of validator_agent keys."""
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":            {"active": "local"},
        "api_local":      {"base_url": "http://localhost:1337/v1",
                           "api_key": "jan", "model": "test-model",
                           "api_format": "openai"},
        "validator_agent": {},
    })
    if system_docs is not None:
        cfg.set("validator_agent", "system_docs", system_docs)
    if system_creative is not None:
        cfg.set("validator_agent", "system_creative", system_creative)
    if system_legacy is not None:
        cfg.set("validator_agent", "system", system_legacy)
    return cfg


class TestValidatorModeSpecificIniKeys:
    """system_docs / system_creative take priority over the legacy 'system' key.

    AUTO-CR-19-1: the legacy 'system' key itself only applies as a fallback
    for task_mode="code" — it is never consulted for docs/creative, even when
    no system_docs / system_creative override is set (see the dedicated
    test_legacy_system_key_does_not_leak_into_docs_without_mode_key below).
    """

    def test_system_docs_key_used_for_docs_mode(self) -> None:
        cfg = _cfg_mode_keys(system_docs="validator for documentation")
        v = _validator("docs", config=cfg)
        assert v._system == "validator for documentation"

    def test_system_creative_key_used_for_creative_mode(self) -> None:
        cfg = _cfg_mode_keys(system_creative="validator for creative writing")
        v = _validator("creative", config=cfg)
        assert v._system == "validator for creative writing"

    def test_system_docs_does_not_affect_code_mode(self) -> None:
        """system_docs must only activate for docs mode, not bleed into code."""
        from tools.auto.inner_loop import _GATE2_SYSTEM_CODE
        cfg = _cfg_mode_keys(system_docs="docs-only prompt")
        v = _validator("code", config=cfg)
        assert v._system == _GATE2_SYSTEM_CODE

    def test_system_docs_does_not_affect_creative_mode(self) -> None:
        """system_docs must not override creative mode — modes are independent."""
        from tools.auto.inner_loop import _GATE2_SYSTEM_CREATIVE
        cfg = _cfg_mode_keys(system_docs="docs-only prompt")
        v = _validator("creative", config=cfg)
        assert v._system == _GATE2_SYSTEM_CREATIVE

    def test_mode_specific_key_wins_over_legacy_system_key(self) -> None:
        """system_docs beats legacy 'system' when both are present."""
        cfg = _cfg_mode_keys(
            system_docs="mode-specific docs prompt",
            system_legacy="old catch-all prompt",
        )
        v = _validator("docs", config=cfg)
        assert v._system == "mode-specific docs prompt"

    def test_legacy_system_key_still_overrides_code_mode(self) -> None:
        """Backward compat: a config with only 'system' still overrides code mode."""
        cfg = _cfg_mode_keys(system_legacy="legacy custom prompt")
        v = _validator("code", config=cfg)
        assert v._system == "legacy custom prompt"

    def test_legacy_system_key_does_not_leak_into_docs_without_mode_key(self) -> None:
        """AUTO-CR-19-1: if system_docs is absent, the legacy 'system' key
        must NOT win — docs mode falls back to its own built-in instead of
        silently inheriting the code-specific prompt."""
        cfg = _cfg_mode_keys(system_legacy="legacy custom prompt")
        v = _validator("docs", config=cfg)
        assert v._system == _GATE2_SYSTEM_DOCS

    def test_independent_overrides_across_all_modes(self) -> None:
        """All three modes can each have their own prompt without interfering."""
        from tools.auto.inner_loop import _GATE2_SYSTEM_CODE
        cfg = _cfg_mode_keys(
            system_docs="docs validator",
            system_creative="creative validator",
        )
        v_code     = _validator("code",     config=cfg)
        v_docs     = _validator("docs",     config=cfg)
        v_creative = _validator("creative", config=cfg)
        assert v_code._system     == _GATE2_SYSTEM_CODE   # no override for code
        assert v_docs._system     == "docs validator"
        assert v_creative._system == "creative validator"


# ─────────────────────────────────────────────────────────────────────────────
# approve() uses self._system, not the bare module constant
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _FakeCoderResult:
    succeeded: bool = True
    files_written: list = field(default_factory=list)
    error: str = ""


@dataclass
class _FakeExecResult:
    passed: bool = True
    exit_code: int = 0
    stdout: str = "ok"
    stderr: str = ""
    traceback: str = ""
    timed_out: bool = False


_TASK = {"id": "T1", "instruction": "add a docstring", "acceptance_check": "true"}


class TestApproveUsesSelfSystem:

    def _capture_system(self, validator: LLMGate2Validator) -> str:
        """Run approve() with a patched LLM and capture the system message sent."""
        captured: dict = {}

        def _fake_request(url, headers, payload, **kwargs):
            captured["system"] = payload["messages"][0]["content"]
            return '{"approved": true, "feedback": "ok", "hints": []}'

        with patch("tools.llm_stream.request_completion", side_effect=_fake_request):
            validator.approve(_TASK, _FakeExecResult(), _FakeCoderResult())

        return captured.get("system", "")

    def test_code_mode_sends_code_prompt(self) -> None:
        v = _validator("code")
        sent = self._capture_system(v)
        assert sent == _GATE2_SYSTEM_CODE

    def test_docs_mode_sends_docs_prompt(self) -> None:
        v = _validator("docs")
        sent = self._capture_system(v)
        assert sent == _GATE2_SYSTEM_DOCS

    def test_creative_mode_sends_creative_prompt(self) -> None:
        v = _validator("creative")
        sent = self._capture_system(v)
        assert sent == _GATE2_SYSTEM_CREATIVE

    def test_config_override_prompt_is_sent(self) -> None:
        """AUTO-CR-19-1: a bare 'system' override does not apply to docs mode,
        so approve() sends the docs built-in, not the override."""
        cfg = _cfg(system_override="OVERRIDE PROMPT")
        v = _validator("docs", config=cfg)
        sent = self._capture_system(v)
        assert sent == _GATE2_SYSTEM_DOCS

    def test_code_mode_config_override_prompt_is_sent(self) -> None:
        """Regression: a bare 'system' override still applies to code mode."""
        cfg = _cfg(system_override="OVERRIDE PROMPT")
        v = _validator("code", config=cfg)
        sent = self._capture_system(v)
        assert sent == "OVERRIDE PROMPT"

    def test_mutating_self_system_changes_what_approve_sends(self) -> None:
        """approve() reads self._system at call time — mutating it changes the prompt."""
        v = _validator("code")
        v._system = "MUTATED PROMPT"
        sent = self._capture_system(v)
        assert sent == "MUTATED PROMPT"

    def test_approve_does_not_use_bare_module_constant(self) -> None:
        """Even if task_mode is unknown, approve() must use self._system (the fallback),
        never reach past self._system to grab _GATE2_SYSTEM_CODE directly.
        """
        v = _validator("code")
        v._system = "SENTINEL"  # replace after construction
        sent = self._capture_system(v)
        assert sent == "SENTINEL", "approve() bypassed self._system"
