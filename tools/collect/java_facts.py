"""tools/collect/java_facts.py — COLLECT-26: Java symbols + imports.

The Java analogue of `ast_facts.extract_symbols`/`extract_imports`
(COLLECT-4), operating on the `tree-sitter` tree `java_parser.parse_java`
(COLLECT-25) returns instead of Python's `ast.Module`. Both extractors
produce the exact same `model.py` record shapes the Python path does —
`FunctionRecord` for symbols, a `List[str]` of dotted names for imports —
so every downstream consumer (`graph.py`, `render.py`, `loader.py`, the
COLLECT-9/10/11 registries) works unmodified regardless of which language
backend produced a given `ModuleRecord`. That cross-language uniformity is
COLLECT-25's whole premise: a parallel backend, not a parallel data model.

Symbol scope, deliberately wider than Python's
--------------------------------------------------
`ast_facts.extract_symbols` only inventories *top-level* functions/classes
— Python nested `def`s are out of scope for COLLECT-4 because a Python
module's meaningful public surface lives at module level. Java has no
module-level functions at all: every method lives inside a type
declaration, so "only top-level" would mean "no methods, ever." COLLECT-26
therefore also descends into each type declaration's body and records its
methods/constructors (and any type nested inside it, recursively) — still
skipping *method bodies* themselves (a local/anonymous class defined
inside a method is out of scope, the same way a Python closure defined
inside a function body is), so the recursion has a real floor.

A nested symbol's `qualname` reflects the containing chain
(`"Foo.java:Circle.helper"` for method `helper` in class `Circle`) — the
same `ClassName.method_name` shape `tools/collect/gates.py` already uses
to cite a Java-style method by name, and what `_repo_defines_function`
there would need if this project's own gates were ever written in Java.
"""

from __future__ import annotations

from typing import List, Optional, Set

from tools.collect.model import FunctionRecord

#: tree-sitter-java node types that introduce a new named type — the
#: Java analogue of Python's `ast.ClassDef`, just four-way instead of
#: one-way (class vs. interface vs. enum vs. record all matter to the
#: JIRA AC, unlike Python where `class` is the only keyword).
_TYPE_DECL_TYPES = frozenset(
    {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}
)

#: A method or constructor — Java's `ast.FunctionDef` analogue. Both are
#: recorded as symbols; only the signature rendering differs (a
#: constructor has no return type to show).
_METHOD_DECL_TYPES = frozenset({"method_declaration", "constructor_declaration"})

#: The three explicit Java access-level keywords `_access_modifier` looks
#: for among a declaration's `modifiers` children. Anything else in that
#: node (`static`, `final`, `abstract`, `sealed`, `non-sealed`,
#: `synchronized`, an `@Annotation`, ...) isn't an access level and is
#: ignored for this purpose.
_ACCESS_KEYWORDS = frozenset({"public", "private", "protected"})

_KEYWORD_FOR_DECL_TYPE = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
}


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _modifiers_node(node):
    """The `modifiers` child of a declaration node, if it has one —
    always its first child when present, but found by type rather than
    positionally, since "always first" is a grammar convention this
    function shouldn't have to assume holds forever.
    """
    for child in node.children:
        if child.type == "modifiers":
            return child
    return None


def _access_modifier(node, *, default: str = "package-private") -> str:
    """One of `"public"`/`"protected"`/`"private"`/`"package-private"` for
    the declaration `node`.

    `default` is what an *absent* access keyword means — and that's not
    the same answer everywhere in Java: a class member with no keyword is
    package-private, but an interface member with no keyword is
    implicitly `public` (JLS §9.4) — `Shape.area()`'s abstract method
    declaration never carries an explicit `public` in idiomatic Java, but
    it unambiguously *is* public. Callers inside an interface body pass
    `default="public"`; everywhere else the ordinary class-member default
    applies.
    """
    mods = _modifiers_node(node)
    if mods is not None:
        for child in mods.children:
            if child.type in _ACCESS_KEYWORDS:
                return child.type
    return default


def _javadoc_first_line(node) -> str:
    """The first non-empty content line of `node`'s immediately preceding
    Javadoc comment (`/** ... */`), or `""` if it has none.

    Distinguishes a real Javadoc from an ordinary `/* ... */` block
    comment by the doubled leading asterisk (`/**`, not just `/*`) — the
    same convention `javadoc` tooling itself uses — and from a `//` line
    comment, which is never treated as documentation here (mirroring how
    a Python module only takes its docstring from an actual
    `ast.get_docstring`, never from a `#` comment above it).
    """
    prev = node.prev_sibling
    if prev is None or prev.type != "block_comment":
        return ""
    text = _text(prev)
    if not text.startswith("/**"):
        return ""
    inner = text[3:]
    if inner.endswith("*/"):
        inner = inner[:-2]
    for raw_line in inner.splitlines():
        line = raw_line.strip()
        if line.startswith("*"):
            line = line[1:].strip()
        if line:
            return line
    return ""


def _permits_list(node) -> Optional[str]:
    """`"Circle, Square"` for a sealed type's `permits Circle, Square`
    clause, or `None` for a non-sealed one. Reads the `permits` field
    directly (present on both `class_declaration` and
    `interface_declaration`) rather than inspecting `_access_modifier`'s
    `sealed`/`non-sealed` keywords, since the permits clause is the
    concrete fact the JIRA AC actually wants recorded, not just a boolean.
    """
    permits = node.child_by_field_name("permits")
    if permits is None:
        return None
    type_list = next((c for c in permits.children if c.type == "type_list"), None)
    if type_list is None:
        return None
    names = [_text(c) for c in type_list.children if c.type == "type_identifier"]
    return ", ".join(names)


def _type_signature(node) -> str:
    """A compact, informative one-line signature for a type declaration —
    Java's richer analogue of Python's placeholder `f"{name}(...)"`
    (`extract_symbols`): the declaration keyword, name, record components
    (if any — this *is* "records recorded with their components", not a
    separate field), and a sealed type's `permits` list, all in one
    grep-able line a human or COLLECT-16's Pass B prompt can read without
    opening the file.
    """
    keyword = _KEYWORD_FOR_DECL_TYPE[node.type]
    name = _text(node.child_by_field_name("name"))
    parts = [keyword, name]
    if node.type == "record_declaration":
        params = node.child_by_field_name("parameters")
        parts[-1] = f"{name}{_text(params) if params is not None else '()'}"
    permits = _permits_list(node)
    if permits is not None:
        parts.append(f"permits {permits}")
    return " ".join(parts)


def _method_signature(node) -> str:
    """`"sum(): int"` / `"helper(): void"` / `"Point(int x, int y)"`
    (constructor, no return type) — Java's return type is worth showing
    (unlike Python, where COLLECT-4 doesn't attempt real signature
    reconstruction at all) because tree-sitter hands it to us as a plain
    field, not something that needs type inference.
    """
    name = _text(node.child_by_field_name("name"))
    params = node.child_by_field_name("parameters")
    params_text = _text(params) if params is not None else "()"
    if node.type == "constructor_declaration":
        return f"{name}{params_text}"
    type_node = node.child_by_field_name("type")
    return_type = _text(type_node) if type_node is not None else "void"
    return f"{name}{params_text}: {return_type}"


def _method_name(node) -> str:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return _text(name_node)
    # A constructor_declaration's "name" field is present in every
    # tree-sitter-java grammar version this project has tested against,
    # but degrade to the enclosing class's own name rather than crash if
    # a future grammar update ever changes that.
    return "<init>"


def _walk_type_body(
    body_node,
    module_path: str,
    containing_qualname: str,
    symbols: List[FunctionRecord],
    *,
    member_default_access: str = "package-private",
) -> None:
    for child in body_node.children:
        if child.type in _TYPE_DECL_TYPES:
            _record_type_decl(child, module_path, containing_qualname, symbols)
        elif child.type in _METHOD_DECL_TYPES:
            _record_method_decl(
                child, module_path, containing_qualname, symbols, default_access=member_default_access
            )
        # Anything else at this level (field_declaration, a plain block,
        # comments, enum_constant, ...) isn't a symbol COLLECT-26 tracks —
        # left alone, not recursed into (a field initializer's anonymous
        # class body, like a Python closure's body, is out of scope).


def _record_type_decl(
    node,
    module_path: str,
    containing_qualname: Optional[str],
    symbols: List[FunctionRecord],
) -> None:
    name = _text(node.child_by_field_name("name"))
    qualname = f"{containing_qualname}.{name}" if containing_qualname else name
    access = _access_modifier(node)
    symbols.append(
        FunctionRecord(
            qualname=f"{module_path}:{qualname}",
            module=module_path,
            lineno=node.start_point[0] + 1,
            signature=_type_signature(node),
            docstring_first_line=_javadoc_first_line(node),
            is_private=(access != "public"),
            access_modifier=access,
        )
    )
    body = node.child_by_field_name("body")
    if body is not None:
        # JLS §9.4: an interface member with no explicit access keyword
        # is implicitly public, unlike a class member (implicitly
        # package-private) — the one place this recursion needs to know
        # what kind of type it's inside, not just walk uniformly.
        member_default = "public" if node.type == "interface_declaration" else "package-private"
        _walk_type_body(body, module_path, qualname, symbols, member_default_access=member_default)


def _record_method_decl(
    node,
    module_path: str,
    containing_qualname: str,
    symbols: List[FunctionRecord],
    *,
    default_access: str = "package-private",
) -> None:
    name = _method_name(node)
    qualname = f"{containing_qualname}.{name}"
    access = _access_modifier(node, default=default_access)
    symbols.append(
        FunctionRecord(
            qualname=f"{module_path}:{qualname}",
            module=module_path,
            lineno=node.start_point[0] + 1,
            signature=_method_signature(node),
            docstring_first_line=_javadoc_first_line(node),
            is_private=(access != "public"),
            access_modifier=access,
        )
    )


def extract_java_symbols(tree, module_path: str) -> List[FunctionRecord]:
    """Every class/interface/enum/record, plus every method/constructor
    nested inside one (at any nesting depth — a class inside a class is
    walked too), in `tree` (COLLECT-26).

    Sorted by `(lineno, qualname)`, matching `ast_facts.extract_symbols`'s
    own stable-order convention (COLLECT-3): a rerun over unchanged
    source never reorders symbols for reasons unrelated to the code.
    """
    symbols: List[FunctionRecord] = []
    root = tree.root_node
    for child in root.children:
        if child.type in _TYPE_DECL_TYPES:
            _record_type_decl(child, module_path, None, symbols)
    return sorted(symbols, key=lambda s: (s.lineno, s.qualname))


def _import_target(node) -> Optional[str]:
    is_wildcard = any(c.type == "asterisk" for c in node.children)
    name_node = next(
        (c for c in node.children if c.type in ("scoped_identifier", "identifier")), None
    )
    if name_node is None:
        return None
    text = _text(name_node)
    return f"{text}.*" if is_wildcard else text


def extract_java_imports(tree) -> List[str]:
    """Sorted, deduplicated list of every `import`ed dotted name in
    `tree` — plain (`import a.b.C;` -> `"a.b.C"`), wildcard (`import
    a.b.*;` -> `"a.b.*"`), and static (`import static a.b.C.member;` ->
    `"a.b.C.member"`, indistinguishable in the output from a plain class
    import of the same dotted depth) alike, all in the same flat
    dotted-string shape `ast_facts.extract_imports` already established
    for Python — so `graph.py`'s `resolve_import` needs no per-language
    branch to consume either one (see that module's docstring: it
    already resolves a Java static import's trailing `.member` down to
    its owning class via the exact same longest-prefix fallback built for
    Python's `from x.y import z` coarsening).

    A static import is still just *included*, not specially marked: the
    JIRA AC's "flagged, not silently dropped" means present in the
    output, not tagged with a distinguishing prefix that would break the
    "same shape as Python" contract this whole function exists to keep.
    """
    names: Set[str] = set()
    for node in _walk(tree.root_node):
        if node.type == "import_declaration":
            target = _import_target(node)
            if target is not None:
                names.add(target)
    return sorted(names)


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)
