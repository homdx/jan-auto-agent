"""tests/test_cr11_truncated_plan.py — AUTO-CR-11.

A small model truncates the JSON plan mid-object when it runs out of output
tokens, which made json.loads fail on the whole array → 0 candidates. The
salvage parser recovers the complete objects from the prefix.
"""

from __future__ import annotations

import configparser

import pytest

from tools.auto.architect import _salvage_json_objects, ClusterReviewer


# Mirrors the field log: 5 verbose tasks, the 5th truncated mid-string.
TRUNCATED = """[
  {
    "title": "Add chapter 2 content",
    "instruction": "Write a new section in chapter_2.txt continuing chapter_1",
    "target_files": ["chapter_2.txt"],
    "acceptance_check": "true",
    "cited_location": {"file": "chapter_2.txt", "symbol": null, "line_start": 0, "line_end": 10}
  },
  {
    "title": "Describe the journey",
    "instruction": "Describe the voyage of the Альбатрос",
    "target_files": ["chapter_2.txt"],
    "acceptance_check": "true",
    "cited_location": {"file": "chapter_2.txt", "symbol": null, "line_start": 5, "line_end": 15}
  },
  {
    "title": "Add a cliffhanger ending",
    "instruction": "In chapter_2.txt, add a cliffhanger that sets up the next chapter in the story",
    "target_files": ["chapter_2.txt"],
    "acceptance_check": "true",
    "cited_location": {
      "file": "chapter"""


def test_salvage_recovers_complete_objects():
    objs = _salvage_json_objects(TRUNCATED)
    assert len(objs) == 2  # two complete tasks before the truncated third
    assert objs[0]["title"] == "Add chapter 2 content"
    assert objs[1]["title"] == "Describe the journey"


def test_salvage_handles_braces_inside_strings():
    text = '[{"a": "has { and } braces", "b": 1}, {"c": "ok"'
    objs = _salvage_json_objects(text)
    assert len(objs) == 1
    assert objs[0]["a"] == "has { and } braces"


def test_salvage_empty_when_nothing_complete():
    assert _salvage_json_objects('[{"a": "unterminated') == []


def _architect(task_mode):
    cfg = configparser.ConfigParser()
    for sec in ("architect", "auto"):
        cfg.add_section(sec)
    return ClusterReviewer(
        cfg, base_url="http://localhost:11434", api_key="x",
        model="llama3.1:8b", api_format="ollama", task_mode=task_mode,
    )


def test_parse_candidates_uses_salvage_on_truncation():
    arch = _architect("creative")
    cands = arch._parse_candidates(TRUNCATED, "support", ["chapter_1.txt", "chapter_2.txt"])
    # Both complete tasks survive (creative: file grounding OK, anchors ignored).
    assert len(cands) == 2
    assert all(c.target_files == ["chapter_2.txt"] for c in cands)


def test_creative_max_tokens_override_read():
    cfg = configparser.ConfigParser()
    cfg.add_section("architect")
    cfg.set("architect", "max_tokens", "512")
    cfg.set("architect", "max_tokens_creative", "1024")
    a_code = ClusterReviewer(cfg, base_url="x", api_key="x", model="m",
                             api_format="ollama", task_mode="code")
    a_creative = ClusterReviewer(cfg, base_url="x", api_key="x", model="m",
                                 api_format="ollama", task_mode="creative")
    assert a_code._max_tokens == 512
    assert a_creative._max_tokens == 1024
