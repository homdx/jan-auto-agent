"""tests/test_fix9_run_goal_language_lock.py — AUTO-FIX-9.

Bug found via live simulation (not visible from static code reading): the
AUTO-FIX-5 chapter-1 language-lock fallback used ``task["instruction"]`` as
its language-detection sample. But ``instruction`` is ARCHITECT-AUTHORED LLM
output, and per the entire CR-9/16 language-lock history, small local models
tend to write such fields in English (the system prompt's own language) even
when the story itself is in Russian. A live cold-start run with an
English-authored task instruction reproduced the exact bug AUTO-FIX-5 was
meant to prevent: "LANGUAGE: Write entirely in English" injected into a
Russian story's chapter 1.

Fix: thread the raw --auto GOAL string (verbatim from the CLI/user, never
touched by an LLM) into Coder as ``run_goal``, and prefer it over
task["instruction"] in the language-detection fallback chain.
"""

from __future__ import annotations

import configparser

from tools.auto import coder as coder_mod


def _cfg():
    cfg = configparser.ConfigParser()
    for sec in ("coder", "api", "api_local", "loop", "auto", "context_broker"):
        cfg.add_section(sec)
    cfg.set("coder", "creative_language", "auto")
    cfg.set("api", "active", "local")
    cfg.set("api_local", "num_ctx", "8192")
    return cfg


def test_coder_accepts_run_goal_param():
    c = coder_mod.Coder(
        config=_cfg(), base_url="http://localhost:11434", api_key="x",
        model="llama3.1:8b", api_format="ollama", task_mode="creative",
        run_goal="Написать рассказ о встрече двух друзей.",
    )
    assert c._run_goal == "Написать рассказ о встрече двух друзей."


def test_make_coder_threads_run_goal():
    cfg = _cfg()
    cfg.set("api_local", "base_url", "http://localhost:11434")
    cfg.set("api_local", "api_key", "x")
    cfg.set("api_local", "model", "llama3.1:8b")
    cfg.set("api_local", "api_format", "ollama")
    c = coder_mod.make_coder(cfg, task_mode="creative",
                             run_goal="Написать рассказ по-русски.")
    assert c._run_goal == "Написать рассказ по-русски."


def test_chapter1_locks_russian_even_with_english_task_instruction(tmp_path):
    """The exact bug: architect wrote an ENGLISH task['instruction'] for a
    Russian story's chapter 1 (realistic small-model behaviour — the
    architect's own system prompt is in English). run_goal must win over
    the English instruction so the lock still resolves to Russian."""
    cfg = _cfg()
    c = coder_mod.Coder(
        config=cfg, base_url="http://localhost:11434", api_key="x",
        model="llama3.1:8b", api_format="ollama", task_mode="creative",
        run_goal="Написать рассказ о встрече двух старых друзей.",
    )

    # True cold start: no target content, no predecessor chapters, and an
    # entirely English task instruction (this is what actually happened in
    # the live simulation run).
    task = {
        "id": "AUTO-T1",
        "title": "Chapter 1",
        "instruction": (
            "This is the FIRST chapter. Write the next scene in the story "
            "of two old friends meeting again. Establish the setting."
        ),
        "target_files": ["chapter_1.txt"],
        "cited_location": {"file": "chapter_1.txt", "symbol": None,
                           "line_start": None, "line_end": None},
    }
    prompt = c._build_prompt(task, tmp_path, prior_feedback=[], prefetched_context="")

    assert "Write entirely in Russian" in prompt
    assert "Write entirely in English" not in prompt


def test_falls_back_to_task_instruction_when_run_goal_missing(tmp_path):
    """Older call sites that don't wire run_goal through must not crash and
    should still fall back to task['instruction']/['goal'] as before."""
    cfg = _cfg()
    c = coder_mod.Coder(
        config=cfg, base_url="http://localhost:11434", api_key="x",
        model="llama3.1:8b", api_format="ollama", task_mode="creative",
        # run_goal intentionally omitted -> defaults to ""
    )
    task = {
        "id": "AUTO-T1",
        "title": "Chapter 1",
        "instruction": "Написать первую главу истории о старых друзьях.",
        "target_files": ["chapter_1.txt"],
        "cited_location": {"file": "chapter_1.txt", "symbol": None,
                           "line_start": None, "line_end": None},
    }
    prompt = c._build_prompt(task, tmp_path, prior_feedback=[], prefetched_context="")
    assert "Write entirely in Russian" in prompt
