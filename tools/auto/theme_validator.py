"""Theme/content gate for creative mode (podrugi-3 sim).

Every other creative gate checks CONSISTENCY — Gate-2 catches repetition and
truncation, the continuity gate catches contradictions with the story bible,
the fact gate checks the task's stated facts, the language pre-gate catches
script drift. None of them ever asks whether the chapter's CONTENT crosses a
line the author set for the whole story: a chapter can be perfectly
consistent, well-written, in the right language — and still, say, glamorize
an addiction the story is supposed to treat as a cost. This validator closes
that gap.

Design mirrors :mod:`tools.auto.continuity_validator` deliberately:
bounded revisions, fail-open on every error path, one LLM call per check,
verdict protocol ``APPROVED`` / ``REVISE: <instruction>``.

Configuration (all under ``[validator_agent]``):

``theme_check_creative``
    Boolean master switch, default ``false`` (opt-in: theme guidelines are
    story-specific, there is no sensible universal default).
``theme_guidelines``
    Free-text guidelines the chapter must respect, e.g.::

        theme_guidelines = Рассказ не должен романтизировать курение или
            подавать его как эффективный способ контроля веса; вред и
            эскалация зависимости должны показываться как цена, а не бонус.

``max_theme_revisions``
    Cap on theme-driven revisions per chapter, default ``2``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


_THEME_SYSTEM = (
    "You are a content editor enforcing thematic guidelines for a story. "
    "You get GUIDELINES (what the story must not do) and a NEW chapter. "
    "Judge ONLY against the guidelines — do not critique style, plot, or "
    "consistency. Reply ONE line. APPROVED if the chapter respects the "
    "guidelines. Otherwise reply exactly 'REVISE: <one concrete instruction "
    "describing what violates the guidelines and how to fix it>'."
)


@dataclass
class ThemeVerdict:
    """Outcome of one theme check (same shape as ContinuityVerdict)."""

    approved: bool
    reason: str = ""
    unparseable: bool = False

    def feedback(self) -> str:
        return self.reason or ("approved" if self.approved else "revise")


class ThemeValidator:
    """Bounded, fail-open theme/content checker for creative mode."""

    def __init__(
        self,
        llm_call,
        guidelines: str,
        *,
        max_theme_revisions: int = 2,
    ) -> None:
        self._llm = llm_call
        self.guidelines = (guidelines or "").strip()
        self.max_theme_revisions = max(0, int(max_theme_revisions))

    def check(self, new_text: str) -> ThemeVerdict:
        """Check *new_text* against the configured guidelines. Never raises."""
        if not self.guidelines:
            return ThemeVerdict(approved=True, reason="no guidelines configured")

        # Imported here to avoid circular import; inner_loop imports us.
        from tools.auto.inner_loop import _parse_verdict_soft  # noqa: PLC0415
        from tools.auto.utils import detect_language, language_instruction  # noqa: PLC0415

        lang_instr = language_instruction(detect_language(new_text))
        system = _THEME_SYSTEM + (("\n" + lang_instr) if lang_instr else "")
        if lang_instr:
            # Same carve-out as the continuity validator: the language lock
            # must never swallow the English verdict token itself.
            system += (
                "\nEXCEPTION TO THE LANGUAGE RULE ABOVE: the verdict word "
                "itself — APPROVED or REVISE — must always be written in "
                "English exactly as shown. Only the instruction after "
                "'REVISE:' should be in the story's language."
            )

        user_msg = f"GUIDELINES:\n{self.guidelines}\n\nNEW CHAPTER:\n{new_text}"

        try:
            raw = self._llm(system, user_msg) or ""
        except Exception as exc:  # noqa: BLE001 — fail-open by design
            logger.warning("ThemeValidator: LLM call failed — %s", exc)
            return ThemeVerdict(approved=True, reason="llm error — passed on fail-open")

        approved, reason, unparseable = _parse_verdict_soft(raw)
        if unparseable:
            logger.warning(
                "ThemeValidator: unparseable reply %r — passed on fail-open.", raw[:120]
            )
        return ThemeVerdict(approved=approved, reason=reason, unparseable=unparseable)


def make_theme_validator(config) -> "ThemeValidator | None":
    """Build a :class:`ThemeValidator` from *config*, or ``None`` when disabled.

    Same one-argument-factory shape as
    :func:`tools.auto.continuity_validator.make_continuity_validator`.
    """
    enabled = config.getboolean(
        "validator_agent", "theme_check_creative", fallback=False
    )
    if not enabled:
        logger.debug("ThemeValidator: disabled (theme_check_creative not set).")
        return None

    guidelines = config.get("validator_agent", "theme_guidelines", fallback="").strip()
    if not guidelines:
        logger.warning(
            "ThemeValidator: theme_check_creative=true but theme_guidelines is "
            "empty — gate disabled (nothing to enforce)."
        )
        return None

    max_rev = config.getint("validator_agent", "max_theme_revisions", fallback=2)

    try:
        from tools.auto.summary_memory import _make_llm_call  # noqa: PLC0415
        llm_call = _make_llm_call(config, task_mode="creative")
    except Exception as exc:  # noqa: BLE001 — never block the loop on setup
        logger.warning("make_theme_validator: could not build LLM call — %s", exc)
        return None

    logger.info("ThemeValidator: enabled (max_theme_revisions=%d).", max_rev)
    return ThemeValidator(llm_call, guidelines, max_theme_revisions=max_rev)
