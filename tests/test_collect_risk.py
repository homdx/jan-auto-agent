"""tests/test_collect_risk.py — COLLECT-13.

* A module with no tests and a large blast-radius ranks above a small,
  well-covered module.
* The score is a deterministic pure function of its inputs (same inputs,
  same output, repeated runs) — COLLECT-3.
* Zero coverage contributes the largest single bonus (dominates unless
  another module is comparably loaded on every other axis).
* An undocumented fail-open site outranks a documented one, all else equal.
"""

from pathlib import Path

from tools.collect.model import GuardedAccess, ModuleRecord
from tools.collect.registries import FailOpenEntry
from tools.collect.risk import compute_risk_index

REPO_ROOT = Path(__file__).parent.parent


def _module(path, guarded_accesses=()):
    return ModuleRecord(path=path, guarded_accesses=tuple(guarded_accesses))


# ── ranking: uncovered + high blast-radius beats small + covered ────────


def test_uncovered_high_blast_radius_module_ranks_above_small_covered_one():
    modules = [
        _module("pkg/big_risky.py"),
        _module("pkg/small_safe.py"),
    ]
    imported_by = {
        "pkg/big_risky.py": frozenset({"a.py", "b.py", "c.py", "d.py"}),
        "pkg/small_safe.py": frozenset(),
    }
    test_map = {
        "pkg/big_risky.py": (),  # zero coverage
        "pkg/small_safe.py": ("tests/test_small_safe.py",),
    }
    index = compute_risk_index(
        modules, imported_by=imported_by, test_map=test_map
    )
    ranked_paths = [e.path for e in index]
    assert ranked_paths.index("pkg/big_risky.py") < ranked_paths.index(
        "pkg/small_safe.py"
    )
    big = next(e for e in index if e.path == "pkg/big_risky.py")
    assert big.zero_coverage is True
    assert big.blast_radius == 4


# ── determinism ───────────────────────────────────────────────────────────


def test_score_is_deterministic_across_repeated_runs():
    modules = [_module("pkg/a.py"), _module("pkg/b.py")]
    imported_by = {"pkg/a.py": frozenset({"pkg/b.py"}), "pkg/b.py": frozenset()}
    test_map = {"pkg/a.py": (), "pkg/b.py": ("tests/test_b.py",)}

    first = compute_risk_index(modules, imported_by=imported_by, test_map=test_map)
    second = compute_risk_index(modules, imported_by=imported_by, test_map=test_map)
    assert [e.to_dict() for e in first] == [e.to_dict() for e in second]


def test_index_sorted_by_score_desc_then_path_ascending_tiebreak():
    modules = [_module("pkg/z.py"), _module("pkg/a.py")]
    # identical inputs on both -> identical score -> tie broken by path.
    imported_by = {"pkg/z.py": frozenset(), "pkg/a.py": frozenset()}
    test_map = {"pkg/z.py": (), "pkg/a.py": ()}
    index = compute_risk_index(modules, imported_by=imported_by, test_map=test_map)
    assert index[0].score == index[1].score
    assert [e.path for e in index] == ["pkg/a.py", "pkg/z.py"]


# ── zero coverage bonus dominates ─────────────────────────────────────────


def test_zero_coverage_bonus_outranks_a_handful_of_unguarded_sites():
    guarded_module = ModuleRecord(
        path="pkg/many_unguarded.py",
        guarded_accesses=(
            GuardedAccess(location="pkg/many_unguarded.py:1", access="x[0]", status="UNGUARDED"),
            GuardedAccess(location="pkg/many_unguarded.py:2", access="x[1]", status="UNGUARDED"),
        ),
    )
    zero_cov_module = ModuleRecord(path="pkg/untested.py")
    modules = [guarded_module, zero_cov_module]
    imported_by = {"pkg/many_unguarded.py": frozenset(), "pkg/untested.py": frozenset()}
    test_map = {
        "pkg/many_unguarded.py": ("tests/test_many_unguarded.py",),
        "pkg/untested.py": (),
    }
    index = compute_risk_index(modules, imported_by=imported_by, test_map=test_map)
    by_path = {e.path: e for e in index}
    assert by_path["pkg/untested.py"].score > by_path["pkg/many_unguarded.py"].score


# ── undocumented fail-open outranks documented ────────────────────────────


def test_undocumented_fail_open_outranks_documented_fail_open():
    modules = [_module("pkg/silent.py"), _module("pkg/documented.py")]
    imported_by = {"pkg/silent.py": frozenset(), "pkg/documented.py": frozenset()}
    test_map = {"pkg/silent.py": ("t.py",), "pkg/documented.py": ("t.py",)}
    registry = [
        FailOpenEntry(location="pkg/silent.py:10", exception_type="Exception", rationale=None),
        FailOpenEntry(
            location="pkg/documented.py:10",
            exception_type="Exception",
            rationale="deliberately silent, see #123",
        ),
    ]
    index = compute_risk_index(
        modules,
        imported_by=imported_by,
        fail_open_registry=registry,
        test_map=test_map,
    )
    by_path = {e.path: e for e in index}
    assert by_path["pkg/silent.py"].undocumented_fail_open_count == 1
    assert by_path["pkg/documented.py"].undocumented_fail_open_count == 0
    assert by_path["pkg/silent.py"].score > by_path["pkg/documented.py"].score


# ── defaults / totality ───────────────────────────────────────────────────


def test_missing_test_map_never_flags_zero_coverage():
    modules = [_module("pkg/a.py")]
    index = compute_risk_index(modules, imported_by={"pkg/a.py": frozenset()})
    assert index[0].zero_coverage is False


def test_index_covers_every_module_even_with_no_edges_or_registry():
    modules = [_module("pkg/a.py"), _module("pkg/b.py")]
    index = compute_risk_index(modules, imported_by={})
    assert {e.path for e in index} == {"pkg/a.py", "pkg/b.py"}
    for e in index:
        assert e.blast_radius == 0


# ── real repo smoke test ──────────────────────────────────────────────────


def test_real_repo_index_is_sorted_and_total():
    from tools.collect.graph import import_edges, imported_by as reverse_index
    from tools.collect.registries import build_fail_open_registry
    from tools.collect.scanner import scan_repo
    from tools.collect.test_map import build_test_map

    modules = scan_repo(REPO_ROOT)
    edges = import_edges(modules)
    reverse = reverse_index(edges)
    registry = build_fail_open_registry(modules, root=REPO_ROOT)
    tmap = build_test_map(REPO_ROOT, modules)

    index = compute_risk_index(
        modules,
        imported_by=reverse,
        fail_open_registry=registry,
        test_map=tmap,
        root=REPO_ROOT,
    )
    assert {e.path for e in index} == {m.path for m in modules}
    scores = [e.score for e in index]
    assert scores == sorted(scores, reverse=True)


def test_loc_degrades_to_zero_on_undecodable_source_instead_of_raising(tmp_path):
    """BUGFIX regression: `_loc` used to catch only `OSError`, so a
    module whose file on disk isn't valid UTF-8 (e.g. it changed between
    the scan and this risk-index pass) raised a bare `UnicodeDecodeError`
    out of `compute_risk_index` — contradicting `_loc`'s own docstring
    ("degrades to 'no size signal' rather than raising and taking down
    the whole index")."""
    (tmp_path / "bad.py").write_bytes(b"x = 1\n# not valid utf-8: \xff\xfe\n")
    modules = [_module("bad.py")]
    index = compute_risk_index(modules, imported_by={}, root=tmp_path)
    assert index[0].loc == 0
