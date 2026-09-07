"""A3: max_files_per_review <= 0 must fail at config read, not at review time.

``max_files_per_review`` is used as the step of ``range(0, len(files),
self._max_files_per_review)`` in ClusterReviewer.review_clusters()
(architect.py). ``int("0")`` parses fine — only NON-NUMERIC values were
guarded — so a plausible config value like ``max_files_per_review = 0``
constructed the reviewer successfully and then raised
``ValueError: range() arg 3 must not be zero`` deep inside the review
phase, for every non-empty cluster.

Failing at construction (config load) is the point: the operator sees the
bad value next to the config key it came from, not a traceback deep in the
architect with the whole plan phase already burned.

Deliberately NOT wrapped in try/except (no fallback to the default): a
silent substitution would turn a misconfiguration into a run that reviewed
6 files at a time when the operator asked for 0, and the operator would
never learn their config was ignored.
"""

import configparser

import pytest

from tools.auto.architect import ClusterReviewer, _DEFAULT_MAX_FILES_PER_REVIEW

_API = {
    "active": "local",
    "verify_ssl": "false",
}

_API_LOCAL = {
    "base_url": "http://localhost:1337/v1",
    "api_key": "test",
    "model": "test-model",
    "api_format": "openai",
}


def _cfg(max_files_per_review):
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       _API,
        "api_local": _API_LOCAL,
        "architect": {"temperature": "0.2", "max_tokens": "512",
                      "max_files_per_review": str(max_files_per_review)},
    })
    return cfg


class TestNonPositiveStepRejectedAtRead:
    @pytest.mark.parametrize("value", [0, -1, -6, -100])
    def test_nonpositive_raises_valueerror(self, value):
        with pytest.raises(ValueError, match="max_files_per_review must be >= 1"):
            ClusterReviewer(_cfg(value), **{
                "base_url": "http://localhost:1337/v1",
                "api_key": "test",
                "model": "test-model",
            })

    def test_error_names_the_key(self):
        """The message must let the operator find the offending config key."""
        with pytest.raises(ValueError) as excinfo:
            ClusterReviewer(_cfg(0), **{
                "base_url": "http://localhost:1337/v1",
                "api_key": "test",
                "model": "test-model",
            })
        assert "max_files_per_review" in str(excinfo.value)
        assert "0" in str(excinfo.value)

    def test_zero_does_not_construct(self):
        """A reviewer built with a zero step can only fail later, at
        review time, with a confusing 'range() arg 3 must not be zero'."""
        with pytest.raises(ValueError):
            ClusterReviewer(_cfg(0), **{
                "base_url": "http://localhost:1337/v1",
                "api_key": "test",
                "model": "test-model",
            })


class TestValidValuesStillAccepted:
    def test_one_is_valid(self):
        r = ClusterReviewer(_cfg(1), **{
            "base_url": "http://localhost:1337/v1",
            "api_key": "test",
            "model": "test-model",
        })
        assert r._max_files_per_review == 1

    def test_typical_value_is_valid(self):
        r = ClusterReviewer(_cfg(6), **{
            "base_url": "http://localhost:1337/v1",
            "api_key": "test",
            "model": "test-model",
        })
        assert r._max_files_per_review == 6

    def test_omitted_key_uses_default(self):
        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "api": _API,
            "api_local": _API_LOCAL,
            "architect": {"temperature": "0.2", "max_tokens": "512"},
        })
        r = ClusterReviewer(cfg, **{
            "base_url": "http://localhost:1337/v1",
            "api_key": "test",
            "model": "test-model",
        })
        assert r._max_files_per_review == _DEFAULT_MAX_FILES_PER_REVIEW

    def test_nonnumeric_still_falls_back_to_default(self):
        """The pre-existing guard for non-numeric values (max_files_per_review
        = five) must keep working — it is a different failure mode: the value
        is unreadable, so degrading to the default is right. A numeric 0 is
        readable and means something, so it is refused outright."""
        r = ClusterReviewer(_cfg("five"), **{
            "base_url": "http://localhost:1337/v1",
            "api_key": "test",
            "model": "test-model",
        })
        assert r._max_files_per_review == _DEFAULT_MAX_FILES_PER_REVIEW
