"""tests/test_cr10_taskmode_and_grounding.py — AUTO-CR-10.

Two failure modes from the field:
  1. A misspelled task_mode ("creativy") silently degraded to code mode, so
     every creative candidate was rejected for missing acceptance_check /
     strict cited_location. → normalize_task_mode corrects + warns.
  2. For generating a NEW chapter, llama3.1:8b cites the not-yet-existing
     target file (or omits cited_location). Gate1 then rejects everything.
     → _parse_candidates (creative) auto-grounds on an EXISTING chapter.
"""

from __future__ import annotations

import configparser
import json

import pytest

from tools.auto.utils import normalize_task_mode
from tools.auto.architect import ClusterReviewer


# ── task_mode normalisation ──────────────────────────────────────────────────

def test_exact_modes_unchanged():
    assert normalize_task_mode("creative") == ("creative", None)
    assert normalize_task_mode("docs") == ("docs", None)
    assert normalize_task_mode("code") == ("code", None)


def test_typo_corrected_with_warning():
    mode, warn = normalize_task_mode("creativy")
    assert mode == "creative"
    assert warn and "creative" in warn


def test_case_and_space_insensitive():
    assert normalize_task_mode("  Creative ")[0] == "creative"


def test_unknown_falls_back_to_code_with_warning():
    mode, warn = normalize_task_mode("story")
    assert mode == "code"
    assert warn and "unknown" in warn.lower()


def test_empty_defaults_quietly():
    assert normalize_task_mode("") == ("code", None)
    assert normalize_task_mode(None) == ("code", None)


# ── creative grounding auto-repair ───────────────────────────────────────────

def _architect(task_mode):
    cfg = configparser.ConfigParser()
    for sec in ("architect", "auto"):
        cfg.add_section(sec)
    return ClusterReviewer(
        cfg, base_url="http://localhost:11434", api_key="x",
        model="llama3.1:8b", api_format="ollama", task_mode=task_mode,
    )


def test_creative_remaps_nonexistent_cited_file_to_existing_chapter():
    arch = _architect("creative")
    # Model cited chapter_2.txt (the new target, does not exist) — must be
    # remapped to chapter_1.txt (the only existing file in the cluster).
    payload = json.dumps([
        {
            "title": "Write chapter 2",
            "instruction": "Continue the story into chapter 2",
            "target_files": ["chapter_2.txt"],
            "cited_location": {"file": "chapter_2.txt", "symbol": None,
                               "line_start": None, "line_end": None},
        }
    ])
    cands = arch._parse_candidates(payload, "support", ["chapter_1.txt"])
    assert len(cands) == 1
    assert cands[0].cited_location.file == "chapter_1.txt"
    assert cands[0].target_files == ["chapter_2.txt"]


def test_creative_synthesizes_missing_cited_location():
    arch = _architect("creative")
    payload = json.dumps([
        {
            "title": "Write chapter 2",
            "instruction": "Continue the story into chapter 2",
            "target_files": ["chapter_2.txt"],
            "acceptance_check": "true",
            # no cited_location at all
        }
    ])
    cands = arch._parse_candidates(payload, "support", ["chapter_1.txt"])
    assert len(cands) == 1
    assert cands[0].cited_location.file == "chapter_1.txt"


def test_creative_picks_latest_numbered_chapter_as_anchor():
    arch = _architect("creative")
    payload = json.dumps([
        {
            "title": "Write chapter 4",
            "instruction": "Continue",
            "target_files": ["chapter_4.txt"],
            "cited_location": {"file": "nonexistent.txt", "symbol": None,
                               "line_start": None, "line_end": None},
        }
    ])
    cands = arch._parse_candidates(
        payload, "support", ["chapter_1.txt", "chapter_3.txt", "chapter_2.txt"]
    )
    assert len(cands) == 1
    assert cands[0].cited_location.file == "chapter_3.txt"  # highest existing


def test_code_mode_does_not_autorepair():
    arch = _architect("code")
    payload = json.dumps([
        {
            "title": "fix",
            "instruction": "fix it",
            "target_files": ["mod.py"],
            "acceptance_check": "pytest -q",
            # missing cited_location → code mode must still reject
        }
    ])
    cands = arch._parse_candidates(payload, "support", ["mod.py"])
    assert cands == []
