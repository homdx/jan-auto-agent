"""tools/auto/continuity_validator.py — AUTO-CR-23-3: continuity gate.

Design
------
AUTO-CR-23-1 gave us a persisted **story bible** (durable, must-not-contradict
facts) and AUTO-CR-23-2 made sure it is always injected into the coder's
prompt. That covers the model *knowing* the facts up front. This module is
the catch-net for when it still gets something wrong anyway: a check, run
after Gate-2 approves, that compares the freshly generated chapter against
(bible + previous chapter) and — on a genuine contradiction — returns an
**actionable** edit instruction the coder can act on directly
("replace the hero's green jacket with a grey jacket"), not a vague
diagnosis ("jacket wrong").

This mirrors :mod:`tools.auto.fact_validator` (AUTO-CR-20) exactly:

* **Narrow** — only direct contradictions of an established fact (attribute,
  clothing, location, relationship, age) are flagged. New events, new scenes,
  and added detail are explicitly *not* flagged — fiction is allowed to grow.
* **Fail-open** — any LLM error or unparseable reply is treated as APPROVED
  so a rambling 8B reply can never hard-block the pipeline.
* **Bounded** — the caller (InnerLoop) grants at most
  ``max_continuity_revisions`` continuity-driven rejections per chapter, then
  accepts with a warning.
* **Creative-only** — the factory returns ``None`` when the feature is
  disabled.

Public surface::

    from tools.auto.continuity_validator import (
        ContinuityValidator, ContinuityVerdict, make_continuity_validator,
        find_previous_chapter_text, read_story_bible,
    )
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# llm_call(system, user) -> str   (same callable contract used everywhere)
LlmCall = Callable[[str, str], str]

# Matches "chapter_07", "chapter_7", "Chapter_07.md", etc. — mirrors
# context_assembler.py / canon_validator.py so "chapter index" means the
# same thing everywhere in the creative pipeline.
_CHAPTER_RE = re.compile(r"chapter[_\-\s]?(\d+)", re.IGNORECASE)

_DEFAULT_BIBLE_PATH = "story_bible.md"

# ── Continuity gate system prompt ──────────────────────────────────────────

_CONTINUITY_SYSTEM = (
    "You are a continuity checker. You get KNOWN FACTS (a story bible and the "
    "previous chapter) and a NEW chapter. Reply ONE line. "
    "APPROVED if the new chapter contradicts nothing in KNOWN FACTS. "
    "Otherwise REVISE: followed by a CONCRETE edit instruction that names the "
    "exact change to make, e.g. 'replace the hero's green jacket with a grey "
    "jacket' or 'change Aisha from second grade to first grade'. "
    "Flag ONLY direct contradictions of an established fact (attribute, "
    "clothing, location, relationship, age). Do NOT flag new events, new "
    "scenes, or added detail. "
    "A character name that appears in the NEW chapter but NOT in KNOWN FACTS "
    "is a NEW character — do NOT treat it as a contradiction or rename it to "
    "an existing character. Only flag a name if the SAME scene/role is "
    "explicitly attributed to a different name in KNOWN FACTS. "
    "Check gender/pronoun consistency: if KNOWN FACTS establish a character as "
    "female (она / Ей / -ла endings) and the NEW chapter uses male forms (он / "
    "стоял / спросил), or vice-versa, that is a contradiction — REVISE with the "
    "concrete fix, e.g. 'change the captain from он/стоял to она/стояла'. "
    "No JSON, no preamble."
)


# ── Result object ─────────────────────────────────────────────────────────────

@dataclass
class ContinuityVerdict:
    """Outcome of one continuity check.

    Attributes
    ----------
    approved:
        True when the new chapter is consistent with the known facts (or the
        check failed open due to an error).
    reason:
        Non-empty string describing the contradiction when ``approved`` is
        False (the LLM's verbatim REVISE instruction); an explanatory note
        when ``unparseable`` is True.
    unparseable:
        True when the LLM reply contained no recognised verdict token.  The
        verdict is treated as approved (fail-open); callers may log a
        warning.
    """

    approved: bool
    reason: str
    unparseable: bool = False

    def feedback(self) -> str:
        """Render as coder-facing feedback.

        Returns the concrete edit instruction **verbatim** — e.g. "replace
        the hero's green jacket with a grey jacket" — so the coder gets an
        actionable instruction, not a vague diagnosis like "jacket wrong".
        Empty string when approved.
        """
        if self.approved:
            return ""
        return self.reason


# ── Validator ─────────────────────────────────────────────────────────────────

class ContinuityValidator:
    """Bounded, fail-open continuity checker for creative mode (AUTO-CR-23-3).

    Parameters
    ----------
    llm_call:
        ``llm_call(system, user) -> str`` — same contract as the rest of the
        pipeline.  A stub is sufficient for unit tests.
    max_continuity_revisions:
        Advertised cap on continuity-driven revisions per chapter.  Enforced
        by InnerLoop; exposed here so the loop reads it from one place.
    """

    def __init__(
        self,
        llm_call: LlmCall,
        *,
        max_continuity_revisions: int = 1,
    ) -> None:
        self._llm = llm_call
        self.max_continuity_revisions = max(0, int(max_continuity_revisions))

    # ── Main entry ────────────────────────────────────────────────────────────

    def check(self, known_facts: str, new_text: str) -> ContinuityVerdict:
        """Check *new_text* against *known_facts*.  Never raises.

        Parameters
        ----------
        known_facts:
            The story bible plus the previous chapter (or whatever subset of
            those is available — either may be empty).
        new_text:
            The freshly generated chapter text to check.

        Returns
        -------
        ContinuityVerdict
            ``approved=True`` on consistency or any failure (fail-open).
            ``approved=False`` only when the LLM explicitly returns
            ``REVISE``.
        """
        # Import here to avoid a circular import; inner_loop imports us.
        from tools.auto.inner_loop import _parse_verdict_soft  # noqa: PLC0415

        from tools.auto.utils import detect_language, language_instruction  # noqa: PLC0415

        lang_instr = language_instruction(detect_language(new_text))
        system = _CONTINUITY_SYSTEM + (("\n" + lang_instr) if lang_instr else "")
        if lang_instr:
            # The LANGUAGE lock above tells the model not to translate
            # anything — which, taken literally, also swallows the verdict
            # token itself (observed in the wild: a Russian chapter produced
            # "НЕОБХОДИМО ИЗМЕНИТЬ: ..." instead of "REVISE: ...", which
            # _parse_verdict_soft cannot recognise, so it failed open and a
            # real contradiction slipped through uncaught). Carve the verdict
            # word out of the language lock explicitly, stated last so it
            # isn't overridden by the broader "output {language} only" rule.
            system += (
                "\nEXCEPTION TO THE LANGUAGE RULE ABOVE: the verdict word "
                "itself — APPROVED or REVISE — must always be written in "
                "English exactly as shown, never translated or transliterated. "
                "Only the edit instruction that follows 'REVISE:' should be in "
                "the story's language."
            )

        user_msg = f"KNOWN FACTS:\n{known_facts}\n\nNEW CHAPTER:\n{new_text}"

        try:
            raw = self._llm(system, user_msg) or ""
        except Exception as exc:  # noqa: BLE001 — fail-open by design
            logger.warning("ContinuityValidator: LLM call failed — %s", exc)
            return ContinuityVerdict(
                approved=True, reason="llm error — passed on fail-open", unparseable=False,
            )

        approved, reason, unparseable = _parse_verdict_soft(raw)

        if unparseable:
            logger.warning(
                "ContinuityValidator: unparseable reply %r — passed on fail-open.", raw[:120]
            )

        if not approved:
            logger.info("ContinuityValidator: REVISE — %s", reason)

        return ContinuityVerdict(approved=approved, reason=reason, unparseable=unparseable)


# ── Helpers: bible + previous-chapter lookup (used by the inner-loop wiring) ──

def read_story_bible(base_dir: "str | Path", path: str = _DEFAULT_BIBLE_PATH) -> str:
    """Return the contents of ``story_bible.md`` under *base_dir*, or ``""``.

    Never raises; missing/unreadable file degrades to an empty string, which
    the continuity check tolerates (known facts may legitimately be empty).
    """
    try:
        return (Path(base_dir) / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _chapter_index(filename: str) -> "int | None":
    """Return the integer chapter number from a filename, or ``None``."""
    m = _CHAPTER_RE.search(Path(filename).name)
    return int(m.group(1)) if m else None


def find_previous_chapter_text(chapter_file: str, base_dir: "str | Path") -> str:
    """Return the full text of the highest-numbered chapter below *chapter_file*.

    Scans *base_dir* for ``.md``/``.txt`` files matching the ``chapter_<N>``
    naming pattern, picks the one with the largest index strictly less than
    *chapter_file*'s, and reads it. Returns ``""`` (never raises) when:

    * *chapter_file* isn't a recognisable ``chapter_<N>`` name,
    * no earlier chapter file is found, or
    * the chosen file can't be read.
    """
    base = Path(base_dir)
    idx = _chapter_index(chapter_file)
    if idx is None:
        return ""

    best_idx: "int | None" = None
    best_name: "str | None" = None
    try:
        candidates = list(base.glob("*"))
    except OSError:
        return ""

    target_name = Path(chapter_file).name
    for p in candidates:
        if p.suffix.lower() not in (".md", ".txt"):
            continue
        if p.name == target_name:
            continue
        cidx = _chapter_index(p.name)
        if cidx is None or cidx >= idx:
            continue
        if best_idx is None or cidx > best_idx:
            best_idx = cidx
            best_name = p.name

    if best_name is None:
        return ""
    try:
        return (base / best_name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ── Factory ───────────────────────────────────────────────────────────────────

def make_continuity_validator(config) -> "ContinuityValidator | None":
    """Build a :class:`ContinuityValidator` from *config*, or ``None`` when
    disabled.

    Reads ``[validator_agent] continuity_check_creative`` (boolean, default
    ``false``) and ``max_continuity_revisions`` (int, default ``1``).

    The LLM callable is built straight from *config* (same approach as
    :func:`tools.auto.canon_validator.make_canon_validator`), so this stays a
    one-argument factory — the caller doesn't need to thread API settings
    through separately.

    Returns ``None`` when the feature flag is off so the wiring in
    :func:`tools.auto.inner_loop.make_inner_loop` can remain a no-op with a
    simple ``if continuity_validator is not None`` guard.
    """
    enabled = config.getboolean("validator_agent", "continuity_check_creative", fallback=False)
    if not enabled:
        logger.debug("ContinuityValidator: disabled (continuity_check_creative not set).")
        return None

    max_rev = config.getint("validator_agent", "max_continuity_revisions", fallback=1)

    try:
        from tools.auto.summary_memory import _make_llm_call  # noqa: PLC0415
        llm_call = _make_llm_call(config, task_mode="creative")
    except Exception as exc:  # noqa: BLE001 — never block the loop on setup
        logger.warning("make_continuity_validator: could not build LLM call — %s", exc)
        return None

    logger.info(
        "ContinuityValidator: enabled (max_continuity_revisions=%d).", max_rev
    )
    return ContinuityValidator(llm_call, max_continuity_revisions=max_rev)
