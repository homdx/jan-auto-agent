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

    Recognizes, anywhere inside an `and`/`or` combination:
    `not x`, `not x.get(k)` (-> pair), `len(x) == 0`, `x is None`.
    """
    names: Set[str] = set()
    pairs: Set[Tuple[str, object]] = set()

    def visit(node: ast.expr) -> None:
        if isinstance(node, ast.BoolOp):
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
    ):
        return -slice_node.operand.value
    return None


def _subscript_index_repr(slice_node: ast.expr) -> str:
    """Human-readable index for the `access` field, e.g. `-1`, `0`, `i`."""
    literal = _subscript_key(slice_node)
    if literal is not None or isinstance(slice_node, ast.Constant):
        return repr(literal) if isinstance(literal, str) else str(literal)
    if isinstance(slice_node, ast.Name):
        return slice_node.id
    return "?"


def _is_indexed_access(node: ast.Subscript) -> bool:
    """Whether `node` is a single-element indexed access (`x[i]`, `x[-1]`,
    `x[0]`) on a bare name — not a slice (`x[a:b]`) and not a deeper
    attribute/call chain, which COLLECT-7 is scoped to leave alone."""
    if not isinstance(node.value, ast.Name):
        return False
    sl = node.slice
    if isinstance(sl, ast.Slice):
        return False
    return isinstance(sl, ast.Constant) or isinstance(sl, ast.Name) or (
        isinstance(sl, ast.UnaryOp) and isinstance(sl.op, ast.USub)
    )


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
            self._propagate_alias(stmt, guarded_names, guarded_pairs, guard_desc)

            if isinstance(stmt, ast.If):
                names, pairs = _falsy_guard_targets(stmt.test)
                if _block_terminates(stmt.body) and (names or pairs):
                    desc = f"early-return at {self.module_path}:{stmt.lineno}"
                    for n in names:
                        guarded_names.add(n)
                        guard_desc.setdefault(n, desc)
                    guarded_pairs |= pairs
                self._walk(stmt.body, set(guarded_names), set(guarded_pairs), dict(guard_desc))
                self._walk(stmt.orelse, set(guarded_names), set(guarded_pairs), dict(guard_desc))
            elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
                for attr in ("body", "orelse"):
                    self._walk(getattr(stmt, attr, []), set(guarded_names), set(guarded_pairs), dict(guard_desc))
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                self._walk(stmt.body, set(guarded_names), set(guarded_pairs), dict(guard_desc))
            elif isinstance(stmt, ast.Try):
                for attr in _COMPOUND_BODY_ATTRS:
                    self._walk(getattr(stmt, attr, []), set(guarded_names), set(guarded_pairs), dict(guard_desc))
                for handler in stmt.handlers:
                    self._walk(handler.body, set(guarded_names), set(guarded_pairs), dict(guard_desc))


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
