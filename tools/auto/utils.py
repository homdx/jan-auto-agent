"""tools/auto/utils.py — Shared micro-utilities for the autonomous mode."""
from __future__ import annotations

import configparser
from datetime import datetime
from typing import Any


def _ts() -> str:
    """Return the current local time as an ISO-8601 string (YYYY-MM-DDTHH:MM:SS)."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _cfg_mode(
    config: configparser.ConfigParser,
    section: str,
    key: str,
    task_mode: str,
    fallback: Any = None,
) -> Any:
    """Read a mode-specific config key with graceful fallback (AUTO-CR-3).

    Priority (highest -> lowest):

    1. ``{key}_{task_mode}`` in *section*  -- e.g. ``max_tokens_creative``
    2. ``{key}``            in *section*  -- e.g. ``max_tokens``
    3. *fallback*

    Works for any ``task_mode`` value; when ``task_mode`` is ``"code"`` (the
    default) step 1 is effectively skipped because ``max_tokens_code`` is
    unlikely to be set, so it behaves identically to a plain ``config.get``.

    Parameters
    ----------
    config:
        Parsed ``agents.ini``.
    section:
        INI section name (e.g. ``"coder"``).
    key:
        Base key name (e.g. ``"max_tokens"``).
    task_mode:
        Current task mode (e.g. ``"creative"``).
    fallback:
        Value returned when neither key variant is present.

    Returns
    -------
    str | None
        The raw string value, or *fallback* when not found.  Callers are
        responsible for casting to ``int`` / ``float`` as required.
    """
    mode_key = f"{key}_{task_mode}"
    val = config.get(section, mode_key, fallback=None)
    if val is not None and str(val).strip():
        return val
    val = config.get(section, key, fallback=None)
    if val is not None and str(val).strip():
        return val
    return fallback


# ── AUTO-CR-9: language consistency for creative mode ────────────────────────

def detect_language(text: str) -> "str | None":
    """Best-effort detection of the dominant script/language of *text*.

    Returns a human-readable language NAME suitable for dropping into an LLM
    instruction (e.g. ``"Russian"``, ``"English"``), or ``None`` when the text
    is too short / ambiguous to decide.

    Intentionally lightweight (no external deps): it distinguishes the scripts
    that matter for this project by counting characters. Cyrillic vs Latin is
    the common case (Russian source, English drift); a few other scripts are
    recognised so the instruction is correct if the source is in them.
    """
    if not text:
        return None
    counts = {
        "Russian": 0,    # Cyrillic
        "English": 0,    # Latin
        "Greek": 0,
        "Arabic": 0,
        "Hebrew": 0,
        "Chinese": 0,
        "Japanese": 0,
    }
    for ch in text:
        o = ord(ch)
        if 0x0400 <= o <= 0x04FF:
            counts["Russian"] += 1
        elif (0x41 <= o <= 0x5A) or (0x61 <= o <= 0x7A):
            counts["English"] += 1
        elif 0x0370 <= o <= 0x03FF:
            counts["Greek"] += 1
        elif 0x0600 <= o <= 0x06FF:
            counts["Arabic"] += 1
        elif 0x0590 <= o <= 0x05FF:
            counts["Hebrew"] += 1
        elif 0x4E00 <= o <= 0x9FFF:
            counts["Chinese"] += 1
        elif (0x3040 <= o <= 0x30FF):
            counts["Japanese"] += 1

    total = sum(counts.values())
    if total < 8:           # too little signal to be sure
        return None
    lang, n = max(counts.items(), key=lambda kv: kv[1])
    if n == 0 or (n / total) < 0.30:
        return None
    return lang


def resolve_creative_language(
    config: "configparser.ConfigParser | None",
    context_text: str = "",
    task_mode: str = "creative",
) -> "str | None":
    """Resolve the language a creative chapter must be written in.

    Priority:
      1. Explicit ``[coder] creative_language`` (or ``language_creative``) in
         config — lets the author force a language regardless of detection.
      2. Auto-detection from *context_text* (the story so far).
      3. ``None`` — caller omits the language instruction (model decides).
    """
    if config is not None:
        for key in ("creative_language", "language_creative"):
            if config.has_option("coder", key):
                val = (config.get("coder", key) or "").strip()
                if val and val.lower() not in ("auto", "detect", ""):
                    return val
    return detect_language(context_text)


def language_instruction(language: "str | None") -> str:
    """Return a strong, prompt-ready language-lock instruction, or ``""``.

    Used by the creative coder/summary/canon prompts so a small model
    (e.g. llama3.1:8b) does not drift into English when the source is Russian.
    """
    if not language:
        return ""
    return (
        f"LANGUAGE: Write entirely in {language}. The story so far is in "
        f"{language}; you MUST continue in the SAME language. Do not switch to "
        f"another language, do not translate, do not add text in any other "
        f"language. Output {language} only."
    )


# ── AUTO-CR-10: task_mode normalisation (typo-tolerant) ──────────────────────

_KNOWN_TASK_MODES = ("code", "docs", "creative")


def normalize_task_mode(value: "str | None") -> tuple[str, "str | None"]:
    """Normalise a configured ``task_mode`` to a known mode.

    Returns ``(mode, warning)``. ``warning`` is ``None`` when *value* was an
    exact known mode, otherwise a human-readable message describing the
    correction (so the controller can log it loudly instead of silently
    degrading — e.g. ``creativy`` → ``creative``).

    Mapping:
      * exact match (case/space-insensitive) → that mode
      * starts with ``creat`` → ``creative``     (catches ``creativy``, ``creativ``)
      * starts with ``doc``   → ``docs``
      * starts with ``cod``   → ``code``
      * anything else         → ``code`` (with a warning)
    """
    raw = (value or "").strip()
    low = raw.lower()
    if low in _KNOWN_TASK_MODES:
        return low, None
    if not low:
        return "code", None  # empty/missing → default, no warning

    if low.startswith("creat"):
        guess = "creative"
    elif low.startswith("doc"):
        guess = "docs"
    elif low.startswith("cod"):
        guess = "code"
    else:
        return (
            "code",
            f"unknown task_mode {raw!r} — expected one of "
            f"{_KNOWN_TASK_MODES}; falling back to 'code'.",
        )
    return (
        guess,
        f"task_mode {raw!r} is not exact — interpreting as {guess!r}. "
        f"Set [auto] task_mode = {guess} in agents.ini to silence this.",
    )
