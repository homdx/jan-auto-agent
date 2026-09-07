"""A3b: a config value this code *deliberately* refuses must not be swallowed
by _build_plan_validator()'s "never block the run on setup" guard.

(Same audit bucket as A3, test_bugfix_max_files_per_review_nonpositive.py,
which made ClusterReviewer refuse max_files_per_review <= 0 at config read.)

ClusterReviewer() raises ValueError on purpose for a config value an operator
explicitly set to something invalid:

    config [architect] max_files_per_review must be >= 1, got 0

That is a deliberate rejection, not a setup hiccup — falling back to the
default was rejected there on the grounds that the operator would never
learn their config was ignored.

_build_plan_validator() wraps the ClusterReviewer(...) construction in
``except Exception: logger.warning(...); return None``. So the same config
value that makes the main architect review path abort with a clear message
instead made the creative plan-validator path degrade to None, with only a
warning line in the log: the whole creative plan phase then ran with NO
plan validation at all, and nothing told the operator that
``max_files_per_review = 0`` had been rejected. Two paths, two behaviours
for the same bad value.

The fix makes _build_plan_validator() re-raise a config rejection (the
distinct ConfigValueError ClusterReviewer raises for it) while keeping
"never block the run on setup" for the incidental failures the guard was
written for — a missing [api_*] section, a missing key, a malformed
boolean such as ``validate_plan_creative = maybe`` (see
test_bugfix_plan_validator_malformed_config.py). Those still return None.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path

import pytest

from tools.auto.pipeline import _build_plan_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _base_cfg() -> configparser.ConfigParser:
    """A config that would build a plan validator if the feature is on."""
    cfg = configparser.ConfigParser()
    cfg.read_string(
        """
[api]
active     = local
verify_ssl = false

[api_local]
base_url   = http://localhost:11434/v1
api_key    = x
model      = test-model
api_format = openai

[architect]
validate_plan_creative = true
max_tasks_creative     = 1
temperature            = 0.2
max_tokens             = 512
max_file_chars         = 1500
max_files_per_review   = 3
"""
    )
    return cfg


class TestRejectedConfigPropagates:
    """A deliberately refused config value must not silently disable the
    creative plan validator."""

    @pytest.mark.parametrize("value", ["0", "-1", "-6"])
    def test_nonpositive_max_files_per_review_raises(self, value):
        cfg = _base_cfg()
        cfg["architect"]["max_files_per_review"] = value
        with pytest.raises(ValueError, match="max_files_per_review must be >= 1"):
            _build_plan_validator(cfg, "creative")

    def test_rejection_message_names_the_key_and_value(self):
        """The operator must be able to find the offending key."""
        cfg = _base_cfg()
        cfg["architect"]["max_files_per_review"] = "0"
        with pytest.raises(ValueError) as excinfo:
            _build_plan_validator(cfg, "creative")
        msg = str(excinfo.value)
        assert "max_files_per_review" in msg
        assert "0" in msg

    def test_rejection_is_not_swallowed(self):
        """The old behaviour returned None, which read as 'feature off'."""
        cfg = _base_cfg()
        cfg["architect"]["max_files_per_review"] = "0"
        with pytest.raises(ValueError):
            _build_plan_validator(cfg, "creative")


class TestIncidentalSetupFailuresStillDegrade:
    """The guard was written for these. They must keep returning None —
    test_bugfix_plan_validator_malformed_config.py owns the full matrix,
    these are the load-bearing ones for the ConfigValueError split."""

    def test_malformed_validate_plan_creative_returns_none(self):
        cfg = _base_cfg()
        cfg["architect"]["validate_plan_creative"] = "maybe"
        assert _build_plan_validator(cfg, "creative") is None

    def test_missing_architect_section_returns_none(self):
        cfg = configparser.ConfigParser()
        cfg.read_string(
            """
[api]
active     = local
verify_ssl = false

[api_local]
base_url   = http://localhost:11434/v1
api_key    = x
model      = test-model
api_format = openai
"""
        )
        assert _build_plan_validator(cfg, "creative") is None

    def test_missing_api_section_returns_none(self):
        cfg = _base_cfg()
        cfg.remove_section("api_local")
        assert _build_plan_validator(cfg, "creative") is None

    def test_malformed_verify_ssl_returns_none(self):
        cfg = _base_cfg()
        cfg["api"]["verify_ssl"] = "not_a_bool"
        assert _build_plan_validator(cfg, "creative") is None

    def test_feature_disabled_returns_none(self):
        cfg = _base_cfg()
        cfg["architect"]["validate_plan_creative"] = "false"
        assert _build_plan_validator(cfg, "creative") is None

    def test_non_creative_mode_returns_none(self):
        cfg = _base_cfg()
        cfg["architect"]["max_files_per_review"] = "0"
        assert _build_plan_validator(cfg, "code") is None


class TestHappyPathUnaffected:
    def test_valid_config_still_builds_validator(self):
        assert _build_plan_validator(_base_cfg(), "creative") is not None

    def test_omitted_max_files_per_review_uses_default(self):
        """No key at all means the default (6), not a rejection."""
        cfg = _base_cfg()
        del cfg["architect"]["max_files_per_review"]
        result = _build_plan_validator(cfg, "creative")
        assert result is not None
        assert result._max_files_per_review == 6

    def test_nonnumeric_max_files_per_review_still_falls_back(self):
        """An unreadable value degrades to the default (a warning, not an
        error) — only a *readable* value that this code refuses is a
        rejection. Guarded separately in
        test_bugfix_max_files_per_review_nonpositive.py."""
        cfg = _base_cfg()
        cfg["architect"]["max_files_per_review"] = "five"
        result = _build_plan_validator(cfg, "creative")
        assert result is not None
        assert result._max_files_per_review == 6

    def test_one_is_accepted(self):
        cfg = _base_cfg()
        cfg["architect"]["max_files_per_review"] = "1"
        result = _build_plan_validator(cfg, "creative")
        assert result is not None
        assert result._max_files_per_review == 1
