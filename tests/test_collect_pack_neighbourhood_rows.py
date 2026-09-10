"""tests/test_collect_pack_neighbourhood_rows.py — PLAN-v2 V3.

The first rows that carry information the target file's own source cannot
supply: who imports this, what this imports, and what tests it.

  V3-1  `callers` leads with the count — every importer, test or not — and
        names only the non-test ones, capped at five, remainder announced.
  V3-2  `calls_into` lists first-party imports, capped at five.
  V3-3  `tests` gives the count plus up to three names, or
        `no test covers this file` for a `zero_coverage` module.
  V3-4  A module with no shipped importer renders the entry-point form, never
        an empty row.
  V3-5  Paths render exactly as the artifact records them: relative, never
        rewritten or basename-stripped.
  V3-6  A module absent from the model still yields `""` overall.

Only structural dataclasses are built here — no git repo, no LLM. The
end-to-end section at the bottom runs a real `collect` against a tiny
throwaway repo so the rows are exercised through the actual loader rather
than only against hand-built tables, and the live section pins the ticket's
acceptance criteria against this repo's own tree — built from the producer's
graph and test-map passes directly, so it never skips for a missing
`.collect/` artifact (which is gitignored).

Acceptance drift, measured against the artifact on disk: the ticket asks for
"7 non-test importers" for `tools/auto/coder.py`. The 25 total reproduces;
the non-test figure does not — the live artifact records 25 importers, 23 of
them test files, leaving 2 production callers. The row's *semantics* are the
ticket's (count first, non-test names, capped); the parenthetical number is
whatever the artifact says, so the drift lives in the assertion, not the code.
`main.py` is likewise not in `entry_points` (13 test files import it), so the
entry-point form keys off "no shipped importer", not on artifact membership —
and keeps that 13 on the line, since "nothing imports this" would be false.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.auto.context_assembler import (
    _CALLERS_PREFIX,
    _CALLS_INTO_PREFIX,
    _ENTRY_POINT_NOTE,
    _ENTRY_POINT_TESTS_ONLY_NOTE,
    _NO_TESTS_NOTE,
    _PACK_ROWS,
    _TESTS_PREFIX,
    _capped_names,
    _row_callers,
    _row_calls_into,
    _row_tests,
    build_collect_context_block,
)
from tools.collect import cli as cli_mod
from tools.collect import loader as loader_mod
from tools.collect.loader import STATUS_FRESH, CollectModel
from tools.collect.model import ModuleRecord
from tools.collect.graph import entry_points, import_edges, imported_by as reverse_index
from tools.collect.risk import RiskEntry
from tools.collect.scanner import scan_repo
from tools.collect.test_map import build_test_map, zero_coverage


REPO_ROOT = Path(__file__).resolve().parent.parent


# ── helpers ────────────────────────────────────────────────────────────────


def _module(path: str = "pkg/a.py") -> ModuleRecord:
    return ModuleRecord(path=path)


def _risk(path: str, blast_radius: int) -> RiskEntry:
    return RiskEntry(
        path=path,
        loc=10,
        blast_radius=blast_radius,
        unguarded_count=0,
        undocumented_fail_open_count=0,
        zero_coverage=False,
        score=0,
    )


def _model(
    modules=(),
    *,
    import_edges=None,
    imported_by=None,
    test_map=None,
    zero_coverage=(),
    risk_index=(),
) -> CollectModel:
    """A fresh `CollectModel` with only the tables the row under test needs.

    Defaults are empty containers, never `None`, matching the loader — an
    artifact that never wrote a table reads as "nothing known", which is the
    fail-open path every renderer must survive.
    """
    return CollectModel(
        status=STATUS_FRESH,
        modules=tuple(modules),
        import_edges=dict(import_edges or {}),
        imported_by=dict(imported_by or {}),
        test_map={k: tuple(v) for k, v in (test_map or {}).items()},
        zero_coverage_list=tuple(zero_coverage),
        risk_index=tuple(risk_index),
    )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def _tests_only(count: int) -> str:
    """The entry-point form for a module whose only importers are tests."""
    return _CALLERS_PREFIX + _ENTRY_POINT_TESTS_ONLY_NOTE.format(
        count=count, noun="file" if count == 1 else "files", verb="does" if count == 1 else "do",
    )


def _row(block: str, prefix: str) -> str:
    """The block's one line starting with `prefix`, or `""` when absent."""
    for line in block.split("\n"):
        if line.startswith(prefix):
            return line
    return ""


# A graph used by most of the row tests below. `pkg/hub.py` is imported by 7
# shipped modules and 3 test modules, imports 7 shipped modules, and is
# covered by 7 test files — so every cap in this file has a remainder to cut.
CALLERS_HEAVY = tuple(f"pkg/c{i}.py" for i in range(1, 8))
TEST_CALLERS = (
    "tests/test_one.py",
    "tests_bugfix/test_bugfix_one.py",
    "tests/test_two.py",
)
CALLS_INTO = tuple(f"tools/pkg/t{i}.py" for i in range(1, 8))
TESTS = tuple(f"tests/test_t{i}.py" for i in range(1, 8))

HUB_GRAPH_MODULES = ("pkg/hub.py",) + CALLERS_HEAVY + TEST_CALLERS + CALLS_INTO


def _hub_model() -> CollectModel:
    """The hub above, with blast radii running *against* the alphabetical
    order so a cap that kept the alphabetically first callers would be caught."""
    risk = [_risk(path, 10 * i) for i, path in enumerate(CALLERS_HEAVY, start=1)]
    imported_by = {
        "pkg/hub.py": CALLERS_HEAVY + TEST_CALLERS,
    }
    for path in HUB_GRAPH_MODULES:
        imported_by.setdefault(path, ())
    import_edges = {
        "pkg/hub.py": CALLS_INTO,
    }
    for path in HUB_GRAPH_MODULES:
        import_edges.setdefault(path, ())
    test_map = {path: () for path in HUB_GRAPH_MODULES}
    test_map["pkg/hub.py"] = TESTS
    return _model(
        [_module(p) for p in HUB_GRAPH_MODULES],
        import_edges=import_edges,
        imported_by=imported_by,
        test_map=test_map,
        zero_coverage=[p for p, v in test_map.items() if not v],
        risk_index=risk,
    )


# ── row wiring ─────────────────────────────────────────────────────────────


def test_pack_rows_lead_with_the_three_neighbourhood_rows():
    """V3 prepends callers/calls_into/tests above the three rows V2 ported and
    V5 puts `neighbours` next — still above the V2 rows; `public_symbols`
    stays last, it is the one row the target file's own source makes
    redundant."""
    names = [name for name, _ in _PACK_ROWS]
    assert names == [
        "callers",
        "calls_into",
        "tests",
        "neighbours",
        "contract",
        "config_read",
        "public_symbols",
    ]
    assert names[-1] == "public_symbols"
    assert names[:3] == ["callers", "calls_into", "tests"]


def test_every_pack_row_has_a_name_and_a_callable_renderer():
    assert len(_PACK_ROWS) == 7
    for name, render in _PACK_ROWS:
        assert isinstance(name, str) and name
        assert callable(render)


def test_neighbourhood_rows_render_before_the_v2_rows_in_the_block():
    """The tuple order IS the emitted order: the new facts lead the block."""
    model = _hub_model()
    lines = build_collect_context_block(model, "pkg/hub.py").split("\n")
    labels = [line.split(":")[0] for line in lines[2:]]
    assert labels == ["callers", "calls_into", "tests"]


# ── V3-1: callers ──────────────────────────────────────────────────────────


def test_callers_counts_every_importer_but_names_only_the_non_test_ones():
    """The count is the fact — every importer, test or not — while the names
    are the courtesy, and test importers are the `tests` row's job."""
    line = _row_callers(_hub_model(), "pkg/hub.py", None)
    assert line == (
        "callers: 10 modules import this (7 non-test): "
        "pkg/c7.py, pkg/c6.py, pkg/c5.py, pkg/c4.py, pkg/c3.py, +2"
    )
    for caller in TEST_CALLERS:
        assert caller not in line
    for caller in ("pkg/c1.py", "pkg/c2.py"):
        assert caller not in line  # cut, and announced by the +2


def test_callers_announces_the_remainder_of_its_own_list():
    """`+2` must count the non-test callers that were cut, not the ten
    importers — otherwise the row reads as hiding more than it does."""
    line = _row_callers(_hub_model(), "pkg/hub.py", None)
    assert line.endswith("pkg/c3.py, +2")


def test_callers_omits_the_parenthetical_when_it_would_restate_the_total():
    model = _model(
        [_module("pkg/a.py"), _module("pkg/b.py")],
        imported_by={"pkg/a.py": ("pkg/b.py",), "pkg/b.py": ()},
    )
    assert _row_callers(model, "pkg/a.py", None) == "callers: 1 module imports this: pkg/b.py"


def test_callers_keeps_the_heaviest_callers_when_capped():
    """`callers_of` ranks by the caller's own blast radius; a cap must keep
    that order as a prefix rather than falling back to alphabetical."""
    line = _row_callers(_hub_model(), "pkg/hub.py", None)
    named = [p for p in line.rsplit(": ", 1)[1].split(", ") if not p.startswith("+")]
    assert named == [f"pkg/c{i}.py" for i in range(7, 2, -1)]


def test_callers_renders_the_entry_point_form_for_a_leaf_module():
    """AC: an entry-point module with no callers gets a line, not a gap."""
    model = _model([_module("pkg/leaf.py")], imported_by={"pkg/leaf.py": ()})
    assert _row_callers(model, "pkg/leaf.py", None) == f"callers: {_ENTRY_POINT_NOTE}"


def test_callers_renders_the_entry_point_form_when_every_importer_is_a_test():
    """`main.py`: 13 importers, all under `tests/` — nothing shipped imports
    it, and that is the fact a rename decision needs. The test-importer count
    is kept on the line: "nothing imports this" would be false, and the number
    is still a fact the file's own source cannot supply."""
    model = _model(
        [_module("main.py"), _module("tests/test_main.py")],
        imported_by={"main.py": ("tests/test_main.py",), "tests/test_main.py": ()},
    )
    line = _row_callers(model, "main.py", None)
    assert line == _tests_only(1)
    assert line.startswith(f"{_CALLERS_PREFIX}entry point — ")
    assert "tests/test_main.py" not in line  # the tests row's job, not this one's


def test_callers_tests_only_form_pluralises_its_count():
    model = _model(
        [_module("main.py"), _module("tests/test_a.py"), _module("tests/test_b.py")],
        imported_by={
            "main.py": ("tests/test_a.py", "tests/test_b.py"),
            "tests/test_a.py": (),
            "tests/test_b.py": (),
        },
    )
    assert _row_callers(model, "main.py", None) == _tests_only(2)
    assert _row_callers(model, "main.py", None).endswith("; 2 test files do")


def test_callers_is_absent_when_the_artifact_never_collected_the_graph():
    """A pre-V1 artifact has no `imported_by`: the importer set is *unknown*,
    not empty, so no entry-point claim is made."""
    model = _model([_module("pkg/a.py")])
    assert _row_callers(model, "pkg/a.py", None) == ""


def test_callers_is_absent_for_a_module_outside_the_graph_table():
    model = _model(
        [_module("pkg/a.py"), _module("pkg/unknown.py")],
        imported_by={"pkg/a.py": ()},
    )
    assert _row_callers(model, "pkg/unknown.py", None) == ""


def test_callers_is_absent_for_an_unknown_module():
    assert _row_callers(_hub_model(), "pkg/nowhere.py", None) == ""


def test_callers_is_absent_on_an_absent_model():
    assert _row_callers(CollectModel(status="absent"), "pkg/a.py", None) == ""


# ── V3-2: calls_into ───────────────────────────────────────────────────────


def test_calls_into_caps_at_five_and_announces_the_remainder():
    line = _row_calls_into(_hub_model(), "pkg/hub.py", None)
    assert line == f"calls_into: {', '.join(CALLS_INTO[:5])}, +2"


def test_calls_into_lists_every_import_when_within_the_cap():
    model = _model(
        [_module("pkg/a.py"), _module("tools/x.py"), _module("tools/y.py")],
        import_edges={"pkg/a.py": ("tools/x.py", "tools/y.py"), "tools/x.py": (), "tools/y.py": ()},
    )
    assert _row_calls_into(model, "pkg/a.py", None) == "calls_into: tools/x.py, tools/y.py"


def test_calls_into_is_absent_when_the_module_imports_nothing():
    model = _model([_module("pkg/leaf.py")], import_edges={"pkg/leaf.py": ()})
    assert _row_calls_into(model, "pkg/leaf.py", None) == ""


def test_calls_into_is_absent_when_the_artifact_never_collected_the_graph():
    model = _model([_module("pkg/a.py")])
    assert _row_calls_into(model, "pkg/a.py", None) == ""


def test_calls_into_drops_a_target_the_module_table_does_not_know():
    """`calls_into` filters against the module table; a partial artifact must
    not surface a phantom dependency."""
    model = _model(
        [_module("pkg/a.py"), _module("tools/real.py")],
        import_edges={"pkg/a.py": ("tools/real.py", "tools/phantom.py")},
    )
    assert _row_calls_into(model, "pkg/a.py", None) == "calls_into: tools/real.py"


def test_calls_into_is_absent_for_an_unknown_module():
    assert _row_calls_into(_hub_model(), "pkg/nowhere.py", None) == ""


# ── V3-3: tests ────────────────────────────────────────────────────────────


def test_tests_renders_the_count_plus_up_to_three_names():
    line = _row_tests(_hub_model(), "pkg/hub.py", None)
    assert line == f"tests: 7 files: {', '.join(TESTS[:3])}, +4"


def test_tests_counts_singular_files_as_file():
    model = _model(
        [_module("pkg/a.py")],
        test_map={"pkg/a.py": ("tests/test_a.py",)},
    )
    assert _row_tests(model, "pkg/a.py", None) == "tests: 1 file: tests/test_a.py"


def test_tests_announces_no_remainder_when_every_name_fits():
    model = _model(
        [_module("pkg/a.py")],
        test_map={"pkg/a.py": ("tests/t1.py", "tests/t2.py", "tests/t3.py")},
    )
    assert _row_tests(model, "pkg/a.py", None) == (
        "tests: 3 files: tests/t1.py, tests/t2.py, tests/t3.py"
    )


def test_tests_renders_the_no_tests_form_for_a_zero_coverage_module():
    """AC: the absence is the more useful fact, and it costs nothing."""
    model = _model(
        [_module("pkg/uncovered.py")],
        test_map={"pkg/uncovered.py": ()},
        zero_coverage=("pkg/uncovered.py",),
    )
    assert _row_tests(model, "pkg/uncovered.py", None) == f"tests: {_NO_TESTS_NOTE}"


def test_tests_renders_the_no_tests_form_when_zero_coverage_is_missing_from_the_artifact():
    """An older artifact can carry a `test_map` entry with no covering file and
    no `zero_coverage` key at all. The empty entry is the same fact read two
    ways, so the row must still say so."""
    model = _model([_module("pkg/uncovered.py")], test_map={"pkg/uncovered.py": ()})
    assert _row_tests(model, "pkg/uncovered.py", None) == f"tests: {_NO_TESTS_NOTE}"


def test_tests_is_absent_when_the_artifact_has_no_opinion_about_the_module():
    """168 of this repo's 469 modules are in neither `test_map` nor
    `zero_coverage`; inventing a coverage claim for them would be the bug."""
    model = _model([_module("pkg/a.py")], test_map={"pkg/other.py": ()})
    assert _row_tests(model, "pkg/a.py", None) == ""


def test_tests_drops_non_string_junk_from_a_malformed_test_map():
    """Fail open: a half-written artifact cannot turn a row into an exception."""
    model = _model([_module("pkg/a.py")], test_map={"pkg/a.py": ("tests/t1.py", None, 7)})
    assert _row_tests(model, "pkg/a.py", None) == "tests: 1 file: tests/t1.py"


def test_tests_is_absent_for_an_unknown_module():
    assert _row_tests(_hub_model(), "pkg/nowhere.py", None) == ""


# ── V3-5: paths ────────────────────────────────────────────────────────────


def test_paths_render_exactly_as_the_artifact_records_them():
    """AC: paths always relative. The rows neither basename-strip (which would
    collide `tools/__init__.py` with any other `__init__.py`) nor prefix
    absolutely."""
    model = _hub_model()
    rendered = []
    for row in (_row_callers, _row_calls_into, _row_tests):
        line = row(model, "pkg/hub.py", None)
        assert line
        # rsplit: the callers row's own text contains a colon after the count
        rendered.append(line.rsplit(": ", 1)[1])
    joined = ", ".join(rendered)
    for token in joined.replace(", +", " ").split(", "):
        path = token.rsplit(", ", 1)[0]
        assert path.startswith(("pkg/", "tools/", "tests/")), path
        assert not path.startswith("/") and not path.startswith(".."), path
    assert "pkg/hub.py" not in joined  # the target is never listed as its own neighbour


def test_capped_names_never_loses_an_entry_silently():
    """The announced remainder always reconciles with the list it came from."""
    paths = tuple(f"m{i}.py" for i in range(12))
    assert _capped_names(paths, 5) == "m0.py, m1.py, m2.py, m3.py, m4.py, +7"
    assert _capped_names(paths[:5], 5) == "m0.py, m1.py, m2.py, m3.py, m4.py"
    assert _capped_names(paths[:1], 5) == "m0.py"
    assert _capped_names((), 5) == ""
    # a cap larger than the list is not a cap at all
    assert _capped_names(paths[:3], 50) == "m0.py, m1.py, m2.py"


@pytest.mark.parametrize(
    ("render", "prefix", "total", "cap"),
    [
        (_row_callers, _CALLERS_PREFIX, len(CALLERS_HEAVY), 5),
        (_row_calls_into, _CALLS_INTO_PREFIX, len(CALLS_INTO), 5),
        (_row_tests, _TESTS_PREFIX, len(TESTS), 3),
    ],
)
def test_each_row_names_at_most_its_cap_and_reconciles_the_rest(render, prefix, total, cap):
    """The ticket's caps, observed: five callers, five callees, three test
    files. The hub exceeds all three, so each row must announce the remainder,
    and named-plus-announced must equal the row's own total — no entry is ever
    cut silently."""
    line = render(_hub_model(), "pkg/hub.py", None)
    named = [p for p in line.rsplit(": ", 1)[1].split(", ") if not p.startswith("+")]
    announced = int(line.rsplit("+", 1)[1])
    assert len(named) == cap
    assert len(named) + announced == total
    assert line.startswith(prefix)


# ── V3-6 and the block-level invariants V3 inherits from V2 ────────────────


def test_a_module_absent_from_the_model_yields_no_block():
    """AC: unknown target -> `""` overall, so a caller that concatenates the
    result loses nothing."""
    assert build_collect_context_block(_hub_model(), "pkg/nowhere.py") == ""


def test_none_and_absent_models_yield_no_block():
    assert build_collect_context_block(None, "pkg/hub.py") == ""
    assert build_collect_context_block(CollectModel(status="absent"), "pkg/hub.py") == ""
    assert (
        build_collect_context_block(CollectModel(status="absent"), "pkg/hub.py", budget=2000)
        == ""
    )


def test_a_block_with_no_rows_at_all_is_still_empty():
    """A module the graph never saw has no neighbourhood facts to offer, and a
    header with nothing behind it is worse than no header."""
    model = _model([_module("pkg/lone.py")])
    assert build_collect_context_block(model, "pkg/lone.py") == ""


def test_the_three_new_rows_return_empty_string_not_none():
    model = _model([_module("pkg/lone.py")])
    assert _row_callers(model, "pkg/lone.py", None) == ""
    assert _row_calls_into(model, "pkg/lone.py", None) == ""
    assert _row_tests(model, "pkg/lone.py", None) == ""


def test_renderers_accept_the_remaining_argument_the_loop_passes():
    """Every renderer has the `(model, target, remaining)` signature the loop
    calls with; the new rows ignore it because each is one line."""
    model = _hub_model()
    for render in (_row_callers, _row_calls_into, _row_tests):
        assert render(model, "pkg/hub.py", None) == render(model, "pkg/hub.py", 42)
        assert render(model, "pkg/hub.py", 1) == render(model, "pkg/hub.py", None)


def test_block_never_exceeds_its_budget_with_the_new_rows():
    model = _hub_model()
    for budget in range(60, 3200, 37):
        block = build_collect_context_block(model, "pkg/hub.py", budget=budget)
        assert len(block) <= budget, f"block of {len(block)} chars exceeded budget {budget}"


def test_no_neighbourhood_row_is_split_mid_line_by_the_budget():
    """A cut row is a whole row — the budget never truncates one."""
    model = _hub_model()
    full = build_collect_context_block(model, "pkg/hub.py")
    forms = {
        _CALLERS_PREFIX: _row(full, _CALLERS_PREFIX),
        _CALLS_INTO_PREFIX: _row(full, _CALLS_INTO_PREFIX),
        _TESTS_PREFIX: _row(full, _TESTS_PREFIX),
    }
    for budget in range(60, 1500, 23):
        for line in build_collect_context_block(model, "pkg/hub.py", budget=budget).split("\n"):
            for prefix, whole in forms.items():
                if line.startswith(prefix):
                    assert line == whole, f"{line!r} is not the whole row {whole!r}"


def test_budget_none_still_renders_every_neighbourhood_row_in_full():
    model = _hub_model()
    assert build_collect_context_block(model, "pkg/hub.py", budget=None) == \
        build_collect_context_block(model, "pkg/hub.py")
    for prefix in (_CALLERS_PREFIX, _CALLS_INTO_PREFIX, _TESTS_PREFIX):
        assert _row(build_collect_context_block(model, "pkg/hub.py"), prefix)


def test_rendering_is_deterministic_across_two_independently_built_models():
    """COLLECT-3: two models built from the same tables agree byte for byte,
    so the pack a coder sees does not depend on iteration order."""
    a = _hub_model()
    b = _model(
        [_module(p) for p in reversed(HUB_GRAPH_MODULES)],
        import_edges=dict(a.import_edges),
        imported_by=dict(a.imported_by),
        test_map=dict(a.test_map),
        zero_coverage=list(a.zero_coverage_list),
        risk_index=tuple(reversed(a.risk_index)),
    )
    assert build_collect_context_block(a, "pkg/hub.py") == build_collect_context_block(b, "pkg/hub.py")


def test_a_row_that_raises_is_dropped_but_the_pack_survives(monkeypatch):
    """Fail-open house rule: one broken renderer degrades to no row, never to
    no block, and never into a run."""
    import tools.auto.context_assembler as ca

    model = _hub_model()

    def _boom(model_, target, remaining):
        raise RuntimeError("artifact is malformed")

    monkeypatch.setattr(
        ca,
        "_PACK_ROWS",
        (
            ("callers", _boom),
            ("calls_into", _row_calls_into),
            ("tests", _row_tests),
        ),
    )
    block = build_collect_context_block(model, "pkg/hub.py")
    assert block.startswith("COLLECT MODEL")
    assert _CALLS_INTO_PREFIX in block
    assert _TESTS_PREFIX in block
    assert _CALLERS_PREFIX not in block


# ── end to end: a real collect, real loader, real block ────────────────────


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    # Seed contracts and the gate map are not under test here; neutralising
    # them keeps the collected graph and test map deterministic.
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


@pytest.fixture
def neighbourhood_repo(tmp_path: Path) -> Path:
    """A repo whose graph is known by hand.

    Edges:   core <- mid <- leaf ;  core <- tests/test_core.py ;
             leaf <- tests/test_leaf.py ;  orphan is isolated.
    So `core` has one shipped importer and one covering test, `leaf` has
    imports but none but a test importer, and `orphan` is an entry point
    with zero coverage.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text("import os\n\ndef core_fn():\n    return 1\n", encoding="utf-8")
    (pkg / "mid.py").write_text(
        "from pkg.core import core_fn\n\ndef mid_fn():\n    return core_fn()\n", encoding="utf-8"
    )
    (pkg / "leaf.py").write_text(
        "from pkg.mid import mid_fn\n\ndef leaf_fn():\n    return mid_fn()\n", encoding="utf-8"
    )
    (pkg / "orphan.py").write_text("\ndef orphan_fn():\n    return 0\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_core.py").write_text(
        "from pkg.core import core_fn\n\ndef test_core():\n    assert core_fn() == 1\n",
        encoding="utf-8",
    )
    (tests / "test_leaf.py").write_text(
        "from pkg.leaf import leaf_fn\n\ndef test_leaf():\n    assert leaf_fn() == 1\n",
        encoding="utf-8",
    )

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    cli_mod.action_collect(tmp_path)
    return tmp_path


def test_a_collected_repo_renders_the_neighbourhood_rows_through_the_loader(neighbourhood_repo):
    """The rows must fire on data the producer actually writes, not only on
    hand-built tables — and every path must come out relative."""
    model = loader_mod.load(neighbourhood_repo)

    block = build_collect_context_block(model, "pkg/core.py")
    assert block.split("\n") == [
        "COLLECT MODEL (static facts, do not contradict):",
        "module: pkg/core.py",
        "callers: 2 modules import this (1 non-test): pkg/mid.py",
        "tests: 1 file: tests/test_core.py",
        "public_symbols: pkg/core.py:core_fn",
    ]
    # `core.py` imports only `os`, which the producer drops as stdlib, so the
    # row is absent rather than claiming an empty import list.
    assert _CALLS_INTO_PREFIX not in block
    assert _row_calls_into(model, "pkg/core.py", None) == ""


def test_a_collected_entry_point_renders_the_entry_point_form(neighbourhood_repo):
    """`pkg/orphan.py` has neither importer nor covering test: both absences
    are rendered as facts, and the block is not empty for lack of them."""
    model = loader_mod.load(neighbourhood_repo)

    block = build_collect_context_block(model, "pkg/orphan.py")
    assert f"callers: {_ENTRY_POINT_NOTE}" in block
    assert f"tests: {_NO_TESTS_NOTE}" in block
    assert "module: pkg/orphan.py" in block


def test_a_collected_leaf_with_only_test_importers_is_an_entry_point(neighbourhood_repo):
    """`pkg/leaf.py` is imported by exactly one test file: nothing shipped
    imports it, so it renders the entry-point form rather than naming its own
    test."""
    model = loader_mod.load(neighbourhood_repo)

    line = _row_callers(model, "pkg/leaf.py", None)
    assert line == _tests_only(1)
    assert "tests/test_leaf.py" not in line


# ── the ticket's acceptance numbers, against this repo's own tree ──────────


@pytest.fixture(scope="module")
def live_model() -> CollectModel:
    """This repo's graph and test map, built straight from the source tree.

    `.collect/` is gitignored and rebuilt per machine, so loading the artifact
    would make these AC tests skip on every fresh checkout — exactly where a
    regression would go unnoticed. The producer's own graph and test-map
    passes are cheap (a few seconds for ~500 modules) and need no LLM, so the
    model is assembled from them directly; `risk_index` is left empty, which
    only affects the order `callers_of` lists names in, not which names.
    """
    modules = scan_repo(REPO_ROOT)
    edges = import_edges(modules)
    reverse = reverse_index(edges)
    tmap = build_test_map(REPO_ROOT, modules)
    return CollectModel(
        status=STATUS_FRESH,
        modules=tuple(modules),
        import_edges={k: tuple(sorted(v)) for k, v in edges.items()},
        imported_by={k: tuple(sorted(v)) for k, v in reverse.items()},
        entry_points=tuple(entry_points(edges, reverse=reverse)),
        test_map=tmap,
        zero_coverage_list=tuple(zero_coverage(tmap)),
    )


def test_live_coder_py_says_its_total_and_names_its_non_test_importers(live_model):
    """AC: `tools/auto/coder.py` names its non-test importers and says 25.

    The ticket says 25 total and 7 non-test; the tree at V2 measures 25 and
    2. Both numbers drift as files come and go, so the assertion derives
    them from the model and pins what cannot drift: the total is every
    recorded importer, the parenthetical is the non-test subset, and the two
    production callers are named.
    """
    target = "tools/auto/coder.py"
    line = _row_callers(live_model, target, None)
    total = len(live_model.imported_by[target])
    non_test = live_model.callers_of(target)
    assert total >= 20, "coder.py should still be one of the most-imported modules"
    assert line.startswith(f"callers: {total} modules import this ({len(non_test)} non-test): ")
    assert "tools/auto/inner_loop.py" in line
    assert "tools/skills/loader.py" in line
    for caller in live_model.imported_by[target]:
        if caller.startswith("tests/"):
            assert caller not in line


def test_live_main_py_renders_the_entry_point_form(live_model):
    """AC: `main.py` renders the entry-point form. It is not in the tree's
    `entry_points` — its test files import it — so the row keys off "no
    shipped importer", which is the blast radius the fact pack serves, and
    keeps the test-importer count rather than claiming nothing imports it."""
    assert "main.py" not in live_model.entry_points
    importers = live_model.imported_by.get("main.py", ())
    assert importers, "main.py must have importers to be interesting"
    assert not live_model.callers_of("main.py")
    line = _row_callers(live_model, "main.py", None)
    assert line == _tests_only(len(importers))
    assert line in build_collect_context_block(live_model, "main.py").split("\n")


def test_live_true_entry_point_renders_the_plain_form(live_model):
    """A module nothing at all imports — test or shipped — gets the plain
    entry-point sentence, with no test count to report."""
    assert live_model.entry_points, "the tree should have standalone scripts"
    entry = next(p for p in live_model.entry_points if live_model.module(p) is not None)
    assert _row_callers(live_model, entry, None) == f"callers: {_ENTRY_POINT_NOTE}"


def test_live_zero_coverage_module_renders_the_no_tests_form(live_model):
    """AC: a `zero_coverage` module renders the no-tests form, and a covered
    one renders the count plus names instead."""
    zero = live_model.zero_coverage()
    assert zero, "the tree should carry a zero-coverage worklist"
    uncovered = zero[0]
    assert _row_tests(live_model, uncovered, None) == f"tests: {_NO_TESTS_NOTE}"

    covered = next(p for p, tests in live_model.test_map.items() if tests and p != uncovered)
    line = _row_tests(live_model, covered, None)
    count = len(live_model.test_map[covered])
    assert line.startswith(f"tests: {count} {'file' if count == 1 else 'files'}: ")
    assert _NO_TESTS_NOTE not in line


def test_live_module_absent_from_the_model_yields_no_block(live_model):
    """AC: a module absent from the model still yields `""` overall."""
    assert build_collect_context_block(live_model, "definitely/not/a/module.py") == ""
