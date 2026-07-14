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

from tools.collect.model import ConfigRead, FunctionRecord

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


def extract_config_reads(tree: ast.Module, module_path: str) -> List[ConfigRead]:
    """Every `config.get*(section, key, fallback=...)` call site in `tree`
    (COLLECT-5).

    Recognizes the four `ConfigParser` reader methods (`get`, `getint`,
    `getboolean`, `getfloat`) — but only when a `fallback=` keyword (or a
    third positional arg) is present, which is what separates a config
    read from an unrelated `.get(...)` call (`dict.get`, `os.environ.get`,
    ...) using the same method name. `section` must be a string literal to
    be recorded (a dynamically-computed section can't be attributed
    statically, so those call sites are skipped rather than guessed at).

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
        if not (isinstance(func, ast.Attribute) and func.attr in _CONFIG_READ_METHODS):
            continue

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
    # Stable order (COLLECT-3 determinism): by section then key.
    return sorted(reads, key=lambda r: (r.section, r.key))
