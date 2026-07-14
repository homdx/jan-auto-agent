"""tools/collect/scanner.py — COLLECT-4: AST-scanner (modules, symbols, imports).
Extended by COLLECT-25 with Java 17+ dispatch (EPIC H).

Pass A's entry point (EPIC B). No LLM anywhere in this module — every
`ModuleRecord` it produces is built directly from a language parser (`ast`
for Python, `tree-sitter-java` for Java), so every field on it is
`provenance="static"` by construction (COLLECT-1).

Tree walking is reused, not reimplemented: `RepoIngestor.walk`
(`tools/auto/repo_ingest.py`) already knows this codebase's skip-dirs /
max-depth / max-file-size conventions, so `scan_repo` calls it rather than
rolling a second, possibly-divergent walk. This module adds the
extension-based language filter (`tools.collect.lang.detect_language`, in
place of the old hardcoded `.py`-suffix check) and the per-language parse
step on top.

A file that fails to parse (`SyntaxError` for Python, an unparseable tree
for Java) is recorded as a `ModuleRecord` with `parse_error` set and empty
`public_symbols`/`imports` — it does not abort the scan. That's the
COLLECT-4 AC (and, per COLLECT-25, the same guarantee for `.java`): one
broken file must never take down coverage of the rest of the tree.
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
from tools.collect.dataflow import extract_guarded_accesses
from tools.collect.java_parser import parse_java
from tools.collect.lang import Language, detect_language
from tools.collect.model import ModuleRecord


def scan_module(source: str, module_path: str) -> ModuleRecord:
    """Build one Python `ModuleRecord` from already-read `source` text.

    Kept separate from file I/O so it's directly unit-testable against a
    literal source string (and reusable by later Pass A stages —
    COLLECT-5/6/7 — that need the same parsed tree without re-reading the
    file from disk).
    """
    try:
        tree = ast.parse(source, filename=module_path)
    except SyntaxError as exc:
        return ModuleRecord(path=module_path, parse_error=str(exc), language=Language.PYTHON)
    return ModuleRecord(
        path=module_path,
        public_symbols=tuple(extract_symbols(tree, module_path)),
        imports=tuple(extract_imports(tree)),
        config_reads=tuple(extract_config_reads(tree, module_path)),
        except_sites=tuple(extract_except_sites(tree, module_path)),
        guarded_accesses=tuple(extract_guarded_accesses(tree, module_path)),
        language=Language.PYTHON,
    )


def scan_java_module(source: str, module_path: str) -> ModuleRecord:
    """Build one Java `ModuleRecord` from already-read `source` text.

    COLLECT-25 only wires up parsing + the `parse_error` contract; the
    actual symbol/import/except/guarded-access extractors land in
    COLLECT-26/27 on top of the tree `java_parser.parse_java` returns access
    to. Until then a successfully-parsed Java file intentionally produces an
    *empty-but-valid* record (all structural tuples empty, no `parse_error`)
    rather than a record full of guessed facts — "nothing extracted yet"
    must never look like "nothing exists here" (`parse_error` unset) or
    "this file is broken" (`parse_error` set) when neither is true.
    """
    result = parse_java(source, module_path)
    if result.error is not None:
        return ModuleRecord(path=module_path, parse_error=result.error, language=Language.JAVA)
    if result.has_error:
        # tree-sitter recovered a tree but flagged internal ERROR nodes —
        # record this as a parse_error too (COLLECT-25's own scope stops at
        # "don't extract facts from an unreliable tree"; COLLECT-26/27 can
        # later extract from the *unaffected* regions of such a tree if
        # that turns out to be worth the extra complexity).
        return ModuleRecord(
            path=module_path,
            parse_error=f"Java source has syntax errors: {module_path}",
            language=Language.JAVA,
        )
    return ModuleRecord(path=module_path, language=Language.JAVA)


def scan_repo(
    root: Path,
    *,
    config: Optional[configparser.ConfigParser] = None,
) -> List[ModuleRecord]:
    """Walk `root` (via `RepoIngestor.walk`, filtered by
    `lang.detect_language`) and return one `ModuleRecord` per recognized
    file, sorted by path — the same stable order
    `tools/collect/manifest.discover_files` produces, so Pass A output and
    the manifest's file list line up.

    A file whose extension `detect_language` doesn't recognize (anything
    other than `.py`/`.java` today) is silently excluded from the walk, the
    same as an unsupported extension always was before COLLECT-25 — this
    is a filter, not a new failure mode.
    """
    root = Path(root)
    ingestor = RepoIngestor(root, config)
    scannable = sorted(p for p in ingestor.walk() if detect_language(p) is not None)

    modules: List[ModuleRecord] = []
    for rel in scannable:
        source = (root / rel).read_text(encoding="utf-8")
        language = detect_language(rel)
        if language == Language.JAVA:
            modules.append(scan_java_module(source, rel))
        else:
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
