"""tests/test_cr19_validator_prompt.py — AUTO-CR-19-1: Gate-2 validator
system-prompt resolution must not let the legacy code-specific
``[validator_agent] system`` key leak into non-code ``task_mode``s.

Background
----------
``agents.ini`` / ``agents_32k.ini`` ship a code-specific legacy
``[validator_agent] system`` key ("You are a code completeness
validator…").  Before this fix, ``_resolve_validator_system`` (and the
inline logic it replaced) fell back to that bare ``system`` key for
*every* task_mode when no mode-specific override (``system_creative``,
``system_docs``, …) was set — so a creative run silently inherited the
code-completeness prompt and Gate-2 rubber-stamped every chapter.

These tests cover both the standalone resolver function and the
``LLMGate2Validator`` construction path that wraps it.
"""

import configparser

import pytest

from tools.auto.inner_loop import (
    LLMGate2Validator,
    _resolve_validator_system,
    _GATE2_SYSTEM_CREATIVE,
    _GATE2_SYSTEM_DOCS,
    _GATE2_SYSTEM_CODE,
)


_LEGACY_CODE_SYSTEM = (
    "You are a code completeness validator. Check: 1) Is the function body "
    "complete and not cut off? 2) Are all called names either in imports, "
    "related_code, or standard library? 3) Any missing definitions that "
    "should be found? Reply exactly: APPROVED if complete. "
    "REJECTED: <one-line reason> | MISSING: name1, name2 if incomplete."
)


def _cfg(**validator_agent_kv) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict({"validator_agent": validator_agent_kv})
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Core resolver tests (spec'd in AUTO-CR-19-1)
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveValidatorSystem:

    def test_creative_ignores_legacy_system_key(self):
        """Legacy code-specific `system` set, no `system_creative` override,
        task_mode=creative → resolves to the creative builtin, never the
        legacy code prompt."""
        cfg = _cfg(system=_LEGACY_CODE_SYSTEM)
        resolved = _resolve_validator_system(cfg, "creative")

        assert resolved == _GATE2_SYSTEM_CREATIVE
        assert "APPROVED" in resolved
        assert "REVISE" in resolved
        assert "function" not in resolved.lower()
        # Not the legacy JSON-contract code prompt (the creative builtin's
        # own "Do NOT return JSON" guard is fine — that's a negative
        # instruction, not a JSON-output contract like the code prompt's).
        assert '"approved"' not in resolved
        assert "JSON object" not in resolved

    def test_creative_system_creative_override_wins(self):
        """An explicit `system_creative` override always wins, regardless of
        the legacy `system` key."""
        cfg = _cfg(system=_LEGACY_CODE_SYSTEM, system_creative="X")
        resolved = _resolve_validator_system(cfg, "creative")
        assert resolved == "X"

    def test_code_mode_still_uses_legacy_system(self):
        """Regression: code mode must still consult the bare `system` key —
        only non-code modes are walled off from it."""
        cfg = _cfg(system=_LEGACY_CODE_SYSTEM)
        resolved = _resolve_validator_system(cfg, "code")
        assert resolved == _LEGACY_CODE_SYSTEM

    def test_docs_mode_also_ignores_legacy_system_key(self):
        """Same protection extends to docs mode (not just creative)."""
        cfg = _cfg(system=_LEGACY_CODE_SYSTEM)
        resolved = _resolve_validator_system(cfg, "docs")
        assert resolved == _GATE2_SYSTEM_DOCS

    def test_docs_system_docs_override_wins(self):
        cfg = _cfg(system=_LEGACY_CODE_SYSTEM, system_docs="Y")
        resolved = _resolve_validator_system(cfg, "docs")
        assert resolved == "Y"

    def test_creative_with_no_keys_at_all_falls_back_to_builtin(self):
        """No `system` and no `system_creative` set → builtin, same as before."""
        cfg = _cfg()
        resolved = _resolve_validator_system(cfg, "creative")
        assert resolved == _GATE2_SYSTEM_CREATIVE

    def test_code_mode_with_no_system_key_falls_back_to_code_builtin(self):
        cfg = _cfg()
        resolved = _resolve_validator_system(cfg, "code")
        assert resolved == _GATE2_SYSTEM_CODE

    def test_config_none_falls_back_to_builtin_for_mode(self):
        assert _resolve_validator_system(None, "creative") == _GATE2_SYSTEM_CREATIVE
        assert _resolve_validator_system(None, "code") == _GATE2_SYSTEM_CODE

    def test_unrecognised_mode_falls_back_to_code_builtin(self):
        """An unknown task_mode (no entry in _GATE2_SYSTEMS) degrades to the
        code builtin rather than raising."""
        cfg = _cfg()
        resolved = _resolve_validator_system(cfg, "some_future_mode")
        assert resolved == _GATE2_SYSTEM_CODE

    def test_legacy_system_value_is_stripped(self):
        """Whitespace around the configured value is trimmed."""
        cfg = _cfg(system="  " + _LEGACY_CODE_SYSTEM + "  \n")
        resolved = _resolve_validator_system(cfg, "code")
        assert resolved == _LEGACY_CODE_SYSTEM


# ─────────────────────────────────────────────────────────────────────────────
# LLMGate2Validator construction — confirms the wiring inside __init__
# uses the fixed resolver, not a re-implementation that could drift.
# ─────────────────────────────────────────────────────────────────────────────

class TestLLMGate2ValidatorWiring:

    def _make_validator(self, task_mode: str, cfg: configparser.ConfigParser) -> LLMGate2Validator:
        # Other sections referenced by __init__ (search_agent / coder probe)
        # are optional; LLMGate2Validator tolerates a minimal config.
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

    def test_creative_validator_does_not_inherit_legacy_code_prompt(self):
        cfg = _cfg(system=_LEGACY_CODE_SYSTEM)
        v = self._make_validator("creative", cfg)
        assert v._system == _GATE2_SYSTEM_CREATIVE
        assert "code completeness validator" not in v._system

    def test_code_validator_still_gets_legacy_prompt(self):
        cfg = _cfg(system=_LEGACY_CODE_SYSTEM)
        v = self._make_validator("code", cfg)
        assert v._system == _LEGACY_CODE_SYSTEM

    def test_creative_validator_honours_explicit_override(self):
        cfg = _cfg(system=_LEGACY_CODE_SYSTEM, system_creative="custom creative prompt")
        v = self._make_validator("creative", cfg)
        assert v._system == "custom creative prompt"
