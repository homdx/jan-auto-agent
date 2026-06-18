"""tests/test_cr16_edit_mode_and_cleanup.py — AUTO-CR-16.

From the "fix inconsistencies" run:
  * editing chapter_1 (no predecessor) gave the coder an EMPTY context, so it
    invented a new English chapter and destroyed the Russian original;
  * only target_files[0] was ever loaded;
  * the synopsis still kept meta-commentary parentheticals inside bullets.

CR-16:
  (a) load current content of ALL target files; edit-aware closing instruction;
  (b) strip meta parentheticals from synopsis bullets;
  (c) configurable creative attempt cap.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from tools.auto.coder import Coder
from tools.auto.summary_memory import _clean_bullet_list, _strip_meta_parentheticals


RU1 = "# Глава 1\nКапитан Рейес стоит на мостике «Альбатроса». Ей сорок."


def _coder(task_mode="creative"):
    cfg = configparser.ConfigParser()
    for sec in ("coder", "api", "api_local"):
        cfg.add_section(sec)
    cfg.set("api", "active", "local")
    cfg.set("api_local", "num_ctx", "32768")
    cfg.set("coder", "max_tokens_creative", "2048")
    return Coder(config=cfg, base_url="http://x", api_key="x", model="m",
                 api_format="ollama", task_mode=task_mode)


# ── (a) edit-aware context ────────────────────────────────────────────────────

def test_loads_current_content_of_target(tmp_path):
    (tmp_path / "chapter_1.txt").write_text(RU1, encoding="utf-8")
    c = _coder()
    ctx = c._build_creative_file_contents(["chapter_1.txt"], tmp_path)
    assert "Рейес" in ctx                 # target's own content is present
    assert "FILES TO REVISE" in ctx
    assert "no prior chapters" not in ctx


def test_loads_all_target_files(tmp_path):
    (tmp_path / "chapter_1.txt").write_text("Глава 1. Рейес.", encoding="utf-8")
    (tmp_path / "chapter_2.txt").write_text("Глава 2. Мира.", encoding="utf-8")
    (tmp_path / "chapter_3.txt").write_text("Глава 3. Льды.", encoding="utf-8")
    c = _coder()
    ctx = c._build_creative_file_contents(
        ["chapter_1.txt", "chapter_2.txt", "chapter_3.txt"], tmp_path
    )
    assert "Рейес" in ctx and "Мира" in ctx and "Льды" in ctx
    assert ctx.count("<<<FILE:") == 3


def test_is_edit_detection(tmp_path):
    (tmp_path / "chapter_1.txt").write_text(RU1, encoding="utf-8")
    (tmp_path / "chapter_2.txt").write_text("", encoding="utf-8")
    c = _coder()
    assert c._creative_is_edit(["chapter_1.txt"], tmp_path) is True
    assert c._creative_is_edit(["chapter_2.txt"], tmp_path) is False


def test_edit_task_language_locks_to_russian(tmp_path):
    (tmp_path / "chapter_1.txt").write_text(RU1, encoding="utf-8")
    c = _coder()
    task = {"id": "t1", "title": "fix", "instruction": "Исправь нестыковки",
            "target_files": ["chapter_1.txt"],
            "cited_location": {"file": "chapter_1.txt"}}
    prompt = c._build_prompt(task, tmp_path, prior_feedback=[], prefetched_context="")
    assert "Write entirely in Russian" in prompt
    # edit-aware closing instruction, not "write a new chapter"
    assert "revised text" in prompt.lower()


def test_empty_target_uses_continuation_instruction(tmp_path):
    # chapter_2 empty, chapter_1 present → continuation (write new chapter 2)
    (tmp_path / "chapter_1.txt").write_text(RU1, encoding="utf-8")
    (tmp_path / "chapter_2.txt").write_text("", encoding="utf-8")
    c = _coder()
    task = {"id": "t1", "title": "Глава 2", "instruction": "Продолжи",
            "target_files": ["chapter_2.txt"],
            "cited_location": {"file": "chapter_1.txt"}}
    prompt = c._build_prompt(task, tmp_path, prior_feedback=[], prefetched_context="")
    assert "Write the complete chapter now as plain prose" in prompt


# ── (b) synopsis cleanup ──────────────────────────────────────────────────────

def test_strip_meta_parenthetical():
    s = "Captain wants to speak with the cook (this bullet should be removed because it's not relevant)"
    assert _strip_meta_parentheticals(s) == "Captain wants to speak with the cook"


def test_keep_genuine_parenthetical():
    s = "The ship (the Albatross) sails north"
    # "Albatross" has no meta markers → kept
    assert "(the Albatross)" in _strip_meta_parentheticals(s)


def test_clean_bullet_list_drops_meta_tail():
    reply = ("• The crew is seasoned (this bullet is partially correct; it "
             "doesn't mention the cargo)")
    out = _clean_bullet_list(reply)
    assert "• The crew is seasoned" in out
    assert "partially correct" not in out


# ── (c) configurable attempts ─────────────────────────────────────────────────

def test_creative_attempts_override():
    from tools.auto.utils import _cfg_mode
    cfg = configparser.ConfigParser()
    cfg.add_section("auto")
    cfg.set("auto", "max_attempts_per_task", "5")
    cfg.set("auto", "max_attempts_per_task_creative", "8")
    assert int(_cfg_mode(cfg, "auto", "max_attempts_per_task", "creative", "5")) == 8
    assert int(_cfg_mode(cfg, "auto", "max_attempts_per_task", "code", "5")) == 5
