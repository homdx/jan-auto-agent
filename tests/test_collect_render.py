"""tests/test_collect_render.py — COLLECT-18.

* Every fact that shows up in a rendered page can be found in the
  canonical JSON of the input it came from (no MD facts without a JSON
  source).
* Rendering is deterministic: two renders of the same input are
  byte-identical.
* AC: MD and JSON cannot diverge — proven operationally via an ablation
  test: removing a record from the input removes its trace from the MD.
"""

from pathlib import Path

from tools.collect._determinism import canonical_dumps
from tools.collect.config_map import ConfigMapEntry, SiblingGap
from tools.collect.gates import GateEntry
from tools.collect.graph import entry_points as compute_entry_points
from tools.collect.graph import import_edges, imported_by
from tools.collect.model import ContractRecord
from tools.collect.registries import build_fail_open_registry
from tools.collect.render import (
    PAGE_NAMES,
    render_all,
    render_architecture,
    render_config_map,
    render_contracts,
    render_fail_open_registry,
    render_gates,
    render_glossary,
    render_module_map,
    render_risk_index,
    render_test_map,
)
from tools.collect.risk import RiskEntry
from tools.collect.scanner import scan_repo
from tools.collect.test_map import build_test_map, thin_coverage, zero_coverage

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo"


def _mini_repo_context():
    """Build the same shape of facts a real `render_all` caller (the
    COLLECT-19 CLI) would have lying around after Pass A + EPIC C/D, but
    over the small, fully-known `collect_mini_repo` fixture."""
    modules = scan_repo(FIXTURE_ROOT)
    edges = import_edges(modules)
    reverse = imported_by(edges)
    entries = compute_entry_points(edges, reverse)
    fail_open = build_fail_open_registry(modules, root=FIXTURE_ROOT)
    test_map = build_test_map(FIXTURE_ROOT, modules)
    zero = zero_coverage(test_map)
    thin = thin_coverage(test_map)
    return {
        "modules": modules,
        "import_edges": edges,
        "imported_by": reverse,
        "entry_points": entries,
        "fail_open_registry": fail_open,
        "test_map": test_map,
        "zero_coverage": zero,
        "thin_coverage": thin,
    }


# ── render_all: shape ────────────────────────────────────────────────────


def test_render_all_produces_exactly_the_nine_named_pages():
    ctx = _mini_repo_context()
    pages = render_all(**ctx)
    assert set(pages) == set(PAGE_NAMES)
    assert len(PAGE_NAMES) == 9


def test_render_is_byte_identical_across_two_runs():
    ctx = _mini_repo_context()
    first = render_all(**ctx)
    second = render_all(**ctx)
    assert first == second


# ── ARCHITECTURE.md / MODULE_MAP.md trace to JSON ───────────────────────


def test_architecture_facts_trace_to_import_graph_json():
    ctx = _mini_repo_context()
    md = render_architecture(
        ctx["modules"],
        import_edges=ctx["import_edges"],
        imported_by=ctx["imported_by"],
        entry_points=ctx["entry_points"],
    )
    graph_json = canonical_dumps(
        {"import_edges": {k: sorted(v) for k, v in ctx["import_edges"].items()}},
        check_forbidden=False,
    )
    for path in ctx["import_edges"]:
        assert path in md
        assert path in graph_json


def test_module_map_symbols_trace_to_module_json():
    ctx = _mini_repo_context()
    md = render_module_map(ctx["modules"])
    modules_json = canonical_dumps([m.to_dict() for m in ctx["modules"]])
    for m in ctx["modules"]:
        assert m.path in md
        assert m.path in modules_json
        for sym in m.public_symbols:
            assert sym.qualname in md
            assert sym.qualname in modules_json


def test_module_map_ablation_removing_a_module_removes_its_trace():
    """AC: MD is derived from JSON — it cannot show a fact its input
    doesn't have. Drop one module from the input and its whole section
    must vanish from the rendered page."""
    ctx = _mini_repo_context()
    full_md = render_module_map(ctx["modules"])
    assert "pkg/unguarded.py" in full_md

    without_one = [m for m in ctx["modules"] if m.path != "pkg/unguarded.py"]
    ablated_md = render_module_map(without_one)
    assert "pkg/unguarded.py" not in ablated_md
    # everything else is untouched
    for m in without_one:
        assert m.path in ablated_md


# ── CONTRACTS.md ─────────────────────────────────────────────────────────


def test_contracts_md_traces_every_contract_to_input():
    contracts = [
        ContractRecord(
            name="mini_repo_guard_contract",
            description="get_current never indexes an empty stack.",
            kind="seed",
            known_edge="pkg/prompt_store.py:get_current",
        ),
    ]
    md = render_contracts(contracts)
    contracts_json = canonical_dumps(
        [{"name": c.name, "known_edge": c.known_edge, "description": c.description} for c in contracts]
    )
    assert "mini_repo_guard_contract" in md
    assert "pkg/prompt_store.py:get_current" in md
    assert "mini_repo_guard_contract" in contracts_json


def test_contracts_md_empty_input_says_none():
    md = render_contracts([])
    assert "No contracts recorded" in md


# ── FAIL_OPEN_REGISTRY.md ────────────────────────────────────────────────


def test_fail_open_registry_md_lists_every_site_from_input():
    ctx = _mini_repo_context()
    md = render_fail_open_registry(ctx["fail_open_registry"])
    assert len(ctx["fail_open_registry"]) > 0  # the fixture has a known pass-based site
    for entry in ctx["fail_open_registry"]:
        assert entry.location in md
        assert entry.exception_type in md


# ── GATES.md ─────────────────────────────────────────────────────────────


def test_gates_md_traces_every_gate_to_input():
    gates = [
        GateEntry(
            name="mini_gate",
            module="pkg/error_handling.py",
            parser="read_optional",
            protocol="line verdict: OK / FAIL",
            fail_mode="open",
            extra_llm_call=False,
            config_switch="[collect] mini_gate",
            config_default="false",
        ),
    ]
    md = render_gates(gates)
    assert "mini_gate" in md
    assert "pkg/error_handling.py" in md
    assert "read_optional" in md


def test_gates_md_empty_input_says_none():
    md = render_gates([])
    assert "No gates recorded" in md


# ── TEST_MAP.md ──────────────────────────────────────────────────────────


def test_test_map_md_lists_zero_coverage_from_mini_repo():
    ctx = _mini_repo_context()
    # collect_mini_repo has no tests/ directory of its own, so every module
    # is on the zero-list — a known, checkable property of this fixture.
    assert set(ctx["zero_coverage"]) == {m.path for m in ctx["modules"]}
    md = render_test_map(ctx["test_map"], zero_coverage=ctx["zero_coverage"], thin_coverage=ctx["thin_coverage"])
    for path in ctx["zero_coverage"]:
        assert path in md


# ── RISK_INDEX.md ────────────────────────────────────────────────────────


def test_risk_index_md_preserves_input_order_without_resorting():
    # Deliberately NOT alphabetical / NOT score order, to prove the
    # renderer doesn't quietly re-sort behind risk.compute_risk_index's back.
    entries = [
        RiskEntry(path="pkg/z.py", loc=5, blast_radius=0, unguarded_count=0, undocumented_fail_open_count=0, zero_coverage=False, score=5),
        RiskEntry(path="pkg/a.py", loc=500, blast_radius=9, unguarded_count=3, undocumented_fail_open_count=2, zero_coverage=True, score=999),
    ]
    md = render_risk_index(entries)
    z_pos = md.index("pkg/z.py")
    a_pos = md.index("pkg/a.py")
    assert z_pos < a_pos  # input order (z before a) preserved, not re-sorted


def test_risk_index_md_empty_input_says_none():
    assert "No modules scored" in render_risk_index([])


# ── CONFIG_MAP.md ────────────────────────────────────────────────────────


def test_config_map_md_traces_entries_and_sibling_gaps():
    entries = [
        ConfigMapEntry(
            section="collect",
            key_template="threshold_{task_mode}",
            readers=("pkg/config_reader.py",),
            fallbacks=(10,),
            has_mode_override=True,
            concrete_keys=("threshold_code", "threshold_creative"),
        ),
    ]
    gaps = [
        SiblingGap(section="coder", key="num_ctx_creative", present_in="agents.ini", missing_in=("agents_32k.ini",)),
    ]
    md = render_config_map(entries, sibling_gaps=gaps)
    assert "threshold_{task_mode}" in md
    assert "threshold_creative" in md
    assert "num_ctx_creative" in md
    assert "agents_32k.ini" in md


def test_config_map_md_empty_input_says_none():
    md = render_config_map([], sibling_gaps=[])
    assert "No config reads recorded" in md
    assert "No gaps detected" in md


# ── GLOSSARY.md ──────────────────────────────────────────────────────────


def test_glossary_is_fixed_and_deterministic():
    first = render_glossary()
    second = render_glossary()
    assert first == second
    assert "static" in first
    assert "fail-open" in first
    assert "blast radius" in first
