"""tests/test_bugfix_check_improvements_gt_and_title_fallback.py

Two bugs, both in check_improvements.py's ground-truth handling:

1. `find_false_positive`'s title_fallback branch required BOTH sides to
   have zero target_files (`and`), contradicting its own docstring
   ("one/both sides have no target_files at all"). A task with no target
   files vs. a known-FP that does have some (or vice versa) fell through
   silently and matched nothing even with an identical title.

2. The top-level file's ground-truth heading regex only matched
   `## Confirmed FALSE POSITIVE` literally, missing the
   `## Bucket N — Confirmed FALSE POSITIVE` heading style and multi-ID
   rows (`T3/T5`) that real GROUND-TRUTH.md files use — silently treating
   the whole file as empty.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from check_improvements import find_false_positive, _parse_ground_truth_table


def test_title_fallback_matches_when_only_one_side_has_target_files():
    task = {"target_files": set(), "title": "Fix the null pointer bug in parser"}
    fp = {"target_files": {"app.py"}, "title": "Fix the null pointer bug in parser"}
    matched, kind = find_false_positive(task, [fp], 0.5)
    assert matched is fp
    assert kind == "title_fallback"


def test_title_fallback_still_matches_when_neither_side_has_target_files():
    task = {"target_files": set(), "title": "Fix the null pointer bug in parser"}
    fp = {"target_files": set(), "title": "Fix the null pointer bug in parser"}
    matched, kind = find_false_positive(task, [fp], 0.5)
    assert matched is fp
    assert kind == "title_fallback"


def test_title_fallback_does_not_match_dissimilar_titles():
    task = {"target_files": set(), "title": "Fix the null pointer bug"}
    fp = {"target_files": {"app.py"}, "title": "Completely unrelated issue"}
    matched, kind = find_false_positive(task, [fp], 0.5)
    assert matched is None
    assert kind == ""


def test_ground_truth_table_parses_bucket_heading():
    text = (
        "## Bucket 1 \u2014 Confirmed FALSE POSITIVE\n\n"
        "| ID | Location | Reason |\n"
        "|----|----------|--------|\n"
        "| T7 | util.py:20 | some reason |\n"
    )
    tasks = _parse_ground_truth_table(text)
    assert [t["id"] for t in tasks] == ["AUTO-T7"]


def test_ground_truth_table_parses_multi_id_row():
    text = (
        "## Confirmed FALSE POSITIVE\n\n"
        "| ID | Location | Reason |\n"
        "|----|----------|--------|\n"
        "| T3/T5 | app.py:10 | shared root cause |\n"
    )
    tasks = _parse_ground_truth_table(text)
    assert [t["id"] for t in tasks] == ["AUTO-T3", "AUTO-T5"]


def test_ground_truth_table_still_parses_plain_heading():
    text = (
        "## Confirmed FALSE POSITIVE\n\n"
        "| ID | Location | Reason |\n"
        "|----|----------|--------|\n"
        "| T1 | main.py:5 | plain heading still works |\n"
    )
    tasks = _parse_ground_truth_table(text)
    assert [t["id"] for t in tasks] == ["AUTO-T1"]
