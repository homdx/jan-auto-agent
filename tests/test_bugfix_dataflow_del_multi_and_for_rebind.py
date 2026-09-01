"""tests/test_bugfix_dataflow_del_multi_and_for_rebind.py — BUGFIX
(audit): two gaps in dataflow.py's guard-invalidation coverage.

1. `del a[0], b[0]` only invalidated a guard on `a` — `_mutated_name_in_stmt`
   returned on the FIRST subscript target found in a multi-target
   `ast.Delete`, so `b` was never processed.

2. A name reused as a `for` loop's target rebinds it exactly like a bare
   `x = <expr>` assignment does, but neither `_invalidate_reassigned` nor
   `_rebound_names_in` recognized `ast.For`/`ast.AsyncFor` at all — only
   `ast.Assign` invalidated a guard.

Both left a guard wrongly marked GUARDED (status="GUARDED") for an
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
