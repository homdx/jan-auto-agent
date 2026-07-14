"""tools/collect/bughunt_filter.py — COLLECT-22: bughunt-suppression by
static facts (the payoff point of the whole antihallucination chain).

Before a bughunt candidate is surfaced to the person, this module asks the
loader (COLLECT-21) whether the candidate's location is already known to
be safe — guarded, fail-open, or covered by a contract. A candidate that
contradicts a **static** fact is auto-suppressed (or downgraded); a
candidate that only contradicts an LLM's prose (a module's `purpose`/
`notes`) is *not* auto-suppressed, because prose was never consulted in
the first place — `CollectModel.is_safe` only ever reads
`guarded_accesses`/`FAIL_OPEN_REGISTRY`/`CONTRACTS` (COLLECT-11 /
COLLECT-21), never an `LLMSummary`. So "only real facts suppress" holds
by construction here exactly the same way COLLECT-1's provenance
isolation holds by construction: there is no code path in this module
that could look at `ModuleRecord.summary` even if it wanted to.

This is the last of the four antihallucination checkpoints named in
`COLLECT_JIRA_BREAKDOWN.md`'s dependency map (A -> B -> E/COLLECT-17 ->
G/COLLECT-22): even if Pass B's summarizer invented something, that
invention never reaches this filter's suppression decision.

Every suppression (and every non-suppression) is logged with its reason,
per COLLECT-22's AC — a caller that wants an audit trail of *why* a
candidate disappeared doesn't have to reconstruct it from the return
value alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from tools.collect.loader import CollectModel, load
from tools.collect.registries import SafetyAnswer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BughuntCandidate:
    """One bug candidate as bughunt (or a human) would state it, before
    any suppression logic runs.

    `location` must match the `"path/to/module.py:line"` shape
    `GuardedAccess.location`/`ExceptSite.location` use — the same
    convention COLLECT-11's `AlreadySafeIndex.query` already expects.
    `access` disambiguates a line with more than one indexed access (see
    `AlreadySafeIndex.query`'s own docstring); leave it `None` when the
    candidate doesn't name one specific expression.
    `claim` is free text describing what the candidate alleges (e.g.
    `"stack[-1] will raise IndexError on an empty stack"`) — carried
    through only for logging/reporting, never consulted by the
    suppression decision itself.
    """

    location: str
    claim: str = ""
    access: Optional[str] = None


@dataclass(frozen=True)
class SuppressionVerdict:
    """The filter's answer for one candidate: whether it was suppressed,
    why, and the concrete static evidence backing that reason (when
    there is any)."""

    candidate: BughuntCandidate
    suppressed: bool
    reason: str  # "guarded" | "fail_open" | "contract" | "unguarded" | "unknown" | "ambiguous_location"
    detail: Optional[str] = None
    provenance: str = ""


def _log_verdict(verdict: SuppressionVerdict) -> None:
    candidate = verdict.candidate
    if verdict.suppressed:
        logger.info(
            "bughunt-suppress: %s suppressed at %s (reason=%s%s) — claim: %r",
            candidate.location,
            candidate.location,
            verdict.reason,
            f", {verdict.detail}" if verdict.detail else "",
            candidate.claim,
        )
    else:
        logger.info(
            "bughunt-suppress: %s NOT suppressed (reason=%s%s) — claim: %r",
            candidate.location,
            verdict.reason,
            f", {verdict.detail}" if verdict.detail else "",
            candidate.claim,
        )


def suppress(
    candidates: Sequence[BughuntCandidate],
    model: CollectModel,
    *,
    root: Optional[Path] = None,
) -> List[SuppressionVerdict]:
    """Run every candidate through `model.is_safe()` and return one
    `SuppressionVerdict` per candidate, in the same order given.

    A candidate is suppressed exactly when `is_safe()` answers `safe=True`
    — i.e. `reason` is `"guarded"`, `"fail_open"`, or `"contract"`. Every
    other reason (`"unguarded"`, `"unknown"`, `"ambiguous_location"`)
    leaves the candidate un-suppressed, deliberately failing closed: an
    ambiguous or unrecognised location is not evidence of safety, so it
    must still be shown, not silently dropped.

    On an absent/unavailable `model` (COLLECT-21's "no model" case),
    `is_safe()` itself answers `unknown` for everything, so this is a
    no-op pass-through — no candidate is ever suppressed when there is no
    collect data to suppress it with.
    """
    verdicts: List[SuppressionVerdict] = []
    for candidate in candidates:
        answer: SafetyAnswer = model.is_safe(candidate.location, access=candidate.access, root=root)
        verdict = SuppressionVerdict(
            candidate=candidate,
            suppressed=answer.safe,
            reason=answer.reason,
            detail=answer.detail,
            provenance=answer.provenance,
        )
        _log_verdict(verdict)
        verdicts.append(verdict)
    return verdicts


def surviving_candidates(verdicts: Sequence[SuppressionVerdict]) -> List[BughuntCandidate]:
    """The convenience view a bughunt caller actually wants: candidates
    that were *not* suppressed, in the same relative order."""
    return [v.candidate for v in verdicts if not v.suppressed]


def suppress_for_root(
    root: Path,
    candidates: Sequence[BughuntCandidate],
    *,
    config=None,
    config_path: Optional[str] = None,
) -> List[SuppressionVerdict]:
    """Convenience one-shot: load the collect model for `root` (COLLECT-21)
    and immediately filter `candidates` against it. Prefer calling
    `loader.load` once and reusing the resulting `CollectModel` across many
    bughunt runs instead of this, when the caller already has one on hand
    — this exists for callers (a future `/bughunt` integration, an
    ad-hoc script) that don't."""
    model = load(root, config=config, config_path=config_path)
    return suppress(candidates, model, root=root)
