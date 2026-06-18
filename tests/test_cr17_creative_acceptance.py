"""tests/test_cr17_creative_acceptance.py — AUTO-CR-17.

The architect (small model) invented shell acceptance checks like
"diff chapter_1.txt chapter_2.txt" for an edit goal. Those run for real in the
executor and FAIL on every attempt (distinct chapters never diff-equal),
burning the whole task. In creative mode acceptance must be forced to the
"true" no-op; prose quality is judged by Gate-2 / canon, not a shell test.
"""

from __future__ import annotations

import configparser
import json

import pytest

from tools.auto.architect import ClusterReviewer


def _arch(task_mode):
    cfg = configparser.ConfigParser()
    for s in ("architect", "auto"):
        cfg.add_section(s)
    return ClusterReviewer(cfg, base_url="http://x", api_key="x", model="m",
                           api_format="ollama", task_mode=task_mode)


def test_creative_overrides_diff_acceptance():
    payload = json.dumps([{
        "title": "Sync ship descriptions",
        "instruction": "Make the ship description identical in ch1 and ch2",
        "target_files": ["chapter_1.txt", "chapter_2.txt"],
        "acceptance_check": "diff chapter_1.txt chapter_2.txt",
        "cited_location": {"file": "chapter_1.txt", "symbol": None,
                           "line_start": None, "line_end": None},
    }])
    cands = _arch("creative")._parse_candidates(
        payload, "support", ["chapter_1.txt", "chapter_2.txt"]
    )
    assert len(cands) == 1
    assert cands[0].acceptance_check == "true"   # diff neutralised
    assert cands[0].target_files == ["chapter_1.txt", "chapter_2.txt"]


def test_code_mode_keeps_real_acceptance():
    payload = json.dumps([{
        "title": "fix bug",
        "instruction": "fix it",
        "target_files": ["mod.py"],
        "acceptance_check": "pytest -q",
        "cited_location": {"file": "mod.py", "symbol": "f",
                           "line_start": 1, "line_end": 2},
    }])
    cands = _arch("code")._parse_candidates(payload, "support", ["mod.py"])
    assert len(cands) == 1
    assert cands[0].acceptance_check == "pytest -q"   # untouched in code mode
