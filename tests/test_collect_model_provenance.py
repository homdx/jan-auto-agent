"""tests/test_collect_model_provenance.py — COLLECT-1.

Verifies the antihallucination guarantee at the heart of EPIC A:

  * A structural/AST-only fact (GuardedAccess, ExceptSite, ConfigRead) can
    never be constructed with provenance="llm" — the "llm path" throws.
  * The whitelist {purpose, notes} is the complete LLM-writable surface:
    LLMSummary accepts exactly those fields, and it is the only type an
    LLM-facing summarizer can produce.
  * FunctionRecord/ModuleRecord structural fields have no setter reachable
    from the LLM side; `with_llm_summary` can only ever attach an
    LLMSummary, never touch a structural field.
"""

import dataclasses

import pytest

from tools.collect.model import (
    ConfigRead,
    ExceptSite,
    FunctionRecord,
    GuardedAccess,
    LLMSummary,
    ModuleRecord,
    Provenance,
    ProvenanceViolation,
)


# ── 1. Attempting to write a static-only record via the "llm path" throws ────


@pytest.mark.parametrize(
    "factory",
    [
        lambda: GuardedAccess(location="mod.py:10", access="x[-1]", status="UNGUARDED", provenance=Provenance.LLM),
        lambda: ExceptSite(location="mod.py:20", exception_type="OSError", body_kind="pass", provenance=Provenance.LLM),
        lambda: ConfigRead(section="collect", key="staleness", provenance=Provenance.LLM),
    ],
)
def test_static_fact_rejects_llm_provenance(factory):
    with pytest.raises(ProvenanceViolation):
        factory()


def test_guarded_access_rejects_derived_too():
    """GuardedAccess is pure Pass A output; even 'derived' isn't allowed."""
    with pytest.raises(ProvenanceViolation):
        GuardedAccess(location="mod.py:1", access="y[0]", status="UNGUARDED", provenance=Provenance.DERIVED)


def test_static_fact_default_provenance_is_static():
    site = ExceptSite(location="coder.py:718", exception_type="Exception", body_kind="pass", is_fail_open=True)
    assert site.provenance == Provenance.STATIC


# ── 2. Whitelist {purpose, notes} is respected ───────────────────────────────


def test_llm_summary_accepts_only_whitelisted_fields():
    summary = LLMSummary(purpose="parses verdicts", notes="fail-open on bad JSON")
    assert summary.purpose == "parses verdicts"
    assert summary.notes == "fail-open on bad JSON"
    assert summary.provenance == Provenance.LLM
    field_names = {f.name for f in dataclasses.fields(summary)}
    assert field_names == {"purpose", "notes", "provenance"}


def test_llm_summary_rejects_non_llm_provenance():
    with pytest.raises(ProvenanceViolation):
        LLMSummary(purpose="x", provenance=Provenance.STATIC)


# ── 3. No setter for structural fields is reachable from the LLM side ───────


def test_function_record_is_frozen_no_structural_setter():
    fn = FunctionRecord(qualname="pkg.mod.f", module="pkg/mod.py", lineno=10, signature="f(x)")
    with pytest.raises(dataclasses.FrozenInstanceError):
        fn.qualname = "hacked"  # type: ignore[misc]


def test_with_llm_summary_only_touches_summary_field():
    fn = FunctionRecord(qualname="pkg.mod.f", module="pkg/mod.py", lineno=10, signature="f(x)")
    summary = LLMSummary(purpose="does a thing")
    updated = fn.with_llm_summary(summary)

    # structural fields identical
    assert updated.qualname == fn.qualname
    assert updated.module == fn.module
    assert updated.lineno == fn.lineno
    assert updated.signature == fn.signature
    # only summary changed
    assert updated.summary is summary
    assert fn.summary is None  # original untouched (immutable)


def test_with_llm_summary_rejects_non_llmsummary_payload():
    fn = FunctionRecord(qualname="pkg.mod.f", module="pkg/mod.py", lineno=10, signature="f(x)")
    with pytest.raises(TypeError):
        fn.with_llm_summary("just a string, not an LLMSummary")  # type: ignore[arg-type]


def test_module_record_field_provenance_isolates_llm_fields():
    ga = GuardedAccess(location="prompt_store.py:71", access="stack[-1]", guard="early-return at L71", status="GUARDED")
    mod = ModuleRecord(path="tools/prompt_store.py", guarded_accesses=(ga,))
    prov = mod.field_provenance()
    assert prov["guarded_accesses"] == Provenance.STATIC

    mod2 = mod.with_llm_summary(LLMSummary(purpose="stores prompt stack"))
    prov2 = mod2.field_provenance()
    assert prov2["purpose"] == Provenance.LLM
    assert prov2["guarded_accesses"] == Provenance.STATIC  # unaffected by the LLM write
