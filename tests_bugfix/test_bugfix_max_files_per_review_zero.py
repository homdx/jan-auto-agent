"""tests_bugfix/test_bugfix_max_files_per_review_zero.py

``[architect] max_files_per_review`` is read with a plain ``int()`` wrapped in
try/except ValueError, so it only rejects non-numeric values.  ``0`` parses
fine and reaches the batch split inside ``ClusterReviewer.review_clusters``:

    files[i:i + self._max_files_per_review]
        for i in range(0, len(files), self._max_files_per_review)

``range(0, n, 0)`` raises ``ValueError: range() arg 3 must not be zero`` for
ANY non-empty cluster — a single plausible config value kills the whole review
phase, once per cluster.

The failure is deferred: it happens at run time, deep in the architect, far
from the config key that caused it, so the operator gets a traceback instead of
a config error.  The non-numeric case (``max_files_per_review = five``) is
already guarded and falls back to the default with a warning; the numeric-zero
case sailed past it.

Fix under test: validate the range at read time and raise a ValueError that
names the config key, instead of surfacing ``range() arg 3 must not be zero``
three hundred lines later.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.architect import ClusterReviewer, RepoCluster


def _cfg(architect_overrides: dict | None = None) -> configparser.ConfigParser:
    arch = {
        "temperature": "0.2",
        "max_tokens": "512",
        "max_file_chars": "2000",
        "max_files_per_review": "2",
    }
    arch.update(architect_overrides or {})
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url":   "http://localhost:1337/v1",
            "api_key":    "test",
            "model":      "test-model",
            "api_format": "openai",
        },
        "architect": arch,
    })
    return cfg


def _reviewer(cfg: configparser.ConfigParser) -> ClusterReviewer:
    return ClusterReviewer(cfg, base_url="http://localhost:1337/v1", api_key="test",
                           model="test-model", api_format="openai")


class TestZeroStepRejectedAtRead:
    def test_zero_raises_value_error_naming_the_key(self):
        """THE BUG: ``0`` must not be accepted as a batch size.

        Previously it reached ``range(0, n, 0)`` inside review_clusters and
        raised ``ValueError: range() arg 3 must not be zero`` per cluster.
        """
        with pytest.raises(ValueError, match="max_files_per_review must be >= 1"):
            _reviewer(_cfg({"max_files_per_review": "0"}))

    def test_negative_raises_value_error(self):
        with pytest.raises(ValueError, match="max_files_per_review must be >= 1"):
            _reviewer(_cfg({"max_files_per_review": "-3"}))

    def test_error_message_names_the_config_key(self):
        with pytest.raises(ValueError) as exc_info:
            _reviewer(_cfg({"max_files_per_review": "0"}))
        assert "[architect]" in str(exc_info.value)
        assert "max_files_per_review" in str(exc_info.value)

    def test_review_clusters_does_not_crash_on_range(self, tmp_path):
        """The reported symptom: previously the config parsed, and review_clusters
        blew up on the first non-empty cluster with
        ``ValueError: range() arg 3 must not be zero``.

        The failure must now happen at the config read, with a message that
        names the key, so the operator fixes the config instead of reading a
        traceback from inside the architect.
        """
        (tmp_path / "a.py").write_text("def a(): pass\n", encoding="utf-8")
        with pytest.raises(ValueError, match="max_files_per_review must be >= 1"):
            reviewer = _reviewer(_cfg({"max_files_per_review": "0"}))
            reviewer.review_clusters(
                [RepoCluster(name="src", patterns=["*"], files=["a.py"])],
                tmp_path, "g",
            )


class TestSanity:
    def test_minimum_valid_value_is_accepted(self, tmp_path):
        """``1`` is a legitimate batch size and must batch into singletons."""
        for name in ("a.py", "b.py"):
            (tmp_path / name).write_text(f"def {name[0]}(): pass\n", encoding="utf-8")
        reviewer = _reviewer(_cfg({"max_files_per_review": "1"}))

        seen: list[list[str]] = []

        def fake_review(self, cluster, base_dir, goal, all_files=None):
            seen.append(list(cluster.files))
            return []

        with patch.object(ClusterReviewer, "_review_one_cluster", fake_review):
            reviewer.review_clusters(
                [RepoCluster(name="src", patterns=["*"], files=["a.py", "b.py"])],
                tmp_path, "g",
            )
        assert seen == [["a.py"], ["b.py"]]

    def test_default_applies_when_key_absent(self):
        cfg = _cfg()
        cfg.remove_option("architect", "max_files_per_review")
        assert _reviewer(cfg)._max_files_per_review == 6

    def test_non_numeric_still_falls_back(self):
        """The pre-existing guard for the non-numeric case must not regress."""
        reviewer = _reviewer(_cfg({"max_files_per_review": "five"}))
        assert reviewer._max_files_per_review == 6

    def test_default_batches_a_large_cluster(self, tmp_path):
        """A non-empty cluster with the default batch size must not raise at all
        — this is the shape of the crash the zero case used to produce."""
        (tmp_path / "big.py").write_text("x = 1\n", encoding="utf-8")
        reviewer = _reviewer(_cfg({"max_files_per_review": "2"}))

        def fake_review(self, cluster, base_dir, goal, all_files=None):
            return []

        with patch.object(ClusterReviewer, "_review_one_cluster", fake_review):
            candidates = reviewer.review_clusters(
                [RepoCluster(name="src", patterns=["*"], files=["big.py"])],
                tmp_path, "g",
            )
        assert candidates == []
