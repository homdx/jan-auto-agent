"""tools/collect/ast_facts.py — COLLECT-4: shared AST-fact helpers.

Pure-AST extraction used by `tools/collect/scanner.py` (Pass A, EPIC B) and
extended in place by later tasks in the same epic: `config_reads`
(COLLECT-5), `except_sites` (COLLECT-6). Nothing here touches an LLM or the
filesystem beyond parsing source that's already been read — that keeps this
module trivially unit-testable against bare `ast.Module` trees.

Two extractors live here today:

* `extract_symbols` — public/private classes and functions at module scope
  (COLLECT-4). Order is stable: by source line, then qualname, so a rerun
  of Pass A never reorders symbols for reasons unrelated to the code
  (COLLECT-3's determinism guarantee starts here).
* `extract_imports` — the set of module names touched by `import` /
  `from ... import ...` statements anywhere in the tree, deduplicated and
  sorted (COLLECT-4).
"""

from __future__ import annotations

import ast
from typing import Any, List, Optional

from tools.collect.model import ConfigRead, ExceptSite, FunctionRecord

#: AST node types that count as a "public symbol" for COLLECT-4 — top-level
#: functions/classes. Nested defs (methods, closures) are intentionally out
#: of scope for this task; they're not part of a module's public surface.
_SYMBOL_NODE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

#: `ConfigParser` reader methods COLLECT-5 recognizes. The idiom this
#: codebase uses everywhere (`agents.ini` access) is
#: ``config.get*(section, key, fallback=...)`` — the presence of a
#: `fallback=` keyword is what distinguishes a config read from an
#: unrelated `.get(...)` call (e.g. `dict.get`), so that keyword is
#: required, not just the method name.
_CONFIG_READ_METHODS = frozenset({"get", "getint", "getboolean", "getfloat"})

#: Attribute-call method names COLLECT-6 treats as "this except body logs
#: the error" — i.e. not silent, even though it doesn't re-raise. Matches
#: both the stdlib `logging` API and the common `logger.*`/`self.logger.*`
#: call-site idiom this codebase uses (the method name is what's checked,
#: not the receiver, since a dedicated logger instance's import alias
#: varies module to module).
_LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})



def extract_symbols(tree: ast.Module, module_path: str) -> List[FunctionRecord]:
    """Public/private top-level classes and functions in `tree`.

    Each becomes a `FunctionRecord` with `provenance` implicitly "static"
    (that's the only value `FunctionRecord`'s frozen dataclass ever carries
    for these fields — see COLLECT-1). Signature is a placeholder shape
    `name(...)`: COLLECT-4's job is symbol inventory, not full signature
    reconstruction.
    """
    symbols: List[FunctionRecord] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, _SYMBOL_NODE_TYPES):
            doc = ast.get_docstring(node) or ""
            first_line = doc.splitlines()[0] if doc else ""
            symbols.append(
                FunctionRecord(
                    qualname=f"{module_path}:{node.name}",
                    module=module_path,
                    lineno=node.lineno,
                    signature=f"{node.name}(...)",
                    docstring_first_line=first_line,
                    is_private=node.name.startswith("_"),
                )
            )
    # Stable order independent of AST traversal quirks: by source position,
    # then qualname as a tiebreaker (COLLECT-3 determinism).
    return sorted(symbols, key=lambda s: (s.lineno, s.qualname))


def extract_all_defined_names(tree: ast.Module) -> "frozenset[str]":
    """Every bare identifier `tree` defines *anywhere* — not just the
    top-level functions/classes `extract_symbols` (COLLECT-4) indexes as
    "public symbols", but also nested `def`s (methods, closures at any
    depth) and assignment targets at module or class-body scope (module-
    level constants, class attributes, `Annotated` constants via
    `AnnAssign`).

    This is deliberately a *different, broader* notion of "exists" than
    `extract_symbols`'s "public symbol" — COLLECT-4 scopes `public_symbols`
    to the top-level surface on purpose ("nested defs... are intentionally
    out of scope for this task; they're not part of a module's public
    surface"), and this function does not change that scoping or feed
    `public_symbols`/`ModuleRecord` at all. It exists purely as a cheap,
    reusable "does this name appear anywhere in the source at all" check
    for callers (COLLECT-17's verifier, specifically) that need to tell a
    genuinely fabricated identifier apart from a real one Pass A's public
    surface just doesn't happen to track — e.g. a module-level constant or
    a class method, both 100% real, neither a "public symbol" by COLLECT-4's
    own definition.

    Permissive in what counts as "defined": every `FunctionDef`/
    `AsyncFunctionDef`/`ClassDef` name at any nesting depth, plus every
    simple `Name` target of an `Assign`/`AnnAssign` *at module or
    class-body scope* — but NOT inside a function/method body.

    BUGFIX (found via adversarial review of an earlier version of this
    exact function): the first version used `ast.walk(tree)`, which visits
    every node regardless of enclosing scope, so an `Assign` target inside
    *any* function body — an ordinary local variable, completely
    unrelated to the module's citable surface — was added to this set the
    same as a module-level constant. That reopened exactly the hole this
    function exists to close: a fabricated citation like
    `"module.py:result"` (`result` being nothing more than some unrelated
    function's local variable somewhere in the file) would be excused as
    "real but unindexed" instead of flagged, because *some* local variable
    in the module happened to share that name — and in a module of any
    real size, generic names like `result`/`data`/`ctx`/`done` are close
    to guaranteed to collide with *something*. A citable name has to be
    reachable via `module.py:name` syntax in the first place; a local
    variable buried inside an unrelated function's body never is, so it
    must not count as "real but unindexed" here. Function/method/class
    *names* themselves are still collected at any nesting depth (a nested
    method or closure is a legitimate thing to cite by name), only their
    *bodies'* local assignments are excluded.

    BUGFIX 2 (found via review of the fix above): a `class` statement's
    own body is always its own scope — its attributes are real, citable
    names — regardless of what *encloses* the `class` statement itself.
    A class defined inside a function (`def f(): class Local: ATTR = 1`)
    was inheriting that function's "local, non-citable" scope for its own
    body, so `Local.ATTR` got excluded the same as an ordinary local
    variable, even though it's a genuine class attribute. A class body
    now always resets to module-like (non-function) scope for its own
    `Assign`/`AnnAssign` targets, no matter where the `class` statement
    sits; only a further-nested `def` (a method) beneath it reintroduces
    function-local scope for names assigned inside *that*.
    """
    names: set = set()

    def _walk(node: ast.AST, in_function_body: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(child.name)
                _walk(child, in_function_body=True)
            elif isinstance(child, ast.ClassDef):
                names.add(child.name)
                # A class body always executes at class-definition time as
                # its own scope — same as module scope — regardless of
                # *where* the `class` statement itself sits. BUGFIX: this
                # used to pass through the enclosing `in_function_body`
                # unchanged, so a class defined *inside* a function (e.g.
                # `def f(): class Local: ATTR = 1`) incorrectly inherited
                # that function's "local, non-citable" scope for its own
                # body, excluding `Local`'s own attributes (`ATTR`) the
                # same as if they were the enclosing function's local
                # variables. A class's own attributes are real, citable
                # names (`module.py:ATTR`-style) no matter how the class
                # itself is nested, so its body always resets to `False`
                # here — only a further-nested `def` inside that class
                # (a method) reintroduces function-local scope beneath it.
                _walk(child, in_function_body=False)
            elif isinstance(child, ast.Assign) and not in_function_body:
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
                _walk(child, in_function_body)
            elif isinstance(child, ast.AnnAssign) and not in_function_body:
                if isinstance(child.target, ast.Name):
                    names.add(child.target.id)
                _walk(child, in_function_body)
            else:
                _walk(child, in_function_body)

    _walk(tree, in_function_body=False)
    return frozenset(names)


def extract_imports(tree: ast.Module) -> List[str]:
    """Sorted, deduplicated list of module names imported anywhere in `tree`.

    `import a.b.c` contributes `"a.b.c"`; `from x.y import z` contributes
    `"x.y"` (the source module, not the imported name) — mirroring the
    convention `tests/_pass_a_stub.py` establishes and COLLECT-3's golden
    fixture already encodes.

    BUGFIX (relative imports): `ast.ImportFrom.level` used to be ignored
    entirely, with two distinct failure modes in the import graph
    (COLLECT-8):

    1. **Misattribution** — `from .model import X` inside `pkg/a.py` was
       recorded as bare `"model"`, which `graph.resolve_import` happily
       matched against an *unrelated top-level* `model.py` if one existed,
       producing a phantom edge to the wrong module while the real
       dependency on `pkg/model.py` vanished. That's exactly the
       "misattributed is worse than missing" failure `build_call_edges`'s
       own docstring forbids, leaking into blast-radius (`imported_by`).
    2. **Silent loss** — without such a name collision the bare `"model"`
       resolved to nothing at all, dropping the edge and (via
       `entry_points`) making package-internal modules look like roots.
       And `from . import util` (`node.module is None`) was never
       recorded in any form.

    Now a relative import keeps its level as leading dots — `from .model
    import X` -> `".model"`, `from ..pkg import y` -> `"..pkg"` — the same
    spelling Python source itself uses, so it stays human-readable in
    `render`/`summarizer` output. For the `from . import name` form
    (`module is None`) each imported alias is recorded as `"." * level +
    alias.name`: for a submodule that's precisely its relative dotted
    name; for a plain symbol imported from the package `__init__`,
    `graph.resolve_import`'s drop-one-trailing-part fallback still lands
    it on the package itself rather than dropping the dependency.
    `graph.resolve_import` resolves the leading dots against the
    *importer's* package (which `import_edges` knows from `m.path`) —
    never against the top-level namespace — so neither failure mode above
    can recur.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            if node.module:
                names.add(prefix + node.module)
            elif node.level:
                # `from . import util` / `from .. import helpers` — the
                # only surviving spelling of the dependency is the alias
                # list itself.
                for alias in node.names:
                    names.add(prefix + alias.name)
    return sorted(names)


def _literal_or_none(node: Optional[ast.AST]) -> Any:
    """Best-effort literal value of a fallback expression. Anything that
    isn't a compile-time literal (a variable, a call, ...) becomes `None`
    rather than raising — a config read is still worth recording even when
    its fallback can't be resolved statically."""
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None


def _fstring_key_and_override(node: ast.JoinedStr) -> "tuple[str, bool]":
    """Render an f-string key expression back to a template string like
    ``"threshold_{task_mode}"``, and report whether it matches the
    `{key}_{task_mode}` mode-override convention (COLLECT-5): any
    formatted (non-literal) part whose expression is the name/attribute
    `task_mode` (however deeply the attribute chain goes, e.g.
    `self.task_mode`) marks the read as a mode-override.
    """
    parts: List[str] = []
    has_mode_override = False
    for value in node.values:
        if isinstance(value, ast.Constant):
            parts.append(str(value.value))
        elif isinstance(value, ast.FormattedValue):
            inner = value.value
            name = _dotted_name(inner)
            parts.append(f"{{{name or 'expr'}}}")
            if name is not None and name.split(".")[-1] == "task_mode":
                has_mode_override = True
    return "".join(parts), has_mode_override


def _dotted_name(node: ast.AST) -> Optional[str]:
    """`a.b.c` style dotted name for a Name/Attribute chain, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _simple_literal_assignments(tree: ast.Module) -> dict:
    """Module-wide map of `name -> literal str value` for variables that are
    assigned a string constant directly (``arch = "architect"``) and never
    assigned a *different* string constant anywhere else in the module.

    This is a deliberately narrow, single-purpose resolver: it exists only
    so `extract_config_reads` can see through the very common local-alias
    idiom this codebase uses for config sections (``arch = "architect"``
    right before ``config.get(arch, ...)``), without doing real scope-aware
    dataflow analysis. A name assigned two different literal values
    anywhere in the module is treated as ambiguous and left unresolved —
    better to skip a call site than misattribute it.
    """
    seen: dict = {}
    ambiguous = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    prev = seen.get(target.id)
                    if prev is not None and prev != node.value.value:
                        ambiguous.add(target.id)
                    seen[target.id] = node.value.value
    return {k: v for k, v in seen.items() if k not in ambiguous}


#: The name of this codebase's shared mode-aware config-read helper
#: (`tools.auto.utils._cfg_mode`). Calling it at all *is* the
#: `{key}_{task_mode}` mode-override convention — it unconditionally tries
#: `{key}_{task_mode}` before falling back to the bare `key` — so a call
#: shaped like this is recognized by name, the same way `_CONFIG_READ_
#: METHODS` recognizes `ConfigParser` methods by name.
_CFG_MODE_HELPER_NAME = "_cfg_mode"


def extract_config_reads(tree: ast.Module, module_path: str) -> List[ConfigRead]:
    """Every `config.get*(section, key, fallback=...)` call site in `tree`,
    plus every `_cfg_mode(config, section, key, task_mode, fallback=...)`
    call site (COLLECT-5).

    Recognizes the four `ConfigParser` reader methods (`get`, `getint`,
    `getboolean`, `getfloat`) — but only when a `fallback=` keyword (or a
    third positional arg) is present, which is what separates a config
    read from an unrelated `.get(...)` call (`dict.get`, `os.environ.get`,
    ...) using the same method name. `section` must be a string literal to
    be recorded (a dynamically-computed section can't be attributed
    statically, so those call sites are skipped rather than guessed at).

    Direct calls are only half the picture: this codebase's actual
    mode-override convention almost never appears as a literal
    `config.get(section, f"{key}_{task_mode}", ...)` at the call site —
    it goes through the shared `_cfg_mode(config, section, key, task_mode,
    fallback=...)` helper (`tools.auto.utils`) instead, which does that
    same `config.get` call *inside its own body*, one module away from
    every real caller. A version of this extractor that only recognized
    literal `config.get*()` shapes would silently record zero
    mode-override reads anywhere `_cfg_mode` is used — which, on this
    codebase, is everywhere the convention is actually used (`coder.py`,
    `architect.py`, `inner_loop.py`, `summary_memory.py`,
    `canon_validator.py`, `repo_ingest.py`, `progress_display.py` — 13+
    call sites, zero of them a direct `config.get*()` call). A `_cfg_mode`
    call is recognized by name (like `_CONFIG_READ_METHODS` recognizes
    `ConfigParser` methods by name) and *always* has_mode_override=True —
    unlike the literal-f-string case, that isn't something to detect from
    the key expression's shape, since calling this helper at all commits
    to trying `{key}_{task_mode}` first, unconditionally.

    The `{key}_{task_mode}` mode-override convention (an f-string key whose
    interpolated part is `task_mode`) is detected and flagged via
    `has_mode_override`; the recorded `key` keeps the template shape
    (e.g. `"threshold_{task_mode}"`) so the config map (COLLECT-14) can
    still group it with its base key.
    """
    aliases = _simple_literal_assignments(tree)
    reads: List[ConfigRead] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        if isinstance(func, ast.Attribute) and func.attr in _CONFIG_READ_METHODS:
            fallback_node = None
            for kw in node.keywords:
                if kw.arg == "fallback":
                    fallback_node = kw.value
                    break
            if fallback_node is None and len(node.args) < 3:
                # No `fallback=` keyword and no positional fallback: this is
                # not the ConfigParser idiom this codebase uses — likely an
                # unrelated `.get(...)` call. Skip it.
                continue
            if fallback_node is None and len(node.args) >= 3:
                fallback_node = node.args[2]

            if len(node.args) < 2:
                continue  # can't determine section/key positionally; skip
            section_node, key_node = node.args[0], node.args[1]

            if isinstance(section_node, ast.Constant) and isinstance(section_node.value, str):
                section = section_node.value
            elif isinstance(section_node, ast.Name) and section_node.id in aliases:
                section = aliases[section_node.id]
            else:
                continue  # dynamic section: can't attribute statically

            has_mode_override = False
            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                key = key_node.value
            elif isinstance(key_node, ast.JoinedStr):
                key, has_mode_override = _fstring_key_and_override(key_node)
            elif isinstance(key_node, ast.Name) and key_node.id in aliases:
                key = aliases[key_node.id]
            else:
                continue  # dynamic, non-f-string/non-alias key: can't attribute statically

            reads.append(
                ConfigRead(
                    section=section,
                    key=key,
                    fallback=_literal_or_none(fallback_node),
                    reader_module=module_path,
                    has_mode_override=has_mode_override,
                )
            )

        elif isinstance(func, ast.Name) and func.id == _CFG_MODE_HELPER_NAME:
            # _cfg_mode(config, section, key, task_mode, fallback=...) —
            # note the leading `config` positional shifts section/key one
            # slot later than the direct-call shape above.
            if len(node.args) < 3:
                continue  # can't determine section/key positionally; skip

            fallback_node = None
            for kw in node.keywords:
                if kw.arg == "fallback":
                    fallback_node = kw.value
                    break
            if fallback_node is None and len(node.args) >= 5:
                fallback_node = node.args[4]

            section_node, key_node = node.args[1], node.args[2]
            if isinstance(section_node, ast.Constant) and isinstance(section_node.value, str):
                section = section_node.value
            elif isinstance(section_node, ast.Name) and section_node.id in aliases:
                section = aliases[section_node.id]
            else:
                continue  # dynamic section: can't attribute statically

            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                base_key = key_node.value
            elif isinstance(key_node, ast.Name) and key_node.id in aliases:
                base_key = aliases[key_node.id]
            else:
                continue  # dynamic base key: can't attribute statically

            reads.append(
                ConfigRead(
                    section=section,
                    key=f"{base_key}_{{task_mode}}",
                    fallback=_literal_or_none(fallback_node),
                    reader_module=module_path,
                    has_mode_override=True,
                )
            )

    # Stable order (COLLECT-3 determinism): by section then key.
    return sorted(reads, key=lambda r: (r.section, r.key))


def _exception_type_name(handler: ast.ExceptHandler) -> str:
    """Render an `except` handler's type expression as a string.

    Bare `except:` (no type at all) becomes `"*"` — it is a distinct,
    broader thing from `except Exception:` and shouldn't be conflated with
    it. A tuple of types (`except (KeyError, TypeError):`) is rendered as
    the `|`-joined dotted names, in source order.
    """
    node = handler.type
    if node is None:
        return "*"
    if isinstance(node, ast.Tuple):
        return "|".join(_dotted_name(elt) or "?" for elt in node.elts)
    return _dotted_name(node) or "?"


def _is_log_call(node: ast.AST) -> bool:
    """Whether `node` is a call to a logging-shaped method — `logger.warning(...)`,
    `self._logger.error(...)`, `logging.exception(...)`, etc. Only the final
    attribute name is checked (see `_LOG_METHODS`), not the receiver, since
    the receiver's name/import alias varies module to module."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LOG_METHODS
    )


def _walk_own_scope(node: ast.AST):
    """Like `ast.walk`, but never descends into a nested function/lambda's
    own *body* (a deferred scope — its statements don't run until the
    function is called), while still walking everything that actually
    executes right now, as part of the statement `node` itself: a class's
    own body (a class statement runs its whole body immediately, to build
    the class's namespace — it's not deferred at all), and a def/lambda's
    own decorators and default-argument expressions (evaluated eagerly,
    at def-statement time, in the *enclosing* scope, before the function
    object even exists).

    BUGFIX 1 (found via adversarial review, same bug class already fixed
    elsewhere in this codebase — see `dataflow._walk`'s
    `(FunctionDef, AsyncFunctionDef, ClassDef, Lambda)` scope skip and
    `extract_all_defined_names`'s `in_function_body` tracking):
    `_classify_except_body` used to call plain `ast.walk(stmt)`, which
    visits every descendant regardless of enclosing scope. An except body
    that merely *defines* a nested function containing a bare `raise` (a
    deferred/callback error handler, e.g. `except Exception:\\n    def
    handler(): raise\\n    register(handler)`) doesn't re-raise anything at
    the except site itself — the handler body only runs later, if and when
    something calls it — so the actual except block is a silent swallow
    (fail-open) exactly like a bare `pass` would be. `ast.walk` saw the
    `raise` nested three levels down inside `handler`'s own body and
    classified the whole site `"re-raise", is_fail_open=False` anyway,
    hiding a genuine silent-failure site from COLLECT-6's detector (and,
    downstream, letting a real "this except silently swallows the error"
    Pass B claim about that exact site get dropped by COLLECT-17's
    contradiction_check as a false positive).

    BUGFIX 2 (found via adversarial review of BUGFIX 1's own first cut):
    that first cut stopped descending on `ClassDef` exactly the same way
    as `FunctionDef`/`Lambda` — but a `class` statement's body is *not*
    deferred the way a function body is; it runs immediately, right where
    the `class` statement sits, to build the class object (this is the
    exact same distinction `extract_all_defined_names`'s own "BUGFIX 2"
    already documents for a different reason). `except Exception:\\n
    class Deferred:\\n        logger.error(...)` really does log at the
    except site — the class statement executing *is* what runs that log
    call — so stopping at `ClassDef` produced a fresh false "pass"/
    fail-open classification for a handler that, in fact, logs. Only a
    *further*-nested `def`/`lambda` inside that class body (an actual
    method) is still deferred, and that's handled automatically: the
    recursive call reaches the method as an ordinary child and stops
    there on its own.

    Also walks a def/lambda's `decorator_list` and default-argument
    expressions (via its `arguments` node) rather than skipping them along
    with the body — both run at the def/lambda statement's own execution
    time, in the enclosing scope, not when the function is later called
    (e.g. `@log_and_register(logger.info("registering"))` logs right now,
    not "only if called later"; same for a `def handler(x=logger.error(...))`
    default value).
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        yield node
        for deco in node.decorator_list:
            yield from _walk_own_scope(deco)
        yield from _walk_own_scope(node.args)
        return
    if isinstance(node, ast.Lambda):
        yield node
        yield from _walk_own_scope(node.args)
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _walk_own_scope(child)


def _classify_except_body(body: List[ast.stmt]) -> "tuple[str, bool]":
    """`(body_kind, is_fail_open)` for one except handler's body (COLLECT-6).

    Priority, evaluated over every statement in the body (`ast.walk`, so a
    logged/raised/continued/returned statement nested one level inside an
    `if` inside the handler still counts):

    1. A bare `raise` (no exception expression, i.e. re-raise-the-caught-one)
       anywhere -> ``"re-raise"``. Never fail-open: the exception keeps
       propagating.
    2. A logging call anywhere -> ``"log"``. Not silent: someone will see it.
    3. A `continue` anywhere -> ``"continue"``. Control flow, not a silent
       swallow — the loop keeps going, but visibly (COLLECT-6 AC:
       `coder.py:866`'s `except OSError: continue` is *not* fail-open).
    4. A `return` anywhere -> ``"return"``. Also control flow, not silence.
    5. Anything else (including a bare `pass`, or any body that doesn't hit
       1-4) -> ``"pass"``, ``is_fail_open=True``: nothing observable happens
       and execution just falls through past the except block. This is the
       category responsible for the majority of false positives in bug
       hunts (COLLECT-6's whole reason for existing).
    """
    has_bare_raise = False
    has_log = False
    has_continue = False
    has_return = False
    for stmt in body:
        for sub in _walk_own_scope(stmt):
            if isinstance(sub, ast.Raise) and sub.exc is None:
                has_bare_raise = True
            elif _is_log_call(sub):
                has_log = True
            elif isinstance(sub, ast.Continue):
                has_continue = True
            elif isinstance(sub, ast.Return):
                has_return = True
    if has_bare_raise:
        return "re-raise", False
    if has_log:
        return "log", False
    if has_continue:
        return "continue", False
    if has_return:
        return "return", False
    return "pass", True


def extract_except_sites(tree: ast.Module, module_path: str) -> List[ExceptSite]:
    """Every `except` handler in `tree`, classified (COLLECT-6).

    For each `ast.ExceptHandler`: the exception type it catches
    (`_exception_type_name`), how its body reacts (`_classify_except_body`),
    and whether that reaction is fail-open (silent swallow — `is_fail_open`).
    This is pure AST, so every `ExceptSite` is `provenance="static"` by
    construction (COLLECT-1) — Pass B/the LLM never touches this list.

    `location` is `"{module_path}:{lineno}"`, where `lineno` is the
    handler's own line (the `except ...:` line), matching the convention
    the AC references (`coder.py:718`, `coder.py:866`) use.
    """
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    # Stable order (COLLECT-3 determinism): by source line, numerically —
    # not by the formatted `location` string, which would sort "...:10"
    # before "...:9".
    handlers.sort(key=lambda h: h.lineno)

    sites: List[ExceptSite] = []
    for node in handlers:
        body_kind, is_fail_open = _classify_except_body(node.body)
        sites.append(
            ExceptSite(
                location=f"{module_path}:{node.lineno}",
                exception_type=_exception_type_name(node),
                body_kind=body_kind,
                is_fail_open=is_fail_open,
            )
        )
    return sites
