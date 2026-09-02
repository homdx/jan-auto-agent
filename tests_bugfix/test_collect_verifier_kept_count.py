"""tests/test_collect_verifier_kept_count.py — COLLECT-17 regression.

BUGFIX: `verify_repo`'s `kept_count` (part of the `verification_report.json`
payload — COLLECT-17 AC: "все выбросы залогированы", i.e. every claim's
fate is logged/counted) used to be computed by re-running `extract_claims`
on the *rejoined* filtered purpose/notes text. `_verify_text` reassembles
surviving claims by joining their `.text` with a single space — but
sentence-splitting doesn't round-trip through that rejoin: newline-
separated, unpunctuated claims (a real Pass B idiom for bullet-style notes,
e.g. `"Handles retries\nLogs failures\nReturns cached result"`) collapse
back into a single sentence on re-split, since the split regex's `\n+`
alternative has no newline left to match, and the single space the join
inserted doesn't carry terminal punctuation to trigger the other
alternative either. Three surviving, independently-kept claims were
silently reported as one kept claim in the report — a real accounting bug
in the transparency guarantee this report exists for, even though nothing
was actually mis-filtered (the *returned* verified text itself was always
correct; only the *count* in the report was wrong).
"""

from __future__ import annotations

from tools.collect.model import LLMSummary, ModuleRecord
from tools.collect.verifier import extract_claims, verify_repo


def _module(purpose: str = "", notes: str = "") -> ModuleRecord:
    return ModuleRecord(
        path="m.py",
        language="python",
        parse_error=None,
        public_symbols=(),
        imports=(),
        config_reads=(),
        except_sites=(),
        guarded_accesses=(),
        summary=LLMSummary(purpose=purpose, notes=notes),
    )


def test_kept_count_matches_all_claims_surviving_when_nothing_dropped():
    # Three newline-separated, unpunctuated claims — none of them cite
    # anything checkable, so all three survive verification untouched.
    purpose = "Handles retries\nLogs failures\nReturns cached result"
    module = _module(purpose=purpose)

    original_claims = extract_claims(purpose, "m.py")
    assert len(original_claims) == 3  # sanity: this is really 3 claims

    verified, report = verify_repo([module], {"m.py": "x = 1\n"})

    assert report["dropped_count"] == 0
    assert report["kept_count"] == 3
    # the reconstructed text itself was always correct — only the count
    # in the report was wrong, so this must keep passing unchanged.
    assert verified[0].summary.purpose == "Handles retries Logs failures Returns cached result"


def test_kept_count_matches_partial_survival_with_newline_claims():
    # Same newline-separated shape, but one of the three claims cites a
    # location whose line is out of range for the file, so it must be
    # dropped (dropped:no-citation) while the other two survive.
    purpose = "Handles retries\nBreaks at m.py:9999\nReturns cached result"
    module = _module(purpose=purpose)

    verified, report = verify_repo([module], {"m.py": "x = 1\n"})

    assert report["dropped_count"] == 1
    assert report["kept_count"] == 2


def test_kept_count_zero_when_module_has_no_summary():
    module = ModuleRecord(path="m.py", language="python", parse_error=None, summary=None)
    verified, report = verify_repo([module], {"m.py": "x = 1\n"})
    assert report["kept_count"] == 0
    assert report["dropped_count"] == 0
