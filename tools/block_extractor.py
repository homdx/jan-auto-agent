from __future__ import annotations

import ast
import builtins
import keyword
import re
from dataclasses import dataclass
from typing import Iterable, Optional


# ----------------------------
# Generic helpers
# ----------------------------

def _normalize_ext(file_ext: str) -> str:
    ext = (file_ext or "").strip().lower()
    if not ext:
        return ""
    if not ext.startswith("."):
        ext = "." + ext
    return ext


def _split_lines(source: str) -> list[str]:
    return source.splitlines()


def _split_lines_keepends(source: str) -> list[str]:
    return source.splitlines(keepends=True)


def _line_start_offsets(source: str) -> list[int]:
    """
    Returns the character offset for each 1-based line number.
    Index 0 is always 0 (line 1 starts at offset 0).
    """
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _line_number_from_index(offsets: list[int], index: int) -> int:
    """
    Convert a character index into a 1-based line number.
    """
    # Binary search without importing bisect to keep this file simple.
    lo, hi = 0, len(offsets) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if offsets[mid] <= index:
            lo = mid + 1
        else:
            hi = mid - 1
    return max(1, lo)


def _line_start_index(source: str, char_index: int) -> int:
    """
    Return the character index of the start of the line containing char_index.
    """
    nl = source.rfind("\n", 0, char_index)
    return 0 if nl < 0 else nl + 1


def _unique_preserve_order(items: Iterable[str]) -> list[str]:
    seen = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ----------------------------
# Python strategy
# ----------------------------

@dataclass
class _PythonTarget:
    node: ast.AST
    start_line: int
    end_line: int


class _PythonTargetFinder(ast.NodeVisitor):
    def __init__(self, target_name: str):
        self.target_name = target_name
        self.found: Optional[_PythonTarget] = None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self.found is None and node.name == self.target_name:
            self.found = _PythonTarget(node=node, start_line=self._start_line(node), end_line=getattr(node, "end_lineno", node.lineno))
        if self.found is None:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if self.found is None and node.name == self.target_name:
            self.found = _PythonTarget(node=node, start_line=self._start_line(node), end_line=getattr(node, "end_lineno", node.lineno))
        if self.found is None:
            self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self.found is None and node.name == self.target_name:
            self.found = _PythonTarget(node=node, start_line=self._start_line(node), end_line=getattr(node, "end_lineno", node.lineno))
        if self.found is None:
            self.generic_visit(node)

    @staticmethod
    def _start_line(node: ast.AST) -> int:
        """
        Include decorators directly above the definition/class line.
        """
        lineno = getattr(node, "lineno", None) or 1
        decorator_list = getattr(node, "decorator_list", []) or []
        deco_lines = [getattr(d, "lineno", lineno) for d in decorator_list]
        return min([lineno, *deco_lines]) if deco_lines else lineno


def _extract_python_block(source: str, target_name: str) -> str:
    """
    AST-first extraction. This handles:
      - decorated functions/classes
      - nested defs/classes
      - multi-line signatures
      - async defs
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Fallback to a line-based scan when the source is incomplete/broken.
        return _extract_python_block_fallback(source, target_name)

    finder = _PythonTargetFinder(target_name)
    finder.visit(tree)
    if finder.found is None:
        return ""

    lines = _split_lines_keepends(source)
    start = max(1, finder.found.start_line)
    end = max(start, finder.found.end_line)
    if start > len(lines):
        return ""
    end = min(end, len(lines))
    return "".join(lines[start - 1 : end])


def _extract_python_block_fallback(source: str, target_name: str) -> str:
    """
    Best-effort fallback for Python source that cannot be parsed cleanly.
    Uses indentation and simple signature detection.
    """
    lines = _split_lines_keepends(source)
    if not lines:
        return ""

    def_pat = re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(target_name)}\b")
    class_pat = re.compile(rf"^\s*class\s+{re.escape(target_name)}\b")

    start_idx = None
    for i, line in enumerate(lines):
        if def_pat.match(line) or class_pat.match(line):
            start_idx = i
            break
    if start_idx is None:
        return ""

    # Include decorators immediately above.
    decorator_start = start_idx
    while decorator_start > 0 and re.match(r"^\s*@\S+", lines[decorator_start - 1]):
        decorator_start -= 1

    # Find the end of the signature (colon at paren depth zero).
    sig_depth = 0
    body_start_idx = None
    for i in range(start_idx, len(lines)):
        line = lines[i]
        # Roughly ignore comments for signature scanning.
        code_part = line.split("#", 1)[0]

        for ch in code_part:
            if ch in "([{":
                sig_depth += 1
            elif ch in ")]}":
                sig_depth = max(0, sig_depth - 1)
            elif ch == ":" and sig_depth == 0:
                body_start_idx = i + 1
                break
        if body_start_idx is not None:
            break

    if body_start_idx is None:
        return "".join(lines[decorator_start:])

    base_indent = len(lines[start_idx]) - len(lines[start_idx].lstrip(" \t"))
    end_idx = len(lines)

    for i in range(body_start_idx, len(lines)):
        raw = lines[i]
        stripped = raw.strip()

        if not stripped:
            continue

        indent = len(raw) - len(raw.lstrip(" \t"))
        if indent <= base_indent:
            end_idx = i
            break

    return "".join(lines[decorator_start:end_idx])


# ----------------------------
# Brace-based strategy
# ----------------------------

def _brace_scan_end(source: str, open_brace_index: int) -> int:
    """
    Scan from the opening brace and return the index just after the matching
    closing brace. Braces inside strings/comments are ignored.

    This is generic enough for JS/TS/Go/Java/C/C++/Rust-style block syntax.
    """
    n = len(source)
    i = open_brace_index
    depth = 0

    state = "code"  # code, single, double, backtick, line_comment, block_comment, char
    escape = False

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if state == "line_comment":
            if ch == "\n":
                state = "code"
            i += 1
            continue

        if state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 2
            else:
                i += 1
            continue

        if state in {"single", "double", "backtick", "char"}:
            if escape:
                escape = False
                i += 1
                continue
            if ch == "\\":
                escape = True
                i += 1
                continue
            quote = {"single": "'", "double": '"', "backtick": "`", "char": "'"}[state]
            if ch == quote:
                state = "code"
            i += 1
            continue

        # code
        if ch == "/" and nxt == "/":
            state = "line_comment"
            i += 2
            continue
        if ch == "/" and nxt == "*":
            state = "block_comment"
            i += 2
            continue
        if ch == "'":
            state = "single"
            i += 1
            continue
        if ch == '"':
            state = "double"
            i += 1
            continue
        if ch == "`":
            state = "backtick"
            i += 1
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1

        i += 1

    return n


def _brace_candidate_patterns(target_name: str) -> list[re.Pattern[str]]:
    name = re.escape(target_name)
    return [
        # function foo(...) {
        re.compile(rf"(?m)^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+{name}\s*\("),
        # func foo(...) { / func (r T) foo(...) {
        re.compile(rf"(?m)^\s*func\s+(?:\([^)]+\)\s*)?{name}\s*\("),
        # const foo = (...) => { / let foo = async (...) => {
        re.compile(rf"(?m)^\s*(?:export\s+)?(?:const|let|var)\s+{name}\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
        # class method / constructor / free function-style defs in C-like syntax:
        #   foo(...) {
        #   async foo(...) {
        #   static foo(...) {
        re.compile(rf"(?m)^\s*(?:public|private|protected|static|async|final|export|default|\s)*{name}\s*\("),
        # C-like method/function WITH a return type (Java/C/C++/Kotlin/etc.):
        #   public String foo(    static int foo(    void foo(    List<X> foo(    int[] foo(
        # Allows optional annotations + modifiers, then a return-type token
        # (identifier with optional generics / array / dotted name), then `foo(`.
        re.compile(
            rf"(?m)^\s*"
            rf"(?:@[\w.]+(?:\([^)]*\))?\s*)*"
            rf"(?:(?:public|private|protected|static|final|abstract|synchronized|"
            rf"native|default|transient|volatile|strictfp|export|async|inline|"
            rf"virtual|const|extern|unsafe|suspend|open|override|fun|fn|pub)\s+)*"
            rf"[A-Za-z_$][\w$.]*(?:<[^>{{}}]*>)?(?:\[\s*\])*\s+"
            rf"{name}\s*\("
        ),
        # class X { foo(...) { ... } }
        re.compile(rf"(?m)^\s*{name}\s*\("),
        # type declarations in C-like / JVM / Rust syntax:
        #   public class App {   interface Foo {   struct Bar {   enum E {   record R(   trait T {
        re.compile(
            rf"(?m)^\s*(?:@[\w.]+(?:\([^)]*\))?\s*)*(?:[A-Za-z_$][\w$]*\s+)*?"
            rf"(?:class|interface|struct|enum|trait|record|object|namespace)\s+{name}\b"
        ),
    ]


def _extract_brace_block(source: str, target_name: str) -> str:
    patterns = _brace_candidate_patterns(target_name)
    best_start = None
    best_open_brace = None

    for pat in patterns:
        for match in pat.finditer(source):
            # Search for the first opening brace after the signature begins.
            scan_from = match.start()
            open_idx = None

            # Simple char-by-char scan to the first real "{"
            state = "code"
            escape = False
            i = scan_from
            while i < len(source):
                ch = source[i]
                nxt = source[i + 1] if i + 1 < len(source) else ""

                if state == "line_comment":
                    if ch == "\n":
                        state = "code"
                    i += 1
                    continue
                if state == "block_comment":
                    if ch == "*" and nxt == "/":
                        state = "code"
                        i += 2
                    else:
                        i += 1
                    continue
                if state in {"single", "double", "backtick", "char"}:
                    if escape:
                        escape = False
                        i += 1
                        continue
                    if ch == "\\":
                        escape = True
                        i += 1
                        continue
                    quote = {"single": "'", "double": '"', "backtick": "`", "char": "'"}[state]
                    if ch == quote:
                        state = "code"
                    i += 1
                    continue

                # code
                if ch == "/" and nxt == "/":
                    state = "line_comment"
                    i += 2
                    continue
                if ch == "/" and nxt == "*":
                    state = "block_comment"
                    i += 2
                    continue
                if ch == "'":
                    state = "single"
                    i += 1
                    continue
                if ch == '"':
                    state = "double"
                    i += 1
                    continue
                if ch == "`":
                    state = "backtick"
                    i += 1
                    continue
                if ch == "{":
                    open_idx = i
                    break

                i += 1

            if open_idx is None:
                continue

            start = _line_start_index(source, match.start())
            end = _brace_scan_end(source, open_idx)
            if best_start is None or start < best_start:
                best_start = start
                best_open_brace = (open_idx, end)

    if best_start is None or best_open_brace is None:
        return ""

    _, end = best_open_brace
    return source[best_start:end]


# ----------------------------
# Public API
# ----------------------------

def extract_block(source: str, target_name: str, file_ext: str) -> str:
    """
    Extract a named code block from source text for any supported language.

    Returns:
        The full code block, or "" if the target is not found.
    """
    ext = _normalize_ext(file_ext)

    if ext == ".py":
        return _extract_python_block(source, target_name)

    # Default to brace-based strategy for JS/TS/Go/Java/C/C++/Rust/etc.
    return _extract_brace_block(source, target_name)


def extract_imports(source: str, file_ext: str) -> list[str]:
    """
    Language-aware import extraction.

    Python:
        import os
        from x import y
    JS/TS:
        import ... from "..."
        require(...)
    Go:
        import "fmt"
        import alias "path/to/pkg"
    Java/Rust/C-like:
        best-effort import/use extraction
    """
    ext = _normalize_ext(file_ext)

    if ext == ".py":
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    imports.append(alias.asname or alias.name)
        return _unique_preserve_order(imports)

    if ext in {".js", ".jsx", ".ts", ".tsx"}:
        items: list[str] = []

        # import defaultName, { a as b, c } from 'x'
        for m in re.finditer(r"(?m)^\s*import\s+([^;]+?)\s+from\s+['\"`][^'\"`]+['\"`]", source):
            clause = m.group(1).strip()
            if clause.startswith("* as "):
                items.append(clause[5:].strip())
            elif clause.startswith("{"):
                inner = clause.strip("{} ").strip()
                for part in inner.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    if " as " in part:
                        items.append(part.split(" as ", 1)[1].strip())
                    else:
                        items.append(part)
            elif "," in clause:
                default_part, rest = clause.split(",", 1)
                items.append(default_part.strip())
                inner = rest.strip()
                if inner.startswith("{") and inner.endswith("}"):
                    inner = inner[1:-1]
                for part in inner.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    if " as " in part:
                        items.append(part.split(" as ", 1)[1].strip())
                    else:
                        items.append(part)
            else:
                items.append(clause)

        # require() bindings
        for m in re.finditer(r"(?m)^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\s*\(", source):
            items.append(m.group(1))

        # destructuring require
        for m in re.finditer(r"(?m)^\s*(?:const|let|var)\s*\{([^}]+)\}\s*=\s*require\s*\(", source):
            inner = m.group(1)
            for part in inner.split(","):
                part = part.strip()
                if not part:
                    continue
                if ":" in part:
                    items.append(part.split(":", 1)[1].strip())
                else:
                    items.append(part)

        return _unique_preserve_order(items)

    if ext == ".go":
        items = []
        for m in re.finditer(r'(?m)^\s*import\s+(?:([A-Za-z_][\w]*)\s+)?["`]([^"`]+)["`]', source):
            alias, path = m.group(1), m.group(2)
            if alias:
                items.append(alias)
            else:
                items.append(path.rstrip("/").split("/")[-1])

        for block in re.finditer(r"(?ms)^\s*import\s*\((.*?)\)", source):
            inner = block.group(1)
            for line in inner.splitlines():
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                m = re.match(r'(?:(?:([A-Za-z_][\w]*)|\.|_)\s+)?["`]([^"`]+)["`]', line)
                if m:
                    alias, path = m.group(1), m.group(2)
                    if alias:
                        items.append(alias)
                    else:
                        items.append(path.rstrip("/").split("/")[-1])
        return _unique_preserve_order(items)

    if ext in {".java", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".rs"}:
        items = []

        # Java / C-like imports.
        for m in re.finditer(r"(?m)^\s*import\s+(?:static\s+)?([A-Za-z_][\w\.]*)\s*;", source):
            path = m.group(1)
            items.append(path.split(".")[-1])

        # Rust use statements.
        for m in re.finditer(r"(?m)^\s*use\s+([^;]+);", source):
            inner = m.group(1).strip()
            # Very best-effort: pull identifiers after :: or inside braces.
            parts = re.findall(r"[A-Za-z_][\w]*", inner)
            items.extend(parts)

        return _unique_preserve_order(items)

    return []


def find_references(block: str, file_ext: str) -> list[str]:
    """
    Scan a block body and return names of called functions/classes not in
    builtins for that language.

    Python example:
        find_references("os.path.join(x)") -> ["os.path.join"]
    """
    ext = _normalize_ext(file_ext)

    if ext == ".py":
        try:
            tree = ast.parse(block)
        except SyntaxError:
            # Conservative fallback: dotted call names only.
            return _find_python_references_regex(block)

        builtin_names = set(dir(builtins)) | set(keyword.kwlist)

        refs: list[str] = []

        def dotted_name(expr: ast.AST) -> Optional[str]:
            if isinstance(expr, ast.Name):
                return expr.id
            if isinstance(expr, ast.Attribute):
                base = dotted_name(expr.value)
                if base:
                    return f"{base}.{expr.attr}"
                return expr.attr
            return None

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = dotted_name(node.func)
                if not name:
                    continue
                root = name.split(".", 1)[0]
                if root in builtin_names:
                    continue
                refs.append(name)

        return _unique_preserve_order(refs)

    # Generic fallback for brace-based languages:
    # collect identifiers or dotted names followed by "(" while avoiding keywords.
    return _find_generic_references(block)


def _find_python_references_regex(block: str) -> list[str]:
    """
    Fallback reference extraction for Python when AST parsing fails.
    """
    builtin_names = set(dir(builtins)) | set(keyword.kwlist)
    # Dotted call names like os.path.join(
    pattern = re.compile(r"\b([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+|[A-Za-z_][\w]*)\s*\(")
    refs = []
    for m in pattern.finditer(block):
        name = m.group(1)
        root = name.split(".", 1)[0]
        if root in builtin_names:
            continue
        refs.append(name)
    return _unique_preserve_order(refs)


def _find_generic_references(block: str) -> list[str]:
    """
    Best-effort reference extraction for non-Python languages.
    """
    # Exclude common control-flow and declaration keywords.
    excluded = {
        "if", "for", "while", "switch", "catch", "return", "new",
        "function", "func", "class", "struct", "enum", "case",
        "sizeof", "typeof", "delete", "throw", "await", "async",
    }

    # Match dotted or simple calls: foo(...), obj.method(...), ns::call(...).
    pattern = re.compile(r"\b([A-Za-z_][\w]*(?:(?:\.|::)[A-Za-z_][\w]*)*)\s*\(")

    refs = []
    for m in pattern.finditer(block):
        name = m.group(1)
        base = name.split(".", 1)[0].split("::", 1)[0]
        if base in excluded:
            continue
        refs.append(name)
    return _unique_preserve_order(refs)


def get_context_lines(source: str, target_name: str, before: int = 10, file_ext: str = ".py") -> str:
    """
    Return N lines before the block start, not including the block itself.

    If the target is not found, returns "".
    """
    ext = _normalize_ext(file_ext)
    if ext == ".py":
        try:
            tree = ast.parse(source)
            finder = _PythonTargetFinder(target_name)
            finder.visit(tree)
            if finder.found is None:
                return ""
            start_line = finder.found.start_line
        except SyntaxError:
            start_line = _find_block_start_line_fallback(source, target_name, ext)
            if start_line is None:
                return ""
    else:
        start_line = _find_block_start_line_fallback(source, target_name, ext)
        if start_line is None:
            return ""

    lines = _split_lines_keepends(source)
    if not lines:
        return ""

    start_idx = max(0, start_line - 1 - before)
    end_idx = max(0, start_line - 1)
    return "".join(lines[start_idx:end_idx])


def _find_block_start_line_fallback(source: str, target_name: str, file_ext: str) -> Optional[int]:
    """
    Best-effort line number for block start, used by get_context_lines().
    """
    ext = _normalize_ext(file_ext)
    lines = _split_lines_keepends(source)

    if ext == ".py":
        def_pat = re.compile(rf"^\s*(?:async\s+)?def\s+{re.escape(target_name)}\b")
        class_pat = re.compile(rf"^\s*class\s+{re.escape(target_name)}\b")
        for i, line in enumerate(lines, start=1):
            if def_pat.match(line) or class_pat.match(line):
                # include decorators directly above
                j = i - 1
                while j >= 1 and re.match(r"^\s*@\S+", lines[j - 1]):
                    j -= 1
                return j
        return None

    patterns = _brace_candidate_patterns(target_name)
    for pat in patterns:
        m = pat.search(source)
        if m:
            return source.count("\n", 0, m.start()) + 1
    return None