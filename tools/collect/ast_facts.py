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
from typing import List

from tools.collect.model import FunctionRecord

#: AST node types that count as a "public symbol" for COLLECT-4 — top-level
#: functions/classes. Nested defs (methods, closures) are intentionally out
#: of scope for this task; they're not part of a module's public surface.
_SYMBOL_NODE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


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
