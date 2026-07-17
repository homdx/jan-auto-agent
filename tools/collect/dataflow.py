"""tools/collect/dataflow.py — COLLECT-7: guarded_accesses inventory.

For every indexed access (`x[i]`, `x[-1]`, `x[0]`) in a function body, this
module does a local, intra-procedural dataflow check: is there, above it in
the same control-flow path, an early-exit guard (`if not x: return/continue/
raise`, `if len(x) == 0: ...`, `if x is None: ...`, or a `sys.exit(...)`
call playing the same role) that makes the index provably safe?

Why this matters (COLLECT-7's whole reason for existing): "`stack[-1]` will
raise IndexError" is the single most common false positive in bug hunts on
this codebase — and it's usually wrong, because the access sits right after
exactly this kind of early return. A record here of `GUARDED(<where>)`
directly refutes that class of false positive; `UNGUARDED` is reserved for
accesses that genuinely have no such guard.

This is pure AST + a bounded, sequential walk of each function's own
statement list — no LLM, no cross-function inference — so every
`GuardedAccess` this module produces is `provenance="static"` by
construction (COLLECT-1).

Two guard shapes are recognized, matching the two real-repo reference
cases in the COLLECT-7 spec:

* Direct guard — `if not x:` / `if len(x) == 0:` / `if x is None:`
  (optionally `or`-combined) whose body ends in a terminating statement
  (`return`, `raise`, `continue`, `break`, or a `sys.exit(...)`/`exit(...)`
  call) guards `x` itself for every subscript access on `x` after that
  point in the same block — this is `prompt_store.get_current`'s toy
  mirror (`if not stack: return None`) and the real
  `view_trace.find_trace_file` case (`if not candidates: sys.exit(...)`).

* Aliased guard — `if not x or not x.get(k): return` guards the
  `(x, k)` pair; a later `y = x[k]` or `y = x.get(k)` makes `y` itself
  guarded, so a subsequent `y[-1]` is recognized too. This is what makes
  the *real* `prompt_store.get_current` provable: the guard checks
  `entry.get("stack")`, and `stack = entry["stack"]` is the alias that
  carries the guarantee forward to `stack[-1]`.

Anything outside these two shapes is left `UNGUARDED` rather than guessed
at — a missed guard is a false positive downstream (COLLECT-22 can absorb
that), but a *wrongly claimed* guard would poison the antihallucination
guarantee this whole epic exists for.
"""

from __future__ import annotations

import ast
from typing import Dict, List, Optional, Set, Tuple

from tools.collect.ast_facts import _dotted_name
from tools.collect.model import GuardedAccess

#: Statement types that unconditionally end control flow within the block
#: they appear in.
_TERMINATING_STMT_TYPES = (ast.Return, ast.Raise, ast.Continue, ast.Break)

#: Callable names treated as process-terminating for guard purposes — e.g.
#: `sys.exit(...)` in `view_trace.find_trace_file`'s real guard, which
#: never returns any more than a `return` would.
_TERMINATING_CALL_NAMES = frozenset({"sys.exit", "exit", "quit"})

#: Compound-statement body attributes to keep walking, in order, when
#: descending through a block without changing dataflow scope.
_COMPOUND_BODY_ATTRS = ("body", "orelse", "finalbody")


def _is_terminating_stmt(stmt: ast.stmt) -> bool:
    if isinstance(stmt, _TERMINATING_STMT_TYPES):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        name = _dotted_name(stmt.value.func)
        if name in _TERMINATING_CALL_NAMES:
            return True
    return False


def _block_terminates(body: List[ast.stmt]) -> bool:
    return bool(body) and _is_terminating_stmt(body[-1])


def _falsy_guard_targets(test: ast.expr) -> "Tuple[Set[str], Set[Tuple[str, object]]]":
    """Names and `(name, key)` pairs a boolean test guards-as-falsy.

    Recognizes `not x`, `not x.get(k)` (-> pair), `len(x) == 0`, `x is None`
    at the top level, and inside any nesting depth of `or`-combinations —
    but *not* inside an `and`.

    That `and` exclusion is load-bearing, not an oversight: this function
    is only ever called on the test of an `if` whose body terminates
    (`_block_terminates`), so what it's computing is "what do we know for
    sure once we're past this `if` without having terminated" — i.e. once
    `test` evaluated False. For `test = A or B`, `test` False means both
    `A` and `B` were individually False (De Morgan), so every falsy-check
    found inside an `or` chain is individually refuted and its name/pair
    is genuinely guaranteed truthy afterward — recursing through nested
    `or`s is correct. But for `test = A and B`, `test` False only means
    *at least one* of `A`/`B` was False — not which one — so no
    individual name inside an `and` can be marked guaranteed. BUGFIX: this
    function used to recurse into `ast.BoolOp` regardless of `.op`,
    treating `if not a and not b: raise` the same as `if not a or not b:
    raise` and marking *both* `a` and `b` guaranteed truthy afterward.
    They are not: `a=[]`, `b=[1]` makes `not a and not b` False (so the
    guard doesn't fire) while leaving `a` empty, so a later `a[0]` still
    raises `IndexError` even though it was being recorded GUARDED —
    poisoning exactly the anti-hallucination guarantee this module exists
    to provide (COLLECT-7).
    """
    names: Set[str] = set()
    pairs: Set[Tuple[str, object]] = set()

    def visit(node: ast.expr) -> None:
        if isinstance(node, ast.BoolOp):
            if not isinstance(node.op, ast.Or):
                return  # `and`: false-as-a-whole doesn't refute any single operand
            for value in node.values:
                visit(value)
            return
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            inner = node.operand
            if isinstance(inner, ast.Name):
                names.add(inner.id)
            elif (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "get"
                and isinstance(inner.func.value, ast.Name)
                and inner.args
                and isinstance(inner.args[0], ast.Constant)
            ):
                pairs.add((inner.func.value.id, inner.args[0].value))
            return
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            op = node.ops[0]
            left = node.left
            right = node.comparators[0]
            if (
                isinstance(op, ast.Eq)
                and isinstance(left, ast.Call)
                and isinstance(left.func, ast.Name)
                and left.func.id == "len"
                and left.args
                and isinstance(left.args[0], ast.Name)
                and isinstance(right, ast.Constant)
                and right.value == 0
            ):
                names.add(left.args[0].id)
            elif (
                isinstance(op, ast.Is)
                and isinstance(left, ast.Name)
                and isinstance(right, ast.Constant)
                and right.value is None
            ):
                names.add(left.id)
            return

    visit(test)
    return names, pairs


def _subscript_key(slice_node: ast.expr) -> Optional[object]:
    """Constant literal a subscript's slice denotes, else None (skips
    slice-objects like `a:b`, which aren't a single indexed access)."""
    if isinstance(slice_node, ast.Constant):
        return slice_node.value
    if (
        isinstance(slice_node, ast.UnaryOp)
        and isinstance(slice_node.op, ast.USub)
        and isinstance(slice_node.operand, ast.Constant)
        and isinstance(slice_node.operand.value, (int, float, complex))
        and not isinstance(slice_node.operand.value, bool)
    ):
        return -slice_node.operand.value
    return None


def _subscript_index_repr(slice_node: ast.expr) -> str:
    """Human-readable index for the `access` field, e.g. `-1`, `0`, `i`,
    or (BUGFIX, see `_is_indexed_access`) `keys[0]` for a nested subscript
    index like `cache[keys[0]]`."""
    literal = _subscript_key(slice_node)
    if literal is not None or isinstance(slice_node, ast.Constant):
        return repr(literal) if isinstance(literal, str) else str(literal)
    if isinstance(slice_node, ast.Name):
        return slice_node.id
    if (
        isinstance(slice_node, ast.Subscript)
        and isinstance(slice_node.value, ast.Name)
        and _is_indexed_access(slice_node)
    ):
        return f"{slice_node.value.id}[{_subscript_index_repr(slice_node.slice)}]"
    return "?"


def _is_indexed_access(node: ast.Subscript) -> bool:
    """Whether `node` is a single-element indexed access (`x[i]`, `x[-1]`,
    `x[0]`) on a bare name — not a slice (`x[a:b]`) and not a deeper
    attribute/call chain, which COLLECT-7 is scoped to leave alone.

    BUGFIX: a nested indexed access as the slice itself — `cache[keys[0]]`
    — used to fail every branch here (its slice is an `ast.Subscript`, not
    a `Constant`/`Name`/negated-`Constant`), so the *outer* access was
    silently never cataloged at all: only the inner `keys[0]` showed up in
    `guarded_accesses`, even though `cache[...]` is exactly as much a
    crash-capable indexed access (`KeyError`/`IndexError`) as any other
    site COLLECT-7 tracks, and it's the one a guard on `cache` (or on the
    aliased pair) is actually about. Recursing through `_is_indexed_access`
    on a nested `Subscript` slice keeps this scoped to chains of the same
    "name, indexed by something itself in this shape" pattern — not an
    open door to arbitrary expressions like `cache[func()]` or
    `cache[a + b]`, which remain out of scope exactly as before.
    """
    if not isinstance(node.value, ast.Name):
        return False
    sl = node.slice
    if isinstance(sl, ast.Slice):
        return False
    if isinstance(sl, ast.Constant) or isinstance(sl, ast.Name):
        return True
    if isinstance(sl, ast.UnaryOp) and isinstance(sl.op, ast.USub):
        return True
    if isinstance(sl, ast.Subscript):
        return _is_indexed_access(sl)
    return False


#: Statement types whose `body` (and, where present, `orelse`/`handlers`/
#: `finalbody`) is a nested statement *list* that `_FunctionGuardWalker._walk`
#: recurses into separately, one statement at a time, with correctly
#: updated guard state. For these types, `_record_accesses_in_stmt` must
#: look only at the statement's own "head" expression(s) — the parts
#: evaluated before any nested block runs — and must not redescend into
#: that nested block itself, or every access inside it gets recorded twice:
#: once here (prematurely, before the block's own guard has been applied)
#: and once more, correctly, by the recursive walk. That premature copy is
#: not just noise — for an access guarded by a condition inside the very
#: block being prematurely scanned (e.g. `if not candidates: sys.exit()`
#: followed by `return candidates[-1]` in the same `if p.is_dir():` body),
#: it is recorded UNGUARDED, directly contradicting the correct GUARDED
#: record the recursive walk produces for that exact same site.
_BLOCK_OWNING_STMT_TYPES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)

#: Field names holding each block-owning type's own head expression(s) —
#: i.e. every field *except* the nested statement-list fields the
#: recursive walk already covers on its own.
_HEAD_ONLY_FIELDS: Dict[type, Tuple[str, ...]] = {
    ast.If: ("test",),
    ast.For: ("target", "iter"),
    ast.AsyncFor: ("target", "iter"),
    ast.While: ("test",),
    ast.With: ("items",),
    ast.AsyncWith: ("items",),
    ast.Try: (),  # body/handlers/orelse/finalbody are all nested statement lists
}


def _own_scan_sources(stmt: ast.stmt) -> List[ast.AST]:
    """AST nodes `_record_accesses_in_stmt` should scan for `stmt` itself.

    For an ordinary (non-block-owning) statement, that's just the statement
    unchanged — it has no nested statement-list children for the recursive
    walk to revisit later, so a single full scan is correct and complete.

    For a block-owning statement (`if`/`for`/`while`/`with`/`try`), that's
    only its head expression(s) (e.g. an `if`'s `test`); its `body`/
    `orelse`/`handlers`/`finalbody` are nested statement lists that
    `_FunctionGuardWalker._walk` recurses into right after this call
    returns, scanning each of those statements individually with the
    guard state `stmt` itself may have just introduced.
    """
    if not isinstance(stmt, _BLOCK_OWNING_STMT_TYPES):
        return [stmt]
    sources: List[ast.AST] = []
    for field in _HEAD_ONLY_FIELDS[type(stmt)]:
        value = getattr(stmt, field, None)
        if isinstance(value, list):
            sources.extend(value)
        elif value is not None:
            sources.append(value)
    return sources


def _nested_stmt_lists(stmt: ast.stmt) -> List[List[ast.stmt]]:
    """Every nested statement-list `_walk` recurses into for `stmt` — i.e.
    exactly the lists a *rebind buried inside them* could be invisible to
    the enclosing scope if we only ever mutated the copy handed to that
    recursive call."""
    lists: List[List[ast.stmt]] = []
    for attr in ("body", "orelse", "finalbody"):
        value = getattr(stmt, attr, None)
        if isinstance(value, list):
            lists.append(value)
    for handler in getattr(stmt, "handlers", []):
        lists.append(handler.body)
    return lists


def _rebound_names_in(stmts: List[ast.stmt]) -> Set[str]:
    """Names any statement in `stmts` — at any nesting depth, but never
    crossing into a nested function/class's own separate dataflow scope —
    rebinds via a bare `x = <expr>` (the same shape `_invalidate_reassigned`
    recognizes for the top-level, same-block case).

    BUGFIX: `_walk`'s recursive descent into a compound statement's own
    body (`try`/`for`/`while`/`with`) hands that recursive call a *copy* of
    `guarded_names`/`guarded_pairs`/`guard_desc` — correct for guards
    discovered inside the block (they shouldn't leak out), but it also
    means a rebind *inside* that block only ever invalidated the copy, so
    the outer scope kept citing the pre-rebind guard for every statement
    after the block ends. Concretely: `if not x: raise ValueError` then
    `try: x = maybe()\\n except Exception: pass` then `return x[-1]` — the
    guard on `x` is stale the moment `x = maybe()` runs, on *any* path
    through the `try` (even the exceptional one, conservatively), but
    without this the walker kept citing the pre-`try` guard for `x[-1]`
    after it. Worst-case-conservative by design (same posture as the rest
    of this module, see docstring): a rebind found anywhere in the nested
    block invalidates the guard for what follows, regardless of whether
    that particular sub-path actually executes it — a missed guard
    (false positive downstream) is acceptable, a wrongly-kept one is not.
    """
    names: Set[str] = set()
    for s in stmts:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue  # separate dataflow scope — a rebind there is irrelevant here
        if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name):
            names.add(s.targets[0].id)
        for nested in _nested_stmt_lists(s):
            names |= _rebound_names_in(nested)
    return names


class _FunctionGuardWalker:
    """Sequential guard-tracking walker over one function's statement tree.

    `guarded_names`/`guarded_pairs` are mutated in place as guard-if
    statements are passed, so later *sibling* statements in the same block
    see them — that's what lets a guard positioned earlier in a function
    body cover an access positioned later in that same body. Descent into
    a nested block (`if`/`for`/`while`/`try`) takes a *copy* of the current
    guard state so a guard discovered only inside that nested block doesn't
    leak back out to its enclosing scope.
    """

    def __init__(self, module_path: str) -> None:
        self.module_path = module_path
        self.results: List[GuardedAccess] = []

    def run(self, func: "ast.FunctionDef | ast.AsyncFunctionDef") -> List[GuardedAccess]:
        self._walk(func.body, set(), set(), {})
        return self.results

    def _record_accesses_in_stmt(
        self,
        stmt: ast.stmt,
        guarded_names: Set[str],
        guard_desc: Dict[str, str],
    ) -> None:
        for source in _own_scan_sources(stmt):
            for node in ast.walk(source):
                if not (isinstance(node, ast.Subscript) and _is_indexed_access(node)):
                    continue
                name = node.value.id  # type: ignore[union-attr]
                index_repr = _subscript_index_repr(node.slice)
                is_guarded = name in guarded_names
                self.results.append(
                    GuardedAccess(
                        location=f"{self.module_path}:{node.lineno}",
                        access=f"{name}[{index_repr}]",
                        guard=guard_desc.get(name) if is_guarded else None,
                        status="GUARDED" if is_guarded else "UNGUARDED",
                    )
                )

    def _invalidate_reassigned(
        self,
        stmt: ast.stmt,
        guarded_names: Set[str],
        guarded_pairs: Set[Tuple[str, object]],
        guard_desc: Dict[str, str],
    ) -> None:
        """Drop any guarantee this walker holds about a name that `stmt`
        rebinds, *before* `_propagate_alias` gets a chance to reinstate it
        from the new right-hand side.

        BUGFIX (COLLECT-7 follow-up): `guarded_names`/`guarded_pairs` were
        only ever added to, never removed from — a name proven safe by an
        early-return guard stayed marked GUARDED for the rest of the
        function even after being rebound to a completely different value.
        Concretely: `if not x: return` proves the *original* `x` truthy;
        `x = other_func()` immediately after replaces that object with
        whatever `other_func()` returns, which the guard says nothing
        about, yet the walker kept citing the stale early-return as the
        reason `x[-1]` afterward was GUARDED. Confirmed against this exact
        codebase: `tools/prompt_store.py`'s `rollback()` calls
        `entry["stack"].pop()` (which can empty the list) and *then*
        `stack = entry["stack"]` — before this fix, that second assignment
        re-triggered `_propagate_alias`'s pair rule (`stack` aliasing the
        already-guarded `(entry, "stack")` pair) and re-marked `stack`
        GUARDED with no awareness that `.pop()` had just run, citing the
        function's original entry guard as justification for a site that
        guard no longer establishes anything about. A `GuardedAccess` this
        wrong is worse than a missed one (see module docstring): Pass C's
        `contradiction_check` (COLLECT-17) trusts `status="GUARDED"`
        unconditionally and drops any Pass B claim that the access can
        crash as `dropped:contradicts-guard` — so a real, correctly
        reported crash-site claim about a rebound name gets silently
        thrown away as a supposed false positive.

        Only a bare `x = <expr>` (single `Name` target) rebinds a name;
        `x[i] = ...`/`x.attr = ...`/tuple-unpacking targets don't replace
        what `x` itself refers to, so they're left alone here (and were
        never something `_propagate_alias` recognized as a target either).
        """
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
            return
        target = stmt.targets[0].id
        guarded_names.discard(target)
        guard_desc.pop(target, None)
        # `target` no longer denotes whatever dict it used to alias, so any
        # `(target, key)` pair guarantee is stale too — a later `target[k]`
        # alias-propagation must not be able to resurrect it.
        for pair in {p for p in guarded_pairs if p[0] == target}:
            guarded_pairs.discard(pair)

    def _invalidate_rebinds_in_nested_block(
        self,
        stmt: ast.stmt,
        guarded_names: Set[str],
        guarded_pairs: Set[Tuple[str, object]],
        guard_desc: Dict[str, str],
    ) -> None:
        """After recursing into `stmt`'s own nested statement list(s)
        (`if`/`for`/`while`/`with`/`try`) with a *copy* of the guard state,
        drop the guarantee — in the *outer*, still-live guard state — for
        any name that copy's walk saw rebound anywhere inside. See
        `_rebound_names_in`'s docstring for why this can't just be left to
        `_invalidate_reassigned`, which only ever looks at `stmt` itself,
        never its nested body.
        """
        rebound: Set[str] = set()
        for nested in _nested_stmt_lists(stmt):
            rebound |= _rebound_names_in(nested)
        if not rebound:
            return
        for name in rebound:
            guarded_names.discard(name)
            guard_desc.pop(name, None)
        for pair in {p for p in guarded_pairs if p[0] in rebound}:
            guarded_pairs.discard(pair)

    def _propagate_alias(
        self,
        stmt: ast.stmt,
        guarded_names: Set[str],
        guarded_pairs: Set[Tuple[str, object]],
        guard_desc: Dict[str, str],
    ) -> None:
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
            return
        target = stmt.targets[0].id
        value = stmt.value
        base: Optional[str] = None
        key: object = None
        if isinstance(value, ast.Subscript) and isinstance(value.value, ast.Name):
            base = value.value.id
            key = _subscript_key(value.slice)
        elif (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "get"
            and isinstance(value.func.value, ast.Name)
            and value.args
            and isinstance(value.args[0], ast.Constant)
        ):
            base = value.func.value.id
            key = value.args[0].value
        if base is not None and (base, key) in guarded_pairs:
            guarded_names.add(target)
            if base in guard_desc:
                guard_desc.setdefault(target, guard_desc[base])

    def _walk(
        self,
        stmts: List[ast.stmt],
        guarded_names: Set[str],
        guarded_pairs: Set[Tuple[str, object]],
        guard_desc: Dict[str, str],
    ) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue  # separate dataflow scope — handled on its own pass

            self._record_accesses_in_stmt(stmt, guarded_names, guard_desc)
            self._invalidate_reassigned(stmt, guarded_names, guarded_pairs, guard_desc)
            self._propagate_alias(stmt, guarded_names, guarded_pairs, guard_desc)

            if isinstance(stmt, ast.If):
                # BUGFIX: `stmt.body` runs on exactly the *opposite* polarity
                # from what a terminating guard proves — `if not x: ... `
                # proves `x` truthy only for code that runs *after* this
                # `if` (or in its `orelse`), never for the guard's own body,
                # where `test` was True, i.e. `x` is the very thing the test
                # found falsy. Snapshot the pre-guard state for the body's
                # walk *before* adding the new guard below, so an access
                # inside the guard's own body (e.g. `if not x: y = x[0];
                # return y`) is walked with the guard state as it stood
                # before this `if`, not after — the body sees no guard on
                # `x` at all (correctly: the true fact there is that `x` is
                # falsy, not that it's guarded-truthy), while `orelse` and
                # every sibling statement after this `if` correctly do.
                body_guarded_names = set(guarded_names)
                body_guarded_pairs = set(guarded_pairs)
                body_guard_desc = dict(guard_desc)
                names, pairs = _falsy_guard_targets(stmt.test)
                if _block_terminates(stmt.body) and (names or pairs):
                    desc = f"early-return at {self.module_path}:{stmt.lineno}"
                    for n in names:
                        guarded_names.add(n)
                        guard_desc.setdefault(n, desc)
                    guarded_pairs |= pairs
                self._walk(stmt.body, body_guarded_names, body_guarded_pairs, body_guard_desc)
                self._walk(stmt.orelse, set(guarded_names), set(guarded_pairs), dict(guard_desc))
                self._invalidate_rebinds_in_nested_block(stmt, guarded_names, guarded_pairs, guard_desc)
            elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
                for attr in ("body", "orelse"):
                    self._walk(getattr(stmt, attr, []), set(guarded_names), set(guarded_pairs), dict(guard_desc))
                self._invalidate_rebinds_in_nested_block(stmt, guarded_names, guarded_pairs, guard_desc)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                self._walk(stmt.body, set(guarded_names), set(guarded_pairs), dict(guard_desc))
                self._invalidate_rebinds_in_nested_block(stmt, guarded_names, guarded_pairs, guard_desc)
            elif isinstance(stmt, ast.Try):
                for attr in _COMPOUND_BODY_ATTRS:
                    self._walk(getattr(stmt, attr, []), set(guarded_names), set(guarded_pairs), dict(guard_desc))
                for handler in stmt.handlers:
                    self._walk(handler.body, set(guarded_names), set(guarded_pairs), dict(guard_desc))
                self._invalidate_rebinds_in_nested_block(stmt, guarded_names, guarded_pairs, guard_desc)


def extract_guarded_accesses(tree: ast.Module, module_path: str) -> List[GuardedAccess]:
    """Every indexed access (`x[i]`/`x[-1]`/`x[0]`) in `tree`, classified
    GUARDED/UNGUARDED by the local dataflow check above (COLLECT-7).

    Each function/method's body is walked independently (a guard in one
    function never covers an access in another). Order is stable
    (COLLECT-3 determinism): by source line, then the rendered `access`
    text as a tiebreaker for same-line cases.
    """
    accesses: List[GuardedAccess] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            accesses.extend(_FunctionGuardWalker(module_path).run(node))
    accesses.sort(key=lambda g: (int(g.location.rsplit(":", 1)[-1]), g.access))
    return accesses
