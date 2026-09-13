"""tests_bugfix/test_bugfix_verifier_negated_crash_words.py

Regression test for a bug in tools/collect/verifier.py's claim-kind
classification (extract_claims, found in the verified-bugs audit #6).

`_CRASH_WORDS_RE` matches the literal crash word ("crash", "throw", ...)
even when it is NEGATED inside a safety phrase ("stack[-1] will not crash",
"it won't throw", "there is no risk of crash"). The classification checked
`_CRASH_WORDS_RE` against the raw sentence first, so these safety sentences
were classified `kind="access_crash"` and the `access_safe` branch (checked
afterwards) could never run.

That misclassification had two observable consequences, both wrong:

1. A FALSE safety affirmation about a genuinely UNGUARDED access
   ("stack[-1] will not crash ...") was treated as an access_crash claim.
   contradiction_check has no UNGUARDED check for access_crash claims (only
   for access_safe ones), so the false safety claim survived into the
   artifact unchecked — the exact hallucination the access_safe/REASON_CONTRADICTS_CRASH
   machinery exists to drop.
2. A TRUE safety sentence about a genuinely GUARDED access was treated as a
   crash claim and then dropped as "dropped:contradicts-guard" — the true
   sentence was deleted as if it were a fabrication.

The fix: probe the crash keywords on a copy of the sentence with every safe
phrasing removed (`_SAFE_WORDS_RE.sub("", sentence)`), and extended
`_SAFE_WORDS_RE` with all persons/tenses of negation plus "never" so the
negated crash words ("won't crash", "never throws", "doesn't fail") are
recognized as safety language at all. A sentence that contains BOTH a real
crash assertion and safety language still keeps the existing crash-wins
behavior.
"""

from __future__ import annotations

from tools.collect.model import GuardedAccess, ModuleRecord
from tools.collect.verifier import (
    REASON_CONTRADICTS_CRASH,
    Claim,
    contradiction_check,
    extract_claims,
    verify_claims,
)


def _unguarded_module(prompt: str = "pkg/real_bug.py") -> ModuleRecord:
    return ModuleRecord(
        path=prompt,
        guarded_accesses=(
            GuardedAccess(
                location="pkg/real_bug.py:10",
                access="stack[-1]",
                status="UNGUARDED",
            ),
        ),
    )


# ── extraction: negated crash words must classify as access_safe ─────────────


def test_extract_claims_negated_crash_word_is_access_safe():
    module = _unguarded_module()
    claims = extract_claims(
        "stack[-1] will not crash: the caller guarantees a non-empty stack.",
        module.path,
        frozenset(),
    )
    assert len(claims) == 1
    assert claims[0].kind == "access_safe"
    assert claims[0].access == "stack[-1]"


def test_extract_claims_cannot_crash_is_access_safe():
    module = _unguarded_module()
    claims = extract_claims(
        "stack[-1] cannot crash on empty input.",
        module.path,
        frozenset(),
    )
    assert len(claims) == 1
    assert claims[0].kind == "access_safe"


def test_extract_claims_wont_throw_is_access_safe():
    module = _unguarded_module()
    claims = extract_claims(
        "cache[key] won't throw on a miss.",
        module.path,
        frozenset(),
    )
    assert len(claims) == 1
    assert claims[0].kind == "access_safe"
    assert claims[0].access == "cache[key]"


def test_extract_claims_never_fails_is_access_safe():
    module = _unguarded_module()
    claims = extract_claims(
        "item[0] never fails on an empty cache.",
        module.path,
        frozenset(),
    )
    assert len(claims) == 1
    assert claims[0].kind == "access_safe"


def test_extract_claims_no_risk_of_crash_is_access_safe():
    module = _unguarded_module()
    claims = extract_claims(
        "stack[-1] has no risk of crash here.",
        module.path,
        frozenset(),
    )
    assert len(claims) == 1
    assert claims[0].kind == "access_safe"


# ── extraction: genuinely affirmative crash language stays access_crash ──────


def test_extract_claims_affirmative_crash_still_access_crash():
    module = _unguarded_module()
    for sentence in (
        "stack[-1] will crash on an empty stack.",
        "stack[-1] crashes with an IndexError if the stack is empty.",
        "stack[-1] raises IndexError on an empty stack.",
        "stack[-1] is unguarded.",
    ):
        claims = extract_claims(sentence, module.path, frozenset())
        assert len(claims) == 1, sentence
        assert claims[0].kind == "access_crash", sentence
        assert claims[0].access == "stack[-1]", sentence


def test_extract_claims_mixed_crash_and_safety_keeps_crash_wins():
    module = _unguarded_module()
    claims = extract_claims(
        "stack[-1] won't crash but raises ValueError on a bad type.",
        module.path,
        frozenset(),
    )
    assert len(claims) == 1
    assert claims[0].kind == "access_crash"


# ── end-to-end: the misclassification that shipped ───────────────────────────


def test_false_safety_claim_on_unguarded_access_is_dropped():
    """Before the fix this sentence was classified access_crash, so
    contradiction_check found no UNGUARDED match (that check only exists for
    access_safe) and the false safety affirmation survived unchanged."""
    module = _unguarded_module()
    claims = extract_claims(
        "stack[-1] will not crash: the caller guarantees a non-empty stack.",
        module.path,
        frozenset(),
    )
    kept, dropped = verify_claims(
        claims, module=module,
        known_symbols=frozenset(), line_counts={module.path: 20},
        fail_open_locs=frozenset(),
    )
    assert kept == []
    assert len(dropped) == 1
    assert dropped[0].reason == REASON_CONTRADICTS_CRASH


def test_true_safety_claim_on_guarded_access_survives():
    """Before the fix this sentence was classified access_crash and then
    dropped as 'contradicts-guard' — the true sentence was deleted."""
    module = ModuleRecord(
        path="pkg/real_bug.py",
        guarded_accesses=(
            GuardedAccess(
                location="pkg/real_bug.py:10",
                access="stack[-1]",
                status="GUARDED",
                guard="if stack:",
            ),
        ),
    )
    claims = extract_claims(
        "stack[-1] will not crash: the length guard prevents an empty stack.",
        module.path,
        frozenset(),
    )
    assert len(claims) == 1
    assert claims[0].kind == "access_safe"
    assert contradiction_check(claims[0], module, frozenset()) is None


def test_false_safety_claim_contradiction_reason():
    """Direct contradiction_check parity: an access_safe claim citing an
    UNGUARDED access must report the crash-risk contradiction."""
    module = _unguarded_module()
    claim = Claim(
        text="stack[-1] will not crash",
        module=module.path,
        kind="access_safe",
        access="stack[-1]",
    )
    reason, detail = contradiction_check(claim, module, frozenset())
    assert reason == REASON_CONTRADICTS_CRASH
    assert "UNGUARDED" in detail