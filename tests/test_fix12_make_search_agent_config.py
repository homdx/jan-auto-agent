"""tests/test_fix12_make_search_agent_config.py — AUTO-FIX-12.

Bug found by comparing the two places SearchAgent gets constructed:
main.py's Orchestrator._build_agents() (used by the interactive/one-shot
pipeline) reads timeout, ssl_context (from [api] verify_ssl), skip_dirs,
max_depth, and max_file_kb from agents.ini. tools/search_agent.make_search_
agent() (used exclusively by tools/auto/inner_loop.py to build auto mode's
internal SearchAgent) read only model/base_url/api_key/api_format and
silently left the other five at SearchAgent's class defaults.

Confirmed via direct call with a config that sets non-default values for
all five keys: every one came back as the class default instead of the
configured value.

Impact:
  - verify_ssl=false (documented for the api_remote HTTPS profile) was not
    honoured for auto mode's internal noise-filter LLM call, so that call's
    TLS handshake would fail against a self-signed/internal cert and
    SearchAgent._evaluate_with_llm's fail-open except-clause would silently
    approve every reference — auto mode's noise filtering permanently
    disabled with no visible error whenever a remote HTTPS profile with
    verify_ssl=false was active.
  - A user's customised [search] skip_dirs / max_depth / max_file_kb was
    ignored specifically by auto mode's search step.

Fix: make_search_agent() now reads the same [api]/[loop]/[search] keys
Orchestrator._build_agents() reads, building an unverified SSLContext when
verify_ssl=false exactly like main.py already does.
"""

from __future__ import annotations

import configparser

from tools.search_agent import make_search_agent, SearchAgent, _DEFAULT_SKIP_DIRS


def _configured_ini() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_string(
        """
        [api]
        active = local
        verify_ssl = false

        [api_local]
        model = qwen3:8b
        base_url = https://my-secure-llm.internal
        api_key = secret
        api_format = openai

        [loop]
        timeout_seconds = 999

        [search]
        skip_dirs = node_modules,.venv,vendor
        max_depth = 9
        max_file_kb = 42
        """
    )
    return cfg


class TestConfiguredValuesAreForwarded:
    def test_timeout_from_loop_section(self):
        sa = make_search_agent(_configured_ini())
        assert sa.timeout == 999

    def test_ssl_context_built_when_verify_ssl_false(self):
        sa = make_search_agent(_configured_ini())
        assert sa.ssl_context is not None

    def test_skip_dirs_from_search_section(self):
        sa = make_search_agent(_configured_ini())
        assert sa.skip_dirs == ["node_modules", ".venv", "vendor"]

    def test_max_depth_from_search_section(self):
        sa = make_search_agent(_configured_ini())
        assert sa.max_depth == 9

    def test_max_file_kb_from_search_section(self):
        sa = make_search_agent(_configured_ini())
        assert sa.max_file_kb == 42

    def test_model_and_base_url_still_forwarded(self):
        # Pre-existing behaviour that must not regress.
        sa = make_search_agent(_configured_ini())
        assert sa.model == "qwen3:8b"
        assert sa.base_url == "https://my-secure-llm.internal"
        assert sa.api_key == "secret"


class TestDefaultsPreservedWhenUnconfigured:
    def test_config_none_returns_plain_default_agent(self):
        sa = make_search_agent(None)
        assert isinstance(sa, SearchAgent)
        assert sa.timeout == 120
        assert sa.ssl_context is None
        assert sa.skip_dirs == _DEFAULT_SKIP_DIRS

    def test_empty_config_falls_back_cleanly(self):
        sa = make_search_agent(configparser.ConfigParser())
        assert sa.timeout == 240  # matches Orchestrator's own [loop] fallback
        assert sa.ssl_context is None
        assert sa.max_depth == 2
        assert sa.max_file_kb == 500
        assert sa.skip_dirs == _DEFAULT_SKIP_DIRS

    def test_verify_ssl_true_gives_no_ssl_context(self):
        cfg = configparser.ConfigParser()
        cfg.read_string("[api]\nactive = local\nverify_ssl = true\n")
        sa = make_search_agent(cfg)
        assert sa.ssl_context is None
