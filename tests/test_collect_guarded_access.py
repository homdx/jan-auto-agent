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

import pytest

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
    # Real get_current: stack = entry["stack"] aliases the checked key, so stack[-1] is GUARDED.
    source = (REPO_ROOT / "tools" / "prompt_store.py").read_text(encoding="utf-8")
    accesses = _accesses(source, "tools/prompt_store.py")
    by_access = {(a.location, a.access): a for a in accesses}
    # Line number follows tools/prompt_store.py::get_current; update it if that method moves.
    site = by_access[("tools/prompt_store.py:78", "stack[-1]")]
    assert site.status == "GUARDED"
    assert site.guard


def test_real_view_trace_find_trace_file_is_guarded_via_sys_exit():
    # view_trace.find_trace_file ends its guard in sys.exit, which still counts as terminating.
    source = (REPO_ROOT / "tools" / "auto" / "view_trace.py").read_text(encoding="utf-8")
    accesses = _accesses(source, "tools/auto/view_trace.py")
    by_access = {(a.location, a.access): a for a in accesses}
    site = by_access[("tools/auto/view_trace.py:182", "candidates[-1]")]
    assert site.status == "GUARDED"
    assert site.guard


# ── reassignment invalidates a stale guard ──────────────────────────────────


def test_reassigned_name_loses_its_guard():
    # Regression: rebinding a checked name drops its guard, so a stale GUARDED cannot suppress a real crash claim.
    source = (
        "def f(entry):\n"
        "    if not entry or not entry.get('queue'):\n"
        "        return None\n"
        "    queue = entry['queue']\n"
        "    queue = refresh(queue)\n"
        "    return queue[-1]\n"
    )
    accesses = _accesses(source, "pkg/reassign.py")
    by_access = {(a.location, a.access): a for a in accesses}
    site = by_access[("pkg/reassign.py:6", "queue[-1]")]
    assert site.status == "UNGUARDED"
    assert site.guard is None


def test_reassigned_name_end_to_end_survives_verification():
    # End to end: a crash claim on a reassigned access survives verification (not dropped as contradicts-guard).
    from tools.collect.scanner import scan_module
    from tools.collect.verifier import extract_claims, verify_claims

    mod_path = "pkg/reassign.py"
    source = (
        "def f(entry):\n"
        "    if not entry or not entry.get('queue'):\n"
        "        return None\n"
        "    queue = entry['queue']\n"
        "    queue = refresh(queue)\n"
        "    return queue[-1]\n"
    )
    module = scan_module(source, mod_path)
    known_symbols = frozenset(s.qualname for s in module.public_symbols)

    claims = extract_claims(
        "queue[-1] crashes with IndexError when refresh(queue) returns an empty list.",
        mod_path, known_symbols,
    )
    kept, dropped = verify_claims(
        claims, module=module, known_symbols=known_symbols,
        line_counts={mod_path: source.count("\n") + 1}, fail_open_locs=frozenset(),
    )
    assert dropped == []
    assert len(kept) == 1


# ── guard-shape coverage ────────────────────────────────────────────────────


def test_if_x_is_none_does_not_guard_subscript_access():
    # x is None proves non-None, not non-empty: x[0] is still unguarded; not x and len(x) == 0 still guard (bug fix).
    source = (
        "def f(x):\n"
        "    if x is None:\n"
        "        return None\n"
        "    return x[0]\n"
    )
    accesses = _accesses(source, "m.py")
    assert accesses[0].status == "UNGUARDED"


def test_guard_scoped_to_its_own_function_only():
    # A guard covers only its own function, since dataflow is per function.
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
    # A guard holds only on the path where it ran, so the else branch does not inherit it.
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


# Regression: a block-owning statement's accesses were recorded twice, once before the guard, causing a GUARDED/UNGUARDED conflict.


def test_no_duplicate_or_conflicting_records_anywhere_in_module():
    # Minimal reproduction of the duplicate-and-conflict shape: a guard inside an unrelated outer if.
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
    matches = [a for a in accesses if (a.location, a.access) == ("tools/auto/view_trace.py:182", "candidates[-1]")]
    assert len(matches) == 1, f"expected exactly one record, got {matches!r}"
    assert matches[0].status == "GUARDED"


def test_real_analyze_logs_done_site_appears_exactly_once_and_is_guarded():
    # Real analyze_logs.py site: the guard/access pair must be recorded exactly once (the line pin is updated as the file grows).
    source = (REPO_ROOT / "analyze_logs.py").read_text(encoding="utf-8")
    accesses = _accesses(source, "analyze_logs.py")
    matches = [a for a in accesses if (a.location, a.access) == ("analyze_logs.py:1695", "done[-1]")]
    assert len(matches) == 1, f"expected exactly one record, got {matches!r}"
    assert matches[0].status == "GUARDED"


def test_no_conflicting_status_for_same_site_across_whole_real_repo():
    # No site may carry both GUARDED and UNGUARDED anywhere in the real repo.
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


# Regression: a non-numeric constant under unary minus must not crash the scan (parse-only, never executed).


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


# Regression: an and-combined falsy guard only proves "a or b truthy", never "a truthy" (COLLECT-7).


def test_and_combined_falsy_guard_does_not_mark_either_name_guarded():
    source = (
        "def f(a, b):\n"
        "    if not a and not b:\n"
        "        raise ValueError('both missing')\n"
        "    return a[0]\n"
    )
    accesses = _accesses(source, "m.py")
    assert len(accesses) == 1
    assert accesses[0].access == "a[0]"
    assert accesses[0].status == "UNGUARDED"
    assert accesses[0].guard is None


def test_and_combined_falsy_guard_actually_unsafe_at_runtime():
    # Ground truth: a=[] and b=[1] pass the guard but a[0] still raises, so UNGUARDED is correct.
    def f(a, b):
        if not a and not b:
            raise ValueError("both missing")
        return a[0]

    with pytest.raises(IndexError):
        f([], [1])


def test_or_combined_falsy_guard_still_marks_both_names_guarded():
    # Must not regress: "not a or not b" guarantees both names truthy after the raise.
    source = (
        "def f(a, b):\n"
        "    if not a or not b:\n"
        "        raise ValueError('missing')\n"
        "    return a[0], b[0]\n"
    )
    accesses = _accesses(source, "m.py")
    assert {a.access: a.status for a in accesses} == {
        "a[0]": "GUARDED",
        "b[0]": "GUARDED",
    }


def test_and_nested_inside_or_does_not_leak_names_out():
    # Names under a nested and inside or are not guaranteed: no leak out of the nested expression.
    source = (
        "def f(a, b, c):\n"
        "    if not a or (not b and not c):\n"
        "        raise ValueError('missing')\n"
        "    return a[0], b[0], c[0]\n"
    )
    accesses = _accesses(source, "m.py")
    assert {a.access: a.status for a in accesses} == {
        "a[0]": "GUARDED",
        "b[0]": "UNGUARDED",
        "c[0]": "UNGUARDED",
    }


# Regression: x.get(k, truthy_default) does not guard k, since the default skips the guard body.


def test_get_with_truthy_default_not_guarded():
    """`if not x.get(k, 'default'): return` does not prove k is present
    when the default is truthy — the guard is False when k is missing, so
    the body doesn't run and k is still absent. The aliased access must be
    UNGUARDED, not GUARDED."""
    source = (
        'def f(x):\n'
        '    if not x.get("key", "default"):\n'
        '        return None\n'
        '    y = x["key"]\n'
        '    return y[-1]\n'
    )
    accesses = _accesses(source, "m.py")
    y_access = [a for a in accesses if a.access == "y[-1]"]
    assert y_access, "y[-1] access not found"
    assert y_access[0].status == "UNGUARDED", (
        "truthy default does not guard k — y[-1] must be UNGUARDED"
    )


def test_get_with_truthy_default_one_not_guarded():
    """Same as above but with integer 1 as the truthy default."""
    source = (
        'def f(x):\n'
        '    if not x.get("key", 1):\n'
        '        return None\n'
        '    y = x["key"]\n'
        '    return y[-1]\n'
    )
    accesses = _accesses(source, "m.py")
    y_access = [a for a in accesses if a.access == "y[-1]"]
    assert y_access, "y[-1] access not found"
    assert y_access[0].status == "UNGUARDED"


def test_get_with_falsy_default_still_guarded():
    """Regression guard: a falsy default (None, 0, False, '') still
    produces a GUARDED access — the guard fires when k is missing."""
    for default_expr in ("None", "0", "False", '""'):
        source = (
            f'def f(x):\n'
            f'    if not x.get("key", {default_expr}):\n'
            f'        return None\n'
            f'    y = x["key"]\n'
            f'    return y[-1]\n'
        )
        accesses = _accesses(source, "m.py")
        y_access = [a for a in accesses if a.access == "y[-1]"]
        assert y_access, f"y[-1] not found for default={default_expr!r}"
        assert y_access[0].status == "GUARDED", (
            f"falsy default {default_expr!r} should still guard k — "
            f"y[-1] must be GUARDED, got {y_access[0].status}"
        )


def test_get_no_default_still_guarded():
    """Regression guard: no default at all (x.get(k)) — the guard fires
    when k is missing (get returns None, which is falsy)."""
    source = (
        'def f(x):\n'
        '    if not x.get("key"):\n'
        '        return None\n'
        '    y = x["key"]\n'
        '    return y[-1]\n'
    )
    accesses = _accesses(source, "m.py")
    y_access = [a for a in accesses if a.access == "y[-1]"]
    assert y_access, "y[-1] access not found"
    assert y_access[0].status == "GUARDED"


def test_get_with_non_constant_default_not_guarded():
    """A non-constant default (x.get(k, func())) can't be evaluated
    statically — conservatively not guarded (a missed guard is acceptable,
    a wrongly-kept one is not)."""
    source = (
        'def f(x):\n'
        '    if not x.get("key", make_default()):\n'
        '        return None\n'
        '    y = x["key"]\n'
        '    return y[-1]\n'
    )
    accesses = _accesses(source, "m.py")
    y_access = [a for a in accesses if a.access == "y[-1]"]
    assert y_access, "y[-1] access not found"
    assert y_access[0].status == "UNGUARDED"


def test_mutating_pop_after_guard_invalidates_it():
    # Regression: stack.pop() after the guard can empty it again; the guard is invalidated.
    source = (
        "def rollback(stack):\n"
        "    if not stack:\n"
        "        return None\n"
        "    stack.pop()\n"
        "    return stack[-1]\n"
    )
    accesses = _accesses(source, "m.py")
    assert {a.access: a.status for a in accesses} == {"stack[-1]": "UNGUARDED"}


def test_del_after_guard_invalidates_it():
    source = (
        "def g(d, keys):\n"
        "    if not d:\n"
        "        return None\n"
        "    del d[keys[0]]\n"
        "    return d[keys[0]]\n"
    )
    accesses = _accesses(source, "m.py")
    statuses = {a.access: a.status for a in accesses}
    assert statuses["d[keys[0]]"] == "UNGUARDED"


def test_pop_before_guard_is_unaffected():
    # Sanity: a mutation before the guard invalidates nothing, and the guard applies after it.
    source = (
        "def f(stack):\n"
        "    stack.append(1)\n"
        "    if not stack:\n"
        "        return None\n"
        "    return stack[-1]\n"
    )
    accesses = _accesses(source, "m.py")
    assert {a.access: a.status for a in accesses} == {"stack[-1]": "GUARDED"}


def test_pairs_only_guard_alias_does_not_crash_and_is_guarded():
    # Regression: a pairs-only guard must not crash the run; its alias is recorded with a guard description.
    source = (
        "def rollback(entry):\n"
        "    if not entry.get('stack'):\n"
        "        return None\n"
        "    stack = entry['stack']\n"
        "    return stack[-1]\n"
    )
    accesses = _accesses(source, "m.py")  # must not raise
    by_access = {a.access: a for a in accesses}
    assert by_access["stack[-1]"].status == "GUARDED"
    assert by_access["stack[-1]"].guard is not None


def test_pairs_and_names_combined_guard_still_works():
    # The real get_current shape (bare-name and pairs checks in one or) must keep working.
    source = (
        "def get_current(entry):\n"
        "    if not entry or not entry.get('stack'):\n"
        "        return None\n"
        "    stack = entry['stack']\n"
        "    return stack[-1]\n"
    )
    accesses = _accesses(source, "m.py")
    by_access = {a.access: a for a in accesses}
    assert by_access["stack[-1]"].status == "GUARDED"
    assert by_access["stack[-1]"].guard == "early-return at m.py:2"
