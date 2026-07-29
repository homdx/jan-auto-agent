"""tests/test_dataflow_alias_mutation.py — a guard must not survive a
mutation reached through a same-scope ALIAS of the guarded name.

The just-landed mutation-invalidation fix (_mutated_name_in_stmt) closes
`if not stack: return None` / `stack.pop()` / `stack[-1]` — but it
invalidates only the exact bare name the mutator method is called ON.
Reached through an alias, the guard survived:

    if not stack: return None
    alt = stack
    alt.pop()
    return stack[-1]         # still claimed GUARDED — wrong

`alt` and `stack` are the same object; `alt.pop()` can empty a
single-element `stack` exactly as `stack.pop()` would, so `stack[-1]` can
genuinely IndexError. A GuardedAccess this wrong is worse than a missed
one (this module's own docstring): COLLECT-17's `contradiction_check`
trusts `status="GUARDED"` unconditionally and silently drops a correct
crash-site claim about such a site.

This tracks exactly one hop of bare-Name-to-bare-Name aliasing, not
general alias analysis, matching the same "bare name receiver" scope
`_invalidate_reassigned` and `_mutated_name_in_stmt` already use.
"""

from __future__ import annotations

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


class TestAliasMutationInvalidatesGuard:
    def test_mutation_via_alias_invalidates_original_name(self):
        """The exact reported bug."""
        src = """
def f(stack):
    if not stack: return None
    alt = stack
    alt.pop()
    return stack[-1]
"""
        assert _last_status(src) == "UNGUARDED"

    def test_mutation_via_original_name_invalidates_the_alias_too(self):
        """Symmetric direction: alias created, THEN original is mutated —
        an access through the alias afterward must not be trusted either
        (though it was never positively guarded via the alias in the first
        place under this module's existing, narrower alias-establishment
        rules — this pins that it stays unguarded, not that this fix
        changes it)."""
        src = """
def f(stack):
    if not stack: return None
    alt = stack
    stack.pop()
    return alt[-1]
"""
        assert _last_status(src) == "UNGUARDED"

    def test_rebinding_the_alias_breaks_the_connection(self):
        """Once `alt` is reassigned to something else, mutating the FORMER
        alias target must not spuriously invalidate the original name."""
        src = """
def f(stack, other):
    if not stack: return None
    alt = stack
    alt = other
    other.pop()
    return stack[-1]
"""
        assert _last_status(src) == "GUARDED"

    def test_mutating_an_unrelated_name_does_not_over_invalidate(self):
        """Sanity: the fix must not become trigger-happy and invalidate
        names that were never aliased to the mutated one."""
        src = """
def f(stack, other):
    if not stack: return None
    other.pop()
    return stack[-1]
"""
        assert _last_status(src) == "GUARDED"

    def test_no_mutation_at_all_stays_guarded(self):
        """Sanity: a guard with no mutation anywhere must be unaffected."""
        src = """
def f(stack):
    if not stack: return None
    return stack[-1]
"""
        assert _last_status(src) == "GUARDED"

    def test_direct_mutation_no_alias_still_works(self):
        """Sanity: the already-landed direct-mutation fix must be
        unaffected by adding alias tracking alongside it."""
        src = """
def f(stack):
    if not stack: return None
    stack.pop()
    return stack[-1]
"""
        assert _last_status(src) == "UNGUARDED"
