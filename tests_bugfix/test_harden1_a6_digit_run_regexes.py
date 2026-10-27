"""tests_bugfix/test_harden1_a6_digit_run_regexes.py — HARDEN-1 A6: the two
regexes that read a run of digits are anchored at its first digit.

``story_bible._AGE_RE = (\\d+)\\s*(?:лет|год)`` and
``judge_epic_round._HAS_DURATION = \\d+(?:\\.\\d+)?\\s*[smh]`` both start their
digit run with an unanchored ``\\d+``, so on a line of thousands of digits every
start position was tried and each backtracked through the whole run: 3.7 s /
4.3 s at 10 000 digits and 13.9 s / 14.1 s at 20 000. A run of that length in
one line is unlikely, which is why the reach is low — but nothing capped it.

``(?<!\\d)`` starts the run at its first digit, so the match on ordinary text is
unchanged and a long run costs one attempt instead of one per position.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.story_bible import _AGE_RE  # noqa: E402


def _load_judge():
    path = PROJECT_ROOT / "scripts" / "judge_epic_round.py"
    spec = importlib.util.spec_from_file_location("judge_epic_round_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


judge = _load_judge()


class TestTheAgeRegex:
    def test_twenty_thousand_digits_are_fast(self):
        started = time.perf_counter()
        _AGE_RE.findall("1" * 20000)
        assert time.perf_counter() - started < 1.0

    @pytest.mark.parametrize("line, expected", [
        ("Ей 30 лет, работает 5 лет", ["30", "5"]),
        ("Мужчина 41 год", ["41"]),
        ("41год", ["41"]),
        ("no digits here", []),
    ])
    def test_ordinary_matches_are_unchanged(self, line, expected):
        assert _AGE_RE.findall(line) == expected

    def test_a_digit_that_follows_another_digit_does_not_start_a_second_match(self):
        assert _AGE_RE.findall("1231 год") == ["1231"]


class TestTheDurationRegex:
    def test_twenty_thousand_digits_are_fast(self):
        started = time.perf_counter()
        judge._HAS_DURATION.search("1" * 20000)
        assert time.perf_counter() - started < 1.0

    @pytest.mark.parametrize("line, expected", [
        ("===== 3 passed in 1.23s =====", "1.23s"),
        ("=== 2 failed in 0.9s ===", "0.9s"),
        ("in 5m 3s", "5m"),
        ("no timing on this line", None),
    ])
    def test_ordinary_matches_are_unchanged(self, line, expected):
        found = judge._HAS_DURATION.search(line)
        assert (found.group(0) if found else None) == expected

    def test_the_stats_line_is_still_recognised(self):
        assert judge._counts("===== 1 passed in 0.42s =====") == (1, 1)
