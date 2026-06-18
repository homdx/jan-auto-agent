"""tools/auto/fact_validator.py — AUTO-CR-20-1: Gate-3 task-level fact checker.

Design
------
Gate-2 (LLMGate2Validator) checks coherence, continuity, and language but does
**not** verify the generated text against the task's required facts.  This
module fills that hole with a narrow, single-question check:

    Does the generated text CONTRADICT any explicit fact stated in the task?

The check is:

* **Narrow** — contradictions only; missing detail, style, and omissions are
  explicitly *not* flagged (to avoid the fidelity-verifier infinite-REVISE trap).
* **Fail-open** — any LLM error or unparseable reply is treated as APPROVED so
  a rambling 8B reply can never hard-block the pipeline.
* **Bounded** — the caller (InnerLoop) grants at most ``max_fact_revisions``
  Gate-3-driven rejections per task, then accepts with a warning.
* **Creative-only** — the factory returns ``None`` when the feature is disabled
  or the task mode is not ``"creative"``.

Public surface::

    from tools.auto.fact_validator import FactValidator, FactVerdict, make_fact_validator
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

# llm_call(system, user) -> str   (same callable contract used everywhere)
LlmCall = Callable[[str, str], str]

# ── Gate-3 system prompt ──────────────────────────────────────────────────────

_GATE3_SYSTEM_FACTS = (
    "You are a fact-compliance checker. You are given a TASK (which states "
    "required facts) and TEXT produced for it. Reply ONE line. "
    "First token APPROVED or REVISE. "
    "Reply REVISE **only** when the TEXT states something that directly "
    "CONTRADICTS an explicit fact in the TASK "
    "(e.g. TASK says 'does not work' but TEXT says 'works as a teacher'; "
    "TASK says 'first grade' but TEXT says 'second grade'). "
    "Do NOT REVISE for missing detail, style, or rhythm. "
    "If nothing contradicts, reply APPROVED. "
    "If REVISE, name the contradicted fact. "
    "No JSON, no preamble."
)


# ── Result object ─────────────────────────────────────────────────────────────

@dataclass
class FactVerdict:
    """Outcome of one Gate-3 fact check.

    Attributes
    ----------
    approved:
        True when the text is consistent with the task's stated facts (or the
        check failed open due to an error).
    reason:
        Non-empty string describing the contradiction when ``approved`` is
        False; an explanatory note when ``unparseable`` is True.
    unparseable:
        True when the LLM reply contained no recognised verdict token.  The
        verdict is treated as approved (fail-open); callers may log a warning.
    """

    approved: bool
    reason: str
    unparseable: bool

    def feedback(self) -> str:
        """Render as Gate-2-style prescriptive feedback for the coder.

        The returned string can be appended to the existing feedback list so
        the coder receives it on the next attempt, exactly as canon feedback is
        delivered.
        """
        if self.approved:
            return ""
        return (
            "FACT CONFLICT — your text contradicts an explicit fact in the task. "
            f"Revise so the text matches: {self.reason}"
        )


# ── Validator ─────────────────────────────────────────────────────────────────

class FactValidator:
    """Bounded, fail-open Gate-3 fact checker for creative mode (AUTO-CR-20).

    Parameters
    ----------
    llm_call:
        ``llm_call(system, user) -> str`` — same contract as the rest of the
        pipeline.  A stub is sufficient for unit tests.
    max_fact_revisions:
        Advertised cap on fact-driven revisions per task.  Enforced by
        InnerLoop; exposed here so the loop reads it from one place.
    """

    def __init__(
        self,
        llm_call: LlmCall,
        *,
        max_fact_revisions: int = 1,
    ) -> None:
        self._llm = llm_call
        self.max_fact_revisions = max(0, int(max_fact_revisions))

    # ── Main entry ────────────────────────────────────────────────────────────

    def check(self, task: dict, text: str) -> FactVerdict:
        """Check *text* against the facts stated in *task*.  Never raises.

        Parameters
        ----------
        task:
            The task dict as used throughout the pipeline.  The check reads
            ``task["instruction"]`` and, if present, ``task.get("goal", "")``.
        text:
            The generated chapter/section text produced by the coder.

        Returns
        -------
        FactVerdict
            ``approved=True`` on consistency or any failure (fail-open).
            ``approved=False`` only when the LLM explicitly returns ``REVISE``.
        """
        # Import here to avoid a circular import; inner_loop imports us.
        from tools.auto.inner_loop import _parse_verdict_soft  # noqa: PLC0415

        instruction = (task.get("instruction") or "").strip()
        goal = (task.get("goal") or "").strip()

        task_block = instruction
        if goal:
            task_block = f"GOAL:\n{goal}\n\nINSTRUCTION:\n{instruction}"

        user_msg = f"TASK:\n{task_block}\n\nTEXT:\n{text}"

        try:
            raw = self._llm(_GATE3_SYSTEM_FACTS, user_msg) or ""
        except Exception as exc:  # noqa: BLE001 — fail-open by design
            logger.warning("FactValidator: LLM call failed — %s", exc)
            return FactVerdict(approved=True, reason="llm error — passed on fail-open", unparseable=False)

        approved, reason, unparseable = _parse_verdict_soft(raw)

        if unparseable:
            logger.warning(
                "FactValidator: unparseable reply %r — passed on fail-open.", raw[:120]
            )

        if not approved:
            logger.info("FactValidator: REVISE — %s", reason)

        return FactVerdict(approved=approved, reason=reason, unparseable=unparseable)


# ── Factory ───────────────────────────────────────────────────────────────────

def make_fact_validator(
    config,
    *,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    api_format: str = "",
) -> "FactValidator | None":
    """Build a :class:`FactValidator` from *config*, or ``None`` when disabled.

    Reads ``[validator_agent] fact_check_creative`` (boolean, default ``false``)
    and ``max_fact_revisions`` (int, default ``1``).

    The ``base_url``, ``api_key``, ``model``, and ``api_format`` keyword
    arguments are accepted for call-site compatibility with the InnerLoop
    factory signature; the LLM callable is built from *config* (the same way
    :func:`tools.auto.canon_validator.make_canon_validator` works).

    Returns ``None`` when the feature flag is off so the wiring in
    :func:`tools.auto.inner_loop.make_inner_loop` can remain a no-op with a
    simple ``if fact_validator is not None`` guard.
    """
    enabled = config.getboolean("validator_agent", "fact_check_creative", fallback=False)
    if not enabled:
        logger.debug("FactValidator: disabled (fact_check_creative not set).")
        return None

    max_rev = config.getint("validator_agent", "max_fact_revisions", fallback=1)

    try:
        from tools.auto.summary_memory import _make_llm_call  # noqa: PLC0415
        llm_call = _make_llm_call(config, task_mode="creative")
    except Exception as exc:  # noqa: BLE001 — never block the loop on setup
        logger.warning("make_fact_validator: could not build LLM call — %s", exc)
        return None

    logger.info(
        "FactValidator: enabled (max_fact_revisions=%d).", max_rev
    )
    return FactValidator(llm_call, max_fact_revisions=max_rev)
