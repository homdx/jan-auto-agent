"""tools/auto/gate_verdict.py — the one interface every gate's verdict reports through.

Before this module, each gate's verdict had its own ad-hoc shape:

* Gate 1 (:class:`~tools.auto.gate1_filter.FilterResult`) exposed
  ``accepted`` (and had no ``feedback()`` at all).
* Gate 2 (:meth:`~tools.auto.inner_loop.LLMGate2Validator.approve`)
  returned a bare ``(bool, str)`` tuple with no object at all.
* Gate 3's seven validators each defined their own dataclass —
  :class:`~tools.auto.canon_validator.CanonResult` used ``has_conflict``
  (inverted: ``True`` means rejected), while every other verdict
  (:class:`~tools.auto.fact_validator.FactVerdict`,
  :class:`~tools.auto.continuity_validator.ContinuityVerdict`,
  :class:`~tools.auto.delta_validator.DeltaVerdict`,
  :class:`~tools.auto.existence_validator.ExistenceVerdict`,
  :class:`~tools.auto.theme_validator.ThemeVerdict`,
  :class:`~tools.auto.prosody.ProsodyVerdict`) used ``approved``. Six of
  the seven carried a ``feedback()`` method; ``FilterResult`` did not.

:func:`~tools.auto.gate_registry.run_gates` bridged the split with two
per-gate rejection predicates (``_rejected_by_conflict`` for canon,
``_rejected_by_approved`` for the rest) so the shared loop body did not
need to know which shape a given gate's verdict had — but the split was
still there, and a new gate author had to remember which predicate to
wire up and whether their verdict needed a ``has_conflict`` or an
``approved`` field.

This module declares the single contract every gate verdict now honours:

    approved : bool   — True when the gate PASSES (the attempt is accepted).
                       A gate that fails-open on an error returns
                       ``approved=True`` so a broken check never blocks
                       the pipeline.
    feedback() -> str — the coder-facing message. Empty (or a short
                       "accepted" note) when ``approved``; a concrete,
                       actionable description of what to fix when not.

Every verdict object implements this structurally (duck typing — the
Protocol is :func:`runtime_checkable <typing.runtime_checkable>` so it
can be used in ``isinstance`` checks but no verdict inherits from it).
The shared gate runner in :mod:`tools.auto.gate_registry` now reads
``verdict.approved`` and ``verdict.feedback()`` for *every* gate through
this one interface, and the two per-gate rejection predicates have
collapsed into a single ``not verdict.approved``.

What this does NOT change
-------------------------
Behaviour is unchanged. Every verdict keeps its existing fields and
methods (``CanonResult.has_conflict``, ``FilterResult.accepted``,
``Gate2Verdict`` keeps a tuple-unpackable ``approve()`` return for the
many tests that unpack it). The unified interface is *additive*: a new
``approved`` property / method on the two verdicts that did not have one
(CanonResult, FilterResult), and a shared protocol that documents the
contract they all already honoured.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class GateVerdict(Protocol):
    """The one interface every gate's verdict reports through.

    Every gate in the pipeline — Gate 1 (:class:`FilterResult`), Gate 2
    (:class:`Gate2Verdict` / :meth:`LLMGate2Validator.approve_verdict`),
    and every Gate-3 validator (:class:`CanonResult`,
    :class:`FactVerdict`, :class:`ContinuityVerdict`,
    :class:`DeltaVerdict`, :class:`ExistenceVerdict`,
    :class:`ThemeVerdict`, :class:`ProsodyVerdict`) — returns an object
    with these two members.

    Attributes
    ----------
    approved:
        ``True`` when the gate accepts the attempt (the candidate/file/
        chapter passes the check, or the check failed open). ``False``
        when the gate rejects it and wants the coder to try again.
    feedback:
        A coder-facing message string. Empty when ``approved`` is
        ``True``; a concrete, actionable description of the problem
        when ``False``.

    The inverse of ``approved`` is what :func:`~tools.auto.gate_registry.
    run_gates` tests to decide whether a gate rejected an attempt
    (``not verdict.approved``); ``feedback()`` is what the coder sees on
    that rejection.
    """

    approved: bool

    def feedback(self) -> str: ...


def is_gate_verdict(obj: object) -> bool:
    """Return ``True`` if *obj* conforms to the :class:`GateVerdict` protocol.

    A convenience for tests and the shared gate runner — every shipped
    verdict satisfies this, but a stub or a ``None`` (the fail-open
    return from a gate that errored) does not.
    """
    return (
        hasattr(obj, "approved")
        and hasattr(obj, "feedback")
        and callable(getattr(obj, "feedback"))
    )
