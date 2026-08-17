"""tests/test_collect_bridge.py — COLLECT-24: tools.auto.collect_bridge.

Covers CollectBridge in isolation (no live LLM, no --auto pipeline):
* staleness fallback (fresh only; stale/absent both behave as "no data")
* budget-aware context_for() with LLM shrink vs hard truncation
* pull_symbol() structural fact resolution
"""

from __future__ import annotations

from types import SimpleNamespace

from tools.auto.collect_bridge import CollectBridge, make_collect_bridge
from tools.collect.loader import CollectModel, STATUS_ABSENT, STATUS_FRESH, STATUS_STALE
from tools.collect.model import ModuleRecord, FunctionRecord, ContractRecord


def _symbol(qualname: str, signature: str = "") -> FunctionRecord:
    module = qualname.rsplit(".", 1)[0] if "." in qualname else qualname
    return FunctionRecord(qualname=qualname, module=module, lineno=1, signature=signature)


def _module(path: str, symbols=()) -> ModuleRecord:
    return ModuleRecord(path=path, public_symbols=tuple(symbols))


def _fresh_model(modules=(), contracts=()) -> CollectModel:
    return CollectModel(status=STATUS_FRESH, modules=tuple(modules), contracts=tuple(contracts))


# ── staleness fallback ──────────────────────────────────────────────────


def test_usable_true_only_for_fresh():
    fresh = CollectModel(status=STATUS_FRESH)
    stale = CollectModel(status=STATUS_STALE)
    absent = CollectModel(status=STATUS_ABSENT)

    assert CollectBridge(fresh).usable is True
    assert CollectBridge(stale).usable is False
    assert CollectBridge(absent).usable is False
    assert CollectBridge(None).usable is False


def test_stale_context_for_returns_empty_no_llm_check():
    """Product decision: stale == absent from --auto's point of view. No
    special LLM 'is this still accurate' check — just a plain fallback."""
    module = _module("pkg/a.py", [_symbol("pkg.a.foo")])
    stale = CollectModel(status=STATUS_STALE, modules=(module,))
    bridge = CollectBridge(stale)
    assert bridge.context_for("pkg/a.py") == ""
    assert bridge.pull_symbol("foo") == ""


def test_absent_context_for_returns_empty():
    bridge = CollectBridge(CollectModel(status=STATUS_ABSENT))
    assert bridge.context_for("pkg/a.py") == ""
    assert bridge.pull_symbol("foo") == ""


# ── static context: budget + shrink ─────────────────────────────────────


def test_context_for_under_budget_returns_raw_block_no_llm_call():
    module = _module("pkg/a.py", [_symbol("pkg.a.foo", "foo() -> int")])
    model = _fresh_model([module])

    calls = []
    def _summarizer(system, user):
        calls.append((system, user))
        return "SHOULD NOT BE CALLED"

    bridge = CollectBridge(model, max_context_chars=5000, summarizer_call=_summarizer)
    block = bridge.context_for("pkg/a.py")
    assert "pkg/a.py" in block
    assert calls == []          # under budget -> summarizer never invoked
    assert bridge.shrink_calls == 0


def test_context_for_over_budget_triggers_shrink_call():
    symbols = [_symbol(f"pkg.a.func_{i}", f"func_{i}(x, y, z) -> int") for i in range(40)]
    module = _module("pkg/a.py", symbols)
    model = _fresh_model([module])

    def _summarizer(system, user):
        return "shrunk summary of the module"

    bridge = CollectBridge(model, max_context_chars=100, summarizer_call=_summarizer)
    block = bridge.context_for("pkg/a.py")
    assert block == "shrunk summary of the module"
    assert bridge.shrink_calls == 1


def test_context_for_over_budget_shrink_overshoot_falls_back_to_truncation():
    symbols = [_symbol(f"pkg.a.func_{i}", f"func_{i}(x, y, z) -> int") for i in range(40)]
    module = _module("pkg/a.py", symbols)
    model = _fresh_model([module])

    def _summarizer(system, user):
        return "x" * 10_000  # LLM ignored the budget -> must not be trusted

    bridge = CollectBridge(model, max_context_chars=100, summarizer_call=_summarizer)
    block = bridge.context_for("pkg/a.py")
    assert "truncated by CollectBridge" in block
    assert len(block) < 10_000  # never trusts an overshooting shrink verbatim


def test_context_for_over_budget_no_summarizer_hard_truncates():
    symbols = [_symbol(f"pkg.a.func_{i}", f"func_{i}(x, y, z) -> int") for i in range(40)]
    module = _module("pkg/a.py", symbols)
    model = _fresh_model([module])

    bridge = CollectBridge(model, max_context_chars=100, summarizer_call=None)
    block = bridge.context_for("pkg/a.py")
    assert "truncated by CollectBridge" in block
    assert bridge.shrink_calls == 0


def test_context_for_over_budget_summarizer_exception_falls_back_to_truncation():
    symbols = [_symbol(f"pkg.a.func_{i}", f"func_{i}(x, y, z) -> int") for i in range(40)]
    module = _module("pkg/a.py", symbols)
    model = _fresh_model([module])

    def _boom(system, user):
        raise RuntimeError("provider down")

    bridge = CollectBridge(model, max_context_chars=100, summarizer_call=_boom)
    block = bridge.context_for("pkg/a.py")
    assert "truncated by CollectBridge" in block


def test_context_for_many_joins_and_skips_empty():
    module_a = _module("pkg/a.py", [_symbol("pkg.a.foo", "foo() -> int")])
    model = _fresh_model([module_a])
    bridge = CollectBridge(model, max_context_chars=5000)
    joined = bridge.context_for_many(["pkg/a.py", "pkg/does_not_exist.py"])
    assert "pkg/a.py" in joined
    assert joined.count("COLLECT MODEL") == 1


def test_context_for_many_empty_when_nothing_matches():
    model = _fresh_model([])
    bridge = CollectBridge(model)
    assert bridge.context_for_many(["pkg/nope.py"]) == ""


# ── pull_symbol ──────────────────────────────────────────────────────────


def test_pull_symbol_exact_qualname_match():
    module = _module("pkg/a.py", [_symbol("pkg.a.foo", "foo(x: int) -> int")])
    model = _fresh_model([module])
    bridge = CollectBridge(model)
    block = bridge.pull_symbol("pkg.a.foo")
    assert "pkg/a.py" in block
    assert "foo(x: int) -> int" in block


def test_pull_symbol_bare_name_match():
    module = _module("pkg/a.py", [_symbol("pkg.a.Widget.render", "render(self) -> str")])
    model = _fresh_model([module])
    bridge = CollectBridge(model)
    block = bridge.pull_symbol("render")
    assert "render" in block


def test_pull_symbol_includes_contracts():
    module = _module("pkg/a.py", [_symbol("pkg.a.foo")])
    contract = ContractRecord(
        name="no_none_return", known_edge="pkg.a.foo",
        description="never returns None",
    )
    model = _fresh_model([module], contracts=[contract])
    bridge = CollectBridge(model)
    block = bridge.pull_symbol("foo")
    assert "no_none_return" in block
    assert "never returns None" in block


def test_pull_symbol_unknown_returns_empty():
    model = _fresh_model([_module("pkg/a.py", [_symbol("pkg.a.foo")])])
    bridge = CollectBridge(model)
    assert bridge.pull_symbol("totally_unknown_symbol") == ""


def test_pull_symbol_empty_name_returns_empty():
    model = _fresh_model([_module("pkg/a.py", [_symbol("pkg.a.foo")])])
    bridge = CollectBridge(model)
    assert bridge.pull_symbol("") == ""
    assert bridge.pull_symbol("   ") == ""


# ── GATE1-CTX-1/-2: contracts_for_symbol / tests_covering ───────────────


def test_contracts_for_symbol_returns_matching_contracts():
    module = _module("pkg/a.py", [_symbol("pkg.a.foo")])
    contract = ContractRecord(
        name="fail_open", known_edge="pkg.a.foo", description="never raises",
    )
    model = _fresh_model([module], contracts=[contract])
    bridge = CollectBridge(model)
    contracts = bridge.contracts_for_symbol("foo")
    assert len(contracts) == 1
    assert contracts[0].name == "fail_open"


def test_contracts_for_symbol_unknown_returns_empty_list():
    model = _fresh_model([_module("pkg/a.py", [_symbol("pkg.a.foo")])])
    bridge = CollectBridge(model)
    assert bridge.contracts_for_symbol("nope") == []


def test_contracts_for_symbol_stale_model_returns_empty():
    module = _module("pkg/a.py", [_symbol("pkg.a.foo")])
    contract = ContractRecord(name="x", known_edge="pkg.a.foo", description="y")
    stale = CollectModel(status=STATUS_STALE, modules=(module,), contracts=(contract,))
    bridge = CollectBridge(stale)
    assert bridge.contracts_for_symbol("foo") == []


def test_tests_covering_returns_tuple_from_test_map():
    model = CollectModel(status=STATUS_FRESH, test_map={"pkg/a.py": ("tests/test_a.py",)})
    bridge = CollectBridge(model)
    assert bridge.tests_covering("pkg/a.py") == ("tests/test_a.py",)


def test_tests_covering_missing_file_returns_empty_tuple():
    model = CollectModel(status=STATUS_FRESH, test_map={})
    bridge = CollectBridge(model)
    assert bridge.tests_covering("pkg/nope.py") == ()


def test_tests_covering_stale_model_returns_empty():
    stale = CollectModel(status=STATUS_STALE, test_map={"pkg/a.py": ("tests/test_a.py",)})
    bridge = CollectBridge(stale)
    assert bridge.tests_covering("pkg/a.py") == ()


def test_tests_covering_empty_file_arg_returns_empty():
    model = CollectModel(status=STATUS_FRESH, test_map={"pkg/a.py": ("tests/test_a.py",)})
    bridge = CollectBridge(model)
    assert bridge.tests_covering("") == ()


# ── make_collect_bridge factory ─────────────────────────────────────────


def test_make_collect_bridge_disabled_by_default(tmp_path):
    import configparser
    cfg = configparser.ConfigParser()
    bridge = make_collect_bridge(tmp_path, cfg, None, task_mode="code")
    assert bridge is None


def test_make_collect_bridge_docs_mode_uses_use_in_doc_flag(tmp_path, monkeypatch):
    import configparser
    cfg = configparser.ConfigParser()
    cfg["collect"] = {"use_in_auto": "false", "use_in_doc": "true", "llm_summaries": "false"}

    monkeypatch.setattr(
        "tools.collect.loader.load",
        lambda base_dir, config=None, config_path=None: CollectModel(status=STATUS_FRESH),
    )
    bridge = make_collect_bridge(tmp_path, cfg, None, task_mode="docs")
    assert bridge is not None
    assert bridge.usable is True


def test_make_collect_bridge_use_in_auto_true_but_load_raises_returns_none(tmp_path, monkeypatch):
    import configparser
    cfg = configparser.ConfigParser()
    cfg["collect"] = {"use_in_auto": "true"}

    def _boom(base_dir, config=None, config_path=None):
        raise RuntimeError("disk error")

    monkeypatch.setattr("tools.collect.loader.load", _boom)
    bridge = make_collect_bridge(tmp_path, cfg, None, task_mode="code")
    assert bridge is None
