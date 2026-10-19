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
from typing import FrozenSet, List, Optional

from tools.auto.repo_ingest import RepoIngestor
from tools.collect.manifest import skip_dirs_from_config as _collect_skip_dirs
from tools.collect.ast_facts import (
    extract_config_reads,
    extract_except_sites,
    extract_from_import_names,
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


#: The collect stage's own walk limits, read from `[collect]` (HARDEN-24 B). Both default at
#: least as wide as the values the stage used to borrow from `[search]` (depth 2, 500 KB),
#: so a `[collect]`-less config collects at least as much as before; a repo that nests its
#: sources three folders deep or carries a generated 600 KB file no longer loses it.
DEFAULT_COLLECT_MAX_DEPTH = 6
DEFAULT_COLLECT_MAX_FILE_KB = 4096


def _collect_int(config: Optional[configparser.ConfigParser], key: str, default: int) -> int:
    """One `[collect]` int limit, fail-open: an absent key, a non-int or a negative value
    gives the default rather than raising into a run (a malformed key is "no collect data",
    never an abort)."""
    if config is None:
        return default
    try:
        raw = config.get("collect", key, fallback=None)
    except (configparser.Error, KeyError, TypeError):
        return default
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return value if value > 0 else default


def collect_walk_limits(config: Optional[configparser.ConfigParser]) -> tuple[int, int]:
    """The `(max_depth, max_file_kb)` the collect walk uses: `[collect] max_depth` and
    `[collect] max_file_kb`, each defaulting to at least the `[search]` values the stage
    used to read. The Architect keeps its own `[search]`/`[architect]` numbers — this pair
    is the collect stage's, and nothing else reads it."""
    return (
        _collect_int(config, "max_depth", DEFAULT_COLLECT_MAX_DEPTH),
        _collect_int(config, "max_file_kb", DEFAULT_COLLECT_MAX_FILE_KB),
    )


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
        from_imports=tuple(extract_from_import_names(tree)),
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
        # Tree-sitter recovered a tree but flagged error nodes: record a parse_error rather than trust facts from it.
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


def scan_file(
    source: str,
    module_path: str,
    *,
    config: Optional[configparser.ConfigParser] = None,
) -> ModuleRecord:
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

    `config` is accepted (and should be passed by every caller that has
    one) for the exact same reason `scan_repo`'s own `_language()` helper
    checks `java_extensions_from_config(config)` before falling back to
    `detect_language`: a project with a non-default `[collect]
    java_extensions` (e.g. `.jav`) would otherwise hit this function's
    `detect_language`-only check, fail to recognize the file as Java, and
    reproduce the exact "valid Java recorded as broken Python"
    misclassification this function exists to prevent — just for a
    custom extension instead of the default one. BUGFIX: `scan_file` used
    to ignore `config` entirely (it didn't even accept the parameter),
    so `cli.action_module`'s `--module <path>` on a custom-extension Java
    file silently overwrote a correct `language="java"` record with a
    bogus `language="python"`/`parse_error` one, even though a full
    `scan_repo` pass on the same tree got it right.

    An extension neither `java_extensions_from_config(config)` nor
    `detect_language` recognizes falls back to the Python path
    (`scan_module`) rather than raising — matches this function's
    callers, which only ever invoke it for a path they've already
    decided is scannable one way or another; this is a last-resort
    default, not a silent misclassification of a file nothing upstream
    would have selected in the first place.
    """
    if Path(module_path).suffix.lower() in java_extensions_from_config(config):
        return scan_java_module(source, module_path)
    if detect_language(module_path) == Language.JAVA:
        return scan_java_module(source, module_path)
    return scan_module(source, module_path)


def language_for(
    rel_path: str,
    config: Optional[configparser.ConfigParser] = None,
    *,
    java_exts: Optional[FrozenSet[str]] = None,
) -> Optional[str]:
    """`scan_repo`'s own extension/language decision, factored out so a
    single file can be classified the exact same way the walk does:
    `[collect] java_extensions` first (it can widen the fixed `.java`
    entry to project-specific generated-source extensions `detect_language`
    does not know about), then `detect_language`. `None` means "this
    extension recognizes no language at all".

    Two callers, deliberately: `scan_repo`'s own walk filter, and
    `cli.action_module`, which uses it to reject a `--module <path>` whose
    extension no language in this collector recognizes at all (a
    `README.md`, a config file, anything without an extension). It checks
    *recognition*, not `[collect] languages` enablement, on purpose —
    `--module Foo.java` on a Python-only repo is the documented COLLECT-28
    escape hatch for patching one Java file in by hand, and a full scan
    would not have recorded it either way.

    `java_exts` is `java_extensions_from_config(config)` already resolved —
    `scan_repo` resolves it once for the whole walk rather than re-reading
    the config for every file."""
    if java_exts is None:
        java_exts = java_extensions_from_config(config)
    if Path(rel_path).suffix.lower() in java_exts:
        return Language.JAVA
    return detect_language(rel_path)


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

    HARDEN-24 B: the walk runs on the collect stage's own `[collect]`
    limits, not on the Architect's `[search]` numbers. The files those
    limits left out are returned by `scan_repo_ex` and recorded on the
    manifest, so a depth or a size limit says so instead of dropping the
    file from the model with no word of it.
    """
    return _scan_repo(root, config)[0]


def scan_repo_ex(
    root: Path,
    *,
    config: Optional[configparser.ConfigParser] = None,
) -> "tuple[List[ModuleRecord], List[tuple[str, str]]]":
    """`scan_repo` plus `(path, reason)` for every file its walk skipped (HARDEN-24 B).

    The reasons are the walk's own: `"too deep"` (past `[collect] max_depth`),
    `"too large"` (past `[collect] max_file_kb`) and `"unreadable"` (the walk
    could not stat it). Sorted by path, and empty when nothing was skipped —
    which is also what a config that names no limits gives, since a malformed
    limit degrades to the default rather than raising.
    """
    modules, skipped = _scan_repo(root, config)
    return modules, sorted(skipped)


def _scan_repo(
    root: Path,
    config: Optional[configparser.ConfigParser],
) -> "tuple[List[ModuleRecord], List[tuple[str, str]]]":
    root = Path(root)
    # skip_dirs prunes the walk itself, so the artifact's module list and the manifest agree; no key means the defaults.
    # The depth and size limits are the collect stage's own (HARDEN-24 B), and the walk is asked to
    # name what it leaves out rather than drop it silently.
    max_depth, max_file_kb = collect_walk_limits(config)
    ingestor = RepoIngestor(
        root, config,
        extra_skip_dirs=_collect_skip_dirs(config),
        max_depth=max_depth,
        max_file_kb=max_file_kb,
        collect_skips=True,
    )
    enabled = enabled_languages(config)
    java_exts = java_extensions_from_config(config)

    def _language(rel_path: str) -> Optional[str]:
        return language_for(rel_path, config, java_exts=java_exts)

    scannable = sorted(
        p for p in ingestor.walk()
        if (lang := _language(p)) is not None and lang in enabled
    )

    modules: List[ModuleRecord] = []
    for rel in scannable:
        try:
            # Read as utf-8-sig: a BOM-prefixed file (Notepad, some IDEs) must parse.
            source = (root / rel).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            # An unreadable or non-UTF-8 file is recorded as a parse_error, not a scan abort; the other modules are kept.
            language = Language.JAVA if _language(rel) == Language.JAVA else Language.PYTHON
            modules.append(ModuleRecord(path=rel, parse_error=f"{rel}: {exc}", language=language))
            continue
        # A bug in symbol extraction must not abort the scan of other files: catch it per file.
        try:
            if _language(rel) == Language.JAVA:
                modules.append(scan_java_module(source, rel))
            else:
                modules.append(scan_module(source, rel))
        except Exception as exc:  # noqa: BLE001 — fail-open by design, see above
            language = Language.JAVA if _language(rel) == Language.JAVA else Language.PYTHON
            modules.append(
                ModuleRecord(
                    path=rel,
                    parse_error=f"{rel}: symbol extraction crashed — {exc}",
                    language=language,
                )
            )
    return modules, list(ingestor.skipped_files)


def scan_repo_payload(root: Path, *, config: Optional[configparser.ConfigParser] = None):
    """The structural JSON payload for the whole tree: a list of
    `ModuleRecord.to_dict()`, in the same stable path order `scan_repo`
    returns. This is what COLLECT-3's canonicalization/golden-fixture
    harness serializes, and what `scan_repo` itself supersedes
    `tests/_pass_a_stub.py` for (see that module's docstring).
    """
    return [m.to_dict() for m in scan_repo(root, config=config)]
