"""tools/auto/utils.py — Shared micro-utilities for the autonomous mode."""
from __future__ import annotations

import configparser
import hashlib
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


def _ts() -> str:
    """Return the current local time as an ISO-8601 string (YYYY-MM-DDTHH:MM:SS)."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def safe_filename_component(value: str) -> str:
    """Return a filesystem-safe version of *value* for use as a file/dir name.

    Strips path separators, leading dots, and anything else that could let a
    crafted id (e.g. ``"../../evil"``) escape the intended directory when
    interpolated into a path. Keeps only alphanumerics, hyphens, and
    underscores. Shared by StateStore (task directories) and TicketStore
    (ticket files) so both apply the identical rule to ids that flow between
    them (a ticket id is typically derived from a task id).
    """
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", value)
    safe = safe.strip("_") or "task"
    return safe


# ── Shared round-file accounting ─────────────────────────────────────────────
# Shared by OuterLoop (resume) and AutoController (BLOCKED-reset) so both
# agree on what "already exhausted its rounds" means.
_FEEDBACK_ROUND_RE = re.compile(r"feedback_round_(\d+)\.md$")


def highest_completed_round(task_dir: "str | Path") -> int:
    """Return the highest ``feedback_round_<N>.md`` round number under *task_dir*.

    Returns 0 if the directory doesn't exist or contains no feedback files
    (i.e. the task has never failed a round yet).
    """
    best = 0
    for p in Path(task_dir).glob("feedback_round_*.md"):
        m = _FEEDBACK_ROUND_RE.search(p.name)
        if m:
            best = max(best, int(m.group(1)))
    return best


def file_set_fingerprint(base_dir: "str | Path", files: "list[str]") -> str:
    """Short, content-aware hash of a set of files (relative path + size + mtime).

    Any change to the file SET (added/removed/renamed) or to a file's
    CONTENT (which changes its size and/or mtime) yields a different
    fingerprint. Used anywhere a cache needs to answer "have these files
    actually changed?" — a plain list-of-paths hash answers a different,
    weaker question (did the *set* of paths change) and silently goes stale
    the moment a file already in the set is edited in place.

    Missing/unreadable files hash as a sentinel rather than raising, so a
    file that was deleted (or briefly inaccessible) still changes the
    fingerprint instead of crashing the caller.
    """
    base = Path(base_dir)
    h = hashlib.sha1()
    for rel in sorted(files):
        h.update(rel.encode("utf-8", "replace"))
        try:
            st = (base / rel).stat()
            h.update(f"|{st.st_size}|{st.st_mtime_ns}|".encode("utf-8"))
        except OSError:
            h.update(b"|?|")
    return h.hexdigest()[:12]


def atomic_write_text(path: "str | Path", content: str) -> None:
    """Write *content* to *path* atomically (temp file + ``os.replace``).

    ``os.replace`` is a single filesystem rename, which POSIX and Windows both
    guarantee is atomic within the same directory/filesystem: *path* always
    ends up holding either the old complete content or the new complete
    content, never a partial write. Plain ``Path.write_text`` gives no such
    guarantee — a process killed mid-write (SIGKILL, OOM-kill, power loss)
    can leave a truncated file behind that later fails ``json.loads``.

    Used for state that must survive an interrupted run (plan.json,
    progress.json, tickets) so a mid-write kill can never corrupt it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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

# ── Input-side token budgeting: chars-per-token varies by script ────────────

# English/Latin prose in llama3.1/qwen tokenizers runs close to ~4 chars per
# token; this is the default, conservative-for-English estimate used
# throughout the project's *input* budget math (context_assembler,
# summary_memory, canon_validator).
_CHARS_PER_TOKEN_DEFAULT = 4.0

# Cyrillic tokenizes far more densely in these tokenizers — commonly
# ~1.5-2.5 chars/token rather than ~4, since many Cyrillic characters cost
# their own BPE token(s). Using the Latin ratio for Cyrillic text
# *under*-counts real token usage, so a context assembler sized with the
# default budget silently overflows num_ctx and Ollama truncates the start
# of the prompt (system prompt / story bible) — the root cause of a class of
# continuity errors that Gate-2 then catches only as a downstream symptom.
_CHARS_PER_TOKEN_CYRILLIC = 2.2

# Fraction of alphabetic characters that must be Cyrillic before we switch
# to the denser ratio. Mirrors the confidence threshold used by
# detect_language() below, kept as a separate constant since the two
# functions answer different questions (script ratio vs. dominant language).
_CYRILLIC_RATIO_THRESHOLD = 0.30


def chars_per_token(text: str) -> float:
    """Estimate characters-per-token for *text*, for sizing input budgets.

    Returns ``_CHARS_PER_TOKEN_CYRILLIC`` (2.2) when at least 30% of the
    text's alphabetic characters are Cyrillic, else the Latin/English-safe
    default of 4.0. Empty or non-alphabetic text falls back to the default.

    This is the input-side counterpart to the project's Cyrillic-aware
    output truncation (``max_tokens_creative``) — without it, budgets
    computed as ``tokens * 4`` let ~2x too much Cyrillic text into a prompt,
    silently overflowing the model's context window.
    """
    if not text:
        return _CHARS_PER_TOKEN_DEFAULT
    cyrillic = sum(1 for ch in text if 0x0400 <= ord(ch) <= 0x04FF)
    total_alpha = sum(1 for ch in text if ch.isalpha())
    if total_alpha == 0:
        return _CHARS_PER_TOKEN_DEFAULT
    if (cyrillic / total_alpha) >= _CYRILLIC_RATIO_THRESHOLD:
        return _CHARS_PER_TOKEN_CYRILLIC
    return _CHARS_PER_TOKEN_DEFAULT


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


def human_duration(seconds: float) -> str:
    """Format *seconds* as a compact human-readable duration.

    Examples: 0.4 -> '0.4s', 42 -> '42s', 125 -> '2m 5s', 3725 -> '1h 2m 5s',
    90061 -> '1d 1h 1m 1s'. Negative values get a leading '-'.
    """
    sign = "-" if seconds < 0 else ""
    s = abs(float(seconds))
    if s < 1:
        return f"{sign}{s:.1f}s"
    s = int(s)
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            parts.append(f"{s // size}{unit}")
            s %= size
    if s or not parts:
        parts.append(f"{s}s")
    return sign + " ".join(parts)
