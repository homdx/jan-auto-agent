"""merge_rows compares incoming rows after the same strip/upper as the existing file.

A padded finding was added as a duplicate of an existing one, and a lowercase truth was
reported as a conflict against the same finding with the same verdict.
"""
from tools.claimcheck.truth import merge_rows


def test_lowercase_truth_of_same_finding_is_not_a_conflict():
    existing = [{"finding": "F1", "truth": "REAL"}]
    add, conflicts = merge_rows(existing, [{"finding": "F1", "truth": "real"}])
    assert add == []
    assert conflicts == []


def test_padded_finding_of_existing_row_is_not_duplicated():
    existing = [{"finding": "F1", "truth": "REAL"}]
    add, conflicts = merge_rows(existing, [{"finding": " F1 ", "truth": "REAL"}])
    assert add == []
    assert conflicts == []


def test_new_padded_row_is_added_normalized():
    add, conflicts = merge_rows([], [{"finding": " F2 ", "truth": "real"}])
    assert conflicts == []
    assert add == [{"finding": "F2", "truth": "REAL"}]


def test_real_conflict_still_reported():
    existing = [{"finding": "F1", "truth": "REAL"}]
    add, conflicts = merge_rows(existing, [{"finding": "F1", "truth": "FALSE"}])
    assert add == []
    assert conflicts == [("F1", "REAL", "FALSE")]
