"""tests/test_llm_profile.py — GATE3-PROFILE-1.

Covers every acceptance criterion of the shared ``resolve_llm_profile()``
helper (tools/auto/llm_profile.py), independent of any of its future
callers (GATE3-PROFILE-2/3/6). Uses a plain configparser.ConfigParser so
this file stays a unit test of the helper alone.
"""

from __future__ import annotations

import configparser
import logging
import sys
from dataclasses import replace
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.llm_profile import LlmSettings, resolve_llm_profile


DEFAULTS = LlmSettings(
    base_url="https://shared.example/v1",
    api_key="shared-key",
    model="shared-model",
    api_format="openai",
    verify_ssl=True,
    think=False,
    temperature=0.1,
    max_tokens=512,
    num_ctx=8192,
    response_format=False,
    think_effort_enabled=False,
    think_effort=None,
)


def _config(**sections) -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    c.read_dict(sections)
    return c


# ─────────────────────────────────────────────────────────────────────────────
# Unset key
# ─────────────────────────────────────────────────────────────────────────────

def test_unset_key_returns_defaults_unchanged():
    config = _config(validator_agent={})
    settings, name = resolve_llm_profile(
        config, "validator_agent", "canon_llm_profile", defaults=DEFAULTS
    )
    assert settings == DEFAULTS
    assert name is None


def test_unset_key_logs_info_naming_provider_and_no_profile(caplog):
    config = _config(validator_agent={})
    with caplog.at_level(logging.INFO):
        resolve_llm_profile(
            config, "validator_agent", "canon_llm_profile", defaults=DEFAULTS
        )
    records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert DEFAULTS.base_url in msg
    assert DEFAULTS.model in msg
    assert "no canon_llm_profile configured" in msg


def test_empty_value_treated_as_unset_not_as_profile_named_empty():
    config = _config(validator_agent={"canon_llm_profile": ""})
    settings, name = resolve_llm_profile(
        config, "validator_agent", "canon_llm_profile", defaults=DEFAULTS
    )
    assert settings == DEFAULTS
    assert name is None


# ─────────────────────────────────────────────────────────────────────────────
# Missing section / missing required options
# ─────────────────────────────────────────────────────────────────────────────

def test_named_section_missing_raises_naming_key_section_and_removal():
    config = _config(validator_agent={"canon_llm_profile": "ghost"})
    with pytest.raises(ValueError) as exc:
        resolve_llm_profile(
            config, "validator_agent", "canon_llm_profile", defaults=DEFAULTS
        )
    msg = str(exc.value)
    assert "canon_llm_profile" in msg
    assert "ghost" in msg
    assert "remove" in msg


@pytest.mark.parametrize(
    "present_keys",
    [
        [],
        ["base_url"],
        ["api_key"],
        ["model"],
        ["base_url", "api_key"],
        ["base_url", "model"],
        ["api_key", "model"],
    ],
)
def test_missing_required_options_raises_listing_all_missing(present_keys):
    section_body = {k: "x" for k in present_keys}
    config = _config(
        validator_agent={"canon_llm_profile": "strong"},
        strong=section_body,
    )
    with pytest.raises(ValueError) as exc:
        resolve_llm_profile(
            config, "validator_agent", "canon_llm_profile", defaults=DEFAULTS
        )
    msg = str(exc.value)
    for missing in {"base_url", "api_key", "model"} - set(present_keys):
        assert missing in msg


def test_required_fields_never_inherited_from_defaults():
    # A profile missing model must raise even though defaults has a model —
    # the required trio is never silently filled from defaults.
    config = _config(
        validator_agent={"canon_llm_profile": "strong"},
        strong={"base_url": "https://a.example", "api_key": "k"},
    )
    with pytest.raises(ValueError):
        resolve_llm_profile(
            config, "validator_agent", "canon_llm_profile", defaults=DEFAULTS
        )


# ─────────────────────────────────────────────────────────────────────────────
# Full profile resolution
# ─────────────────────────────────────────────────────────────────────────────

def test_full_profile_overrides_everything():
    config = _config(
        validator_agent={"canon_llm_profile": "strong"},
        strong={
            "base_url": "https://provider-a.example/v1/",
            "api_key": "a-key",
            "model": "a-model",
            "api_format": "anthropic",
            "verify_ssl": "false",
            "think": "true",
            "temperature": "0.7",
            "max_tokens": "2000",
            "num_ctx": "16384",
            "response_format": "true",
            "think_effort_enabled": "true",
            "think_effort": "high",
        },
    )
    settings, name = resolve_llm_profile(
        config, "validator_agent", "canon_llm_profile", defaults=DEFAULTS
    )
    assert name == "strong"
    assert settings == LlmSettings(
        base_url="https://provider-a.example/v1",  # trailing slash stripped
        api_key="a-key",
        model="a-model",
        api_format="anthropic",
        verify_ssl=False,
        think=True,
        temperature=0.7,
        max_tokens=2000,
        num_ctx=16384,
        response_format=True,
        think_effort_enabled=True,
        think_effort="high",
    )


def test_base_url_trailing_slashes_stripped():
    config = _config(
        validator_agent={"canon_llm_profile": "strong"},
        strong={"base_url": "https://a.example/v1///", "api_key": "k", "model": "m"},
    )
    settings, _ = resolve_llm_profile(
        config, "validator_agent", "canon_llm_profile", defaults=DEFAULTS
    )
    assert settings.base_url == "https://a.example/v1"


@pytest.mark.parametrize(
    "field_name,profile_value,expected,default_override",
    [
        ("api_format", "anthropic", "anthropic", {"api_format": "openai"}),
        ("verify_ssl", "false", False, {"verify_ssl": True}),
        ("think", "true", True, {"think": False}),
        ("temperature", "0.9", 0.9, {"temperature": 0.1}),
        ("max_tokens", "4096", 4096, {"max_tokens": 512}),
        ("num_ctx", "32768", 32768, {"num_ctx": 8192}),
        ("response_format", "true", True, {"response_format": False}),
    ],
)
def test_optional_key_read_from_profile(field_name, profile_value, expected, default_override):
    defaults = replace(DEFAULTS, **default_override)
    config = _config(
        validator_agent={"canon_llm_profile": "strong"},
        strong={
            "base_url": "https://a.example", "api_key": "k", "model": "m",
            field_name: profile_value,
        },
    )
    settings, _ = resolve_llm_profile(
        config, "validator_agent", "canon_llm_profile", defaults=defaults
    )
    assert getattr(settings, field_name) == expected


@pytest.mark.parametrize(
    "field_name,default_override",
    [
        ("api_format", {"api_format": "custom-fmt"}),
        ("verify_ssl", {"verify_ssl": False}),
        ("think", {"think": True}),
        ("temperature", {"temperature": 0.42}),
        ("max_tokens", {"max_tokens": 777}),
        ("num_ctx", {"num_ctx": 4321}),
        ("response_format", {"response_format": True}),
    ],
)
def test_optional_key_falls_back_to_defaults_when_unset_in_profile(field_name, default_override):
    defaults = replace(DEFAULTS, **default_override)
    config = _config(
        validator_agent={"canon_llm_profile": "strong"},
        strong={"base_url": "https://a.example", "api_key": "k", "model": "m"},
    )
    settings, _ = resolve_llm_profile(
        config, "validator_agent", "canon_llm_profile", defaults=defaults
    )
    assert getattr(settings, field_name) == getattr(defaults, field_name)


def test_think_effort_pair_falls_back_together_when_unset():
    defaults = replace(DEFAULTS, think_effort_enabled=True, think_effort="medium")
    config = _config(
        validator_agent={"canon_llm_profile": "strong"},
        strong={"base_url": "https://a.example", "api_key": "k", "model": "m"},
    )
    settings, _ = resolve_llm_profile(
        config, "validator_agent", "canon_llm_profile", defaults=defaults
    )
    assert settings.think_effort_enabled is True
    assert settings.think_effort == "medium"


def test_think_effort_disabled_in_profile_clears_effort_even_if_default_had_one():
    defaults = replace(DEFAULTS, think_effort_enabled=True, think_effort="medium")
    config = _config(
        validator_agent={"canon_llm_profile": "strong"},
        strong={
            "base_url": "https://a.example", "api_key": "k", "model": "m",
            "think_effort_enabled": "false",
        },
    )
    settings, _ = resolve_llm_profile(
        config, "validator_agent", "canon_llm_profile", defaults=defaults
    )
    assert settings.think_effort_enabled is False
    assert settings.think_effort is None


# ─────────────────────────────────────────────────────────────────────────────
# Malformed optional values
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "field_name,bad_value",
    [
        ("temperature", "warm"),
        ("max_tokens", "a-lot"),
        ("num_ctx", "huge"),
        ("verify_ssl", "maybe"),
        ("think", "sorta"),
        ("response_format", "yesish"),
    ],
)
def test_malformed_optional_value_logs_warning_and_keeps_default(field_name, bad_value, caplog):
    config = _config(
        validator_agent={"canon_llm_profile": "strong"},
        strong={
            "base_url": "https://a.example", "api_key": "k", "model": "m",
            field_name: bad_value,
        },
    )
    with caplog.at_level(logging.WARNING):
        settings, name = resolve_llm_profile(
            config, "validator_agent", "canon_llm_profile", defaults=DEFAULTS
        )
    # Must not abort — the run continues with the default value.
    assert name == "strong"
    assert getattr(settings, field_name) == getattr(DEFAULTS, field_name)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# Isolation between profiles / defaults
# ─────────────────────────────────────────────────────────────────────────────

def test_two_profiles_read_in_same_process_share_no_value():
    config = _config(
        validator_agent={
            "canon_llm_profile": "strong",
            "fact_llm_profile": "cheap",
        },
        strong={
            "base_url": "https://a.example", "api_key": "a-key", "model": "a-model",
            "think": "true", "max_tokens": "3000",
        },
        cheap={
            "base_url": "https://b.example", "api_key": "b-key", "model": "b-model",
            "think": "false", "max_tokens": "128",
        },
    )
    settings_a, name_a = resolve_llm_profile(
        config, "validator_agent", "canon_llm_profile", defaults=DEFAULTS
    )
    settings_b, name_b = resolve_llm_profile(
        config, "validator_agent", "fact_llm_profile", defaults=DEFAULTS
    )
    assert name_a == "strong" and name_b == "cheap"
    assert settings_a.base_url != settings_b.base_url
    assert settings_a.model != settings_b.model
    assert settings_a.think != settings_b.think
    assert settings_a.max_tokens != settings_b.max_tokens
    # Neither result is the SAME object as defaults, nor mutates it.
    assert settings_a is not DEFAULTS
    assert settings_b is not DEFAULTS
    assert DEFAULTS.base_url == "https://shared.example/v1"


def test_resolving_a_profile_does_not_mutate_defaults():
    original = replace(DEFAULTS)
    config = _config(
        validator_agent={"canon_llm_profile": "strong"},
        strong={
            "base_url": "https://a.example", "api_key": "k", "model": "m",
            "max_tokens": "9999",
        },
    )
    resolve_llm_profile(
        config, "validator_agent", "canon_llm_profile", defaults=DEFAULTS
    )
    assert DEFAULTS == original


# ─────────────────────────────────────────────────────────────────────────────
# Logging on the profile-configured path
# ─────────────────────────────────────────────────────────────────────────────

def test_configured_profile_logs_one_info_line_naming_provider_model_section(caplog):
    config = _config(
        validator_agent={"canon_llm_profile": "strong"},
        strong={"base_url": "https://a.example", "api_key": "k", "model": "a-model"},
    )
    with caplog.at_level(logging.INFO):
        resolve_llm_profile(
            config, "validator_agent", "canon_llm_profile", defaults=DEFAULTS
        )
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    msg = info_records[0].getMessage()
    assert "https://a.example" in msg
    assert "a-model" in msg
    assert "strong" in msg
