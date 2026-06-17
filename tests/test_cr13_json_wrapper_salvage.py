"""tests/test_cr13_json_wrapper_salvage.py — AUTO-CR-13.

The first fully-working run produced real Russian prose but the model wrapped it
in a JSON object ({"tasks":[{"files":[{"content": "..."}]}]}) and the whole JSON
string was written into chapter_2.txt. Two fixes:
  1. the closing prompt line is mode-aware (creative asks for prose, not JSON);
  2. the prose parser salvages the inner prose if a JSON wrapper still appears.
"""

from __future__ import annotations

import configparser

import pytest

from tools.auto.coder import _extract_prose_from_json, Coder


# Exact wrapper shape from the field log.
LOG_WRAPPER = (
    '{\n  "tasks": [\n    {\n      "id": "AUTO-T1",\n      "files": [\n'
    '        {"name": "chapter_2.txt", "content": "Глава 2\\n\\nКапитан Рейес '
    'стоит на мостике."}\n      ]\n    }\n  ],\n  "context_request": ""\n}'
)


def test_extract_from_tasks_files_wrapper():
    prose = _extract_prose_from_json(LOG_WRAPPER)
    assert prose is not None
    assert prose.startswith("Глава 2")
    assert "Капитан Рейес" in prose
    assert "tasks" not in prose and "context_request" not in prose


def test_extract_from_simple_files_wrapper():
    body = '{"files": [{"path": "ch.txt", "content": "Текст главы."}]}'
    assert _extract_prose_from_json(body) == "Текст главы."


def test_extract_from_top_level_content():
    body = '{"content": "Просто текст."}'
    assert _extract_prose_from_json(body) == "Просто текст."


def test_plain_prose_returns_none():
    # Real prose is not a JSON wrapper → None (caller keeps it as-is).
    assert _extract_prose_from_json("Глава 2\n\nКапитан стоял на мостике.") is None


def test_non_wrapper_json_array_returns_none_or_empty():
    # A JSON array with no content fields yields None.
    assert _extract_prose_from_json('[{"foo": "bar"}]') is None


def test_parse_response_prose_salvages_wrapper():
    cfg = configparser.ConfigParser()
    for sec in ("coder", "api", "api_local"):
        cfg.add_section(sec)
    cfg.set("api", "active", "local")
    cfg.set("api_local", "num_ctx", "8192")
    cfg.set("coder", "max_tokens_creative", "2048")
    c = Coder(config=cfg, base_url="http://x", api_key="x", model="m",
              api_format="ollama", task_mode="creative")

    files, err = c._parse_response_prose(LOG_WRAPPER, "AUTO-T1", ["chapter_2.txt"])
    assert err == ""
    assert len(files) == 1
    assert files[0]["path"] == "chapter_2.txt"
    assert files[0]["content"].startswith("Глава 2")
    assert "tasks" not in files[0]["content"]


def test_creative_closing_instruction_asks_for_prose(tmp_path):
    cfg = configparser.ConfigParser()
    for sec in ("coder", "api", "api_local"):
        cfg.add_section(sec)
    cfg.set("api", "active", "local")
    cfg.set("api_local", "num_ctx", "8192")
    cfg.set("coder", "max_tokens_creative", "2048")
    (tmp_path / "chapter_1.txt").write_text("Глава 1. Начало.", encoding="utf-8")
    c = Coder(config=cfg, base_url="http://x", api_key="x", model="m",
              api_format="ollama", task_mode="creative")
    task = {"id": "t1", "title": "Глава 2", "instruction": "Продолжи",
            "target_files": ["chapter_2.txt"],
            "cited_location": {"file": "chapter_1.txt"}}
    prompt = c._build_prompt(task, tmp_path, prior_feedback=[], prefetched_context="")
    assert "no JSON" in prompt
    assert "Return ONLY the JSON object" not in prompt


def test_code_closing_instruction_still_json():
    cfg = configparser.ConfigParser()
    for sec in ("coder", "api", "api_local"):
        cfg.add_section(sec)
    cfg.set("api", "active", "local")
    c = Coder(config=cfg, base_url="http://x", api_key="x", model="m",
              api_format="ollama", task_mode="code")
    task = {"id": "t1", "title": "fix", "instruction": "fix",
            "target_files": ["mod.py"],
            "cited_location": {"file": "mod.py"}}
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "mod.py").write_text("x=1", encoding="utf-8")
        prompt = c._build_prompt(task, pathlib.Path(d), prior_feedback=[], prefetched_context="")
    assert "Return ONLY the JSON object" in prompt
