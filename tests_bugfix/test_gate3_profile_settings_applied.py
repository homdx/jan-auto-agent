"""GATE3-PROFILE audit — settings a profile resolves must reach the wire.

`resolve_llm_profile` faithfully read `response_format`, `think_effort_enabled`
and `think_effort` from a named profile, logged the profile as applied — and
then `_make_llm_call` discarded them, because it was the one caller that
hand-rolled its own payload instead of going through `build_chat_request`
like Coder, Gate1Filter, Architect and TaskRewriter.

That produced the worst shape of configuration bug: the SAME key worked in
`[gate1] presence_llm_profile` and silently did nothing in
`[validator_agent] canon_llm_profile`. After GATE3-PROFILE-6 both go through
one shared helper, so the divergence had no visible explanation at all.

A second, older instance of the same fault was found while fixing it: the
hand-built `openai` branch had no `think` handling whatsoever, so
`think = false` only ever suppressed reasoning on `api_format = ollama`.

These tests assert on the PAYLOAD, not on the resolved settings object. A
setting that resolves correctly and is then dropped is exactly what shipped.
"""

from __future__ import annotations

import configparser
from unittest.mock import patch

import pytest

import tools.llm_stream as _llm_stream
from tools.auto.llm_profile import LlmSettings
from tools.auto.summary_memory import _default_llm_settings, _make_llm_call


def _cfg(text: str = "") -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read_string(text or "[api]\nactive = x\n\n[api_x]\nbase_url = https://p/v1\n")
    return cfg


def _settings(**kw) -> LlmSettings:
    base = dict(
        base_url="https://p/v1", api_key="k", model="m", api_format="openai",
        temperature=0.2, max_tokens=900, num_ctx=8192, think=True,
        response_format=False, think_effort=None,
    )
    base.update(kw)
    return LlmSettings(**base)


def _payload(settings: LlmSettings, config=None) -> dict:
    captured: dict = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return "ok"

    with patch.object(_llm_stream, "request_completion", side_effect=_fake):
        _make_llm_call(config or _cfg(), "creative", settings=settings)("S", "U")
    return captured["payload"]


# ── response_format reaches the wire ─────────────────────────────────────────

def test_response_format_true_is_sent_openai():
    assert _payload(_settings(response_format=True))["response_format"] == {
        "type": "json_object"
    }


def test_response_format_true_is_sent_ollama():
    """Ollama uses its own `format: json`, not the OpenAI shape."""
    payload = _payload(_settings(api_format="ollama", response_format=True))
    assert payload["format"] == "json"
    assert "response_format" not in payload


@pytest.mark.parametrize("api_format", ["openai", "ollama"])
def test_response_format_false_sends_nothing(api_format):
    """Default off — opting out must not add a field."""
    payload = _payload(_settings(api_format=api_format, response_format=False))
    assert "response_format" not in payload
    assert "format" not in payload


# ── think_effort reaches the wire ────────────────────────────────────────────

def test_think_effort_is_sent_openai():
    assert _payload(_settings(think_effort="high"))["reasoning_effort"] == "high"


def test_think_effort_is_sent_ollama():
    assert _payload(_settings(api_format="ollama", think_effort="high"))["think"] == "high"


@pytest.mark.parametrize("api_format", ["openai", "ollama"])
def test_no_think_effort_leaves_plain_thinking(api_format):
    payload = _payload(_settings(api_format=api_format, think_effort=None))
    assert "reasoning_effort" not in payload
    assert payload.get("think") is not "high"  # noqa: F632 — identity is the point


def test_think_effort_requires_thinking_enabled():
    """`think = false` means suppress; an effort level cannot override that.

    Pinned because the first attempt at this fix passed ``think=None`` to keep
    one payload key byte-identical, and that silently left think_effort dead —
    the builder applies effort only when thinking is explicitly enabled.
    """
    payload = _payload(_settings(think=False, think_effort="high"))
    assert "reasoning_effort" not in payload
    assert payload["thinking"] == {"type": "disabled"}


# ── think = false now works on openai too ────────────────────────────────────

def test_think_false_suppresses_on_openai():
    """The older instance of the same fault: the hand-built openai branch had
    no think handling at all, so suppression only worked for ollama."""
    payload = _payload(_settings(think=False))
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["reasoning"] == {"effort": "low", "exclude": True}


def test_think_false_suppresses_on_ollama():
    assert _payload(_settings(api_format="ollama", think=False))["think"] is False


# ── the request shape is otherwise unchanged ─────────────────────────────────

@pytest.mark.parametrize("api_format", ["openai", "ollama"])
def test_core_payload_fields_survive(api_format):
    payload = _payload(_settings(api_format=api_format))
    assert payload["model"] == "m"
    assert payload["messages"] == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
    ]


def test_openai_keeps_flat_temperature_and_max_tokens():
    payload = _payload(_settings())
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 900


def test_ollama_keeps_options_block():
    """Ollama nests generation settings; a flat payload would be ignored."""
    payload = _payload(_settings(api_format="ollama"))
    assert payload["options"]["temperature"] == 0.2
    assert payload["options"]["num_predict"] == 900
    assert payload["options"]["num_ctx"] == 8192


def test_zero_num_ctx_is_omitted():
    payload = _payload(_settings(api_format="ollama", num_ctx=0))
    assert "num_ctx" not in payload["options"]


# ── defaults read [api], not the dataclass defaults ──────────────────────────

def test_api_response_format_does_not_reach_prose_callers():
    """`[api] response_format = true` must NOT flow into this factory.

    It forces the server to emit a JSON object — correct for Gate 1, the
    Coder and the Architect, and wrong for every caller of _make_llm_call.
    The Gate-3 validators and SummaryMemory parse FREE TEXT: an
    "APPROVED" / "REVISE: ..." verdict, or a prose synopsis. Honouring the
    global switch here would hand them JSON and break all of them at once —
    and agents_128k.ini ships with that key set to true, so a naive "read it
    from [api] like everything else" would have gone straight into a live
    profile.
    """
    cfg = _cfg(
        "[api]\nactive = x\nresponse_format = true\n\n[api_x]\nbase_url = https://p/v1\n"
    )
    assert _default_llm_settings(cfg).response_format is False


def test_shipped_profiles_do_not_turn_on_json_mode_for_gate3():
    """The live-profile guard, asserted against the real files."""
    from pathlib import Path as _P

    root = _P(__file__).resolve().parent.parent
    for profile in sorted(root.glob("agents*.ini")):
        cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        cfg.read(profile, encoding="utf-8")
        assert _default_llm_settings(cfg).response_format is False, profile.name


def test_defaults_response_format_is_off_by_default():
    assert _default_llm_settings(_cfg()).response_format is False


def test_profile_can_still_opt_into_json_mode_explicitly():
    """Per-caller opt-in remains available — only the GLOBAL switch is blocked."""
    from tools.auto.llm_profile import resolve_llm_profile

    cfg = _cfg(
        "[api]\nactive = x\n\n[api_x]\nbase_url = https://p/v1\n\n"
        "[validator_agent]\ncanon_llm_profile = critic\n\n"
        "[critic]\nbase_url = https://c/v1\napi_key = k\nmodel = m\n"
        "response_format = true\n"
    )
    settings, _ = resolve_llm_profile(
        cfg, "validator_agent", "canon_llm_profile",
        defaults=_default_llm_settings(cfg),
    )
    assert settings.response_format is True


def test_defaults_read_think_effort_from_api_section():
    cfg = _cfg(
        "[api]\nactive = x\nthink_effort_enabled = true\nthink_effort = high\n\n"
        "[api_x]\nbase_url = https://p/v1\n"
    )
    settings = _default_llm_settings(cfg)
    assert settings.think_effort_enabled is True
    assert settings.think_effort == "high"


def test_think_effort_ignored_unless_enabled():
    """A level set without the enable flag must not take effect — the enable
    flag is what the cascade in request_completion keys its retries off."""
    cfg = _cfg(
        "[api]\nactive = x\nthink_effort = high\n\n[api_x]\nbase_url = https://p/v1\n"
    )
    assert _default_llm_settings(cfg).think_effort is None


def test_default_settings_flow_through_to_the_payload():
    """End to end with no profile at all: [api] think_effort -> wire.

    ``[summary_memory] think`` must be on: the builder applies an effort
    level only when thinking is enabled, and this factory defaults it to
    false so a <think> block can never truncate a synopsis update.
    """
    cfg = _cfg(
        "[api]\nactive = x\nthink_effort_enabled = true\nthink_effort = high\n\n"
        "[api_x]\nbase_url = https://p/v1\napi_format = openai\n\n"
        "[summary_memory]\nthink = true\n"
    )
    captured: dict = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return "ok"

    with patch.object(_llm_stream, "request_completion", side_effect=_fake):
        _make_llm_call(cfg, "creative")("S", "U")
    assert captured["payload"]["reasoning_effort"] == "high"


# ── profile -> payload, the whole point ──────────────────────────────────────

def test_profile_response_format_reaches_the_payload():
    """The reported bug, end to end through resolve_llm_profile."""
    from tools.auto.llm_profile import resolve_llm_profile

    cfg = _cfg(
        "[api]\nactive = x\n\n[api_x]\nbase_url = https://p/v1\n\n"
        "[validator_agent]\ncanon_llm_profile = critic\n\n"
        "[critic]\nbase_url = https://c/v1\napi_key = ck\nmodel = cm\n"
        "api_format = openai\nresponse_format = true\n"
    )
    settings, _name = resolve_llm_profile(
        cfg, "validator_agent", "canon_llm_profile",
        defaults=_default_llm_settings(cfg),
    )
    assert _payload(settings, cfg)["response_format"] == {"type": "json_object"}


def test_profile_think_effort_reaches_the_payload():
    from tools.auto.llm_profile import resolve_llm_profile

    cfg = _cfg(
        "[api]\nactive = x\n\n[api_x]\nbase_url = https://p/v1\n\n"
        "[validator_agent]\ncanon_llm_profile = critic\n\n"
        "[critic]\nbase_url = https://c/v1\napi_key = ck\nmodel = cm\n"
        "api_format = openai\nthink = true\n"
        "think_effort_enabled = true\nthink_effort = high\n"
    )
    settings, _name = resolve_llm_profile(
        cfg, "validator_agent", "canon_llm_profile",
        defaults=_default_llm_settings(cfg),
    )
    assert _payload(settings, cfg)["reasoning_effort"] == "high"


def test_profile_targets_its_own_provider():
    """Regression guard: the payload must carry the profile's model, not the
    shared one — the settings object is only useful if it is the one used."""
    from tools.auto.llm_profile import resolve_llm_profile

    cfg = _cfg(
        "[api]\nactive = x\n\n[api_x]\nbase_url = https://p/v1\nmodel = shared\n\n"
        "[validator_agent]\ncanon_llm_profile = critic\n\n"
        "[critic]\nbase_url = https://c/v1\napi_key = ck\nmodel = strong\n"
    )
    settings, _name = resolve_llm_profile(
        cfg, "validator_agent", "canon_llm_profile",
        defaults=_default_llm_settings(cfg),
    )
    assert _payload(settings, cfg)["model"] == "strong"


def test_make_llm_call_no_longer_hand_rolls_a_payload():
    """It was the only caller not using the shared builder, which is how two
    settings went missing. Keep it on the shared path."""
    import inspect

    source = inspect.getsource(_make_llm_call)
    assert "build_chat_request" in source
    assert '"num_predict"' not in source
