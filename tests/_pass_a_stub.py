"""tests/_pass_a_stub.py — minimal placeholder "Pass A" for the COLLECT-3
determinism harness.

COLLECT-3 (EPIC A) must land *before* the real AST scanner (COLLECT-4,
EPIC B) exists, but its own test needs something calling itself "Pass A" to
run twice and prove byte-identical. This module is exactly that: a small,
intentionally minimal AST walk (symbols + imports only — no except/guard/
config classification, which is COLLECT-4/5/6/7's job) built only to
exercise `tools.collect._determinism` and `tools.collect.model` against the
`collect_mini_repo` fixture.

This is test scaffolding, not production code — the real scanner in
`tools/collect/scanner.py` supersedes it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List

from tools.collect.manifest import discover_files
from tools.collect.model import FunctionRecord, ModuleRecord


def _extract_symbols(tree: ast.Module, module_path: str) -> List[FunctionRecord]:
    symbols = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
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
    # then name as a tiebreaker.
    return sorted(symbols, key=lambda s: (s.lineno, s.qualname))


def _extract_imports(tree: ast.Module) -> List[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return sorted(names)


def run_pass_a(repo_root: Path) -> List[ModuleRecord]:
    """Walk `repo_root`'s `*.py` files (via the same `discover_files` the
    manifest uses, so ordering is identical and already sorted) and return
    one `ModuleRecord` per file, in path order."""
    repo_root = Path(repo_root)
    modules: List[ModuleRecord] = []
    for rel in discover_files(repo_root):
        source = (repo_root / rel).read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            modules.append(ModuleRecord(path=rel, parse_error=str(exc)))
            continue
        modules.append(
            ModuleRecord(
                path=rel,
                public_symbols=tuple(_extract_symbols(tree, rel)),
                imports=tuple(_extract_imports(tree)),
            )
        )
    return modules


def pass_a_payload(repo_root: Path) -> List[Dict[str, Any]]:
    """The structural JSON payload for the whole tree: a list of
    `ModuleRecord.to_dict()`, in the same stable path order `run_pass_a`
    returns."""
    return [m.to_dict() for m in run_pass_a(repo_root)]
