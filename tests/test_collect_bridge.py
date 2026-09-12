"""tests/test_collect_bridge.py — COLLECT-24: tools.auto.collect_bridge.

Covers CollectBridge in isolation (no live LLM, no --auto pipeline):
* staleness fallback (fresh only; stale/absent both behave as "no data")
* budget-aware context_for() with LLM shrink vs hard truncation
* pull_symbol() structural fact resolution
"""

from __future__ import annotations

from types import SimpleNamespace

from tools.auto.collect_bridge import CollectBridge, make_collect_bridge
from tools.auto.context_assembler import build_collect_context_block
from tools.collect.loader import CollectModel, STATUS_ABSENT, STATUS_FRESH, STATUS_STALE
from tools.collect.model import ModuleRecord, FunctionRecord, ContractRecord, LLMSummary


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


def _heavy_module(target="pkg/a.py", n=40):
    """A module with 40 symbols — enough to overflow a 200-char budget."""
    return _module(target, [_symbol(f"pkg.a.func_{i}", f"func_{i}(x, y, z) -> int") for i in range(n)])


def test_context_for_over_budget_static_shrink_no_llm_call():
    """V6: the budget is passed into build_collect_context_block, so the
    pack's static row-shrinking (V2's _fit_pack_to_budget) handles the
    over-budget block — zero LLM calls, zero shrink_calls."""
    model = _fresh_model([_heavy_module()])

    calls = []
    def _summarizer(system, user):
        calls.append(1)
        return "SHOULD NOT BE CALLED"

    bridge = CollectBridge(model, max_context_chars=200, summarizer_call=_summarizer)
    block = bridge.context_for("pkg/a.py")

    # The block was assembled with a budget and statically shrunk to fit.
    assert len(block) <= 200
    assert "public_symbols:" in block   # symbols are cut, not absent
    assert calls == []                   # no LLM shrink call needed
    assert bridge.shrink_calls == 0


def test_context_for_over_budget_result_within_budget():
    """Every over-budget module's statically-shrunk block must fit the
    bridge's max_context_chars — that is the whole point of passing the
    budget through instead of relying on _shrink."""
    model = _fresh_model([_heavy_module()])
    bridge = CollectBridge(model, max_context_chars=200, summarizer_call=lambda s, u: "x" * 10_000)
    block = bridge.context_for("pkg/a.py")
    assert len(block) <= 200


def test_context_for_over_budget_shrink_still_fires_when_head_exceeds_budget():
    """V6's static shrink can only cut rows, not the head. A parse_error that
    makes the head alone exceed the budget is the one case _shrink still
    fires — the safety net is intact."""
    module = ModuleRecord(
        path="pkg/a.py",
        public_symbols=tuple(_symbol(f"pkg.a.func_{i}", f"func_{i}(x, y, z) -> int") for i in range(5)),
        parse_error="E" * 500,  # head alone exceeds the 200-char budget
    )
    model = _fresh_model([module])

    def _summarizer(system, user):
        return "shrunk to fit"

    bridge = CollectBridge(model, max_context_chars=200, summarizer_call=_summarizer)
    block = bridge.context_for("pkg/a.py")

    # Static shrink could not bring the head within budget, so _shrink fired.
    assert bridge.shrink_calls == 1
    assert block == "shrunk to fit"


def test_context_for_over_budget_no_summarizer_still_truncates_head():
    """No summarizer + head over budget → hard truncation, the old fallback."""
    module = ModuleRecord(
        path="pkg/a.py",
        public_symbols=tuple(_symbol(f"pkg.a.func_{i}") for i in range(5)),
        parse_error="E" * 500,
    )
    model = _fresh_model([module])
    bridge = CollectBridge(model, max_context_chars=200, summarizer_call=None)
    block = bridge.context_for("pkg/a.py")
    assert "truncated by CollectBridge" in block
    assert bridge.shrink_calls == 0


# ── V6: memo and pack_enabled ────────────────────────────────────────────


def test_context_for_memo_avoids_second_llm_call():
    """V6 acceptance: a second context_for("x.py") in one run makes zero
    additional LLM calls — the assembled (and possibly shrunk) block is
    memoized."""
    module = ModuleRecord(
        path="pkg/a.py",
        public_symbols=tuple(_symbol(f"pkg.a.func_{i}") for i in range(5)),
        parse_error="E" * 500,  # forces _shrink on the first call
    )
    model = _fresh_model([module])

    calls = []
    def _summarizer(system, user):
        calls.append(1)
        return "shrunk to fit"

    bridge = CollectBridge(model, max_context_chars=200, summarizer_call=_summarizer)

    block1 = bridge.context_for("pkg/a.py")
    assert len(calls) == 1          # first call paid for shrink

    block2 = bridge.context_for("pkg/a.py")
    assert block2 == block1          # same memoized result
    assert len(calls) == 1           # second call: 0 additional LLM calls


def test_context_for_memo_key_includes_budget_and_pack_enabled():
    """The memo key is (target_file, max_context_chars, pack_enabled) — all
    three components make up the key so a config or budget change busts it."""
    model = _fresh_model([_heavy_module()])
    bridge = CollectBridge(model, max_context_chars=200, pack_enabled=True)
    bridge.context_for("pkg/a.py")

    keys = list(bridge._memo.keys())
    assert len(keys) == 1
    assert keys[0] == ("pkg/a.py", 200, True)


def test_invalidate_drops_memo_entry_for_dirty_path():
    """V9+V6: invalidate() drops memo entries for the dirtied path so a later
    context_for rebuilds (and re-checks dirt) instead of serving a stale
    clean block."""
    model = _fresh_model([_heavy_module()])
    bridge = CollectBridge(model, max_context_chars=200, summarizer_call=None)

    bridge.context_for("pkg/a.py")
    assert "pkg/a.py" in str(bridge._memo.keys())

    bridge.invalidate(["pkg/a.py"])
    # The memo entry for pkg/a.py is gone.
    for key in bridge._memo:
        assert key[0] != "pkg/a.py"


def _neighbourhood_model():
    """Model with callers, callees, tests, and a neighbour that has a purpose
    — enough to populate the V3–V5 rows."""
    target = "pkg/a.py"
    modules = (
        _module(target, [_symbol("pkg.a.foo", "foo() -> int")]),
        ModuleRecord(path="pkg/caller_a.py",
                     summary=LLMSummary(purpose="Calls into the hub.")),
        ModuleRecord(path="pkg/dep_a.py"),
    )
    return CollectModel(
        status=STATUS_FRESH,
        modules=modules,
        import_edges={target: ("pkg/dep_a.py",)},
        imported_by={target: ("pkg/caller_a.py",)},
        test_map={target: ("tests/test_a.py",)},
    )


def test_context_for_pack_disabled_omits_neighbourhood_rows():
    """pack_enabled=False: V3–V5 rows (callers, calls_into, tests, neighbours)
    are absent; only the V2 rows render."""
    model = _neighbourhood_model()

    bridge_on = CollectBridge(model, max_context_chars=5000, pack_enabled=True)
    block_on = bridge_on.context_for("pkg/a.py")
    assert "callers:" in block_on
    assert "calls_into:" in block_on
    assert "tests:" in block_on
    assert "neighbours:" in block_on

    bridge_off = CollectBridge(model, max_context_chars=5000, pack_enabled=False)
    block_off = bridge_off.context_for("pkg/a.py")
    assert "callers:" not in block_off
    assert "calls_into:" not in block_off
    assert "tests:" not in block_off
    assert "neighbours:" not in block_off
    assert "public_symbols:" in block_off  # V2 row survives


def test_context_for_pack_disabled_matches_assembler_directly():
    """pack_enabled=False: context_for output is byte-identical to calling
    build_collect_context_block with the same flags."""
    model = _neighbourhood_model()
    direct = build_collect_context_block(
        model, "pkg/a.py", budget=5000, pack_enabled=False,
    )
    bridge = CollectBridge(model, max_context_chars=5000, pack_enabled=False)
    assert bridge.context_for("pkg/a.py") == direct


def test_context_for_pack_default_is_true():
    """The default is pack_enabled=True — the full pack renders."""
    model = _neighbourhood_model()
    bridge = CollectBridge(model, max_context_chars=5000)
    assert hasattr(bridge, "_pack_enabled")
    assert bridge._pack_enabled is True
    assert "callers:" in bridge.context_for("pkg/a.py")


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


def test_make_collect_bridge_reads_pack_enabled(tmp_path, monkeypatch):
    """V6: make_collect_bridge reads [collect] pack_enabled and threads it
    through to CollectBridge._pack_enabled."""
    import configparser
    cfg = configparser.ConfigParser()
    cfg["collect"] = {
        "use_in_auto": "true", "llm_summaries": "false", "pack_enabled": "true",
    }

    monkeypatch.setattr(
        "tools.collect.loader.load",
        lambda base_dir, config=None, config_path=None: CollectModel(status=STATUS_FRESH),
    )
    bridge = make_collect_bridge(tmp_path, cfg, None, task_mode="code")
    assert bridge is not None
    assert bridge._pack_enabled is True


def test_make_collect_bridge_pack_disabled_false(tmp_path, monkeypatch):
    """pack_enabled=false collapses the pack to V2 rows."""
    import configparser
    cfg = configparser.ConfigParser()
    cfg["collect"] = {
        "use_in_auto": "true", "llm_summaries": "false", "pack_enabled": "false",
    }

    monkeypatch.setattr(
        "tools.collect.loader.load",
        lambda base_dir, config=None, config_path=None: CollectModel(status=STATUS_FRESH),
    )
    bridge = make_collect_bridge(tmp_path, cfg, None, task_mode="code")
    assert bridge is not None
    assert bridge._pack_enabled is False


def test_make_collect_bridge_pack_enabled_malformed_warns_default(tmp_path, monkeypatch):
    """A malformed pack_enabled warns once and falls back to True."""
    import configparser
    cfg = configparser.ConfigParser()
    cfg["collect"] = {
        "use_in_auto": "true", "llm_summaries": "false", "pack_enabled": "not-a-bool",
    }

    monkeypatch.setattr(
        "tools.collect.loader.load",
        lambda base_dir, config=None, config_path=None: CollectModel(status=STATUS_FRESH),
    )
    bridge = make_collect_bridge(tmp_path, cfg, None, task_mode="code")
    assert bridge is not None
    assert bridge._pack_enabled is True  # default on malformed
