"""Regression tests for scripts/harvest_report.py::classify bucket routing.

The bucket a finding lands in decides whether a human ever looks at it. The one
rule that is easy to get wrong: a single CONFIRMED vote. If it is a NEW-* find
it is a discovery and belongs in ACT; if it is a list entry only one reviewer
reached, it is one weak vote and belongs in DISPUTED (and the verification
queue), never in the action list.
"""
import importlib.util
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "harvest_report",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "harvest_report.py",
)
harvest_report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(harvest_report)
classify = harvest_report.classify


def _row(**kw):
    base = {
        "verdict": "FALSE_POSITIVE", "severity": "NONE", "task_id": "AUTO-T1",
        "file": "f.py", "symbol": "S", "evidence": "", "disproof": "",
    }
    base.update(kw)
    return base


@pytest.mark.parametrize("rows, expected", [
    # one reviewer confirmed a list entry, nobody else reached it -> a human decides
    ([_row(verdict="CONFIRMED")], "DISPUTED"),
    # one reviewer confirmed a find that is not on the list at all -> ACT (the discovery)
    ([_row(verdict="CONFIRMED", task_id="NEW-2")], "ACT"),
    # two independent confirms -> ACT
    ([_row(verdict="CONFIRMED"), _row(verdict="CONFIRMED")], "ACT"),
    # confirmed by one, dismissed by another -> DISPUTED
    ([_row(verdict="CONFIRMED"), _row(verdict="FALSE_POSITIVE")], "DISPUTED"),
    # lone ALREADY_FIXED -> FIXED
    ([_row(verdict="ALREADY_FIXED")], "FIXED"),
    # everyone dismissed it -> DISMISSED (the noise floor)
    ([_row(), _row(), _row()], "DISMISSED"),
])
def test_classify_routing(rows, expected):
    assert classify(rows) == expected


def test_solo_non_new_confirm_never_reaches_act():
    """The specific regression: a rubber-stamp reviewer's lone CONFIRMED on a
    list entry used to land in ACT because len(rows) == 1."""
    assert classify([{"verdict": "CONFIRMED", "severity": "MEDIUM",
                      "task_id": "AUTO-T5", "file": "x.py", "symbol": "C.m",
                      "evidence": "e", "disproof": "d"}]) == "DISPUTED"
