"""tests/test_gate1_response_format_config.py — AUTO-JSONMODE-1.

Gate1Filter must read the single GLOBAL `[api] response_format` switch
(not a per-[gate1] setting — one flag governs every LLM call the project
makes) and forward it into every build_chat_request() call it makes, so
JSON-mode enforcement is opt-in via ini and defaults to today's
behaviour (off) when the key is absent.
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


def _config(response_format_value: "str | None") -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    api_section = {"active": "local", "verify_ssl": "false"}
    if response_format_value is not None:
        api_section["response_format"] = response_format_value
    cfg.read_dict({
        "api":       api_section,
        "api_local": {
            "base_url":   "http://localhost:1337/v1",
            "api_key":    "test",
            "model":      "test-model",
            "api_format": "openai",
        },
        "gate1": {"temperature": "0.0", "max_tokens": "64", "skip_llm": "false"},
        "loop":  {"timeout_seconds": "10"},
    })
    return cfg


class TestGate1ReadsGlobalResponseFormatFlag:

    def test_flag_absent_defaults_to_false(self):
        filt = Gate1Filter(
            config=_config(None), base_url="http://localhost:1337/v1",
            api_key="test", model="test-model", api_format="openai", verify_ssl=False,
        )
        assert filt._response_format is False

    def test_flag_true_is_read(self):
        filt = Gate1Filter(
            config=_config("true"), base_url="http://localhost:1337/v1",
            api_key="test", model="test-model", api_format="openai", verify_ssl=False,
        )
        assert filt._response_format is True

    def test_flag_false_is_read(self):
        filt = Gate1Filter(
            config=_config("false"), base_url="http://localhost:1337/v1",
            api_key="test", model="test-model", api_format="openai", verify_ssl=False,
        )
        assert filt._response_format is False

    def test_flag_lives_in_api_section_not_gate1_section(self):
        """A [gate1] response_format key must NOT be picked up — this is a
        deliberately global switch under [api], matching `active`."""
        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "api":       {"active": "local", "verify_ssl": "false"},
            "api_local": {
                "base_url": "http://localhost:1337/v1", "api_key": "test",
                "model": "test-model", "api_format": "openai",
            },
            "gate1": {"temperature": "0.0", "max_tokens": "64",
                      "skip_llm": "false", "response_format": "true"},
            "loop":  {"timeout_seconds": "10"},
        })
        filt = Gate1Filter(
            config=cfg, base_url="http://localhost:1337/v1",
            api_key="test", model="test-model", api_format="openai", verify_ssl=False,
        )
        assert filt._response_format is False


class TestGate1ForwardsFlagToBuildChatRequest:

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        llm_stream_mod._RESPONSE_FORMAT_UNSUPPORTED_URLS.clear()
        yield
        llm_stream_mod._RESPONSE_FORMAT_UNSUPPORTED_URLS.clear()

    def _make_filter(self, response_format_value):
        return Gate1Filter(
            config=_config(response_format_value), base_url="http://localhost:1337/v1",
            api_key="test", model="test-model", api_format="openai", verify_ssl=False,
        )

    def test_true_flag_produces_response_format_payload_field(self):
        """Builds the request the exact same way gate1_filter.py's `_call`
        closure does (same kwargs, same attributes read off `filt`), to
        confirm the resolved `_response_format` attribute actually reaches
        build_chat_request()'s payload — without depending on `_call`'s
        internal closure shape, which is private and may change."""
        filt = self._make_filter("true")
        _, _, payload = llm_stream_mod.build_chat_request(
            base_url=filt._base_url, api_key=filt._api_key, model=filt._model,
            api_format=filt._api_format, temperature=filt._temperature,
            max_tokens=filt._max_tokens, system=filt._system, user_msg="hi",
            num_ctx=filt._num_ctx, think=filt._think,
            response_format=filt._response_format,
        )
        assert payload.get("response_format") == {"type": "json_object"}

    def test_false_flag_omits_response_format_payload_field(self):
        filt = self._make_filter("false")
        _, _, payload = llm_stream_mod.build_chat_request(
            base_url=filt._base_url, api_key=filt._api_key, model=filt._model,
            api_format=filt._api_format, temperature=filt._temperature,
            max_tokens=filt._max_tokens, system=filt._system, user_msg="hi",
            num_ctx=filt._num_ctx, think=filt._think,
            response_format=filt._response_format,
        )
        assert "response_format" not in payload
