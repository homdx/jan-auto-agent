"""tests/test_t1_load_config_malformed.py — AUTO-T1 regression.

Before the fix, a present-but-malformed typed config value (e.g.
  [loop]
  max_iterations = abc
) raised a raw ValueError out of configparser.getint().  fallback= only
covers absent keys, not malformed ones, so the whole process crashed at
startup with an unhandled traceback.

After the fix, each typed read in load_config uses one of the _getint /
_getfloat / _getboolean helpers, which catch ValueError, log a warning, and
return the coded default.  The tests here confirm that every affected key in
load_config degrades gracefully rather than raising.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main import Orchestrator


def _make_orc(ini_text: str) -> Orchestrator:
    """Build a bare Orchestrator (skipping __init__) with a pre-loaded config."""
    orc = Orchestrator.__new__(Orchestrator)
    orc.config = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    orc.config.read_string(ini_text)
    # load_config tries to read the file only when it exists on disk.
    # "nonexistent_xyz.ini" is guaranteed absent, so the file-read step is
    # skipped and the already-populated orc.config is used as-is.
    orc.load_config("nonexistent_xyz.ini")
    return orc


class TestMalformedInt:
    def test_max_iterations_malformed_uses_fallback(self):
        orc = _make_orc("[loop]\nmax_iterations = abc")
        assert orc.max_iterations == 3

    def test_timeout_seconds_malformed_uses_fallback(self):
        orc = _make_orc("[loop]\ntimeout_seconds = not_a_number")
        assert orc.timeout_seconds == 240

    def test_num_ctx_malformed_uses_fallback(self):
        orc = _make_orc("[api]\nactive = local\n[api_local]\nnum_ctx = xyz")
        assert orc.num_ctx == 0

    def test_optimizer_min_runs_malformed_uses_fallback(self):
        orc = _make_orc("[prompt_optimizer]\nmin_runs_before_optimize = ???")
        assert orc.optimizer_min_runs == 3

    def test_search_full_file_max_chars_malformed_uses_fallback(self):
        orc = _make_orc("[search]\nfull_file_max_chars = bad")
        assert orc.search_full_file_max_chars == 12000

    def test_file_editor_max_tokens_malformed_uses_fallback(self):
        orc = _make_orc("[file_editor]\nmax_tokens = !")
        assert orc.file_editor_max_tokens == 0


class TestMalformedFloat:
    def test_trigger_avg_iterations_malformed_uses_fallback(self):
        orc = _make_orc("[prompt_optimizer]\ntrigger_avg_iterations = nope")
        assert orc.optimizer_trigger_avg_iter == pytest.approx(2.0)

    def test_trigger_json_fail_rate_malformed_uses_fallback(self):
        orc = _make_orc("[prompt_optimizer]\ntrigger_json_fail_rate = ??")
        assert orc.optimizer_trigger_json_fail == pytest.approx(0.30)


class TestMalformedBoolean:
    def test_verify_ssl_malformed_uses_fallback(self):
        orc = _make_orc("[api]\nverify_ssl = notabool")
        # fallback is True → ssl_context stays None (full verification)
        assert orc.ssl_context is None

    def test_optimizer_enabled_malformed_uses_fallback(self):
        orc = _make_orc("[prompt_optimizer]\nenabled = notabool")
        assert orc.optimizer_enabled is True

    def test_stream_agents_malformed_uses_fallback(self):
        orc = _make_orc("[output]\nstream_agents = notabool")
        assert orc.stream_agents is False


class TestValidValuesUnchanged:
    """Ensure valid config values still come through correctly (no regression)."""

    def test_valid_int_is_honoured(self):
        orc = _make_orc("[loop]\nmax_iterations = 7")
        assert orc.max_iterations == 7

    def test_valid_float_is_honoured(self):
        orc = _make_orc("[prompt_optimizer]\ntrigger_avg_iterations = 1.5")
        assert orc.optimizer_trigger_avg_iter == pytest.approx(1.5)

    def test_valid_boolean_false_is_honoured(self):
        orc = _make_orc("[output]\nstream_agents = false")
        assert orc.stream_agents is False

    def test_valid_boolean_true_is_honoured(self):
        orc = _make_orc("[output]\nstream_agents = true")
        assert orc.stream_agents is True

    def test_empty_config_uses_all_defaults(self):
        """A totally empty ini should produce all coded defaults (no crash)."""
        orc = _make_orc("")
        assert orc.max_iterations == 3
        assert orc.timeout_seconds == 240
        assert orc.optimizer_enabled is True
        assert orc.stream_agents is False


class TestBuildAgentsAlsoGuarded:
    """AUTO-T1 review finding: _build_agents() has 6 more unguarded typed
    reads (prompt_store.max_versions, prompt_optimizer.temperature,
    search.max_file_kb, search.max_depth, validator_agent.temperature,
    validator_agent.max_hints) reachable from the unguarded __init__ ->
    _build_agents() chain. A malformed value in any of these keys crashed
    startup exactly like the load_config keys did, just via a different
    call path. Confirms the same helper-based fix covers them too.
    """

    def test_full_orchestrator_construction_survives_malformed_config(self, tmp_path):
        bad_ini = tmp_path / "bad_agents.ini"
        bad_ini.write_text(
            "[prompt_store]\nmax_versions = abc\n"
            "[prompt_optimizer]\ntemperature = notafloat\n"
            "[search]\nmax_file_kb = bad\nmax_depth = bad\n"
            "[validator_agent]\ntemperature = bad\nmax_hints = bad\n",
            encoding="utf-8",
        )
        # Full __init__ (not the __new__ shortcut) — exercises the real
        # unguarded __init__ -> _build_agents() chain end to end.
        orc = Orchestrator(config_path=str(bad_ini))
        assert orc.prompt_store.max_versions == 3
        assert orc.search_agent.max_file_kb == 500
        assert orc.search_agent.max_depth == 2
        assert orc.validator_agent.max_hints == 3



    def test_several_bad_keys_all_fall_back(self):
        """Multiple malformed keys in one config must each fall back independently."""
        orc = _make_orc(
            "[loop]\nmax_iterations = bad\ntimeout_seconds = also_bad\n"
            "[prompt_optimizer]\nenabled = notabool\ntrigger_avg_iterations = NaN_string\n"
        )
        assert orc.max_iterations == 3
        assert orc.timeout_seconds == 240
        assert orc.optimizer_enabled is True
        assert orc.optimizer_trigger_avg_iter == pytest.approx(2.0)
