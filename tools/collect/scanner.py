"""tools/collect/scanner.py — COLLECT-4: AST-scanner (modules, symbols, imports).

Pass A's entry point (EPIC B). No LLM anywhere in this module — every
`ModuleRecord` it produces is built directly from `ast.parse`, so every
field on it is `provenance="static"` by construction (COLLECT-1).

Tree walking is reused, not reimplemented: `RepoIngestor.walk`
(`tools/auto/repo_ingest.py`) already knows this codebase's skip-dirs /
max-depth / max-file-size conventions, so `scan_repo` calls it rather than
rolling a second, possibly-divergent walk. This module only adds the
`.py`-suffix filter and the AST parse step on top.

A file that fails to parse (`SyntaxError`) is recorded as a `ModuleRecord`
with `parse_error` set and empty `public_symbols`/`imports` — it does not
abort the scan. That's the COLLECT-4 AC: one broken file must never take
down coverage of the other 50-plus modules in this repo.
"""

from __future__ import annotations

import ast
import configparser
from pathlib import Path
from typing import List, Optional

from tools.auto.repo_ingest import RepoIngestor
from tools.collect.ast_facts import (
    extract_config_reads,
    extract_except_sites,
    extract_imports,
    extract_symbols,
)
from tools.collect.model import ModuleRecord


def scan_module(source: str, module_path: str) -> ModuleRecord:
    """Build one `ModuleRecord` from already-read `source` text.

    Kept separate from file I/O so it's directly unit-testable against a
    literal source string (and reusable by later Pass A stages —
    COLLECT-5/6/7 — that need the same parsed tree without re-reading the
    file from disk).
    """
    try:
        tree = ast.parse(source, filename=module_path)
    except SyntaxError as exc:
        return ModuleRecord(path=module_path, parse_error=str(exc))
    return ModuleRecord(
        path=module_path,
        public_symbols=tuple(extract_symbols(tree, module_path)),
        imports=tuple(extract_imports(tree)),
        config_reads=tuple(extract_config_reads(tree, module_path)),
        except_sites=tuple(extract_except_sites(tree, module_path)),
    )


def scan_repo(
    root: Path,
    *,
    config: Optional[configparser.ConfigParser] = None,
) -> List[ModuleRecord]:
    """Walk `root` (via `RepoIngestor.walk`, filtered to `*.py`) and return
    one `ModuleRecord` per file, sorted by path — the same stable order
    `tools/collect/manifest.discover_files` produces, so Pass A output and
    the manifest's file list line up.
    """
    root = Path(root)
    ingestor = RepoIngestor(root, config)
    py_files = sorted(p for p in ingestor.walk() if p.endswith(".py"))

    modules: List[ModuleRecord] = []
    for rel in py_files:
        source = (root / rel).read_text(encoding="utf-8")
        modules.append(scan_module(source, rel))
    return modules


def scan_repo_payload(root: Path, *, config: Optional[configparser.ConfigParser] = None):
    """The structural JSON payload for the whole tree: a list of
    `ModuleRecord.to_dict()`, in the same stable path order `scan_repo`
    returns. This is what COLLECT-3's canonicalization/golden-fixture
    harness serializes, and what `scan_repo` itself supersedes
    `tests/_pass_a_stub.py` for (see that module's docstring).
    """
    return [m.to_dict() for m in scan_repo(root, config=config)]
