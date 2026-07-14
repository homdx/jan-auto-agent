"""tests/test_collect_verifier_provenance.py — COLLECT-17.

* A `Claim` can only ever be constructed with `provenance="llm"` — the
  same construction-time enforcement COLLECT-1 uses for `LLMSummary`.
* A claim that survives verification (kept, not dropped) stays `llm`; it
  never migrates to `static` or `derived`, no matter how many static
  facts it was successfully checked against.
* `verify_module`/`verify_repo` reattach surviving prose as a plain
  `LLMSummary` — never a `ContractRecord`/`GateRecord`/other
  static-or-derived type — so the antihallucination boundary (only
  `static` facts are ever authoritative) holds even after Pass C approves
  something.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.collect.model import LLMSummary, Provenance, ProvenanceViolation
from tools.collect.scanner import scan_module
from tools.collect.verifier import (
    Claim,
    verify_module,
    verify_repo,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo"


def _prompt_store_module():
    source = (FIXTURE_ROOT / "pkg" / "prompt_store.py").read_text(encoding="utf-8")
    return scan_module(source, "pkg/prompt_store.py"), source


# ── Claim construction-time enforcement ────────────────────────────────────────


def test_claim_defaults_to_llm_provenance():
    claim = Claim(text="x", module="pkg/x.py")
    assert claim.provenance == Provenance.LLM


def test_claim_cannot_be_constructed_as_static():
    with pytest.raises(ProvenanceViolation):
        Claim(text="x", module="pkg/x.py", provenance=Provenance.STATIC)


def test_claim_cannot_be_constructed_as_derived():
    with pytest.raises(ProvenanceViolation):
        Claim(text="x", module="pkg/x.py", provenance=Provenance.DERIVED)


# ── surviving claims stay llm end to end ───────────────────────────────────────


def test_surviving_claim_remains_llm_after_verify_module():
    module, source = _prompt_store_module()
    known = frozenset(sym.qualname for sym in module.public_symbols)
    line_counts = {module.path: source.count("\n") + 1}

    summary = LLMSummary(
        purpose="get_current reads the top of the prompt stack.",
        notes="",
    )
    summarized = module.with_llm_summary(summary)

    verified, dropped = verify_module(
        summarized, known_symbols=known, line_counts=line_counts, fail_open_locs=frozenset(),
    )

    assert verified.summary is not None
    assert verified.summary.provenance == Provenance.LLM
    # The whitelist LLMSummary only has purpose/notes — there is no field
    # for it to have been promoted to "static" or "derived" even by a bug.
    assert set(f for f in vars(verified.summary) if f != "provenance") <= {"purpose", "notes"}


def test_verify_repo_reattaches_plain_llmsummary_not_a_static_type():
    module, source = _prompt_store_module()
    summary = LLMSummary(purpose="get_current returns the last stack element.", notes="")
    summarized = module.with_llm_summary(summary)

    verified_modules, report = verify_repo(
        [summarized], {module.path: source}, root=None,
    )
    verified = verified_modules[0]

    assert isinstance(verified.summary, LLMSummary)
    assert verified.summary.provenance == Provenance.LLM
    # Structural fields untouched, still static — the other half of the
    # antihallucination boundary this module must never cross.
    assert verified.public_symbols == module.public_symbols
    assert verified.field_provenance()["public_symbols"] == Provenance.STATIC


def test_verify_repo_report_is_json_serializable_and_deterministic():
    module, source = _prompt_store_module()
    summary = LLMSummary(
        purpose="stack[-1] crashes with an IndexError if the stack is empty.",
        notes="",
    )
    summarized = module.with_llm_summary(summary)

    _, report1 = verify_repo([summarized], {module.path: source})
    _, report2 = verify_repo([summarized], {module.path: source})

    # json.dumps must not raise (fully JSON-serializable), and two runs on
    # unchanged input produce byte-identical reports (COLLECT-3 determinism).
    assert json.dumps(report1, sort_keys=True) == json.dumps(report2, sort_keys=True)
    assert report1["dropped_count"] >= 1
    assert all(row["reason"] for row in report1["dropped"])


def test_dropped_claim_report_rows_never_claim_static_provenance():
    module, source = _prompt_store_module()
    summary = LLMSummary(
        purpose="stack[-1] crashes with an IndexError if the stack is empty.",
        notes="",
    )
    summarized = module.with_llm_summary(summary)

    _, report = verify_repo([summarized], {module.path: source})
    for row in report["dropped"]:
        # The report is a plain dict describing what was rejected — it must
        # never assert or imply a "provenance" of anything but the claim's
        # own (always "llm") origin; there is no "provenance" key promoting
        # dropped prose to fact status.
        assert "provenance" not in row
