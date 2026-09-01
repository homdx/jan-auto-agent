"""tests/test_bugfix_registries_query_access_quote_normalization.py

Bug: `AlreadySafeIndex.query()`'s `access` filter compared
`a.access == access` raw, with no quote-style normalization — while
verifier.py has a dedicated `_normalize_access()` specifically because
quote-style (`'` vs `"`) differences caused real citations to be dropped
there. A guard recorded as `entry['stack']` (single-quoted) never matched
a query for `entry["stack"]` (double-quoted) — the identical site — so a
correctly-guarded access was reported `unknown` instead of `guarded`,
purely because of which quote character the source happened to use.
"""
from __future__ import annotations

from tools.collect.model import GuardedAccess, ModuleRecord
from tools.collect.registries import build_already_safe_index


def _index_with_single_quoted_guard():
    modules = [
        ModuleRecord(
            path="pkg/fake.py",
            guarded_accesses=(
                GuardedAccess(
                    location="pkg/fake.py:10",
                    access="entry['stack']",
                    status="GUARDED",
                    guard="isinstance check",
                ),
            ),
        )
    ]
    return build_already_safe_index(modules, fail_open_registry=[], contracts=[])


def test_double_quoted_query_matches_single_quoted_recorded_guard():
    index = _index_with_single_quoted_guard()
    answer = index.query("pkg/fake.py:10", access='entry["stack"]')
    assert answer.safe is True
    assert answer.reason == "guarded"


def test_single_quoted_query_matches_recorded_guard_exactly():
    index = _index_with_single_quoted_guard()
    answer = index.query("pkg/fake.py:10", access="entry['stack']")
    assert answer.safe is True
    assert answer.reason == "guarded"


def test_genuinely_different_access_still_does_not_match():
    index = _index_with_single_quoted_guard()
    answer = index.query("pkg/fake.py:10", access="entry['other_key']")
    assert answer.safe is False
