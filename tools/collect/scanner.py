"""tools/collect/scanner.py — COLLECT-4: AST-scanner (modules, symbols, imports).
Extended by COLLECT-25 with Java 17+ dispatch (EPIC H), by COLLECT-28 with
the `[collect] languages` opt-in toggle.

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

Java scanning is opt-in (COLLECT-28): `scan_repo` only walks `.java`
files when `[collect] languages` (`lang.enabled_languages`) includes
`"java"` — the default, and the behavior with no config at all, is
Python-only, unchanged from before COLLECT-25 existed. `scan_file` below
is the single-file dispatcher `scan_repo`'s own loop and `cli.action_
module`'s `--module <path>` both use, so a `--module Foo.java` call
routes through `scan_java_module` rather than misparsing Java as broken
Python — the same guarantee, reachable from two different entry points
instead of only one.
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
from tools.collect.java_facts import extract_java_imports, extract_java_symbols
from tools.collect.java_parser import parse_java
from tools.collect.lang import (
    Language,
    detect_language,
    enabled_languages,
    java_extensions_from_config,
)
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

    COLLECT-25 wired up parsing + the `parse_error` contract; COLLECT-26
    adds the symbol/import extractors on top of the tree `java_parser.
    parse_java` returns access to. `except_sites`/`guarded_accesses` stay
    empty for Java until COLLECT-27 lands their extractors — an *absent*
    fact, not a wrong one: nothing here claims a Java file has no
    fail-open catches or unguarded accesses, only that Pass A hasn't
    looked for them yet, the same "nothing extracted" vs. "nothing
    exists" distinction COLLECT-25's own docstring drew for the
    then-fully-empty record.
    """
    result = parse_java(source, module_path)
    if result.error is not None:
        return ModuleRecord(path=module_path, parse_error=result.error, language=Language.JAVA)
    if result.has_error:
        # tree-sitter recovered a tree but flagged internal ERROR nodes —
        # record this as a parse_error too (extracting facts from an
        # unreliable tree risks exactly the kind of unchecked claim
        # COLLECT-1 exists to prevent).
        return ModuleRecord(
            path=module_path,
            parse_error=f"Java source has syntax errors: {module_path}",
            language=Language.JAVA,
        )
    return ModuleRecord(
        path=module_path,
        public_symbols=tuple(extract_java_symbols(result.tree, module_path)),
        imports=tuple(extract_java_imports(result.tree)),
        language=Language.JAVA,
    )


def scan_file(source: str, module_path: str) -> ModuleRecord:
    """Dispatch to `scan_java_module` or `scan_module` by `module_path`'s
    extension (COLLECT-28).

    The one place both `scan_repo`'s own loop and `cli.action_module`'s
    `--module <path>` route a single already-read file through, so a
    `.java` path reaches `scan_java_module` regardless of which entry
    point is calling — before this existed, `action_module` called
    `scan_module` (the Python-only, `ast.parse`-based path) directly, so
    `--module Foo.java` would `ast.parse` valid Java source, fail with a
    `SyntaxError`, and record it as broken *Python*, mischaracterizing a
    perfectly valid Java file as unparseable code in the wrong language
    entirely.

    An extension `detect_language` doesn't recognize falls back to the
    Python path (`scan_module`) rather than raising — matches this
    function's callers, which only ever invoke it for a path they've
    already decided is scannable one way or another; this is a last-
    resort default, not a silent misclassification of a file nothing
    upstream would have selected in the first place.
    """
    if detect_language(module_path) == Language.JAVA:
        return scan_java_module(source, module_path)
    return scan_module(source, module_path)


def scan_repo(
    root: Path,
    *,
    config: Optional[configparser.ConfigParser] = None,
) -> List[ModuleRecord]:
    """Walk `root` (via `RepoIngestor.walk`, filtered by
    `lang.detect_language` *and* `[collect] languages` — COLLECT-28) and
    return one `ModuleRecord` per recognized, enabled-language file,
    sorted by path — the same stable order
    `tools/collect/manifest.discover_files` produces, so Pass A output and
    the manifest's file list line up.

    A file whose extension `detect_language` doesn't recognize at all
    (anything other than `.py`/`.java` today) is silently excluded from
    the walk, the same as an unsupported extension always was before
    COLLECT-25 existed. A file whose language *is* recognized but isn't
    in `lang.enabled_languages(config)` — Java, without an explicit
    `[collect] languages = python,java` opt-in — is excluded the same
    way: from `scan_repo`'s perspective there's no difference between
    "nothing here recognizes `.java`" and "something recognizes it but
    wasn't asked to look," which is exactly the point — a Python-only
    user sees zero behavior change whether or not `tree-sitter-java`
    happens to be installed.
    """
    root = Path(root)
    ingestor = RepoIngestor(root, config)
    enabled = enabled_languages(config)
    java_exts = java_extensions_from_config(config)

    def _language(rel_path: str) -> Optional[str]:
        # java_extensions can widen which suffixes count as Java beyond
        # lang.py's fixed `.java` entry (e.g. a project-specific
        # generated-sources extension); detect_language alone wouldn't
        # know about those, so check the configurable set first.
        if Path(rel_path).suffix.lower() in java_exts:
            return Language.JAVA
        return detect_language(rel_path)

    scannable = sorted(
        p for p in ingestor.walk()
        if (lang := _language(p)) is not None and lang in enabled
    )

    modules: List[ModuleRecord] = []
    for rel in scannable:
        try:
            source = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # BUGFIX: an unreadable file (permission error, a broken
            # symlink, a file deleted in a race between the walk above and
            # this read, or — most commonly — a `.py`/`.java` file that
            # simply isn't valid UTF-8) used to propagate straight out of
            # `scan_repo`, aborting the *entire* scan and losing every
            # already-collected module along with it. That directly
            # contradicts this module's own documented contract (see the
            # module docstring and `scan_module`'s `SyntaxError` handling):
            # one broken file must never take down coverage of the rest of
            # the tree. Record it the same way an unparseable file already
            # is — `parse_error` set, empty structural fields — and keep
            # going, instead of letting the read failure crash the run.
            language = Language.JAVA if _language(rel) == Language.JAVA else Language.PYTHON
            modules.append(ModuleRecord(path=rel, parse_error=f"{rel}: {exc}", language=language))
            continue
        if _language(rel) == Language.JAVA:
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
