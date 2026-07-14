"""tools/collect/graph.py — COLLECT-8: import/call graph + reverse-index.

Builds two graphs over the module list Pass A already produced (EPIC B),
and the one derived structure every later consumer needs from them: the
reverse import index (``imported_by``, a.k.a. blast-radius).

Two edge kinds:

* **Import edges** (`import_edges`) — a straight consequence of
  `ModuleRecord.imports` (COLLECT-4): for each dotted import name, resolve
  it to a module path *inside this repo* (`resolve_import`) and drop it if
  it isn't one (stdlib/third-party imports don't add to blast-radius here).
  This is pure static-fact composition — no new AST walk, no LLM — so it
  stays `provenance="static"` in spirit even though the graph itself isn't
  a `model.py` record type (it's a plain dict of frozensets, the shape the
  loader/query-API in EPIC G actually wants to consume).

* **Call edges** (`build_call_edges`) — a second, explicitly *heuristic*
  pass: for each module, walk its own source for `ast.Call` sites and
  check whether the called name is an **unambiguous** public symbol
  (COLLECT-4) owned by exactly one *other* module. If so, record an edge
  from the caller's module to that owner. Any call name that maps to zero
  or more-than-one module is skipped — an ambiguous guess is worse than a
  missing edge (the same "skip rather than misattribute" principle
  `ast_facts._simple_literal_assignments` already uses). This is a
  best-effort call graph, not a claim of exhaustive resolution; it is not
  used anywhere in the antihallucination chain (COLLECT-17/22 rely on
  `guarded_accesses` and the fail-open/contract registries, not on this).

``imported_by`` is the reverse index of `import_edges` — the COLLECT-8 AC
is that it's available for *every* module, including modules with zero
importers, so a caller never needs a defensive `.get(path, ())`.

``entry_points`` are simply the modules nothing else in this repo imports:
the natural roots for a call-graph walk (typically `main.py` and any
standalone scripts).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Set

from tools.collect.model import ModuleRecord

#: Dict[module_path, frozenset[module_path]] — the shape every graph in
#: this module produces and consumes: adjacency by relative file path.
Graph = Dict[str, FrozenSet[str]]


def _module_dotted_name(path: str) -> str:
    """`"tools/collect/model.py"` -> `"tools.collect.model"`;
    `"com/example/Point.java"` -> `"com.example.Point"` (COLLECT-26).

    A package's `__init__.py` maps to the *package's* dotted name
    (`"tools/collect/__init__.py"` -> `"tools.collect"`), since that's what
    both `import tools.collect` and the coarser `from tools.collect import
    model` (recorded by `ast_facts.extract_imports` as just `"tools.collect"`
    — see that module's docstring) actually refer to.

    No separate Java branch exists here on purpose, not by oversight: Java
    has no `__init__.py`-equivalent "this file stands for the whole
    package" special case to account for, so the same "replace the file's
    own stem, dot-join the path" rule that already handles every other
    Python file also produces exactly Java's own package+class-name FQN
    convention for a `.java` file — `com/example/Point.java`'s stem is
    `Point`, and `"com.example.Point"` is precisely what `import
    com.example.Point;` (COLLECT-26's `java_facts.extract_java_imports`)
    and a static import's class-qualified prefix both already look like.
    `resolve_import`'s longest-prefix fallback then handles a Java static
    import's trailing `.member` (`import static com.example.Utils.
    helper;` -> `"com.example.Utils.helper"`) the same way it already
    handles Python's `from x.y import z` coarsening — stripping one
    trailing component at a time until a real module matches.
    """
    p = Path(path)
    parts = list(p.parts)
    if parts and parts[-1] == "__init__.py":
        parts = parts[:-1]
    elif parts:
        parts[-1] = p.stem
    return ".".join(parts)


def build_module_index(modules: Iterable[ModuleRecord]) -> Dict[str, str]:
    """`{dotted_name: module_path}` for every module in `modules`."""
    return {_module_dotted_name(m.path): m.path for m in modules}


def resolve_import(dotted: str, index: Dict[str, str]) -> Optional[str]:
    """Resolve one imported dotted name to a local module path, or `None`
    if it names something outside this repo (stdlib/third-party).

    Tries an exact match first (`"tools.prompt_parser"` ->
    `"tools/prompt_parser.py"` — the common case for both `import
    tools.prompt_parser` and `from tools.prompt_parser import X`, since
    `extract_imports` records the *source module* either way). Falls back
    to the longest matching package prefix, so the coarser form recorded
    for `from tools.collect import model` (just `"tools.collect"`) still
    resolves to the package's `__init__.py` instead of being dropped as
    external.
    """
    if dotted in index:
        return index[dotted]
    parts = dotted.split(".")
    while len(parts) > 1:
        parts.pop()
        prefix = ".".join(parts)
        if prefix in index:
            return index[prefix]
    return None


def import_edges(modules: Iterable[ModuleRecord]) -> Graph:
    """`module_path -> frozenset(local module paths it imports)`.

    Every module in `modules` gets an entry, even one with no local
    imports (empty frozenset) — the graph is total over the input set, so
    `imported_by` below never has to guess at membership.
    """
    modules = list(modules)
    index = build_module_index(modules)
    edges: Dict[str, Set[str]] = {m.path: set() for m in modules}
    for m in modules:
        for dotted in m.imports:
            resolved = resolve_import(dotted, index)
            if resolved is not None and resolved != m.path:
                edges[m.path].add(resolved)
    return {path: frozenset(targets) for path, targets in edges.items()}


def imported_by(edges: Graph) -> Graph:
    """Reverse index of `edges`: `module_path -> frozenset(modules that
    import it)` — a module's blast-radius (COLLECT-8 AC).

    Total over the same key set as `edges`: every module `edges` mentions
    (as a source *or* as a target) gets an entry, defaulting to an empty
    frozenset when nothing imports it. That symmetry — every path that
    appears anywhere in `edges` also appears as a key in the result, and
    `b in imported_by(edges)[a]` iff `a in edges[b]` — is exactly what
    `test_collect_graph.py` checks.
    """
    reverse: Dict[str, Set[str]] = {path: set() for path in edges}
    for src, targets in edges.items():
        for tgt in targets:
            reverse.setdefault(tgt, set()).add(src)
    return {path: frozenset(srcs) for path, srcs in reverse.items()}


def entry_points(edges: Graph, reverse: Optional[Graph] = None) -> List[str]:
    """Modules nothing else in this repo imports (zero importers) — the
    natural roots for a call-graph walk. `main.py`/standalone scripts are
    the typical members. Sorted for determinism (COLLECT-3).
    """
    reverse = imported_by(edges) if reverse is None else reverse
    return sorted(path for path, importers in reverse.items() if not importers)


def _called_name(node: ast.Call) -> Optional[str]:
    """The bare name being called: `foo(...)` -> `"foo"`, `obj.foo(...)` ->
    `"foo"` (only the final attribute — same convention
    `ast_facts._is_log_call` uses, since the receiver's alias varies).
    Anything else (a call on a call result, a subscript, ...) -> `None`.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _unambiguous_symbol_owners(modules: Iterable[ModuleRecord]) -> Dict[str, str]:
    """`{short_symbol_name: owning_module_path}`, restricted to symbol names
    owned by exactly one module in `modules`. A name defined in two or more
    modules (e.g. two different `run()` functions) is deliberately left out
    — skip rather than misattribute (see module docstring).
    """
    owners: Dict[str, Set[str]] = {}
    for m in modules:
        for sym in m.public_symbols:
            name = sym.qualname.split(":")[-1]
            owners.setdefault(name, set()).add(m.path)
    return {name: next(iter(paths)) for name, paths in owners.items() if len(paths) == 1}


def build_call_edges(root: Path, modules: Iterable[ModuleRecord]) -> Graph:
    """Best-effort call graph: `module_path -> frozenset(module paths whose
    unambiguous public symbols it calls by name)`.

    Re-reads and re-parses each non-`parse_error` module's source under
    `root` (Pass A's `ModuleRecord` doesn't retain the AST/source — see
    `scanner.py`), which keeps this call separate/optional from the import
    graph above rather than forcing every `import_edges` caller to pay for
    a second parse. A module that fails to read or re-parse is silently
    skipped for call-edge purposes (it already has a `parse_error` from
    Pass A, or the file moved out from under us) — this function never
    raises out of a bad file, matching COLLECT-4's "one broken file can't
    take down the scan" AC.
    """
    modules = list(modules)
    root = Path(root)
    owners = _unambiguous_symbol_owners(modules)

    edges: Dict[str, Set[str]] = {m.path: set() for m in modules}
    for m in modules:
        if m.parse_error:
            continue
        try:
            source = (root / m.path).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=m.path)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name is None:
                continue
            target = owners.get(name)
            if target is not None and target != m.path:
                edges[m.path].add(target)
    return {path: frozenset(targets) for path, targets in edges.items()}
