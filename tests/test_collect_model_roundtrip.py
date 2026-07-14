"""tests/test_collect_model_roundtrip.py — COLLECT-1.

dataclass <-> JSON roundtrip must be lossless, and provenance tags must
survive the trip unchanged.
"""

import json

from tools.collect.model import (
    ConfigRead,
    ExceptSite,
    FunctionRecord,
    GuardedAccess,
    LLMSummary,
    ModuleRecord,
    Provenance,
)


def _make_module() -> ModuleRecord:
    fn = FunctionRecord(
        qualname="tools.prompt_store.get_current",
        module="tools/prompt_store.py",
        lineno=42,
        signature="get_current(stack)",
        docstring_first_line="Return the top of the prompt stack.",
        is_private=False,
    ).with_llm_summary(LLMSummary(purpose="reads the active prompt", notes="fail-open if stack empty"))

    return ModuleRecord(
        path="tools/prompt_store.py",
        public_symbols=(fn,),
        imports=("json", "logging"),
        config_reads=(ConfigRead(section="collect", key="staleness", fallback="warn", reader_module="tools/collect/cli.py"),),
        except_sites=(ExceptSite(location="tools/prompt_store.py:88", exception_type="KeyError", body_kind="pass", is_fail_open=True),),
        guarded_accesses=(GuardedAccess(location="tools/prompt_store.py:71", access="stack[-1]", guard="early-return at L71", status="GUARDED"),),
        parse_error=None,
    ).with_llm_summary(LLMSummary(purpose="manages the prompt stack"))


def test_module_record_roundtrip_via_dict():
    mod = _make_module()
    d = mod.to_dict()
    restored = ModuleRecord.from_dict(d)
    assert restored == mod


def test_module_record_roundtrip_via_json_string():
    mod = _make_module()
    raw = json.dumps(mod.to_dict(), sort_keys=True)
    restored = ModuleRecord.from_dict(json.loads(raw))
    assert restored == mod


def test_roundtrip_preserves_provenance_tags():
    mod = _make_module()
    d = mod.to_dict()

    assert d["guarded_accesses"][0]["provenance"] == Provenance.STATIC
    assert d["except_sites"][0]["provenance"] == Provenance.STATIC
    assert d["config_reads"][0]["provenance"] == Provenance.STATIC
    assert d["summary"]["provenance"] == Provenance.LLM
    assert d["public_symbols"][0]["summary"]["provenance"] == Provenance.LLM

    restored = ModuleRecord.from_dict(d)
    assert restored.field_provenance()["guarded_accesses"] == Provenance.STATIC
    assert restored.field_provenance()["purpose"] == Provenance.LLM
    assert restored.public_symbols[0].field_provenance()["purpose"] == Provenance.LLM


def test_roundtrip_with_no_llm_summary_yet():
    """A freshly Pass-A-only module (no Pass B run) must still roundtrip."""
    mod = ModuleRecord(
        path="tools/new_module.py",
        public_symbols=(FunctionRecord(qualname="tools.new_module.f", module="tools/new_module.py", lineno=1, signature="f()"),),
    )
    d = mod.to_dict()
    restored = ModuleRecord.from_dict(d)
    assert restored == mod
    assert restored.summary is None
    assert restored.public_symbols[0].summary is None


def test_roundtrip_with_parse_error_module():
    """A module that failed to parse (COLLECT-4) still roundtrips cleanly."""
    mod = ModuleRecord(path="tools/broken.py", parse_error="SyntaxError: invalid syntax (line 3)")
    d = mod.to_dict()
    restored = ModuleRecord.from_dict(d)
    assert restored == mod
    assert restored.parse_error == "SyntaxError: invalid syntax (line 3)"
