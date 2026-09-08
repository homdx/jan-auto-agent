"""tests_bugfix/test_bugfix_max_tasks_creative_malformed.py

FIX-1 Bug #6: Unguarded Creative Tasks Config.

``ClusterReviewer._parse_candidates_ex`` (tools/auto/architect.py) caps
creative-mode plans with::

    cap = self._config.getint("architect", "max_tasks_creative", fallback=1)

``ConfigParser``'s ``fallback=`` only covers a *missing* key. A key that is
present but non-numeric (``max_tasks_creative = three``, an empty value, or
a stray leftover from editing agents.ini) raises ``ValueError`` straight out
of ``getint``. Nothing on the path from ``_review_one_cluster`` up to
``--auto`` catches it, so a single malformed value crashes the whole plan
phase -- and it does so *after* the architect's LLM call already succeeded
and produced candidates, discarding that work at the last possible moment.

The fix wraps the read in ``try/except ValueError``, logs a warning naming
the offending key, and degrades to the documented default (``1`` -- the
strictest setting, which is the safe direction for a cap that exists to
stop a small model emitting many overlapping creative tasks, per
AUTO-CR-18). This mirrors the guard style already used for every other
config read in ``ClusterReviewer.__init__`` (see e.g. ``temperature``,
``max_tokens``, ``max_file_chars``) -- guarded inline, not via a shared
helper, so ``extract_config_reads`` (tools/collect/ast_facts.py) still
recognizes this as a literal ``getint`` call site.
"""

from __future__ import annotations

import configparser
import json
import logging

import pytest

from tools.auto.architect import ClusterReviewer


def _reviewer(max_tasks_creative: "str | None" = None) -> ClusterReviewer:
    """A minimal creative-mode ClusterReviewer, optionally with a
    max_tasks_creative value (malformed, valid, or omitted)."""
    cfg = configparser.ConfigParser()
    for section in ("architect", "auto"):
        cfg.add_section(section)
    if max_tasks_creative is not None:
        cfg.set("architect", "max_tasks_creative", max_tasks_creative)
    return ClusterReviewer(
        cfg,
        base_url="http://localhost:11434/v1",
        api_key="x",
        model="test-model",
        api_format="openai",
        verify_ssl=False,
        task_mode="creative",
    )


def _candidates(n: int) -> list[dict]:
    """n well-formed, distinctly-grounded creative candidates."""
    return [
        {
            "title": f"Write chapter {i}",
            "instruction": f"Continue the story into chapter {i}.",
            "acceptance_check": "true",
            "target_files": [f"chapter_{i}.txt"],
            "cited_location": {
                "file": "chapter_1.txt",
                "symbol": None,
                "line_start": 1,
                "line_end": 2,
            },
        }
        for i in range(1, n + 1)
    ]


def _parse(max_tasks_creative: "str | None", n: int = 3) -> list:
    return _reviewer(max_tasks_creative)._parse_candidates(
        json.dumps(_candidates(n)), "support", ["chapter_1.txt"]
    )


class TestMalformedCapDegradesToDefault:
    """Every non-numeric flavor of the config value must fall back to 1,
    never raise, matching the documented, strictest default."""

    def test_non_numeric_value_does_not_raise(self):
        assert len(_parse("three")) == 1

    def test_empty_string_value_does_not_raise(self):
        assert len(_parse("")) == 1

    def test_float_string_value_does_not_raise(self):
        """"2.5" is not a valid int literal -- getint rejects it too."""
        assert len(_parse("2.5")) == 1

    def test_garbage_value_does_not_raise(self):
        assert len(_parse("  not-a-number \x00")) == 1

    def test_malformed_cap_keeps_the_first_candidate_in_order(self):
        cands = _parse("three")
        assert cands[0].title == "Write chapter 1"

    def test_warning_is_logged_naming_the_key(self, caplog):
        with caplog.at_level(logging.WARNING, logger="tools.auto.architect"):
            _parse("three")
        assert any(
            "max_tasks_creative" in rec.message and "malformed" in rec.message
            for rec in caplog.records
        ), "expected a warning naming the malformed config key"


class TestWellFormedCapStillApplies:
    """The fix must not change behavior for values getint already handled."""

    def test_missing_key_defaults_to_one(self):
        assert len(_parse(None)) == 1

    def test_numeric_cap_is_honoured(self):
        assert len(_parse("3")) == 3

    def test_numeric_cap_below_total_trims_correctly(self):
        assert len(_parse("2")) == 2

    def test_negative_cap_is_clamped_to_one(self):
        """getint("-5") parses fine (no ValueError); max(1, cap) clamps it."""
        assert len(_parse("-5")) == 1

    def test_zero_cap_is_clamped_to_one(self):
        assert len(_parse("0")) == 1


class TestNonCreativeModeUnaffected:
    """The cap -- and its guard -- only apply in creative mode."""

    def test_code_mode_ignores_max_tasks_creative(self):
        cfg = configparser.ConfigParser()
        for section in ("architect", "auto"):
            cfg.add_section(section)
        cfg.set("architect", "max_tasks_creative", "not_a_number")
        reviewer = ClusterReviewer(
            cfg,
            base_url="http://localhost:11434/v1",
            api_key="x",
            model="test-model",
            api_format="openai",
            verify_ssl=False,
            task_mode="code",
        )
        items = [
            {
                "title": f"Fix bug {i}",
                "instruction": "Fix it.",
                "acceptance_check": "true",
                "target_files": [f"file_{i}.py"],
                "cited_location": {
                    "file": f"file_{i}.py", "symbol": "f", "line_start": None, "line_end": None,
                },
            }
            for i in range(3)
        ]
        # Malformed max_tasks_creative must not even be consulted (and must
        # certainly not crash) when task_mode != "creative".
        cands = reviewer._parse_candidates(
            json.dumps(items), "support", [f"file_{i}.py" for i in range(3)]
        )
        assert len(cands) == 3
