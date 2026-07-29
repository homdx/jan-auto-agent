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

import ast
from pathlib import Path

from tools.collect.ast_facts import extract_all_defined_names
from tools.collect.model import GuardedAccess, ModuleRecord, Provenance
from tools.collect.scanner import scan_module
from tools.collect.verifier import (
    REASON_NO_CITATION,
    REASON_SIBLING_CITATION_FAILED,
    Claim,
    citation_check,
    extract_claims,
    verify_claims,
    verify_repo,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo"
REPO_ROOT = Path(__file__).parent.parent


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


def test_claim_citing_real_symbol_from_a_different_module_fails_citation_check():
    # BUGFIX (found via self-play hallucination session on this repo):
    # `known_symbols` is the whole repo's flat symbol table (built once by
    # `verify_repo`), so a symbol that's real *somewhere* used to pass
    # `citation_check` even when it belongs to a module Pass B was never
    # shown while summarizing this one (`summarizer.py`'s prompt is scoped
    # to a single module's facts/source — COLLECT-16). A claim like "this
    # module implements backoff_seconds" while summarizing formatter.py is
    # a pure fabrication even though `backoff_seconds` genuinely exists in
    # tools/backoff.py — citation_check must require the citation to
    # belong to the module the claim is actually about.
    known = frozenset({
        "pkg/prompt_store.py:get_current",
        "pkg/other_module.py:some_function",
    })
    claim = Claim(
        text="foo", module="pkg/prompt_store.py",
        symbol="pkg/other_module.py:some_function",
    )
    detail = citation_check(claim, known, {"pkg/prompt_store.py": 20, "pkg/other_module.py": 20})
    assert detail is not None
    assert "pkg/other_module.py" in detail and "pkg/prompt_store.py" in detail


def test_claim_citing_real_location_from_a_different_module_fails_citation_check():
    # Same fabrication shape, via a `path:line` citation instead of a
    # symbol name: the line is genuinely in range for *some* scanned
    # module, just not the one this claim's `purpose`/`notes` field was
    # attached to.
    claim = Claim(
        text="foo", module="pkg/prompt_store.py",
        location="pkg/other_module.py:5",
    )
    detail = citation_check(claim, frozenset(), {"pkg/prompt_store.py": 20, "pkg/other_module.py": 20})
    assert detail is not None
    assert "pkg/other_module.py" in detail and "pkg/prompt_store.py" in detail


def test_cross_module_hallucination_end_to_end_is_dropped():
    # Full extract -> verify path (not just citation_check in isolation),
    # using two real modules from this repo, mirroring exactly the
    # self-play repro that found the bug.
    #
    # NOTE: this sentence cites *two* separate things from the wrong
    # module (a symbol and a location) — since the multi-citation fix,
    # `extract_claims` correctly produces one `Claim` per citation instead
    # of only the first, so both get independently caught and dropped
    # (previously this test only asserted on the first of the two).
    mod_a_path = "tools/formatter.py"
    mod_b_path = "tools/backoff.py"
    repo_root = Path(__file__).parent.parent
    module_a = scan_module((repo_root / mod_a_path).read_text(encoding="utf-8"), mod_a_path)
    module_b = scan_module((repo_root / mod_b_path).read_text(encoding="utf-8"), mod_b_path)
    known_symbols = frozenset(
        sym.qualname for m in (module_a, module_b) for sym in m.public_symbols
    )
    fake_symbol = module_b.public_symbols[0].qualname
    claims = extract_claims(
        f"This module implements {fake_symbol}, defined at {mod_b_path}:1.",
        mod_a_path, known_symbols,
    )
    kept, dropped = verify_claims(
        claims, module=module_a, known_symbols=known_symbols,
        line_counts={mod_a_path: 999, mod_b_path: 999}, fail_open_locs=frozenset(),
    )
    assert kept == []
    assert len(dropped) == 2
    assert {d.reason for d in dropped} == {REASON_NO_CITATION}
    assert any("belongs to module" in d.detail and mod_b_path in d.detail for d in dropped)


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


# ── fabricated `path.py:identifier` symbol citations (COLLECT-17 follow-up) ───
#
# `extract_claims` only ever set `claim.symbol` by matching a sentence
# against a name already in `known_symbols` (`_symbol_patterns`) — an
# outright *invented* symbol never matches any of those patterns, so it
# fell through as `kind="generic"` with `symbol=None` and survived
# untouched, indistinguishable from ordinary unfalsifiable prose. A first
# attempt at closing that gap (routing any `path.py:identifier`-shaped
# mention into `citation_check`) over-corrected: it also caught *real*
# names Pass A's `public_symbols` index doesn't track by design — module-
# level constants and class methods, both out of `extract_symbols`'
# deliberately top-level-only scope (COLLECT-4). The fix has to
# distinguish "invented" from "real but unindexed", via
# `ast_facts.extract_all_defined_names` — a broader, permissive
# "does this name appear anywhere in the module's source" check used only
# to *decline* to flag something, never to positively verify a claim.


def _known_names(source: str) -> "frozenset[str]":
    return extract_all_defined_names(ast.parse(source))


def test_fabricated_symbol_citation_is_flagged_and_dropped():
    module, source = _prompt_store_module()
    known_symbols = frozenset(sym.qualname for sym in module.public_symbols)
    known_names = _known_names(source)

    text = f"It relies on {module.path}:_totally_invented_helper_fn for caching."
    claims = extract_claims(text, module.path, known_symbols, known_names)
    assert len(claims) == 1
    assert claims[0].symbol == f"{module.path}:_totally_invented_helper_fn"

    kept, dropped = verify_claims(
        claims, module=module, known_symbols=known_symbols,
        line_counts={module.path: source.count("\n") + 1}, fail_open_locs=frozenset(),
    )
    assert kept == []
    assert len(dropped) == 1
    assert dropped[0].reason == REASON_NO_CITATION
    assert "_totally_invented_helper_fn" in dropped[0].detail


def test_real_but_unindexed_module_level_constant_is_not_flagged():
    # BUGFIX: found via review of the first attempt at this fix.
    # `tools/collect/summarizer.py:DEFAULT_NUM_CTX` is a real, genuine
    # constant — just not a "public symbol" by COLLECT-4's own top-level-
    # only definition. A citation of it must not be dropped as if it were
    # a fabrication.
    mod_path = "tools/collect/summarizer.py"
    source = (REPO_ROOT / mod_path).read_text(encoding="utf-8")
    assert "DEFAULT_NUM_CTX" in source  # sanity: the constant is real
    module = scan_module(source, mod_path)
    known_symbols = frozenset(sym.qualname for sym in module.public_symbols)
    assert f"{mod_path}:DEFAULT_NUM_CTX" not in known_symbols  # and genuinely unindexed
    known_names = _known_names(source)

    text = f"It reads {mod_path}:DEFAULT_NUM_CTX as the context-window fallback."
    claims = extract_claims(text, mod_path, known_symbols, known_names)
    assert claims[0].symbol is None  # not routed into citation_check at all
    assert claims[0].kind == "generic"

    kept, dropped = verify_claims(
        claims, module=module, known_symbols=known_symbols,
        line_counts={mod_path: source.count("\n") + 1}, fail_open_locs=frozenset(),
    )
    assert dropped == []
    assert len(kept) == 1


def test_real_but_unindexed_class_method_is_not_flagged():
    # Same false-positive shape, via a class method instead of a
    # module-level constant — `render` is a real method of
    # `OutputFormatter`, just nested (out of `public_symbols`' top-level
    # scope) rather than fabricated.
    mod_path = "tools/formatter.py"
    source = (REPO_ROOT / mod_path).read_text(encoding="utf-8")
    module = scan_module(source, mod_path)
    known_symbols = frozenset(sym.qualname for sym in module.public_symbols)
    assert f"{mod_path}:render" not in known_symbols
    known_names = _known_names(source)

    text = f"See {mod_path}:render for the actual formatting logic."
    claims = extract_claims(text, mod_path, known_symbols, known_names)
    assert claims[0].symbol is None
    kept, dropped = verify_claims(
        claims, module=module, known_symbols=known_symbols,
        line_counts={mod_path: source.count("\n") + 1}, fail_open_locs=frozenset(),
    )
    assert dropped == []
    assert len(kept) == 1


def test_cross_module_citation_of_real_unindexed_name_still_dropped():
    # The `known_names` guard is scoped to the module actually being
    # summarized — it must not let a citation of some *other* module's
    # real-but-unindexed name through, since Pass B was never shown that
    # other module's facts/source to have legitimately known that name
    # from in the first place (same reasoning as the cross-module
    # citation-check fix this guard sits alongside).
    mod_a, mod_b = "tools/collect/summarizer.py", "tools/formatter.py"
    src_a = (REPO_ROOT / mod_a).read_text(encoding="utf-8")
    src_b = (REPO_ROOT / mod_b).read_text(encoding="utf-8")
    module_a = scan_module(src_a, mod_a)
    known_symbols = frozenset(sym.qualname for sym in module_a.public_symbols)
    known_names_a = _known_names(src_a)  # only module A's own names

    text = f"This mirrors {mod_b}:render exactly."
    claims = extract_claims(text, mod_a, known_symbols, known_names_a)
    assert claims[0].symbol == f"{mod_b}:render"  # not suppressed — wrong module

    kept, dropped = verify_claims(
        claims, module=module_a, known_symbols=known_symbols,
        line_counts={mod_a: src_a.count("\n") + 1, mod_b: src_b.count("\n") + 1},
        fail_open_locs=frozenset(),
    )
    assert kept == []
    assert dropped[0].reason == REASON_NO_CITATION


def test_full_pipeline_distinguishes_fabricated_from_real_unindexed_names():
    # End-to-end via the real `verify_repo` orchestration (not just
    # `extract_claims`/`verify_claims` in isolation): scans two real
    # modules from this repo, attaches one fabricated and one real-but-
    # unindexed claim to each, and checks the verification report gets
    # both right in a single run.
    mod_a, mod_b = "tools/collect/summarizer.py", "tools/formatter.py"
    src_a = (REPO_ROOT / mod_a).read_text(encoding="utf-8")
    src_b = (REPO_ROOT / mod_b).read_text(encoding="utf-8")
    from tools.collect.model import LLMSummary
    module_a = scan_module(src_a, mod_a).with_llm_summary(
        LLMSummary(
            purpose=(
                f"It relies on {mod_a}:_totally_invented_helper_fn for caching. "
                f"It reads {mod_a}:DEFAULT_NUM_CTX as the context-window fallback."
            ),
            notes="",
        )
    )
    module_b = scan_module(src_b, mod_b).with_llm_summary(
        LLMSummary(purpose=f"See {mod_b}:render for the actual formatting logic.", notes="")
    )

    verified, report = verify_repo(
        [module_a, module_b], {mod_a: src_a, mod_b: src_b},
    )
    by_path = {m.path: m for m in verified}

    # Fabricated citation dropped...
    assert "_totally_invented_helper_fn" not in by_path[mod_a].summary.purpose
    # ...real-but-unindexed constant survives...
    assert "DEFAULT_NUM_CTX" in by_path[mod_a].summary.purpose
    # ...and a real-but-unindexed method in a completely different,
    # LLM-summary-free module survives too (no citation to fail, since it
    # wasn't a known symbol OR flagged as fabricated).
    assert "render" in by_path[mod_b].summary.purpose

    reasons = {d["reason"] for d in report["dropped"]}
    assert REASON_NO_CITATION in reasons


# ── multiple citations in one sentence (COLLECT-17 follow-up) ─────────────────
#
# `extract_claims` used to capture only the *first* location/symbol/access
# match per sentence via `.search()`. A sentence citing two things of the
# same kind — one real, one fabricated — only ever produced one `Claim`,
# carrying the real citation; the fabricated one was never extracted into
# anything `citation_check` could see, so it survived verbatim right next
# to the real citation that (from the verifier's perspective) "vouched for"
# the whole sentence.


def test_two_symbol_citations_one_sentence_real_and_fabricated_both_dropped():
    mod_path = "tools/collect/summarizer.py"
    source = (REPO_ROOT / mod_path).read_text(encoding="utf-8")
    module = scan_module(source, mod_path)
    known_symbols = frozenset(sym.qualname for sym in module.public_symbols)
    real_symbol = "make_summarizer_call"
    assert any(s.endswith(f":{real_symbol}") for s in known_symbols)  # sanity
    known_names = _known_names(source)

    text = (
        f"{real_symbol} and {mod_path}:_totally_invented_helper_fn "
        "both live in this module."
    )
    claims = extract_claims(text, mod_path, known_symbols, known_names)
    assert len(claims) == 2  # one per citation, not one for the whole sentence

    kept, dropped = verify_claims(
        claims, module=module, known_symbols=known_symbols,
        line_counts={mod_path: source.count("\n") + 1}, fail_open_locs=frozenset(),
    )
    # The real citation alone would have survived on its own — but its
    # sentence also makes a fabricated claim, so neither survives.
    assert kept == []
    assert len(dropped) == 2
    reasons = {d.reason for d in dropped}
    assert REASON_NO_CITATION in reasons
    assert REASON_SIBLING_CITATION_FAILED in reasons


def test_two_location_citations_one_sentence_real_and_out_of_range_both_dropped():
    mod_path = "tools/collect/summarizer.py"
    source = (REPO_ROOT / mod_path).read_text(encoding="utf-8")
    module = scan_module(source, mod_path)
    known_symbols = frozenset(sym.qualname for sym in module.public_symbols)
    known_names = _known_names(source)

    text = (
        f"See {mod_path}:1 for the header, and also "
        f"{mod_path}:999999 for the retry policy."
    )
    claims = extract_claims(text, mod_path, known_symbols, known_names)
    assert len(claims) == 2

    kept, dropped = verify_claims(
        claims, module=module, known_symbols=known_symbols,
        line_counts={mod_path: source.count("\n") + 1}, fail_open_locs=frozenset(),
    )
    assert kept == []
    assert len(dropped) == 2
    reasons = {d.reason for d in dropped}
    assert REASON_NO_CITATION in reasons
    assert REASON_SIBLING_CITATION_FAILED in reasons


def test_two_real_citations_one_sentence_both_survive_without_duplicating_text():
    # Sanity/non-regression: a sentence citing *two real* things must
    # still fully survive, and the reconstructed artifact text must not
    # end up with the sentence duplicated (one `Claim` per citation means
    # `kept` now legitimately contains 2 `Claim`s sharing this sentence's
    # `.text` -- `_verify_text` must dedupe when rejoining them).
    mod_path = "tools/collect/summarizer.py"
    source = (REPO_ROOT / mod_path).read_text(encoding="utf-8")
    from tools.collect.model import LLMSummary
    module = scan_module(source, mod_path).with_llm_summary(
        LLMSummary(
            purpose="make_summarizer_call and summarize_repo both live in this module.",
            notes="",
        )
    )
    verified, report = verify_repo([module], {mod_path: source})
    assert report["dropped_count"] == 0
    assert report["kept_count"] == 2  # two citations, two surviving claims
    assert verified[0].summary.purpose == (
        "make_summarizer_call and summarize_repo both live in this module."
    )  # exactly once, not duplicated


def test_sentence_with_no_citations_still_produces_exactly_one_generic_claim():
    # Non-regression: the multi-citation rewrite must not turn an ordinary
    # unfalsifiable sentence into zero claims (or several).
    mod_path = "tools/collect/summarizer.py"
    source = (REPO_ROOT / mod_path).read_text(encoding="utf-8")
    module = scan_module(source, mod_path)
    known_symbols = frozenset(sym.qualname for sym in module.public_symbols)
    known_names = _known_names(source)

    text = "This module coordinates Pass B summarization."
    claims = extract_claims(text, mod_path, known_symbols, known_names)
    assert len(claims) == 1
    assert claims[0].symbol is None
    assert claims[0].location is None
    assert claims[0].access is None
    assert claims[0].kind == "generic"
