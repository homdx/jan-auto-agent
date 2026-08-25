"""tests/test_gate3_profiles.py — GATE3-PROFILE-2 acceptance tests.

Covers the per-validator ``<gate>_llm_profile`` keys for the four
LLM-backed Gate-3 validators (canon, fact, continuity, theme), wired
through ``tools.auto.gate_registry.build_validators`` and resolved via
``tools.auto.llm_profile.resolve_llm_profile``.

Deliberately excludes ``prosody``: ``ProsodyValidator.check`` is pure
syllable/rhyme-scheme arithmetic and never calls an LLM, so
``prosody_llm_profile`` was not wired up (see the comment on its
``GateSpec`` in ``tools/auto/gate_registry.py``) — there is no request
for a prosody profile to redirect.

Each test asserts the URL/model actually used for the *outbound HTTP
request* by monkeypatching ``tools.llm_stream.request_completion`` and
invoking the validator's stored LLM callable directly, rather than
mocking at the ``build_validators`` return-value level — a mis-wired
``settings`` kwarg that never reached ``_make_llm_call`` would still
pass a higher-level mock.

Config is built via ``ConfigParser.read_dict`` on a plain dict-of-dicts
rather than string concatenation: canon's enable flag lives in
``[auto]`` while the others' lives in ``[validator_agent]`` (which is
also where every profile key lives), so naive string concatenation
across gates either misplaced a key or produced a duplicate
``[validator_agent]`` section header — ``ConfigParser`` is strict about
the latter. Building one ``dict`` per section and merging keys into it
sidesteps both.
"""

from __future__ import annotations

import configparser
import copy
import logging

import pytest

from tools.auto.gate_registry import GATES_BY_NAME, build_validators


# ── helpers ──────────────────────────────────────────────────────────────────

_SHARED_SECTIONS = {
    "api": {"active": "local"},
    "api_local": {
        "base_url": "https://shared.example/v1",
        "api_key": "shared-key",
        "model": "shared-model",
        "api_format": "openai",
    },
}

#: Per-gate config needed just to make build_validators construct it,
#: keyed the same way ConfigParser.read_dict wants: {section: {key: val}}.
_GATE_ENABLE = {
    "canon": {"auto": {"canon_check_every": "1"}},
    "fact": {"validator_agent": {"fact_check_creative": "true"}},
    "continuity": {"validator_agent": {"continuity_check_creative": "true"}},
    "theme": {
        "validator_agent": {
            "theme_check_creative": "true",
            "theme_guidelines": "stay hopeful",
        }
    },
}

_PROFILE_KEY = {
    "canon": "canon_llm_profile",
    "fact": "fact_llm_profile",
    "continuity": "continuity_llm_profile",
    "theme": "theme_llm_profile",
}

GATE_NAMES = list(_GATE_ENABLE)


def _merge(*section_dicts: dict) -> dict:
    """Deep-merge {section: {key: val}} dicts, later ones winning per-key."""
    out: dict = {}
    for sections in section_dicts:
        for section, kv in sections.items():
            out.setdefault(section, {}).update(kv)
    return out


def _config(*section_dicts: dict) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict(_merge(*section_dicts))
    return cfg


def _own_provider_sections(gate_name: str, section_name: str = "own_provider") -> dict:
    return _merge(
        _GATE_ENABLE[gate_name],
        {"validator_agent": {_PROFILE_KEY[gate_name]: section_name}},
        {
            section_name: {
                "base_url": "https://own.example/v1",
                "api_key": "own-key",
                "model": "own-model",
                "api_format": "openai",
            }
        },
    )


def _capture_request_completion(monkeypatch, capture: dict, gate_name: str):
    import tools.llm_stream as _llm_stream

    def _fake(*, url, headers, payload, **kwargs):
        capture[gate_name] = (url, payload.get("model"))
        return "APPROVED"

    monkeypatch.setattr(_llm_stream, "request_completion", _fake)


def _build_one(monkeypatch, tmp_path, sections: dict, gate_name: str, capture: dict):
    """Build every gate's validator from *sections* and capture the
    (url, model) *gate_name*'s stored LLM callable would actually hit.
    """
    _capture_request_completion(monkeypatch, capture, gate_name)
    cfg = _config(_SHARED_SECTIONS, sections)
    out = build_validators(cfg, tmp_path, task_mode="creative")
    validator = out[GATES_BY_NAME[gate_name].attr]
    assert validator is not None, f"{gate_name} validator was not constructed"
    validator._llm("system", "user")
    return validator


# ─────────────────────────────────────────────────────────────────────────────
# Default path — no profile configured anywhere
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gate_name", GATE_NAMES)
def test_default_path_uses_shared_provider(monkeypatch, tmp_path, gate_name):
    capture: dict = {}
    _build_one(monkeypatch, tmp_path, _GATE_ENABLE[gate_name], gate_name, capture)

    url, model = capture[gate_name]
    assert "shared.example" in url
    assert model == "shared-model"


# ─────────────────────────────────────────────────────────────────────────────
# Own-profile path — <gate>_llm_profile set directly
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gate_name", GATE_NAMES)
def test_own_profile_path_uses_that_providers_url_and_model(monkeypatch, tmp_path, gate_name):
    capture: dict = {}
    _build_one(
        monkeypatch, tmp_path, _own_provider_sections(gate_name), gate_name, capture
    )

    url, model = capture[gate_name]
    assert "own.example" in url
    assert "shared.example" not in url
    assert model == "own-model"


# ─────────────────────────────────────────────────────────────────────────────
# Inherit-from-validator_llm_profile path — no gate-specific key, but the
# shared [validator_agent] validator_llm_profile is set
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gate_name", GATE_NAMES)
def test_inherits_validator_llm_profile_when_own_key_unset(monkeypatch, tmp_path, gate_name):
    sections = _merge(
        _GATE_ENABLE[gate_name],
        {"validator_agent": {"validator_llm_profile": "judge_provider"}},
        {
            "judge_provider": {
                "base_url": "https://judge.example/v1",
                "api_key": "judge-key",
                "model": "judge-model",
                "api_format": "openai",
            }
        },
    )
    capture: dict = {}
    _build_one(monkeypatch, tmp_path, sections, gate_name, capture)

    url, model = capture[gate_name]
    assert "judge.example" in url
    assert model == "judge-model"


@pytest.mark.parametrize("gate_name", GATE_NAMES)
def test_own_profile_overrides_validator_llm_profile(monkeypatch, tmp_path, gate_name):
    """<gate>_llm_profile wins over validator_llm_profile when both are set."""
    sections = _merge(
        _own_provider_sections(gate_name),
        {"validator_agent": {"validator_llm_profile": "judge_provider"}},
        {
            "judge_provider": {
                "base_url": "https://judge.example/v1",
                "api_key": "judge-key",
                "model": "judge-model",
                "api_format": "openai",
            }
        },
    )
    capture: dict = {}
    _build_one(monkeypatch, tmp_path, sections, gate_name, capture)

    url, model = capture[gate_name]
    assert "own.example" in url
    assert model == "own-model"


# ─────────────────────────────────────────────────────────────────────────────
# Empty-value path — <gate>_llm_profile = (blank) is unset, not "" profile
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gate_name", GATE_NAMES)
def test_empty_profile_value_treated_as_unset(monkeypatch, tmp_path, gate_name):
    sections = _merge(
        _GATE_ENABLE[gate_name],
        {"validator_agent": {_PROFILE_KEY[gate_name]: ""}},
    )
    capture: dict = {}
    _build_one(monkeypatch, tmp_path, sections, gate_name, capture)

    url, model = capture[gate_name]
    assert "shared.example" in url
    assert model == "shared-model"


# ─────────────────────────────────────────────────────────────────────────────
# Error paths — malformed profile raises at construction, never mid-run
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gate_name", GATE_NAMES)
def test_profile_naming_missing_section_raises_at_construction(tmp_path, gate_name):
    sections = _merge(
        _GATE_ENABLE[gate_name],
        {"validator_agent": {_PROFILE_KEY[gate_name]: "ghost"}},
    )
    with pytest.raises(ValueError, match="ghost"):
        build_validators(_config(_SHARED_SECTIONS, sections), tmp_path, task_mode="creative")


@pytest.mark.parametrize("gate_name", GATE_NAMES)
def test_profile_missing_required_option_raises_at_construction(tmp_path, gate_name):
    sections = _merge(
        _GATE_ENABLE[gate_name],
        {"validator_agent": {_PROFILE_KEY[gate_name]: "incomplete"}},
        {"incomplete": {"base_url": "https://incomplete.example"}},  # api_key/model absent
    )
    with pytest.raises(ValueError, match="api_key|model"):
        build_validators(_config(_SHARED_SECTIONS, sections), tmp_path, task_mode="creative")


@pytest.mark.parametrize("gate_name", GATE_NAMES)
def test_broken_validator_llm_profile_raises_even_for_gate_using_own_key(tmp_path, gate_name):
    """A broken validator_llm_profile must raise even when THIS gate has
    its own, valid, gate-specific key — the shared step still has to
    resolve first to serve as the *other* gates' fallback, and a
    misconfigured shared profile must be caught at startup, not silently
    skipped for the one gate that happens not to need it.
    """
    sections = _merge(
        _own_provider_sections(gate_name),
        {"validator_agent": {"validator_llm_profile": "ghost"}},
    )
    with pytest.raises(ValueError, match="ghost"):
        build_validators(_config(_SHARED_SECTIONS, sections), tmp_path, task_mode="creative")


# ─────────────────────────────────────────────────────────────────────────────
# Startup logging — one line per enabled gate naming its resolved provider
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gate_name", GATE_NAMES)
def test_startup_logs_resolved_provider_and_model(tmp_path, gate_name, caplog):
    sections = _own_provider_sections(gate_name)
    with caplog.at_level(logging.INFO):
        out = build_validators(
            _config(_SHARED_SECTIONS, sections), tmp_path, task_mode="creative"
        )
    assert out[GATES_BY_NAME[gate_name].attr] is not None

    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "own.example" in m and "own-model" in m and _PROFILE_KEY[gate_name] in m
        for m in msgs
    ), msgs


# ─────────────────────────────────────────────────────────────────────────────
# Default behaviour unchanged — the byte-for-byte requirement
# ─────────────────────────────────────────────────────────────────────────────

def test_no_new_keys_present_every_gate_still_hits_shared_provider(monkeypatch, tmp_path):
    """A config with NONE of the new keys must send every request to the
    same [api_local] provider it always did — this is the acceptance
    criterion that most needs a regression test, since every existing
    deployment's behaviour depends on it.
    """
    all_enable = _merge(*_GATE_ENABLE.values())
    cfg = _config(_SHARED_SECTIONS, all_enable)

    import tools.llm_stream as _llm_stream

    capture: dict = {}
    for gate_name in GATE_NAMES:

        def _fake(*, url, headers, payload, _gate=gate_name, **kwargs):
            capture[_gate] = (url, payload.get("model"))
            return "APPROVED"

        monkeypatch.setattr(_llm_stream, "request_completion", _fake)
        out = build_validators(copy.deepcopy(cfg), tmp_path, task_mode="creative")
        validator = out[GATES_BY_NAME[gate_name].attr]
        assert validator is not None
        validator._llm("system", "user")

    for gate_name in GATE_NAMES:
        url, model = capture[gate_name]
        assert "shared.example" in url
        assert model == "shared-model"
