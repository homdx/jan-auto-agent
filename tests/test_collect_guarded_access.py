"""tests/test_collect_guarded_access.py — COLLECT-7.

`extract_guarded_accesses` does a local, per-function dataflow check on
every indexed access (`x[i]`/`x[-1]`/`x[0]`): is there a preceding
early-exit guard that makes the index provably safe? This is the second
false-positive killer (after COLLECT-6's except classifier) — the AC
requires that none of the *real*, already-refuted false positives from the
"78 bugs" report get flagged UNGUARDED, so this suite pins both the
mini-repo toy cases and the real `prompt_store.py`/`view_trace.py` sites
by name.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Set, Tuple

from tools.collect.dataflow import extract_guarded_accesses
from tools.collect.model import Provenance

MINI_REPO = Path(__file__).parent / "fixtures" / "collect_mini_repo" / "pkg"
REPO_ROOT = Path(__file__).parent.parent


def _accesses(source: str, module_path: str):
    tree = ast.parse(source, filename=module_path)
    return extract_guarded_accesses(tree, module_path)


# ── mini-repo canonical cases (spec's three named examples) ────────────────


def test_prompt_store_get_current_stack_is_guarded():
    source = (MINI_REPO / "prompt_store.py").read_text(encoding="utf-8")
    accesses = _accesses(source, "pkg/prompt_store.py")
    assert len(accesses) == 1
    site = accesses[0]
    assert site.access == "stack[-1]"
    assert site.status == "GUARDED"
    assert site.guard  # non-empty guard description
    assert site.provenance == Provenance.STATIC


def test_view_trace_find_trace_file_candidates_is_guarded():
    source = (MINI_REPO / "view_trace.py").read_text(encoding="utf-8")
    accesses = _accesses(source, "pkg/view_trace.py")
    assert len(accesses) == 1
    site = accesses[0]
    assert site.access == "candidates[-1]"
    assert site.status == "GUARDED"
    assert site.guard


def test_unguarded_last_item_is_flagged():
    source = (MINI_REPO / "unguarded.py").read_text(encoding="utf-8")
    accesses = _accesses(source, "pkg/unguarded.py")
    assert len(accesses) == 1
    site = accesses[0]
    assert site.access == "items[-1]"
    assert site.status == "UNGUARDED"
    assert site.guard is None


# ── real-repo reference sites: the already-refuted false positives ─────────


def test_real_prompt_store_get_current_is_guarded_via_alias():
    # Real `tools/prompt_store.py::get_current`: the guard checks
    # `entry`/`entry.get("stack")`, and `stack = entry["stack"]` aliases
    # the checked key — `stack[-1]` must still resolve to GUARDED.
    source = (REPO_ROOT / "tools" / "prompt_store.py").read_text(encoding="utf-8")
    accesses = _accesses(source, "tools/prompt_store.py")
    by_access = {(a.location, a.access): a for a in accesses}
    site = by_access[("tools/prompt_store.py:79", "stack[-1]")]
    assert site.status == "GUARDED"
    assert site.guard


def test_real_view_trace_find_trace_file_is_guarded_via_sys_exit():
    # Real `tools/auto/view_trace.py::find_trace_file`: the guard body ends
    # in `sys.exit(...)`, not `return` — must still count as terminating.
    source = (REPO_ROOT / "tools" / "auto" / "view_trace.py").read_text(encoding="utf-8")
    accesses = _accesses(source, "tools/auto/view_trace.py")
    by_access = {(a.location, a.access): a for a in accesses}
    site = by_access[("tools/auto/view_trace.py:176", "candidates[-1]")]
    assert site.status == "GUARDED"
    assert site.guard


# ── guard-shape coverage ────────────────────────────────────────────────────


def test_if_x_is_none_guards_x():
    source = (
        "def f(x):\n"
        "    if x is None:\n"
        "        return None\n"
        "    return x[0]\n"
    )
    accesses = _accesses(source, "m.py")
    assert accesses[0].status == "GUARDED"


def test_guard_scoped_to_its_own_function_only():
    # A guard in one function must never cover an access in a different,
    # unrelated function — dataflow is per-function, not module-wide.
    source = (
        "def guarded(x):\n"
        "    if not x:\n"
        "        return None\n"
        "    return x[-1]\n"
        "\n"
        "def unguarded(y):\n"
        "    return y[-1]\n"
    )
    accesses = _accesses(source, "m.py")
    by_line = {a.location: a for a in accesses}
    assert by_line["m.py:4"].status == "GUARDED"
    assert by_line["m.py:7"].status == "UNGUARDED"


def test_guard_inside_if_branch_does_not_leak_to_else_branch():
    # The guard only holds on the path where it actually executed; the
    # access in the else-branch must not inherit it.
    source = (
        "def f(x, flag):\n"
        "    if flag:\n"
        "        if not x:\n"
        "            return None\n"
        "        return x[-1]\n"
        "    else:\n"
        "        return x[-1]\n"
    )
    accesses = _accesses(source, "m.py")
    by_line = {a.location: a for a in accesses}
    assert by_line["m.py:5"].status == "GUARDED"
    assert by_line["m.py:7"].status == "UNGUARDED"


def test_slice_access_is_not_recorded():
    # `x[a:b]` is a slice, not an indexed access — out of COLLECT-7 scope.
    source = "def f(x):\n    return x[1:3]\n"
    accesses = _accesses(source, "m.py")
    assert accesses == []


def test_variable_index_access_is_recorded():
    source = (
        "def f(items, i):\n"
        "    if not items:\n"
        "        return None\n"
        "    return items[i]\n"
    )
    accesses = _accesses(source, "m.py")
    assert accesses[0].access == "items[i]"
    assert accesses[0].status == "GUARDED"


# ── no-duplicate-record regression ──────────────────────────────────────────
#
# `_record_accesses_in_stmt` used to be called with a full `ast.walk(stmt)`
# even when `stmt` was itself a block-owning statement (`if`/`for`/`while`/
# `with`/`try`) — so every access nested inside such a block got recorded
# twice: once prematurely, by that full walk, *before* any guard introduced
# inside the block had been applied (always UNGUARDED), and once more,
# correctly, by the recursive per-statement re-walk that runs right after.
# When the guarded block sits inside an unrelated outer block (exactly
# `view_trace.find_trace_file`'s shape: `if p.is_dir(): ... if not
# candidates: sys.exit() ... return candidates[-1]`), those two records
# don't just duplicate each other — they *contradict* each other: one
# GUARDED, one UNGUARDED, for the identical site. `test_real_view_trace_
# find_trace_file_is_guarded_via_sys_exit` above didn't catch this because
# it looks the site up through a `{(location, access): a for a in
# accesses}` dict comprehension, which silently keeps only the
# last-inserted (correct) entry and discards the earlier (spurious) one.
# These tests check the raw list directly instead, so a reappearance of
# either the duplication or the contradiction fails immediately.


def test_no_duplicate_or_conflicting_records_anywhere_in_module():
    # Minimal reproduction of the exact shape that produced a real
    # GUARDED/UNGUARDED contradiction: a guard-and-use pair nested inside
    # an unrelated outer `if` whose own test has nothing to do with the
    # guard.
    source = (
        "def f(p, candidates_src):\n"
        "    if p:\n"
        "        candidates = candidates_src()\n"
        "        if not candidates:\n"
        "            return None\n"
        "        return candidates[-1]\n"
        "    return None\n"
    )
    accesses = _accesses(source, "m.py")
    by_key = {}
    for a in accesses:
        key = (a.location, a.access)
        assert key not in by_key, f"{key} recorded more than once: {by_key[key]!r} and {a!r}"
        by_key[key] = a
    assert by_key[("m.py:6", "candidates[-1]")].status == "GUARDED"


def test_real_view_trace_site_appears_exactly_once():
    source = (REPO_ROOT / "tools" / "auto" / "view_trace.py").read_text(encoding="utf-8")
    accesses = _accesses(source, "tools/auto/view_trace.py")
    matches = [a for a in accesses if (a.location, a.access) == ("tools/auto/view_trace.py:176", "candidates[-1]")]
    assert len(matches) == 1, f"expected exactly one record, got {matches!r}"
    assert matches[0].status == "GUARDED"


def test_real_analyze_logs_done_site_appears_exactly_once_and_is_guarded():
    # A second real instance of the same shape, found while regression-
    # testing the fix above: `done[-1]` at analyze_logs.py:1251, guarded by
    # `if not done: return ...` at line 1249, nested inside an unrelated
    # outer block.
    source = (REPO_ROOT / "analyze_logs.py").read_text(encoding="utf-8")
    accesses = _accesses(source, "analyze_logs.py")
    matches = [a for a in accesses if (a.location, a.access) == ("analyze_logs.py:1251", "done[-1]")]
    assert len(matches) == 1, f"expected exactly one record, got {matches!r}"
    assert matches[0].status == "GUARDED"


def test_no_conflicting_status_for_same_site_across_whole_real_repo():
    # Repo-wide defense in depth: whatever the exact duplicate count, no
    # (location, access) site may ever carry both GUARDED and UNGUARDED at
    # once — that directly contradicts the anti-hallucination guarantee
    # this epic exists for.
    from tools.collect.scanner import scan_repo

    modules = scan_repo(REPO_ROOT)
    conflicts = []
    for m in modules:
        by_key: Dict[Tuple[str, str], Set[str]] = {}
        for a in m.guarded_accesses:
            by_key.setdefault((a.location, a.access), set()).add(a.status)
        conflicts.extend(k for k, statuses in by_key.items() if len(statuses) > 1)
    assert conflicts == []


def test_guard_inside_unrelated_for_loop_is_not_duplicated():
    source = (
        "def f(seq, candidates_src):\n"
        "    for item in seq:\n"
        "        candidates = candidates_src(item)\n"
        "        if not candidates:\n"
        "            continue\n"
        "        use(candidates[-1])\n"
    )
    accesses = _accesses(source, "m.py")
    matches = [a for a in accesses if a.access == "candidates[-1]"]
    assert len(matches) == 1
    assert matches[0].status == "GUARDED"


def test_guard_inside_unrelated_try_block_is_not_duplicated():
    source = (
        "def f(flag, candidates_src):\n"
        "    try:\n"
        "        candidates = candidates_src()\n"
        "        if not candidates:\n"
        "            raise ValueError()\n"
        "        return candidates[-1]\n"
        "    except ValueError:\n"
        "        return None\n"
    )
    accesses = _accesses(source, "m.py")
    matches = [a for a in accesses if a.access == "candidates[-1]"]
    assert len(matches) == 1
    assert matches[0].status == "GUARDED"


# ── determinism / provenance ────────────────────────────────────────────────


def test_all_accesses_are_provenance_static():
    source = (MINI_REPO / "prompt_store.py").read_text(encoding="utf-8")
    accesses = _accesses(source, "pkg/prompt_store.py")
    assert all(a.provenance == Provenance.STATIC for a in accesses)


def test_accesses_sorted_by_line_then_access():
    source = (
        "def f(a, b):\n"
        "    x = a[-1]\n"
        "    y = b[-1]\n"
    )
    accesses = _accesses(source, "m.py")
    linenos = [int(a.location.rsplit(':', 1)[-1]) for a in accesses]
    assert linenos == sorted(linenos)


# ── regression: non-numeric constant under unary minus ─────────────────────
#
# `x[-<constant>]` is syntactically valid for *any* constant, not just
# numbers — `x[-"a"]`, `x[-None]`, `x[-b"a"]` all parse fine even though
# they'd raise TypeError if actually executed. Since collect only parses
# (never executes) the scanned repo's source, a file containing one of
# these was enough to crash the whole scan: `_subscript_key` unconditionally
# computed `-slice_node.operand.value`, so on a non-numeric constant that
# raised TypeError with nothing upstream (`scan_module` only catches
# SyntaxError) to stop it taking down the entire collect pass.


def test_scan_module_survives_negated_string_subscript():
    from tools.collect.scanner import scan_module

    source = "def f(x):\n    return x[-\"a\"]\n"
    record = scan_module(source, "weird.py")  # must not raise
    assert record.parse_error is None
    assert len(record.guarded_accesses) == 1
    assert record.guarded_accesses[0].status == "UNGUARDED"


def test_scan_module_survives_negated_none_and_bytes_subscript():
    from tools.collect.scanner import scan_module

    for literal in ('None', 'b"a"'):
        source = f"def f(x):\n    return x[-{literal}]\n"
        record = scan_module(source, "weird.py")  # must not raise
        assert record.parse_error is None
        assert len(record.guarded_accesses) == 1


def test_negated_numeric_subscript_still_works():
    source = "def f(x):\n    return x[-1] + x[-1.5]\n"
    accesses = _accesses(source, "m.py")
    reprs = {a.access for a in accesses}
    assert reprs == {"x[-1]", "x[-1.5]"}
