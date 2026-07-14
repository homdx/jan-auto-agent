"""tests/test_collect_verifier_contradiction.py — COLLECT-17.

* A fabricated claim "`stack[-1]` crashes" against a module where Pass A
  recorded that exact access as GUARDED is dropped as
  `dropped:contradicts-guard`.
* A fabricated claim "silent except at <fail-open location>" against
  COLLECT-9's FAIL_OPEN_REGISTRY is dropped as
  `dropped:contradicts-fail-open`.
* A claim about a *different*, actually-unguarded access is NOT suppressed
  by this mechanism (contradiction-suppression only fires when Pass A's
  facts actually disagree with the claim).
* AC: reproduced against this repo's own real reference sites
  (`prompt_store.py`'s GUARDED `stack[-1]`, and a real FAIL_OPEN_REGISTRY
  entry) as well as the mini-repo fixtures.
"""

from __future__ import annotations

from pathlib import Path

from tools.collect.model import GuardedAccess, ModuleRecord
from tools.collect.registries import build_fail_open_registry, fail_open_locations
from tools.collect.scanner import scan_module, scan_repo
from tools.collect.verifier import (
    REASON_CONTRADICTS_FAIL_OPEN,
    REASON_CONTRADICTS_GUARD,
    REASON_CONTRADICTS_NOT_SILENT,
    Claim,
    contradiction_check,
    extract_claims,
    verify_claims,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo"
REPO_ROOT = Path(__file__).parent.parent


def _prompt_store_module():
    source = (FIXTURE_ROOT / "pkg" / "prompt_store.py").read_text(encoding="utf-8")
    return scan_module(source, "pkg/prompt_store.py")


def _error_handling_module():
    source = (FIXTURE_ROOT / "pkg" / "error_handling.py").read_text(encoding="utf-8")
    return scan_module(source, "pkg/error_handling.py")


# ── access_crash vs GUARDED ─────────────────────────────────────────────────────


def test_stack_minus_one_crashes_claim_is_dropped_as_contradicts_guard():
    module = _prompt_store_module()
    ga = next(g for g in module.guarded_accesses if g.access == "stack[-1]")
    assert ga.status == "GUARDED"  # sanity: this is the reference case

    claim = Claim(
        text="stack[-1] crashes with an IndexError if the stack is empty",
        module=module.path,
        kind="access_crash",
        access="stack[-1]",
    )
    result = contradiction_check(claim, module, frozenset())
    assert result is not None
    reason, detail = result
    assert reason == REASON_CONTRADICTS_GUARD
    assert "GUARDED" in detail or ga.location in detail or (ga.guard or "") in detail


def test_stack_minus_one_crashes_claim_dropped_end_to_end_via_verify_claims():
    module = _prompt_store_module()
    known = frozenset(sym.qualname for sym in module.public_symbols)
    line_counts = {module.path: 20}

    claim = Claim(
        text="stack[-1] crashes with an IndexError if the stack is empty",
        module=module.path,
        kind="access_crash",
        access="stack[-1]",
    )
    kept, dropped = verify_claims(
        [claim], module=module, known_symbols=known, line_counts=line_counts,
        fail_open_locs=frozenset(),
    )
    assert kept == []
    assert len(dropped) == 1
    assert dropped[0].reason == REASON_CONTRADICTS_GUARD


def test_extract_claims_recognizes_access_crash_pattern():
    module = _prompt_store_module()
    text = "stack[-1] will crash if stack is empty."
    claims = extract_claims(text, module.path, frozenset())
    assert len(claims) == 1
    assert claims[0].kind == "access_crash"
    assert claims[0].access == "stack[-1]"


def test_unguarded_access_claim_is_not_suppressed_by_contradiction_check():
    # A genuinely UNGUARDED access must NOT be caught by this mechanism —
    # contradiction-suppression only fires when Pass A disagrees.
    module = ModuleRecord(
        path="pkg/real_bug.py",
        guarded_accesses=(
            GuardedAccess(location="pkg/real_bug.py:10", access="items[0]", status="UNGUARDED"),
        ),
    )
    claim = Claim(
        text="items[0] crashes when items is empty",
        module=module.path,
        kind="access_crash",
        access="items[0]",
    )
    assert contradiction_check(claim, module, frozenset()) is None


# ── silent_except vs FAIL_OPEN_REGISTRY ────────────────────────────────────────


def test_silent_except_claim_dropped_when_location_already_in_fail_open_registry():
    module = _error_handling_module()
    registry = build_fail_open_registry([module])
    locs = fail_open_locations(registry)
    fail_open_site = next(iter(locs))

    claim = Claim(
        text=f"silent except at {fail_open_site} swallows the error",
        module=module.path,
        kind="silent_except",
        location=fail_open_site,
    )
    result = contradiction_check(claim, module, locs)
    assert result is not None
    reason, detail = result
    assert reason == REASON_CONTRADICTS_FAIL_OPEN
    assert fail_open_site in detail


def test_silent_except_claim_not_in_registry_is_not_suppressed():
    module = _error_handling_module()
    claim = Claim(
        text="silent except at pkg/error_handling.py:999 swallows the error",
        module=module.path,
        kind="silent_except",
        location="pkg/error_handling.py:999",
    )
    assert contradiction_check(claim, module, frozenset()) is None


# ── BUGFIX regression: silent_except vs an except_site Pass A proved is ────
# ── NOT silent (logs/re-raises/continues) — the genuine mirror of the ─────
# ── access_crash-vs-GUARDED check, previously missing entirely ────────────
#
# The pre-fix `contradiction_check` only checked `fail_open_locs` for
# `silent_except` claims — which, by construction, can only ever *agree*
# with a claim that happens to be true (the location really is fail-open).
# There was no check at all for the more dangerous case: a fabricated
# "site X silently swallows the exception" about a location Pass A's own
# `except_sites` already classified as logged/re-raised/`continue`d (i.e.
# proven NOT silent) sailed through untouched.


def test_silent_except_claim_dropped_when_site_proven_not_silent():
    module = _error_handling_module()
    log_site = next(s for s in module.except_sites if s.body_kind == "log")
    assert not log_site.is_fail_open  # sanity: this is the reference case

    claim = Claim(
        text=f"the handler at {log_site.location} silently swallows the exception",
        module=module.path,
        kind="silent_except",
        location=log_site.location,
    )
    result = contradiction_check(claim, module, frozenset())
    assert result is not None
    reason, detail = result
    assert reason == REASON_CONTRADICTS_NOT_SILENT
    assert log_site.location in detail
    assert "log" in detail


def test_silent_except_claim_dropped_for_re_raise_site_too():
    module = _error_handling_module()
    reraise_site = next(s for s in module.except_sites if s.body_kind == "re-raise")

    claim = Claim(
        text=f"the handler at {reraise_site.location} silently swallows the exception",
        module=module.path,
        kind="silent_except",
        location=reraise_site.location,
    )
    reason, detail = contradiction_check(claim, module, frozenset())
    assert reason == REASON_CONTRADICTS_NOT_SILENT


def test_silent_except_claim_dropped_end_to_end_via_verify_claims():
    module = _error_handling_module()
    log_site = next(s for s in module.except_sites if s.body_kind == "log")
    known = frozenset(sym.qualname for sym in module.public_symbols)
    line_counts = {module.path: 50}

    claim = Claim(
        text=f"the handler at {log_site.location} silently swallows the exception",
        module=module.path,
        kind="silent_except",
        location=log_site.location,
    )
    kept, dropped = verify_claims(
        [claim], module=module, known_symbols=known, line_counts=line_counts,
        fail_open_locs=frozenset(),
    )
    assert kept == []
    assert len(dropped) == 1
    assert dropped[0].reason == REASON_CONTRADICTS_NOT_SILENT


def test_extract_claims_recognizes_silent_except_pattern():
    text = "This except block silently swallows KeyError at pkg/x.py:14."
    claims = extract_claims(text, "pkg/x.py", frozenset())
    assert len(claims) == 1
    assert claims[0].kind == "silent_except"
    assert claims[0].location == "pkg/x.py:14"


# ── AC: reproduced on this repo's real reference sites ─────────────────────────


def test_real_repo_stack_minus_one_guard_suppresses_fabricated_crash_claim():
    modules = {m.path: m for m in scan_repo(REPO_ROOT)}
    prompt_store = modules["tools/prompt_store.py"]
    guarded_stack_accesses = [
        g for g in prompt_store.guarded_accesses
        if g.access == "stack[-1]" and g.status == "GUARDED"
    ]
    assert guarded_stack_accesses, "expected the real GUARDED stack[-1] reference case"

    claim = Claim(
        text="stack[-1] crashes with IndexError",
        module=prompt_store.path,
        kind="access_crash",
        access="stack[-1]",
    )
    reason, _ = contradiction_check(claim, prompt_store, frozenset())
    assert reason == REASON_CONTRADICTS_GUARD


def test_real_repo_fail_open_registry_suppresses_fabricated_silent_claim():
    modules = list(scan_repo(REPO_ROOT))
    registry = build_fail_open_registry(modules, root=REPO_ROOT)
    locs = fail_open_locations(registry)
    assert any(loc.startswith("tools/auto/") for loc in locs)

    by_path = {m.path: m for m in modules}
    site = next(loc for loc in locs if "auto_metrics.py" in loc or "coder.py" in loc)
    module = by_path[site.split(":")[0]]

    claim = Claim(
        text=f"silent except at {site} hides a real error",
        module=module.path,
        kind="silent_except",
        location=site,
    )
    reason, _ = contradiction_check(claim, module, locs)
    assert reason == REASON_CONTRADICTS_FAIL_OPEN
