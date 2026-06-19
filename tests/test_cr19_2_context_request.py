"""tests/test_cr19_2_context_request.py — AUTO-CR-19-2.

Reconnect the creative CONTEXT_REQUEST pull channel:
  * coder extracts the prose CONTEXT_REQUEST line into CoderResult.missing_context;
  * ContextBroker resolves a chapter-FILE request (e.g. "chapter_2") to that
    file's full content (the section/entity extractors never matched a filename).
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from tools.auto.coder import Coder
from tools.auto.context_broker import ContextBroker


def _coder(task_mode="creative"):
    cfg = configparser.ConfigParser()
    for s in ("coder", "api", "api_local"):
        cfg.add_section(s)
    cfg.set("api", "active", "local")
    cfg.set("api_local", "num_ctx", "32768")
    cfg.set("coder", "max_tokens_creative", "2048")
    return Coder(config=cfg, base_url="http://x", api_key="x", model="m",
                 api_format="ollama", task_mode=task_mode)


# ── prose CONTEXT_REQUEST extraction ─────────────────────────────────────────

def test_extract_prose_context_request():
    body = ("Глава 4\n\nКапитан стояла на мостике.\n\n"
            "CONTEXT_REQUEST: chapter_2, chapter_3")
    assert Coder._extract_context_request_prose(body) == ["chapter_2", "chapter_3"]


def test_extract_prose_context_request_absent():
    assert Coder._extract_context_request_prose("Просто проза без запроса.") == []


def test_json_extractor_still_empty_on_prose():
    # The old JSON extractor must still return [] on prose (that was the bug).
    assert Coder._extract_context_request("Глава 4\nCONTEXT_REQUEST: chapter_2") == []


# ── broker resolves a whole chapter file by name ─────────────────────────────

def test_broker_resolves_chapter_by_number(tmp_path):
    (tmp_path / "chapter_2.txt").write_text(
        "Глава 2\n\nКапитан Рейес и юнга Мира на мостике «Альбатроса».",
        encoding="utf-8",
    )
    b = ContextBroker(max_symbols=5)
    resolved = b.resolve(["chapter_2"], [], tmp_path)
    assert "chapter_2" in resolved
    assert "Рейес" in resolved["chapter_2"]


def test_broker_resolves_by_filename(tmp_path):
    (tmp_path / "chapter_3.md").write_text("Глава 3\n\nЛьды на горизонте.", encoding="utf-8")
    b = ContextBroker(max_symbols=5)
    resolved = b.resolve(["chapter_3.md"], [], tmp_path)
    assert "chapter_3.md" in resolved
    assert "Льды" in resolved["chapter_3.md"]


def test_broker_unknown_chapter_unresolved(tmp_path):
    (tmp_path / "chapter_1.txt").write_text("Глава 1.", encoding="utf-8")
    b = ContextBroker(max_symbols=5)
    resolved = b.resolve(["chapter_9"], [], tmp_path)
    assert "chapter_9" not in resolved


def test_code_symbol_not_resolved_as_file(tmp_path):
    # A code-style symbol must NOT be mistaken for a file.
    (tmp_path / "mod.py").write_text("def my_func():\n    return 1\n", encoding="utf-8")
    b = ContextBroker(max_symbols=5)
    resolved = b.resolve(["my_func"], ["mod.py"], tmp_path)
    # resolved via the normal code path, not the whole-file path
    assert "my_func" in resolved
    assert "def my_func" in resolved["my_func"]


# ── generate() populates missing_context for creative (integration) ──────────

import tools.auto.coder as _coder_mod


def test_generate_populates_missing_context_creative(tmp_path, monkeypatch):
    (tmp_path / "chapter_1.txt").write_text("Глава 1. Рейес на мостике.", encoding="utf-8")
    c = _coder("creative")

    def _fake(url, headers, payload, **kwargs):
        return ("<<<FILE: chapter_2.txt>>>\nГлава 2\n\nКапитан и Мира.\n<<<END>>>\n"
                "CONTEXT_REQUEST: chapter_1")

    monkeypatch.setattr(_coder_mod._llm_stream, "request_completion", _fake)
    res = c.generate(
        {"id": "t1", "title": "Глава 2", "instruction": "Продолжи",
         "target_files": ["chapter_2.txt"],
         "cited_location": {"file": "chapter_1.txt"}},
        tmp_path,
    )
    assert res.missing_context == ["chapter_1"]
    assert res.context_satisfied is False
    # the chapter itself was still written (request line stripped)
    assert res.files_written == ["chapter_2.txt"]
    written = (tmp_path / "chapter_2.txt").read_text(encoding="utf-8")
    assert "CONTEXT_REQUEST" not in written
