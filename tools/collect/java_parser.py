"""tools/collect/java_parser.py — COLLECT-25: tree-sitter-java wrapper.

A thin, fault-tolerant wrapper around `tree-sitter`/`tree-sitter-java` —
this module's entire job is turning "parse this Java source" into a value
(`JavaParseResult`), never an exception, mirroring the contract
`ast.parse`/`SyntaxError` already gives `scanner.scan_module` for Python.
COLLECT-26/27 build the actual symbol/import/except/guarded-access
extractors on top of the `tree` this returns; nothing here interprets the
tree's contents.

Why tree-sitter over `javalang` (the previous draft's choice, per this
epic's own design note): `javalang`'s grammar lags behind Java 17+ syntax
(records, sealed types, pattern-matching `switch`) — the exact constructs
this epic exists to support — while tree-sitter-java tracks current Java
grammar and, by design, never *refuses* to parse: malformed input still
produces a tree, just one with `has_error=True` on the affected nodes,
which is what lets `scan_java_module` give a broken `.java` file the same
"recorded, not fatal" treatment a Python `SyntaxError` gets.

The `tree-sitter`/`tree-sitter-java` packages are optional (see
`requirements.txt`): importing this module must never raise just because
they aren't installed, since the existing Python-only collect pipeline
has no reason to require a Java toolchain. `is_available()` is the
sanctioned way to check before relying on `parse_java` doing real work;
`parse_java` itself degrades to a `JavaParseResult.error` either way
rather than letting an `ImportError` escape into a repo scan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

try:
    import tree_sitter_java as _tree_sitter_java
    from tree_sitter import Language as _TSLanguage
    from tree_sitter import Parser as _TSParser
except ImportError:
    _tree_sitter_java = None
    _TSLanguage = None
    _TSParser = None


@dataclass(frozen=True)
class JavaParseResult:
    """One `parse_java` outcome.

    `tree` — the tree-sitter `Tree`, or `None` when `error` is set
    (parsing never ran at all: the library isn't installed, or the
    underlying parser raised something unexpected).
    `error` — set only for that *hard* failure case. tree-sitter's whole
    design point is that syntactically invalid source is *not* a hard
    failure — it still parses to a tree — so `error` being set is a much
    rarer condition than "this Java file has a typo"; see `has_error`.
    `has_error` — `True` when tree-sitter recovered a tree but flagged
    internal ERROR/MISSING nodes: the source has a real syntax problem,
    but parsing still produced a tree. `scan_java_module` turns this into
    the same `parse_error` contract Python's `SyntaxError` path uses, so
    from `ModuleRecord`'s perspective a broken `.java` file and a broken
    `.py` file look identical: recorded, not fatal, empty structural
    fields, no facts extracted from an unreliable tree.
    """

    tree: Optional[Any]
    error: Optional[str]
    has_error: bool = False


def is_available() -> bool:
    """Whether `tree-sitter` and `tree-sitter-java` are both importable
    in this environment.

    Checked once per call rather than cached as a module-level bool: the
    check itself is just an attribute comparison (no I/O, no re-import
    attempt), and a live check is what lets `tests/test_collect_java_
    parser.py`'s `pytest.mark.skipif(not is_available(), ...)` mean what
    it says regardless of import order across a test session.
    """
    return _tree_sitter_java is not None and _TSLanguage is not None and _TSParser is not None


_parser_singleton: Optional["_TSParser"] = None


def _get_parser() -> "_TSParser":
    """Lazily build and cache the tree-sitter Java `Parser`.

    Built once per process, not once per file: constructing the
    `Language`/`Parser` pair has a small fixed cost not worth repeating
    per `parse_java` call across a whole-repo scan. Only ever called after
    `is_available()`-equivalent guards elsewhere in this module have
    already confirmed the import succeeded, so it doesn't re-check here.
    """
    global _parser_singleton
    if _parser_singleton is None:
        language = _TSLanguage(_tree_sitter_java.language())
        _parser_singleton = _TSParser(language)
    return _parser_singleton


def parse_java(source: str, module_path: str) -> JavaParseResult:
    """Parse `source` (one `.java` file's already-decoded text) with
    tree-sitter-java.

    Never raises. `module_path` isn't consulted by the parser itself
    (tree-sitter needs only the source bytes) — it's accepted so an
    `error` message can name the offending file, mirroring `ast.parse
    (source, filename=...)`'s signature shape for the analogous Python
    entry point in `scanner.scan_module`.
    """
    if not is_available():
        return JavaParseResult(
            tree=None,
            error=(
                f"tree-sitter-java is not installed; cannot parse {module_path} "
                "(pip install tree-sitter tree-sitter-java)"
            ),
        )
    try:
        parser = _get_parser()
        # tree-sitter operates on bytes, not `str`; `errors="replace"` is
        # belt-and-suspenders (by the time `source` reaches this function
        # it has already been successfully decoded once by the caller —
        # see scanner.scan_repo — so re-encoding to UTF-8 should never
        # actually hit a bad character), not a substitute for scan_repo's
        # own read_text(encoding="utf-8").
        tree = parser.parse(source.encode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 - a parser crash degrades to `error`, never aborts the scan
        return JavaParseResult(tree=None, error=f"{module_path}: {exc}")
    return JavaParseResult(tree=tree, error=None, has_error=bool(tree.root_node.has_error))
