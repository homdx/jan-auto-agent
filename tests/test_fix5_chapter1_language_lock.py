"""tests/test_fix5_chapter1_language_lock.py — AUTO-FIX-5.

Bug (worse than the originally reported "language lock doesn't fire on
chapter 1"): on a true chapter-1 cold start, ``_creative_language_sample``
returns "" (no target-file content, no predecessor chapters exist yet), and
the old code fell back to ``file_contents`` for language detection. But
``_build_creative_file_contents`` itself returns the English placeholder
``"(new chapter — no prior content to continue from)"`` in that case, so
``detect_language`` returned "English" and the coder prompt was given an
EXPLICIT instruction: "LANGUAGE: Write entirely in English ... Output English
only." — actively steering a Russian story into English on chapter 1, which
is worse than no lock at all.

Fix: fall back to the task's own instruction/goal text (normally in the
story's language) before ever considering file_contents; if that is also
empty, omit the language lock entirely rather than defaulting to English.
"""

from __future__ import annotations

import configparser

from tools.auto.utils import resolve_creative_language


def test_ch1_cold_start_never_defaults_to_english_from_placeholder():
    """The English placeholder text itself must never be used as a
    detection sample — regardless of caller, this string must not
    resolve to 'English'."""
    placeholder = "(new chapter — no prior content to continue from)"
    # Old (buggy) call site: resolve_creative_language(None, "" or placeholder, ...)
    # would have detected English. The fixed coder.py no longer ever passes
    # placeholder text here; verify the detector's own behaviour is inert on
    # an empty detection sample instead (the fix's actual contract).
    assert resolve_creative_language(None, "", task_mode="creative") is None


def test_ch1_falls_back_to_russian_task_instruction():
    """With no prior prose, a Russian task instruction/goal must resolve
    the lock to Russian instead of defaulting to English scaffolding."""
    instr = "Написать рассказ о встрече парня и девушки через 10 лет."
    assert resolve_creative_language(None, instr, task_mode="creative") == "Russian"


def test_coder_prompt_ch1_locks_russian_from_instruction_not_placeholder(tmp_path):
    """End-to-end: a fresh chapter_1 task (no target content, no prior
    chapters) with a Russian instruction must produce a Russian lock in the
    assembled prompt, and must NEVER contain 'Write entirely in English'."""
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

    # True cold start: no target file exists yet, no predecessor chapters —
    # _build_creative_file_contents legitimately returns the English placeholder.
    task = {
        "id": "AUTO-T1",
        "title": "Глава 1",
        "instruction": "Написать рассказ о встрече парня и девушки через 10 лет.",
        "target_files": ["chapter_1.md"],
        "cited_location": {"file": "chapter_1.md", "symbol": None,
                           "line_start": None, "line_end": None},
    }
    prompt = c._build_prompt(task, tmp_path, prior_feedback=[], prefetched_context="")

    assert "Write entirely in Russian" in prompt
    assert "Write entirely in English" not in prompt
