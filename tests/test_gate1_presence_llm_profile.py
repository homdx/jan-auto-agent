"""tests/test_gate1_presence_llm_profile.py — GATE1-PROVIDER-1/2.

Field request: Gate 1's presence check (`_check_presence` — the only LLM
call Gate1Filter makes; existence checks are pure filesystem/AST) should
be able to use a completely different provider/model than the rest of the
pipeline (Coder, Architect, ...), configured via `[gate1] presence_llm_profile
= <section name>`.

The explicit worry driving this: a single global `[gate1] think = false` /
shared `[api_active]` model+URL is tuned for ONE provider. Pointing
presence-check calls at a different, independently-tuned provider must
NOT let settings leak across models in either direction:

  * A profile's own think/temperature/max_tokens/num_ctx must win when set.
  * Anything the profile leaves unset must fall back to THIS instance's own
    [gate1] value — never a hardcoded default, and never another profile's
    value.
  * The module-level "does this endpoint accept the `reasoning` field"
    memory (tools.llm_stream, GATE1-PROVIDER-1) is keyed by (url, model),
    so the shared provider and the presence profile — different url+model
    pairs — never contaminate each other's cached verdict, in either
    direction (same model/different provider, or different model/same
    provider router).
  * Misconfiguration (missing section, missing required keys) fails loudly
    at construction time rather than silently falling back to the shared
    provider's credentials against a URL they were never meant for.
"""

from __future__ import annotations

import configparser
import json
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.llm_stream as llm_stream_mod
from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import Gate1Filter


@pytest.fixture(autouse=True)
def _reset_reasoning_cache():
    """Process-lifetime global state (tools.llm_stream) — reset around
    every test so tests in this file can't leak into each other or into
    tests in other files, regardless of run order."""
    llm_stream_mod._REASONING_UNSUPPORTED_KEYS.clear()
    yield
    llm_stream_mod._REASONING_UNSUPPORTED_KEYS.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _base_dict(**gate1_overrides) -> dict:
    gate1 = {"temperature": "0.0", "max_tokens": "64", "skip_llm": "false"}
    gate1.update(gate1_overrides)
    return {
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url":   "http://main.example/v1",
            "api_key":    "main-key",
            "model":      "main-model",
            "api_format": "openai",
            "num_ctx":    "9999",
        },
        "gate1": gate1,
        "loop": {"timeout_seconds": "10"},
    }


def _make_filter(cfg_dict: dict) -> Gate1Filter:
    cfg = configparser.ConfigParser()
    cfg.read_dict(cfg_dict)
    return Gate1Filter(
        config=cfg, base_url="http://main.example/v1", api_key="main-key",
        model="main-model", api_format="openai", verify_ssl=False,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "utils.py").write_text(
        textwrap.dedent("""\
            def parse_config(raw):
                return raw
        """),
        encoding="utf-8",
    )
    return tmp_path


def _candidate(*, symbol: str = "parse_config", new_file: bool = False,
               file: str = "tools/utils.py") -> CandidateTask:
    return CandidateTask(
        title="Add input validation", instruction="validate that raw is a dict",
        target_files=[file], acceptance_check="true",
        cited_location=CitedLocation(file=file, symbol=symbol, new_file=new_file),
    )


# ─────────────────────────────────────────────────────────────────────────────
# No profile configured — exact pre-existing behaviour (backward compat)
# ─────────────────────────────────────────────────────────────────────────────

class TestNoProfileConfigured:

    def test_presence_fields_mirror_the_shared_provider(self):
        filt = _make_filter(_base_dict())
        assert filt._presence_base_url == filt._base_url == "http://main.example/v1"
        assert filt._presence_api_key == filt._api_key == "main-key"
        assert filt._presence_model == filt._model == "main-model"
        assert filt._presence_api_format == filt._api_format == "openai"
        assert filt._presence_ssl_context is filt._ssl_context

    def test_presence_settings_mirror_gate1_own_values_not_hardcoded_defaults(self):
        # Deliberately non-default values so a match can't be a coincidence
        # with hardcoded fallback constants (think default False, etc.).
        filt = _make_filter(_base_dict(
            think="true", temperature="0.37", max_tokens="777",
        ))
        assert filt._presence_think is True
        assert filt._presence_think == filt._think
        assert filt._presence_temperature == 0.37 == filt._temperature
        assert filt._presence_max_tokens == 777 == filt._max_tokens
        assert filt._presence_num_ctx == 9999 == filt._num_ctx

    def test_empty_profile_key_behaves_like_absent_key(self):
        """An explicit but blank `presence_llm_profile =` must not be
        treated as a section name to look up."""
        filt = _make_filter(_base_dict(presence_llm_profile="   "))
        assert filt._presence_base_url == filt._base_url


# ─────────────────────────────────────────────────────────────────────────────
# Profile configured — full override
# ─────────────────────────────────────────────────────────────────────────────

class TestProfileFullOverride:

    def _cfg(self, **profile_overrides) -> dict:
        d = _base_dict(presence_llm_profile="gate1_llm", think="false",
                        temperature="0.0", max_tokens="64")
        profile = {
            "base_url":   "https://fallback.example/v1",
            "api_key":    "fallback-key",
            "model":      "fallback-model",
            "api_format": "openai",
            "think":      "true",
            "temperature": "0.55",
            "max_tokens":  "999",
            "num_ctx":     "2048",
        }
        profile.update(profile_overrides)
        d["gate1_llm"] = profile
        return d

    def test_profile_connection_details_win(self):
        filt = _make_filter(self._cfg())
        assert filt._presence_base_url == "https://fallback.example/v1"
        assert filt._presence_api_key == "fallback-key"
        assert filt._presence_model == "fallback-model"
        assert filt._presence_api_format == "openai"

    def test_profile_model_settings_win_over_gate1(self):
        filt = _make_filter(self._cfg())
        assert filt._presence_think is True          # profile: true, [gate1]: false
        assert filt._presence_temperature == 0.55     # profile, not [gate1]'s 0.0
        assert filt._presence_max_tokens == 999        # profile, not [gate1]'s 64
        assert filt._presence_num_ctx == 2048           # profile, not [api_local]'s 9999

    def test_shared_provider_fields_are_untouched(self):
        """Switching the presence check to a profile must never mutate the
        constructor-passed shared provider — Coder/Architect/etc. (which
        this Gate1Filter instance doesn't itself call, but whose config
        this instance was built from) must see it unchanged."""
        filt = _make_filter(self._cfg())
        assert filt._base_url == "http://main.example/v1"
        assert filt._api_key == "main-key"
        assert filt._model == "main-model"

    def test_base_url_trailing_slash_is_stripped(self):
        filt = _make_filter(self._cfg(base_url="https://fallback.example/v1/"))
        assert filt._presence_base_url == "https://fallback.example/v1"

    def test_verify_ssl_false_on_profile_builds_unverified_context(self):
        filt = _make_filter(self._cfg(verify_ssl="false"))
        assert filt._presence_ssl_context is not None

    def test_verify_ssl_defaults_to_constructor_value_when_unset_on_profile(self):
        # Constructor was called with verify_ssl=False in _make_filter();
        # the profile doesn't set its own verify_ssl here.
        filt = _make_filter(self._cfg())
        assert filt._presence_ssl_context is not None


class TestProfilePartialOverrideFallsBackToGate1():
    """Only base_url/api_key/model are required on a profile — everything
    else (think/temperature/max_tokens/num_ctx), when absent from the
    profile, must fall back to THIS Gate1Filter's own [gate1] value, not a
    hardcoded default and not some other section's value."""

    def _cfg_minimal_profile(self) -> dict:
        d = _base_dict(
            presence_llm_profile="gate1_llm",
            think="true", temperature="0.42", max_tokens="321",
        )
        d["gate1_llm"] = {
            "base_url": "https://fallback.example/v1",
            "api_key":  "fallback-key",
            "model":    "fallback-model",
            # no think/temperature/max_tokens/num_ctx on the profile itself
        }
        return d

    def test_think_falls_back_to_gate1(self):
        filt = _make_filter(self._cfg_minimal_profile())
        assert filt._presence_think is True

    def test_temperature_falls_back_to_gate1(self):
        filt = _make_filter(self._cfg_minimal_profile())
        assert filt._presence_temperature == 0.42

    def test_max_tokens_falls_back_to_gate1(self):
        filt = _make_filter(self._cfg_minimal_profile())
        assert filt._presence_max_tokens == 321

    def test_num_ctx_falls_back_to_gate1s_own_num_ctx(self):
        filt = _make_filter(self._cfg_minimal_profile())
        assert filt._presence_num_ctx == 9999  # from [api_local] via [gate1]

    def test_api_format_defaults_to_openai_when_unset_on_profile(self):
        filt = _make_filter(self._cfg_minimal_profile())
        assert filt._presence_api_format == "openai"


# ─────────────────────────────────────────────────────────────────────────────
# Misconfiguration fails loudly, never silently reuses the shared provider
# ─────────────────────────────────────────────────────────────────────────────

class TestProfileMisconfigurationRaises:

    def test_missing_section_raises_with_profile_name(self):
        d = _base_dict(presence_llm_profile="does_not_exist")
        with pytest.raises(ValueError, match="does_not_exist"):
            _make_filter(d)

    def test_missing_base_url_raises_naming_it(self):
        d = _base_dict(presence_llm_profile="gate1_llm")
        d["gate1_llm"] = {"api_key": "k", "model": "m"}
        with pytest.raises(ValueError, match="base_url"):
            _make_filter(d)

    def test_missing_api_key_raises_naming_it(self):
        """Must NOT silently fall back to the shared provider's api_key —
        that would send the main provider's credential to a URL it was
        never meant for."""
        d = _base_dict(presence_llm_profile="gate1_llm")
        d["gate1_llm"] = {"base_url": "https://fallback.example/v1", "model": "m"}
        with pytest.raises(ValueError, match="api_key"):
            _make_filter(d)

    def test_missing_model_raises_naming_it(self):
        d = _base_dict(presence_llm_profile="gate1_llm")
        d["gate1_llm"] = {"base_url": "https://fallback.example/v1", "api_key": "k"}
        with pytest.raises(ValueError, match="model"):
            _make_filter(d)

    def test_missing_multiple_keys_names_all_of_them(self):
        d = _base_dict(presence_llm_profile="gate1_llm")
        d["gate1_llm"] = {"model": "m"}  # base_url and api_key both missing
        with pytest.raises(ValueError) as exc_info:
            _make_filter(d)
        msg = str(exc_info.value)
        assert "base_url" in msg
        assert "api_key" in msg


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: the presence check actually talks to the profile's provider
# ─────────────────────────────────────────────────────────────────────────────

class TestProfileUsedEndToEnd:

    def _cfg(self) -> dict:
        d = _base_dict(presence_llm_profile="gate1_llm", think="false")
        d["gate1_llm"] = {
            "base_url":  "https://fallback.example/v1",
            "api_key":   "fallback-key",
            "model":     "fallback-model",
            "api_format": "openai",
            "think":     "true",
        }
        return d

    def test_llm_call_targets_the_profile_not_the_shared_provider(self, repo):
        filt = _make_filter(self._cfg())
        real_build = llm_stream_mod.build_chat_request
        captured: dict = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return real_build(**kwargs)

        with patch("tools.llm_stream.build_chat_request", side_effect=_capture), \
             patch(
                "tools.llm_stream.request_completion",
                return_value='{"verdict": "confirmed", "evidence": "return raw", '
                             '"reason": "still missing validation"}',
             ):
            accepted, rejected = filt.filter([_candidate()], repo)

        assert len(accepted) == 1
        assert captured["base_url"] == "https://fallback.example/v1"
        assert captured["api_key"] == "fallback-key"
        assert captured["model"] == "fallback-model"
        assert captured["think"] is True

    def test_new_file_candidate_never_calls_the_llm_even_with_broken_profile(self, repo):
        """A new-file candidate skips the presence check entirely (nothing
        existing to check problem-presence against) — a badly configured
        or unreachable presence profile must not break this path, since
        no LLM call happens on it at all."""
        d = _base_dict(presence_llm_profile="gate1_llm")
        d["gate1_llm"] = {
            "base_url": "https://unreachable.invalid/v1",
            "api_key":  "k", "model": "m",
        }
        filt = _make_filter(d)
        c = _candidate(new_file=True, file="tools/brand_new.py")
        # Should complete without attempting any network call.
        accepted, rejected = filt.filter([c], repo)
        assert len(accepted) == 1


# ─────────────────────────────────────────────────────────────────────────────
# GATE1-PROVIDER-1 integration: reasoning-field cache stays per (url, model)
# even when the presence check is routed through a distinct profile.
# ─────────────────────────────────────────────────────────────────────────────

class TestPresenceProfileReasoningCacheIndependence:

    def test_shared_providers_unsupported_mark_does_not_strip_profiles_field(self, repo):
        """The shared/main provider's model previously told us (in some
        earlier call, real or simulated here) that it rejects the
        `reasoning` field. The presence profile is a different (url,
        model) pair entirely and must get its own honest first try."""
        llm_stream_mod.mark_reasoning_field_unsupported(
            "http://main.example/v1/chat/completions", "main-model")

        d = _base_dict(presence_llm_profile="gate1_llm", think="false")
        d["gate1_llm"] = {
            "base_url":  "https://fallback.example/v1",
            "api_key":   "fallback-key",
            "model":     "fallback-model",
            "api_format": "openai",
            "think":     "false",  # think=False is what triggers the reasoning field
        }
        filt = _make_filter(d)

        real_build = llm_stream_mod.build_chat_request
        captured_payload: dict = {}

        def _capture(**kwargs):
            url, headers, payload = real_build(**kwargs)
            captured_payload.clear()
            captured_payload.update(payload)
            return url, headers, payload

        with patch("tools.llm_stream.build_chat_request", side_effect=_capture), \
             patch(
                "tools.llm_stream.request_completion",
                return_value='{"verdict": "confirmed", "evidence": "return raw", '
                             '"reason": "still missing validation"}',
             ):
            filt.filter([_candidate()], repo)

        assert "reasoning" in captured_payload

    def test_marking_profile_unsupported_does_not_affect_shared_provider(self, repo):
        """The reverse direction: the PROFILE's (url, model) having been
        marked unsupported must not affect a hypothetical call against the
        shared provider's own (url, model)."""
        llm_stream_mod.mark_reasoning_field_unsupported(
            "https://fallback.example/v1/chat/completions", "fallback-model")

        assert llm_stream_mod.reasoning_field_is_supported(
            "http://main.example/v1/chat/completions", "main-model") is True
