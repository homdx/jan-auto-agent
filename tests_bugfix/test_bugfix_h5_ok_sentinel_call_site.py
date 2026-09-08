"""H5 — the OK-sentinel guard, exercised through the verifier loop.

The unit contract of :func:`tools.auto.summary_memory._is_ok_sentinel` is
already pinned by :mod:`tests_bugfix.test_bugfix_fix2_9_ok_sentinel` (FIX-2
 #9). What had no test was the *call site*: that
``SummaryFidelityVerifier.verify_and_fix`` actually routes its early exit
through that helper, so a reply the helper rejects still reaches the
correction path instead of ending the loop.

That is the whole consequence of the bug. ``OKAY`` is an interjection, not
an assertion that the summary is faithful; accepting it as the sentinel
stopped the loop before the correction the model was about to send was ever
read, and the wrong summary was kept.

One case needs a different instrument. When the reply is a *bare* ``OKAY``
with no corrections attached, approval and non-approval produce the same
visible result — the loop ends and the summary is kept either way — so no
assertion on the return value can tell which branch ran. Only the log
distinguishes them: the sentinel branch emits "OK on round N" at DEBUG,
while a reply that is neither the sentinel nor a usable bullet list falls
through to the "reply unusable" warning. The last two tests here assert on
that distinction.
"""

from __future__ import annotations

import logging

from tools.auto.summary_memory import SummaryFidelityVerifier

_SOURCE = "Anna is thirty years old and lives in Prague."
_SUMMARY = "- Anna is 25 years old.\n- Anna lives in Prague."
_CORRECTION = "- Anna is 30 years old.\n- Anna lives in Prague."
#: _clean_bullet_list normalises every marker to "•", so the applied
#: correction comes back re-bulleted rather than byte-identical.
_CORRECTED = "• Anna is 30 years old.\n• Anna lives in Prague."


def _verifier(replies: list[str]) -> tuple[SummaryFidelityVerifier, list[str]]:
    """A verifier whose LLM returns *replies* in order, recording each call."""
    seen: list[str] = []

    def llm(system: str, user: str) -> str:
        seen.append(user)
        return replies[len(seen) - 1] if len(seen) <= len(replies) else "OK"

    return SummaryFidelityVerifier(llm, max_fidelity_rounds=3), seen


def _took_the_sentinel_branch(records) -> bool:
    """True when the loop exited through the ``_is_ok_sentinel`` branch.

    That branch is the only one that logs "OK on round N"; every other exit
    (unusable reply, no-change, LLM error, round cap) logs something else or
    nothing. Asserting on the message is therefore the only way to see which
    path a reply took when the returned summary is identical either way.
    """
    return any(
        record.levelno == logging.DEBUG and "OK on round" in record.getMessage()
        for record in records
    )


def test_bare_ok_ends_the_loop_after_one_round() -> None:
    """The sentinel still works — the guard must not have been tightened
    into never matching."""
    verifier, seen = _verifier(["OK"])
    assert verifier.verify_and_fix(_SOURCE, _SUMMARY) == _SUMMARY
    assert len(seen) == 1


def test_okay_is_not_an_approval_and_its_correction_is_applied() -> None:
    """The bug: "OKAY" satisfied both clauses of the old
    ``startswith("OK") and len(reply) <= 4`` guard, so the loop broke and
    the correction that followed was never applied."""
    verifier, _ = _verifier([f"OKAY\n{_CORRECTION}", "OK"])
    assert verifier.verify_and_fix(_SOURCE, _SUMMARY) == _CORRECTED


def test_ok_prefixed_reply_with_corrections_is_not_an_approval() -> None:
    """A reply opening with "OK" on its own line and then listing real
    corrections must be read as corrections. The candidate fixes that
    matched on the first line alone would have discarded these."""
    verifier, _ = _verifier([f"OK\n{_CORRECTION}", "OK"])
    assert verifier.verify_and_fix(_SOURCE, _SUMMARY) == _CORRECTED


def test_unusable_non_sentinel_reply_keeps_the_summary() -> None:
    """Fail-open: a reply that is neither the sentinel nor a usable bullet
    list ends the loop with the summary untouched, so being stricter about
    the sentinel cannot corrupt anything — it costs one warning."""
    verifier, seen = _verifier(["OK.", "OK"])
    assert verifier.verify_and_fix(_SOURCE, _SUMMARY) == _SUMMARY
    assert len(seen) == 1


# ── the bare-OKAY case: same result, different branch ────────────────────────

def test_bare_okay_does_not_take_the_sentinel_branch(caplog) -> None:
    """The reported defect, in the shape that leaves no other trace.

    ``OKAY`` satisfied both halves of the old guard — it starts with "OK" and
    is exactly four characters — so the loop exited as if the verifier had
    confirmed the summary. It returns the same summary either way, so this is
    the one case the return value cannot expose.
    """
    verifier, seen = _verifier(["OKAY"])
    with caplog.at_level(logging.DEBUG):
        out = verifier.verify_and_fix(_SOURCE, _SUMMARY)
    assert out == _SUMMARY
    assert len(seen) == 1
    assert not _took_the_sentinel_branch(caplog.records)


def test_bare_okay_falls_through_to_the_unusable_reply_path(caplog) -> None:
    """Where it goes instead: ``OKAY`` is not a bullet list either, so the
    loop ends on the unusable-reply warning — fail-open, and logged, rather
    than silently recorded as an approval that never happened."""
    verifier, _ = _verifier(["OKAY"])
    with caplog.at_level(logging.DEBUG):
        verifier.verify_and_fix(_SOURCE, _SUMMARY)
    assert not _took_the_sentinel_branch(caplog.records)
    assert any("unusable" in record.getMessage() for record in caplog.records)


def test_bare_ok_does_take_the_sentinel_branch(caplog) -> None:
    """The control: without this, the two tests above would pass even if the
    sentinel branch had been removed altogether."""
    verifier, _ = _verifier(["OK"])
    with caplog.at_level(logging.DEBUG):
        verifier.verify_and_fix(_SOURCE, _SUMMARY)
    assert _took_the_sentinel_branch(caplog.records)
