"""tests/test_cr9_language_consistency.py — AUTO-CR-9.

A small model (llama3.1:8b) tends to drift into English when the source story
is in Russian. These tests cover the language-lock helpers and that the
creative coder injects a same-language instruction derived from the story so
far, with an optional config override.
"""

from __future__ import annotations

import configparser


from tools.auto.utils import (
    detect_language,
    language_instruction,
    resolve_creative_language,
)

RU = "Капитан Рейес стоит на мостике «Альбатроса». Рядом юнга Мира."
EN = "Captain Reyes stood on the bridge of the Albatross beside the cabin boy."


# ── detect_language ──────────────────────────────────────────────────────────

def test_detects_russian():
    assert detect_language(RU) == "Russian"


def test_detects_english():
    assert detect_language(EN) == "English"


def test_short_or_ambiguous_returns_none():
    assert detect_language("12 = 7") is None
    assert detect_language("") is None


# ── instruction ──────────────────────────────────────────────────────────────

def test_language_instruction_mentions_language_and_locks():
    instr = language_instruction("Russian")
    assert "Russian" in instr
    assert "SAME language" in instr


def test_language_instruction_empty_when_none():
    assert language_instruction(None) == ""


# ── resolve (config override > detection) ────────────────────────────────────

def _cfg(value=None):
    c = configparser.ConfigParser()
    c.add_section("coder")
    if value is not None:
        c.set("coder", "creative_language", value)
    return c


def test_resolve_auto_uses_detection():
    assert resolve_creative_language(_cfg("auto"), RU) == "Russian"
    assert resolve_creative_language(_cfg(), EN) == "English"


def test_resolve_explicit_override_wins():
    # Source looks English, but author forced Russian.
    assert resolve_creative_language(_cfg("Russian"), EN) == "Russian"


def test_resolve_none_without_signal():
    assert resolve_creative_language(_cfg("auto"), "123") is None


# ── coder integration: prompt carries the lock ───────────────────────────────

def test_creative_coder_prompt_locks_to_source_language(tmp_path, monkeypatch):
    """The assembled creative prompt must contain a Russian language-lock when
    the story so far is Russian.
    """
    from tools.auto import coder as coder_mod

    cfg = configparser.ConfigParser()
    for sec in ("coder", "api", "api_local", "loop", "auto", "context_broker"):
        cfg.add_section(sec)
    cfg.set("coder", "creative_language", "auto")
    cfg.set("api", "active", "local")
    cfg.set("api_local", "num_ctx", "8192")

    c = coder_mod.Coder(
        config=cfg, base_url="http://localhost:11434", api_key="x",
        model="llama3.1:8b", api_format="ollama", task_mode="creative",
    )

    # Make the assembled "story so far" deterministic and Russian.
    monkeypatch.setattr(
        c, "_build_creative_file_contents",
        lambda target_files, base_dir: "СОДЕРЖАНИЕ ПРЕДЫДУЩЕЙ ГЛАВЫ:\n" + RU,
    )

    task = {
        "id": "t1",
        "title": "Глава 2",
        "instruction": "Продолжи историю",
        "target_files": ["chapter2.txt"],
        "cited_location": {"file": "chapter1.txt", "symbol": None,
                           "line_start": None, "line_end": None},
    }
    prompt = c._build_prompt(task, tmp_path, prior_feedback=[], prefetched_context="")
    assert "Write entirely in Russian" in prompt
    assert "СОДЕРЖАНИЕ ПРЕДЫДУЩЕЙ ГЛАВЫ" in prompt
