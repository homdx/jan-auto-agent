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


def extract_imports(tree: ast.Module) -> List[str]:
    """Sorted, deduplicated list of module names imported anywhere in `tree`.

    `import a.b.c` contributes `"a.b.c"`; `from x.y import z` contributes
    `"x.y"` (the source module, not the imported name) — mirroring the
    convention `tests/_pass_a_stub.py` establishes and COLLECT-3's golden
    fixture already encodes.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
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
        for sub in ast.walk(stmt):
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
