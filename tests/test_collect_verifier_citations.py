"""tests/test_collect_verifier_citations.py — COLLECT-17.

* A claim citing a symbol that doesn't exist in Pass A's index is dropped
  (`dropped:no-citation`); a claim citing a real symbol is kept.
* A claim citing a location whose line is out of range for the file (or
  whose file isn't a scanned module at all) is dropped the same way; a
  claim citing a real, in-range location is kept.
* A claim with no citation at all (`kind="generic"`, nothing to check) is
  never a citation failure — it survives untouched.
"""

from __future__ import annotations

from pathlib import Path

from tools.collect.model import GuardedAccess, ModuleRecord, Provenance
from tools.collect.scanner import scan_module
from tools.collect.verifier import (
    REASON_NO_CITATION,
    Claim,
    citation_check,
    extract_claims,
    verify_claims,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo"


def _prompt_store_module():
    source = (FIXTURE_ROOT / "pkg" / "prompt_store.py").read_text(encoding="utf-8")
    return scan_module(source, "pkg/prompt_store.py"), source


# ── citation_check: symbols ────────────────────────────────────────────────────


def test_claim_citing_unknown_symbol_fails_citation_check():
    module, _ = _prompt_store_module()
    known = frozenset({"pkg/prompt_store.py:get_current"})
    claim = Claim(text="foo", module=module.path, symbol="pkg/prompt_store.py:totally_made_up")
    detail = citation_check(claim, known, {module.path: 20})
    assert detail is not None
    assert "totally_made_up" in detail


def test_claim_citing_real_symbol_passes_citation_check():
    module, _ = _prompt_store_module()
    known = frozenset({"pkg/prompt_store.py:get_current"})
    claim = Claim(text="foo", module=module.path, symbol="pkg/prompt_store.py:get_current")
    assert citation_check(claim, known, {module.path: 20}) is None


# ── citation_check: locations ──────────────────────────────────────────────────


def test_claim_citing_out_of_range_line_fails_citation_check():
    module, source = _prompt_store_module()
    line_counts = {module.path: source.count("\n") + 1}
    claim = Claim(text="foo", module=module.path, location=f"{module.path}:9999")
    detail = citation_check(claim, frozenset(), line_counts)
    assert detail is not None
    assert "out of range" in detail


def test_claim_citing_unknown_module_fails_citation_check():
    claim = Claim(text="foo", module="pkg/x.py", location="pkg/nonexistent_module.py:5")
    detail = citation_check(claim, frozenset(), {"pkg/x.py": 20})
    assert detail is not None
    assert "not found" in detail


def test_claim_citing_real_in_range_line_passes_citation_check():
    module, source = _prompt_store_module()
    line_counts = {module.path: source.count("\n") + 1}
    claim = Claim(text="foo", module=module.path, location=f"{module.path}:2")
    assert citation_check(claim, frozenset(), line_counts) is None


def test_claim_with_malformed_location_fails_citation_check():
    claim = Claim(text="foo", module="pkg/x.py", location="pkg/x.py:not-a-number")
    detail = citation_check(claim, frozenset(), {"pkg/x.py": 20})
    assert detail is not None
    assert "malformed" in detail


# ── generic claims with nothing to cite always survive citation-check ─────────


def test_claim_with_no_citation_at_all_passes_trivially():
    claim = Claim(text="this module handles configuration.", module="pkg/x.py")
    assert citation_check(claim, frozenset(), {}) is None


# ── verify_claims: end-to-end kept/dropped split ───────────────────────────────


def test_verify_claims_drops_fabricated_symbol_keeps_real_one():
    module, source = _prompt_store_module()
    known = frozenset(sym.qualname for sym in module.public_symbols)
    line_counts = {module.path: source.count("\n") + 1}

    fabricated = Claim(
        text="calls a helper function named totally_fake_helper",
        module=module.path,
        symbol=f"{module.path}:totally_fake_helper",
    )
    real = Claim(
        text="defines get_current to read the prompt stack",
        module=module.path,
        symbol=f"{module.path}:get_current",
    )

    kept, dropped = verify_claims(
        [fabricated, real],
        module=module,
        known_symbols=known,
        line_counts=line_counts,
        fail_open_locs=frozenset(),
    )

    assert kept == [real]
    assert len(dropped) == 1
    assert dropped[0].reason == REASON_NO_CITATION
    assert dropped[0].claim is fabricated


# ── extract_claims -> citation-check integration on real prose ────────────────


def test_extract_claims_then_citation_check_end_to_end():
    module, source = _prompt_store_module()
    known = frozenset(sym.qualname for sym in module.public_symbols)
    line_counts = {module.path: source.count("\n") + 1}

    text = (
        f"get_current reads from the stack. "
        f"There is a fabricated_symbol_xyz that does something at {module.path}:9999."
    )
    claims = extract_claims(text, module.path, known)
    kept, dropped = verify_claims(
        claims, module=module, known_symbols=known, line_counts=line_counts,
        fail_open_locs=frozenset(),
    )

    kept_texts = [c.text for c in kept]
    dropped_texts = [d.claim.text for d in dropped]
    assert any("get_current" in t for t in kept_texts)
    assert any("9999" in t for t in dropped_texts)
    assert all(d.reason == REASON_NO_CITATION for d in dropped if "9999" in d.claim.text)


def test_claim_provenance_is_always_llm():
    claim = Claim(text="x", module="pkg/x.py")
    assert claim.provenance == Provenance.LLM


# ── citation_check: access (the fabricated-access gap) ─────────────────────────
#
# `contradiction_check` only ever drops an `access_crash` claim when Pass A
# has a *matching* record that's GUARDED — a crash claim about an access
# expression Pass A has no record of at all (not GUARDED, not UNGUARDED,
# simply absent — because it doesn't correspond to any real subscript site
# in the module) found no contradiction and was silently kept, reaching
# the artifact as a fabricated structural claim. These tests pin the fix:
# an access_crash claim must cite a real, cataloged access site, exactly
# the way a symbol or location claim must cite a real one.


def test_claim_citing_fabricated_access_fails_citation_check():
    module, _ = _prompt_store_module()
    claim = Claim(
        text="cache_registry[-1] crashes",
        module=module.path,
        kind="access_crash",
        access="cache_registry[-1]",  # does not exist anywhere in this module
    )
    known_accesses = frozenset(ga.access for ga in module.guarded_accesses)
    detail = citation_check(claim, frozenset(), {module.path: 20}, known_accesses)
    assert detail is not None
    assert "cache_registry[-1]" in detail


def test_claim_citing_real_cataloged_access_passes_citation_check():
    module, _ = _prompt_store_module()
    real_access = module.guarded_accesses[0].access  # "stack[-1]" in the fixture
    claim = Claim(text="x", module=module.path, kind="access_crash", access=real_access)
    known_accesses = frozenset(ga.access for ga in module.guarded_accesses)
    assert citation_check(claim, frozenset(), {module.path: 20}, known_accesses) is None


def test_generic_claim_mentioning_brackets_without_crash_kind_is_not_access_checked():
    # Scoped to kind="access_crash" only: a claim that merely mentions
    # bracket syntax in passing (kind="generic") was never asserting that
    # access is a cataloged crash site, so it must not be penalized for
    # not matching one.
    module, _ = _prompt_store_module()
    claim = Claim(text="x", module=module.path, kind="generic", access="unrelated[0]")
    assert citation_check(claim, frozenset(), {module.path: 20}, frozenset()) is None


def test_fabricated_access_crash_claim_dropped_end_to_end_via_verify_claims():
    # The exact end-to-end reproduction: a crash claim about an access
    # that doesn't exist anywhere in the module's real guarded_accesses
    # must not survive to the artifact.
    module, source = _prompt_store_module()
    known = frozenset(sym.qualname for sym in module.public_symbols)
    line_counts = {module.path: source.count("\n") + 1}

    text = "The internal cache_registry[-1] entry crashes because it is unguarded."
    claims = extract_claims(text, module.path, known)
    kept, dropped = verify_claims(
        claims, module=module, known_symbols=known, line_counts=line_counts,
        fail_open_locs=frozenset(),
    )
    assert kept == []
    assert len(dropped) == 1
    assert dropped[0].reason == REASON_NO_CITATION
    assert "cache_registry[-1]" in dropped[0].detail


def test_real_unguarded_finding_still_survives_after_access_citation_check():
    # The fix must not overcorrect: a claim about a real, cataloged
    # UNGUARDED access — a genuine finding — must still be kept.
    module = ModuleRecord(
        path="pkg/real_bug.py",
        guarded_accesses=(
            GuardedAccess(location="pkg/real_bug.py:10", access="items[0]", status="UNGUARDED"),
        ),
    )
    claim = Claim(text="x", module=module.path, kind="access_crash", access="items[0]")
    known_accesses = frozenset(ga.access for ga in module.guarded_accesses)
    assert citation_check(claim, frozenset(), {module.path: 20}, known_accesses) is None
