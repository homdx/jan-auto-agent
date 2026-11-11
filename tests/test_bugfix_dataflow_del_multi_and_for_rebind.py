"""tests/test_bugfix_dataflow_del_multi_and_for_rebind.py — BUGFIX
(audit): gaps in dataflow.py's guard-invalidation coverage.

1. `del a[0], b[0]` only invalidated a guard on `a` — `_mutated_name_in_stmt`
   returned on the FIRST subscript target found in a multi-target
   `ast.Delete`, so `b` was never processed.

2. A name reused as a `for` loop's target rebinds it exactly like a bare
   `x = <expr>` assignment does, but neither `_invalidate_reassigned` nor
   `_rebound_names_in` recognized `ast.For`/`ast.AsyncFor` at all — only
   `ast.Assign` invalidated a guard.

3. (AGNES-14) A plain name's deletion — `del a`, `del a, b`, `del (a, b)`
   — removed the name outright, yet the guard on it survived, so a later
   `a[-1]` was reported GUARDED even though it would raise. `_mutated_name_in_stmt`
   only ever returned the containers of *subscript* targets, so no plain
   (packed or not) delete target invalidated anything. An attribute delete
   (`del x.a`) removes only `a`, not `x`, so it correctly leaves the guard
   on `x` alone.

All three left a guard wrongly marked GUARDED (status="GUARDED") for an
access that can genuinely crash, which COLLECT-17's contradiction_check
trusts unconditionally — worse than a missed guard, per this module's
own docstring.
"""

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.collect.dataflow import extract_guarded_accesses


def _last_status(src: str) -> str:
    accesses = extract_guarded_accesses(ast.parse(src), "t.py")
    assert accesses, "expected at least one indexed access to be found"
    return accesses[-1].status


class TestDeleteMultiTargetInvalidatesAll:
    def test_del_second_target_invalidates_its_guard(self):
        src = """
def f(a, b):
    if not a or not b: return None
    del a[0], b[0]
    return b[-1]
"""
        assert _last_status(src) != "GUARDED"

    def test_del_first_target_still_invalidates_its_guard(self):
        """Regression guard: the fix must not have broken the
        already-working first-target case while fixing the second."""
        src = """
def f(a, b):
    if not a or not b: return None
    del a[0], b[0]
    return a[-1]
"""
        assert _last_status(src) != "GUARDED"


class TestForLoopTargetInvalidatesGuard:
    def test_reused_name_as_loop_var_invalidates_guard(self):
        src = """
def f(items, x):
    if not x: return None
    for x in items:
        pass
    return x[-1]
"""
        assert _last_status(src) != "GUARDED"

    def test_unrelated_loop_var_does_not_invalidate_other_guards(self):
        """Regression guard: a for-loop over an unrelated name must not
        invalidate a guard on a name it doesn't touch."""
        src = """
def f(items, x):
    if not x: return None
    for y in items:
        pass
    return x[-1]
"""
        assert _last_status(src) == "GUARDED"


class TestPlainNameDeleteInvalidatesGuard:
    """`del a`, `del a, b`, `del (a, b)` remove the names outright, so a
    later indexed access on a deleted name must no longer cite the earlier
    guard. The delete handling now returns every deleted plain name (packed
    in a target list or tuple or not) at once."""

    def test_del_multi_plain_names_invalidates_guard(self):
        src = """
def f(a, b):
    if not a or not b: return None
    del a, b
    return a[-1]
"""
        assert _last_status(src) != "GUARDED"

    def test_del_tuple_plain_names_invalidates_guard(self):
        src = """
def f(a, b):
    if not a or not b: return None
    del (a, b)
    return a[-1]
"""
        assert _last_status(src) != "GUARDED"

    def test_del_single_plain_name_invalidates_guard(self):
        src = """
def f(a, b):
    if not a or not b: return None
    del a
    return a[-1]
"""
        assert _last_status(src) != "GUARDED"

    def test_del_inside_nested_loop_body_invalidates_guard(self):
        """A delete buried in a nested block's body must still invalidate
        the outer guard for what follows the block."""
        src = """
def f(a, items):
    if not a: return None
    for y in items:
        del a
    return a[-1]
"""
        assert _last_status(src) != "GUARDED"

    def test_del_attribute_does_not_invalidate_receiver_guard(self):
        """Regression guard: `del x.a` removes the attribute `a`, not `x` —
        the guard on `x`'s truthiness for indexed access is unrelated to
        which attributes survive and must keep holding."""
        src = """
def f(x):
    if not x: return None
    del x.a
    return x[-1]
"""
        assert _last_status(src) == "GUARDED"

    def test_del_subscript_container_still_invalidates_guard(self):
        """Regression guard: the pre-existing `del x[i]` container behavior
        must keep working alongside the new plain-name handling."""
        src = """
def f(a):
    if not a: return None
    del a[0]
    return a[-1]
"""
        assert _last_status(src) != "GUARDED"


class TestAsyncForTargetInvalidatesGuard:
    """`async for` targets rebind exactly like `for` targets do — the
    control cases keep their current correct result (a reused name is
    invalidated, an unrelated loop variable leaves other guards alone)."""

    def test_reused_name_as_async_loop_var_invalidates_guard(self):
        src = """
async def f(items, x):
    if not x: return None
    async for x in items:
        pass
    return x[-1]
"""
        assert _last_status(src) != "GUARDED"

    def test_unrelated_async_loop_var_does_not_invalidate_other_guards(self):
        """Regression guard: an async-for over an unrelated name must not
        invalidate a guard on a name it doesn't touch."""
        src = """
async def f(items, x):
    if not x: return None
    async for y in items:
        pass
    return x[-1]
"""
        assert _last_status(src) == "GUARDED"


class TestDeleteShapesBeyondTheFirstTuple:
    """AGNES-14: the other shapes of a delete target, each pinned by its own case."""

    def test_del_list_target_invalidates_guard(self):
        src = """
def f(a, b):
    if not a or not b: return None
    del [a, b]
    return b[-1]
"""
        assert _last_status(src) != "GUARDED"

    def test_del_nested_tuple_target_invalidates_every_guard(self):
        src = """
def f(a, b, c):
    if not a or not b or not c: return None
    del a, (b, c)
    return c[-1]
"""
        assert _last_status(src) != "GUARDED"

    def test_del_attribute_does_not_invalidate_the_guard_of_the_attribute_name(self):
        """`del x.a` removes an attribute of `x`; the local `a` it shares a name with is untouched."""
        src = """
def f(a, x):
    if not a: return None
    del x.a
    return a[-1]
"""
        assert _last_status(src) == "GUARDED"

    def test_del_of_other_names_keeps_an_unrelated_guard(self):
        src = """
def f(a, b, c):
    if not a: return None
    del b, c
    return a[-1]
"""
        assert _last_status(src) == "GUARDED"

    def test_del_four_names_invalidates_the_last_one_too(self):
        src = """
def f(a, b, c, d):
    if not a or not b or not c or not d: return None
    del a, b, c, d
    return d[-1]
"""
        assert _last_status(src) != "GUARDED"

    def test_del_tuple_nested_inside_a_tuple_invalidates_the_innermost_guard(self):
        """`del (a, (b, c))` packs a tuple in a tuple: only a recursive walk reaches `c`."""
        src = """
def f(a, b, c):
    if not a or not b or not c: return None
    del (a, (b, c))
    return c[-1]
"""
        assert _last_status(src) != "GUARDED"


class TestDeletingAnAliasKeepsTheOriginalGuarded:
    """`del b` unbinds the name `b` and leaves the list alone: a guard on `a`
    survives it, while `del b[0]` really changes the shared list."""

    def test_unbinding_an_alias_keeps_the_original_guarded(self):
        src = """
def f(a):
    if not a: return None
    b = a
    del b
    return a[-1]
"""
        assert _last_status(src) == "GUARDED"

    def test_unbinding_an_alias_inside_a_branch_keeps_the_original_guarded(self):
        src = """
def f(a, flag):
    if not a: return None
    b = a
    if flag:
        del b
    return a[-1]
"""
        assert _last_status(src) == "GUARDED"

    def test_unbinding_an_alias_in_a_tuple_target_keeps_the_original_guarded(self):
        src = """
def f(a, c):
    if not a: return None
    b = a
    del (b, c)
    return a[-1]
"""
        assert _last_status(src) == "GUARDED"

    def test_unbinding_the_original_still_drops_its_own_guard(self):
        src = """
def f(a):
    if not a: return None
    b = a
    del a
    return a[-1]
"""
        assert _last_status(src) != "GUARDED"

    def test_deleting_an_element_through_the_alias_still_invalidates_the_shared_list(self):
        for stmt in ("del b[0]", "del (b[0], c)", "del c, b[0]"):
            src = f"""
def f(a, c):
    if not a: return None
    b = a
    {stmt}
    return a[-1]
"""
            assert _last_status(src) != "GUARDED", stmt

    def test_a_rebound_name_after_the_unbinding_is_not_tied_to_the_old_alias(self):
        src = """
def f(a):
    if not a: return None
    b = a
    del b
    b = []
    b.pop()
    return a[-1]
"""
        assert _last_status(src) == "GUARDED"
