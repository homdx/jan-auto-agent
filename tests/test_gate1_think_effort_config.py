"""tests/test_gate1_think_effort_config.py — AUTO-THINKDEPTH-1.

Gate1Filter must read the GLOBAL `[api] think_effort_enabled` +
`[api] think_effort` pair (not per-[gate1]) and forward the depth into
every build_chat_request() call, while leaving the existing per-[gate1]
`think` on/off switch completely untouched.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.gate1_filter import Gate1Filter
import tools.llm_stream as llm_stream_mod


def _config(*, think_effort_enabled=None, think_effort=None, gate1_think=None) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    api_section = {"active": "local", "verify_ssl": "false"}
    if think_effort_enabled is not None:
        api_section["think_effort_enabled"] = think_effort_enabled
    if think_effort is not None:
        api_section["think_effort"] = think_effort
    gate1_section = {"temperature": "0.0", "max_tokens": "64", "skip_llm": "false"}
    if gate1_think is not None:
        gate1_section["think"] = gate1_think
    cfg.read_dict({
        "api":       api_section,
        "api_local": {
            "base_url":   "http://localhost:1337/v1",
            "api_key":    "test",
            "model":      "test-model",
            "api_format": "openai",
        },
        "gate1": gate1_section,
        "loop":  {"timeout_seconds": "10"},
    })
    return cfg


def _make_filter(cfg):
    return Gate1Filter(
        config=cfg, base_url="http://localhost:1337/v1",
        api_key="test", model="test-model", api_format="openai", verify_ssl=False,
    )


class TestGate1ReadsGlobalThinkEffort:

    def test_disabled_by_default(self):
        filt = _make_filter(_config())
        assert filt._think_effort is None

    def test_enabled_reads_depth(self):
        filt = _make_filter(_config(think_effort_enabled="true", think_effort="medium"))
        assert filt._think_effort == "medium"

    def test_enabled_but_empty_depth_is_none(self):
        filt = _make_filter(_config(think_effort_enabled="true", think_effort=""))
        assert filt._think_effort is None

    def test_flag_false_ignores_depth_value(self):
        filt = _make_filter(_config(think_effort_enabled="false", think_effort="high"))
        assert filt._think_effort is None

    def test_does_not_affect_existing_gate1_think_toggle(self):
        """Per-[gate1] think on/off must be completely independent of the
        new global depth switch, in both directions."""
        filt_off = _make_filter(_config(gate1_think="false", think_effort_enabled="true", think_effort="high"))
        assert filt_off._think is False
        assert filt_off._think_effort == "high"

        filt_on = _make_filter(_config(gate1_think="true"))
        assert filt_on._think is True
        assert filt_on._think_effort is None

    def test_depth_key_in_gate1_section_is_not_picked_up(self):
        """A [gate1] think_effort key must be ignored — this is deliberately
        global, under [api], matching response_format's pattern."""
        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "api":       {"active": "local", "verify_ssl": "false"},
            "api_local": {
                "base_url": "http://localhost:1337/v1", "api_key": "test",
                "model": "test-model", "api_format": "openai",
            },
            "gate1": {"temperature": "0.0", "max_tokens": "64", "skip_llm": "false",
                      "think": "true", "think_effort": "high"},
            "loop":  {"timeout_seconds": "10"},
        })
        filt = _make_filter(cfg)
        assert filt._think_effort is None


class TestGate1ForwardsThinkEffort:

    @pytest.fixture(autouse=True)
    def _reset_caches(self):
        llm_stream_mod._REASONING_UNSUPPORTED_KEYS.clear()
        llm_stream_mod._THINK_DEPTH_UNSUPPORTED_KEYS.clear()
        yield
        llm_stream_mod._REASONING_UNSUPPORTED_KEYS.clear()
        llm_stream_mod._THINK_DEPTH_UNSUPPORTED_KEYS.clear()

    def test_enabled_reaches_payload_when_thinking_on(self):
        filt = _make_filter(_config(gate1_think="true", think_effort_enabled="true", think_effort="medium"))
        _, _, payload = llm_stream_mod.build_chat_request(
            base_url=filt._base_url, api_key=filt._api_key, model=filt._model,
            api_format=filt._api_format, temperature=filt._temperature,
            max_tokens=filt._max_tokens, system=filt._system, user_msg="hi",
            num_ctx=filt._num_ctx, think=filt._think,
            response_format=filt._response_format, think_effort=filt._think_effort,
        )
        assert payload.get("reasoning_effort") == "medium"

    def test_enabled_has_no_effect_when_thinking_off(self):
        """[gate1] think is false (default) — global depth switch must not
        turn thinking on by itself."""
        filt = _make_filter(_config(gate1_think="false", think_effort_enabled="true", think_effort="medium"))
        _, _, payload = llm_stream_mod.build_chat_request(
            base_url=filt._base_url, api_key=filt._api_key, model=filt._model,
            api_format=filt._api_format, temperature=filt._temperature,
            max_tokens=filt._max_tokens, system=filt._system, user_msg="hi",
            num_ctx=filt._num_ctx, think=filt._think,
            response_format=filt._response_format, think_effort=filt._think_effort,
        )
        # Existing suppression behaviour, unaffected.
        assert payload.get("reasoning") == {"effort": "low", "exclude": True}
