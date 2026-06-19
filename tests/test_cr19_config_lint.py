"""tests/test_cr19_config_lint.py — AUTO-CR-19-4: startup config lint that
guards AUTO-CR-19-1 from regressing.

Background
----------
AUTO-CR-19-1 fixed the Gate-2 validator system-prompt resolution so the
legacy code-specific ``[validator_agent] system`` key cannot leak into a
non-code ``task_mode``. Nothing, however, warns a user/operator when that
exact trap is configured — e.g. a future edit re-adds a generic
``[coder] system`` or ``[architect] system`` override without a matching
``system_{mode}`` variant. ``_lint_mode_config`` is pure, side-effect-free
logging (via the module logger) that fires once at controller startup, right
after ``task_mode`` is normalised.

These tests exercise the standalone ``_lint_mode_config`` helper directly
(unit-testable in isolation, mirroring the ``test_cr19_validator_prompt.py``
style) rather than spinning up a full ``AutoController``.
"""

import configparser
import logging

from tools.auto.controller import _lint_mode_config


def _cfg(sections: dict) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict(sections)
    return cfg


_LEGACY_CODE_SYSTEM = (
    "You are a code completeness validator. Check: 1) Is the function body "
    "complete and not cut off? 2) Are all called names either in imports, "
    "related_code, or standard library? 3) Any missing definitions that "
    "should be found? Reply exactly: APPROVED if complete. "
    "REJECTED: <one-line reason> | MISSING: name1, name2 if incomplete."
)


# ─────────────────────────────────────────────────────────────────────────────
# Core spec'd tests (from AUTO-CR-19-4)
# ─────────────────────────────────────────────────────────────────────────────

class TestLegacySystemKeyWarning:

    def test_warns_on_legacy_system_in_creative(self, caplog):
        """[validator_agent] system set, no system_creative override,
        task_mode=creative → a WARNING is emitted naming the trap."""
        cfg = _cfg({"validator_agent": {"system": _LEGACY_CODE_SYSTEM}})

        with caplog.at_level(logging.WARNING, logger="tools.auto.controller"):
            warnings = _lint_mode_config(cfg, "creative")

        assert len(warnings) == 1
        assert "[validator_agent] system" in warnings[0]
        assert "task_mode=creative" in warnings[0]
        assert "system_creative" in warnings[0]
        assert "AUTO-CR-19-1" in warnings[0]
        assert any("validator_agent" in msg for msg in caplog.messages)

    def test_no_warning_when_override_present(self, caplog):
        """An explicit system_creative override alongside the legacy key →
        no warning; the trap is already defused."""
        cfg = _cfg({
            "validator_agent": {
                "system": _LEGACY_CODE_SYSTEM,
                "system_creative": "You are a fiction continuity reviewer...",
            }
        })

        with caplog.at_level(logging.WARNING, logger="tools.auto.controller"):
            warnings = _lint_mode_config(cfg, "creative")

        assert warnings == []
        assert caplog.messages == []

    def test_no_warning_in_code_mode(self, caplog):
        """code mode is exactly where the bare `system` key is *supposed* to
        apply — never lint it there."""
        cfg = _cfg({"validator_agent": {"system": _LEGACY_CODE_SYSTEM}})

        with caplog.at_level(logging.WARNING, logger="tools.auto.controller"):
            warnings = _lint_mode_config(cfg, "code")

        assert warnings == []
        assert caplog.messages == []

    def test_no_warning_when_no_legacy_key_set_at_all(self):
        """Nothing configured (relying purely on builtins) → no warning."""
        cfg = _cfg({"validator_agent": {}})
        assert _lint_mode_config(cfg, "creative") == []

    def test_config_none_is_a_safe_noop(self):
        """No config object at all (e.g. very early startup) must never
        raise — pure logging, fail-open."""
        assert _lint_mode_config(None, "creative") == []
        assert _lint_mode_config(None, "code") == []


# ─────────────────────────────────────────────────────────────────────────────
# Extends to [coder] system / [architect] system, not just [validator_agent]
# ─────────────────────────────────────────────────────────────────────────────

class TestOtherLegacySystemSections:

    def test_warns_on_legacy_coder_system_in_docs_mode(self):
        cfg = _cfg({"coder": {"system": "You are a code generator..."}})
        warnings = _lint_mode_config(cfg, "docs")
        assert len(warnings) == 1
        assert "[coder] system" in warnings[0]
        assert "system_docs" in warnings[0]

    def test_warns_on_legacy_architect_system_in_creative_mode(self):
        cfg = _cfg({"architect": {"system": "You are a software architect..."}})
        warnings = _lint_mode_config(cfg, "creative")
        assert len(warnings) == 1
        assert "[architect] system" in warnings[0]
        assert "system_creative" in warnings[0]

    def test_multiple_offending_sections_each_get_their_own_warning(self):
        cfg = _cfg({
            "validator_agent": {"system": _LEGACY_CODE_SYSTEM},
            "coder": {"system": "code gen prompt"},
            "architect": {"system": "architect prompt"},
        })
        warnings = _lint_mode_config(cfg, "creative")
        assert len(warnings) == 3
        joined = " ".join(warnings)
        assert "[validator_agent] system" in joined
        assert "[coder] system" in joined
        assert "[architect] system" in joined

    def test_mode_specific_override_defuses_each_section_independently(self):
        """Each section's own system_{mode} override only defuses that
        section — an override on one doesn't silence warnings for another."""
        cfg = _cfg({
            "validator_agent": {
                "system": _LEGACY_CODE_SYSTEM,
                "system_creative": "fiction reviewer prompt",
            },
            "coder": {"system": "code gen prompt"},
        })
        warnings = _lint_mode_config(cfg, "creative")
        assert len(warnings) == 1
        assert "[coder] system" in warnings[0]


# ─────────────────────────────────────────────────────────────────────────────
# agents_4k.ini-style small num_ctx left in place for creative mode
# ─────────────────────────────────────────────────────────────────────────────

class TestSmallNumCtxWarning:

    def test_warns_on_small_num_ctx_in_creative_without_override(self):
        cfg = _cfg({
            "api": {"active": "local"},
            "api_local": {"num_ctx": "4096"},
        })
        warnings = _lint_mode_config(cfg, "creative")
        assert len(warnings) == 1
        assert "num_ctx=4096" in warnings[0]
        assert "num_ctx_creative" in warnings[0]

    def test_no_warning_when_num_ctx_creative_override_set(self):
        cfg = _cfg({
            "api": {"active": "local"},
            "api_local": {"num_ctx": "4096"},
            "coder": {"num_ctx_creative": "16384"},
        })
        assert _lint_mode_config(cfg, "creative") == []

    def test_no_warning_when_num_ctx_already_large(self):
        """A 32k-style base num_ctx is fine even with no override (matches
        agents_32k.ini, which intentionally leaves num_ctx_creative unset)."""
        cfg = _cfg({
            "api": {"active": "local"},
            "api_local": {"num_ctx": "32768"},
        })
        assert _lint_mode_config(cfg, "creative") == []

    def test_no_num_ctx_warning_outside_creative_mode(self):
        """The num_ctx heuristic is creative-specific (docs mode prompts are
        much smaller); docs mode must not trigger it."""
        cfg = _cfg({
            "api": {"active": "local"},
            "api_local": {"num_ctx": "4096"},
        })
        assert _lint_mode_config(cfg, "docs") == []

    def test_missing_num_ctx_entirely_does_not_warn(self):
        """No [api_local] num_ctx at all → fallback is 0, which is treated as
        'unset', not 'too small'; nothing to warn about."""
        cfg = _cfg({"api": {"active": "local"}})
        assert _lint_mode_config(cfg, "creative") == []

    def test_non_default_active_profile_is_respected(self):
        """The lint must read num_ctx from whichever profile [api] active
        points at, not always api_local."""
        cfg = _cfg({
            "api": {"active": "remote"},
            "api_remote": {"num_ctx": "4096"},
        })
        warnings = _lint_mode_config(cfg, "creative")
        assert len(warnings) == 1
        assert "api_remote" in warnings[0]
        assert "num_ctx=4096" in warnings[0]


# ─────────────────────────────────────────────────────────────────────────────
# Real-world fixture: agents_32k.ini (after CR-19-1's recommended addition)
# must be silent; the un-patched legacy shape must warn.
# ─────────────────────────────────────────────────────────────────────────────

class TestAgentsIniShapes:

    def test_agents_32k_style_config_is_silent(self):
        """Mirrors the *current* agents_32k.ini shape: legacy `system` key
        present, but `system_creative` was added alongside it (AUTO-CR-19-1),
        and num_ctx (32768) is well above the small-context threshold."""
        cfg = _cfg({
            "api": {"active": "local"},
            "api_local": {"num_ctx": "32768"},
            "validator_agent": {
                "system": _LEGACY_CODE_SYSTEM,
                "system_creative": "fiction continuity reviewer prompt",
            },
        })
        assert _lint_mode_config(cfg, "creative") == []

    def test_pre_cr19_1_config_shape_warns(self):
        """Mirrors the *pre-fix* config shape this lint exists to catch:
        legacy `system` key only, no `system_creative` at all."""
        cfg = _cfg({
            "api": {"active": "local"},
            "api_local": {"num_ctx": "32768"},
            "validator_agent": {"system": _LEGACY_CODE_SYSTEM},
        })
        warnings = _lint_mode_config(cfg, "creative")
        assert len(warnings) == 1
        assert "validator_agent" in warnings[0]


# ─────────────────────────────────────────────────────────────────────────────
# Logging side-effect: warnings are actually emitted via the module logger,
# not just returned (the controller startup call site relies on the log
# side-effect, not the return value, to surface this to operators).
# ─────────────────────────────────────────────────────────────────────────────

class TestLoggingSideEffect:

    def test_each_warning_is_logged_at_warning_level(self, caplog):
        cfg = _cfg({"validator_agent": {"system": _LEGACY_CODE_SYSTEM}})
        with caplog.at_level(logging.WARNING, logger="tools.auto.controller"):
            _lint_mode_config(cfg, "creative")

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) == 1
        assert "validator_agent" in warning_records[0].message

    def test_no_log_records_at_all_when_clean(self, caplog):
        cfg = _cfg({
            "validator_agent": {
                "system": _LEGACY_CODE_SYSTEM,
                "system_creative": "fiction reviewer prompt",
            }
        })
        with caplog.at_level(logging.WARNING, logger="tools.auto.controller"):
            _lint_mode_config(cfg, "creative")

        assert caplog.records == []
