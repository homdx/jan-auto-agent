"""tests_bugfix/test_contest_status_punctuation_199.py — Bug 1: _status_of and next_task._status must strip the same punctuation."""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.contest.cli as contest_cli
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import next_task as next_task_mod


PUNCTUATION_CASES = [
    ("queued)", "queued"),
    ("(landed)", "landed"),
    ("landed — judged", "landed"),
    ("landed, judged", "landed"),
    ("`open`", "open"),
    ("'open'", "open"),
    ("[open]", "open"),
    ("{open}", "open"),
    ("<open>", "open"),
    ("queued; judged", "queued"),
    ("queued: judged", "queued"),
    ("queued. judged", "queued"),
    ("queued- judged", "queued"),
]


@pytest.mark.parametrize("body,expected", PUNCTUATION_CASES)
def test_status_of_strips_all_punctuation(body, expected):
    full = f"**Status:** {body}"
    assert contest_cli._status_of(full) == expected


@pytest.mark.parametrize("body,expected", PUNCTUATION_CASES)
def test_next_task_status_matches_cli_status(body, expected):
    full = f"**Status:** {body}"
    assert next_task_mod._status(full) == expected
