from __future__ import annotations

import ast
import builtins
import io
import keyword
import re
from dataclasses import dataclass
from typing import Iterable, Optional


# Generic helpers

def _normalize_ext(file_ext: str) -> str:
    ext = (file_ext or "").strip().lower()
    if not ext:
        return ""
    if not ext.startswith("."):
        ext = "." + ext
    return ext


def _split_lines_keepends(source: str) -> list[str]:
    # Lines are split the way ast counts them (\n, \r, \r\n only), so line numbers match across tools.
    return io.StringIO(source, newline="").readlines()


def _no_bom(source: str) -> str:
    """*source* without a leading BOM: a file saved with one imports fine, and its blocks, imports and
    docstring were not found (the module did not parse, or the first line did not match)."""
    return source[1:] if isinstance(source, str) and source.startswith("\ufeff") else source


def _line_start_index(source: str, char_index: int) -> int:
    """
    Return the character index of the start of the line containing char_index.
    """
    nl = source.rfind("\n", 0, char_index)
    return 0 if nl < 0 else nl + 1


def _effective_match_start(source: str, raw_start: int) -> int:
    """
    Several _brace_candidate_patterns entries begin with a "^\\s*"-shaped
    prefix, and regex "\\s" matches newlines too — so when a definition is
    preceded by a blank line, `^\\s*` can match starting at that blank
    line and sweep across it into the signature's real first line, putting
    match.start() on the blank line rather than on the line the signature
    is actually visible on. Left uncorrected this shows up two ways: a
    spurious leading blank line in the text _extract_brace_block returns,
    and get_context_lines() being off by one line (its "before" window
    ends up anchored to the blank line instead of the definition itself).
    Return the start of the line containing the first non-whitespace
    character at/after raw_start instead.
    """
    i = raw_start
    n = len(source)
    while i < n and source[i] in " \t\r\n":
        i += 1
    return _line_start_index(source, i if i < n else raw_start)


def _unique_preserve_order(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


# Python strategy

@dataclass
class _PythonTarget:
    node: ast.AST
    start_line: int
    end_line: int


class _PythonTargetFinder(ast.NodeVisitor):
    def __init__(self, target_name: str):
        self.target_name = target_name
        self.found: Optional[_PythonTarget] = None
        # Dotted names (Class.method) are matched by the full path, so a qualified citation is found; bare names keep the old behaviour (BUGFIX).
        self._path_stack: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._check(node)

    def _check(self, node: ast.AST) -> None:
        if self.found is not None:
            return
        qualname = ".".join([*self._path_stack, node.name])
        is_match = (
            node.name == self.target_name
            or qualname == self.target_name
            # A dotted target also matches as a suffix of a deeper path, as a bare name already does.
            or ("." in self.target_name and qualname.endswith("." + self.target_name))
        )
        if is_match:
            self.found = _PythonTarget(node=node, start_line=self._start_line(node), end_line=getattr(node, "end_lineno", node.lineno))
            return
        self._path_stack.append(node.name)
        self.generic_visit(node)
        self._path_stack.pop()

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


# Brace-based strategy

_LANG_OF_EXT = {
    ".rs": "rust",
    **{e: "c" for e in (".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx")},
    **{e: "js" for e in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")},
}
#: a Rust char literal: `'a'`, `'\n'`, `'\x41'`, `'\u{1F600}'`; any other `'` is a lifetime or a label
_RUST_CHAR = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F]{1,6}\}|x[0-9a-fA-F]{2}|.)|[^\\'\n])'")
#: words after which a JS `/` starts a regex literal, not a division
_JS_REGEX_AFTER_WORD = frozenset(("return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void",
                                  "throw", "yield", "await", "instanceof"))


def _lang_skip(source: str, i: int, lang: str) -> Optional[int]:
    """Where the code scan goes on when *source[i]* starts something of *lang* the generic scan misreads,
    else None: a Rust lifetime (`'a`, `'static`) is no string — read as one, a body with `&'static str` ran
    on to the next apostrophe, a later function's `// don't` — nor is a C preprocessor line (`#error don't
    compile`), and a JS regex literal (`/}/.test(s)`) holds braces that are no block's."""
    ch = source[i]
    if lang == "rust" and ch == "'":
        m = _RUST_CHAR.match(source, i)
        return m.end() if m else i + 1
    if lang == "c" and ch == "#" and not source[_line_start_index(source, i):i].strip():
        nl = source.find("\n", i)
        while nl >= 0 and source[_line_start_index(source, nl):nl].rstrip().endswith("\\"):
            # A preprocessor line ending in `\` continues on the next line, so its whole body is
            # one token: the `{`/`}` of a `#define X do { … } while (0)` belong to the macro, not to
            # the block being counted (HARDEN-4 D4).
            nl = source.find("\n", nl + 1)
        return len(source) if nl < 0 else nl
    if lang == "js" and ch == "/" and source[i + 1:i + 2] not in ("/", "*") and _js_regex_can_start(source, i):
        return _js_regex_end(source, i)
    return None


def _js_regex_can_start(source: str, i: int) -> bool:
    j = i - 1
    while j >= 0 and source[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return True
    if source[j] == "<":
        # HARDEN-4 D1: `</li>`, `</p>` — a JSX closing tag's `/` follows `<`. Read as a regex it would
        # swallow everything to the next `/` on the line, `}` included, and every following function
        # came out as part of the target block. A `<` never starts a regex either (a comparison's `<`
        # is followed by an operand, never by `/`), so this is safe for plain JS/TS too.
        return False
    if source[j] in "(,=:[!&|?{};+-*%>~^":
        return True
    k = j
    while k >= 0 and (source[k].isalnum() or source[k] in "_$"):
        k -= 1
    return source[k + 1:j + 1] in _JS_REGEX_AFTER_WORD


def _js_regex_end(source: str, i: int) -> Optional[int]:
    """The index past the regex literal that starts at *i* (its flags too), None when the line ends first."""
    j, klass, n = i + 1, False, len(source)
    while j < n:
        ch = source[j]
        if ch == "\n":
            return None
        if ch == "\\":
            j += 2
            continue
        if ch == "[":
            klass = True
        elif ch == "]":
            klass = False
        elif ch == "/" and not klass:
            j += 1
            while j < n and (source[j].isalnum() or source[j] == "_"):
                j += 1
            return j
        j += 1
    return None


def _brace_scan_end(source: str, open_brace_index: int, open_ch: str = "{", close_ch: str = "}",
                    lang: str = "") -> int:
    """
    Scan from the opening bracket character (open_ch, default "{") and return
    the index just after its matching close_ch (default "}"). Brackets
    inside strings/comments are ignored.

    This is generic enough for JS/TS/Go/Java/C/C++/Rust-style block syntax.
    Also reused with open_ch="(", close_ch=")" to skip a balanced argument
    list (see _find_definition_open_brace) — the string/comment handling is
    identical either way, only the bracket pair being counted changes.
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
        if lang:
            skip = _lang_skip(source, i, lang)
            if skip is not None:
                i = skip
                continue
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

        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1

        i += 1

    return n


# Largest signature-to-brace gap: bounds the search so a false match cannot scan the whole file for a brace.
_DEFINITION_SCAN_BOUND = 400


def _find_definition_open_brace(source: str, scan_from: int, has_arg_list: bool, lang: str = "") -> Optional[int]:
    """
    Starting at `scan_from` (the position right after a regex match for a
    candidate definition signature), determine whether this location is a
    genuine definition with a braced body — as opposed to a plain call/
    reference statement, or a brace-less single-expression arrow body — and
    if so, return the index of its opening "{". Returns None otherwise.

    AUTO-BUG (found during review, no prior test caught it precisely — see
    test_block_extractor_brace_langs.py::test_no_false_positive_on_call_site,
    which passes today for the wrong reason, asserted below): the "(...)"-
    shaped patterns in _brace_candidate_patterns match an ordinary function
    CALL just as easily as a definition — "helper(1, 2);" matches the exact
    same regex as "function helper(1, 2) {" — and the arrow-function
    pattern matches a brace-less single-expression body just as easily as a
    braced one. Both used to fall through to an unbounded "scan for the
    next { anywhere later in the file" search with no way to tell it apart
    from a real match, so a call site (or brace-less arrow) appearing
    BEFORE the real definition would walk straight past the rest of its own
    enclosing function, across any other, unrelated code, and return
    whichever "{" happened to come first — silently extracting (and, in
    auto mode, potentially handing an LLM to edit) the wrong block, or a
    block that starts at the call site and improperly spans into an
    unrelated definition. Confirmed with a reproducing case: searching for
    "helper" in a file that calls `helper(1, 2)` inside main() before
    helper() is actually defined returned main()'s tail plus an entire
    unrelated function, and never reached the real helper() body at all.

    Fix: a real definition's "{" always follows its signature within a
    short span containing only whitespace/comments and, for the paren-form
    patterns, the balanced argument list plus perhaps a short return-type/
    throws/generic annotation — never a ";" (a call's statement terminator,
    or a body-less prototype declaration). So: skip the balanced arg list
    when there is one, then require the next significant character to be
    "{" and not ";", within a bounded window.
    """
    n = len(source)
    i = scan_from

    if has_arg_list:
        # The argument list is skipped to its own closing parenthesis, so a default value cannot end the signature early.
        close_paren = _brace_scan_end(source, i - 1, "(", ")", lang)
        if close_paren >= n:
            return None
        i = close_paren

    state = "code"
    escape = False
    bound = min(n, i + _DEFINITION_SCAN_BOUND)

    while i < bound:
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

        if lang:
            skip = _lang_skip(source, i, lang)
            if skip is not None:
                i = skip
                continue
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
            return i
        if ch == ";":
            return None

        i += 1

    return None


def _brace_candidate_patterns(target_name: str) -> list[re.Pattern[str]]:
    name = re.escape(target_name)
    # Generic type parameters between the name and the parenthesis are allowed, so many languages' signatures are found.
    tp = r"(?:\s*<[^(){};]*>|\s*\[[^(){};]*\])?"
    word = r"[A-Za-z_$][\w$.]*(?:<(?:[^<>]|<(?:[^<>]|<[^<>]*>)*>)*>)?(?:\[\s*\])*"
    return [
        # function foo(...) {   function* gen() {   function identity<T>(a: T): T {
        re.compile(rf"(?m)^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*{name}{tp}\s*\("),
        # func foo(...) { / func (r T) foo(...) { / func Map[T, U any](...) {
        re.compile(rf"(?m)^\s*func\s+(?:\([^)]+\)\s*)?{name}{tp}\s*\("),
        # const foo = (...) => { / let foo = async (...) => { / const f = <T>(x: T): T => {
        re.compile(rf"(?m)^\s*(?:export\s+)?(?:const|let|var)\s+{name}\s*(?::[^=;]+?)?=\s*(?:async\s*)?"
                   rf"(?:<[^(){{}};]*>\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::\s*[^=;{{}}]+?)?\s*=>"),
        # C-like method and function definitions without a return type (foo(...) {, static foo(...) {).
        re.compile(rf"(?m)^\s*(?:public|private|protected|static|async|final|export|default|\s)*\*?\s*{name}{tp}\s*\("),
        # C-like definitions with a return type; nested generics are matched to two levels (BUGFIX).
        re.compile(
            rf"(?m)^\s*"
            rf"(?:@[\w.]+(?:\([^)]*\))?\s*)*"
            rf"(?:(?:public|private|protected|static|final|abstract|synchronized|"
            rf"native|default|transient|volatile|strictfp|export|async|inline|"
            rf"virtual|const|extern|unsafe|suspend|open|override|fun|fn|pub)\s+)*"
            rf"(?:<[^(){{}};]*>\s+)?"
            # The words of the return type are matched within a line, so a newline does not join them.
            rf"(?:{word}(?:[ \t]*[*&]+[ \t]*|[ \t]+)){{0,3}}{word}(?:\s*[*&]+\s*|\s+)"
            rf"(?:[\w$]+::)*{name}{tp}\s*\("
        ),
        # class X { foo(...) { ... } }
        re.compile(rf"(?m)^\s*{name}{tp}\s*\("),
        # exports.h = function (...) {   h: function(...) {   Foo.prototype.h = async function* (...) {
        re.compile(rf"(?m)^\s*(?:(?:module\.)?exports\.|[\w$]+\.prototype\.|this\.)?{name}\s*[:=]\s*"
                   rf"(?:async\s+)?function\s*\*?\s*[\w$]*\s*\("),
        # Go: type Server struct {   type Set[T comparable] interface {
        re.compile(rf"(?m)^\s*type\s+{name}{tp}\s+(?:struct|interface)\b"),
        # Type declarations in C-like, JVM and Rust syntax.
        re.compile(
            rf"(?m)^\s*(?:@[\w.]+(?:\([^)]*\))?\s*)*(?:[A-Za-z_$][\w$]*\s+)*?"
            rf"(?:class|interface|struct|enum|trait|record|object|namespace)\s+{name}\b"
        ),
        # HARDEN-4 D2: the heads below name a definition but carry no return type and no keyword of
        # their own, so no pattern above reached them and every one of them answered "not found".
        # They are anchored with [ \t]* on purpose (never \s*): a head that spans lines is not this
        # form, and a multi-line `const x =\n  function y(` must keep resolving to `y`'s own line.
        # const f = function (...) {   let f = async function (...) {
        re.compile(rf"(?m)^[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+{name}[ \t]*=[ \t]*"
                   rf"(?:async[ \t]+)?function[ \t]*\*?[ \t]*[\w$]*[ \t]*\("),
        # module.exports = function m(...) {   (the plain `exports.m = function` form is the row above)
        re.compile(rf"(?m)^[ \t]*module\.exports[ \t]*=[ \t]*(?:async[ \t]+)?function[ \t]*\*?[ \t]*"
                   rf"{name}[ \t]*\("),
        # Go: var F = func(...) {
        re.compile(rf"(?m)^[ \t]*var[ \t]+{name}[ \t]*=[ \t]*func[ \t]*\("),
        # Kotlin/Scala: fun ff() = 1   fun ff() { … }
        re.compile(rf"(?m)^[ \t]*fun[ \t]+{name}{tp}[ \t]*\("),        # Kotlin: modifiers, type parameters before the name and a receiver —
        # `internal suspend inline fun <reified T : Any> load(`, `fun String.shout(`
        re.compile(_kotlin_fun_head(name)),
    ]


def _kotlin_fun_head(name: str) -> str:
    """A Kotlin `fun` head whose type parameters stand before the name (`fun <T : Comparable<T>> max(`),
    with modifiers and annotations ahead of `fun` and an optional receiver (`fun List<T>.second(`)."""
    mods = (r"public|private|protected|internal|open|override|abstract|final|suspend|inline|"
            r"tailrec|operator|infix|external|actual|expect")
    return (rf"(?m)^[ \t]*(?:@[\w.]+(?:\([^)]*\))?\s*)*(?:(?:{mods})[ \t]+)*fun[ \t]+"
            rf"(?:<[^(){{}};=]*>[ \t]*)?(?:[\w.]+(?:<[^(){{}};=]*>)?\??\.)?{name}[ \t]*\(")


#: Signature-to-tail bound for the brace-less forms, same rationale as _DEFINITION_SCAN_BOUND.
_BODYLESS_SCAN_BOUND = 400

#: Characters a line may end on and still continue on the next line (`const clamp = (a, b) =>`,
#: `const total = a +\n  b;`). Everything else ends the logical line.
_CONTINUATION_TAIL = frozenset("=+-*/%&|^?:,.<>([{")


def _bodyless_candidate_patterns(target_name: str) -> list[re.Pattern[str]]:
    """The heads of a definition that has no `{ … }` body at all (HARDEN-4 D2).

    Every one of them is a *declaration* by its own prefix (`const`/`let`/`var`, `module.exports`,
    `var … = func`, `fun`), which is what makes it safe to answer with a block when no brace
    follows: the bare `name(...)` patterns in `_brace_candidate_patterns` deliberately stay out of
    this list, because there the same shape is an ordinary call site
    (`tests_bugfix/test_block_extractor_call_site_vs_definition.py`).
    """
    name = re.escape(target_name)
    tp = r"(?:\s*<[^(){};]*>|\s*\[[^(){};]*\])?"
    return [
        # const double = (x) => x * 2;   const clamp = (a, b) =>\n  a + b;   const f = <T>(x: T): T => x;
        re.compile(rf"(?m)^\s*(?:export\s+)?(?:const|let|var)\s+{name}\s*(?::[^=;]+?)?=\s*(?:async\s*)?"
                   rf"(?:<[^(){{}};]*>\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::\s*[^=;{{}}]+?)?\s*=>"),
        # class A { handle = (e) => e + 1; }   (a class field: no const/let/var, an optional modifier before the name)
        re.compile(rf"(?m)^\s*(?:(?:public|private|protected|static|readonly|override|declare)\s+)*{name}\s*(?::[^=;]+?)?=\s*"
                   rf"(?:async\s*)?(?:<[^(){{}};]*>\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::\s*[^=;{{}}]+?)?\s*=>"),
        # const f = function (a) ;   let f = async function () ;
        re.compile(rf"(?m)^\s*(?:export\s+)?(?:const|let|var)\s+{name}\s*=\s*(?:async\s+)?function\s*\*?\s*"
                   rf"[\w$]*\s*\("),
        # module.exports = function m(a) ;
        re.compile(rf"(?m)^\s*module\.exports\s*=\s*(?:async\s+)?function\s*\*?\s*{name}\s*\("),
        # Go: var F = func(a) ;
        re.compile(rf"(?m)^\s*var\s+{name}\s*=\s*func\s*\("),
        # Kotlin/Scala: fun ff() = 1
        re.compile(rf"(?m)^\s*fun\s+{name}{tp}\s*\("),        # Kotlin: fun <T> T.id() = this
        re.compile(_kotlin_fun_head(name)),
    ]


def _find_definition_end_bodyless(source: str, scan_from: int, has_arg_list: bool, lang: str = "") -> Optional[int]:
    """The end of a definition whose head matches but whose body has no `{ … }` (HARDEN-4 D2).

    Returns the index just past the `;` that ends the statement, or the end of its last logical
    line when there is none (`fun ff() = 1`), or None when nothing conclusive appears inside the
    bound. A line that ends on an operator continues on the next one, so a wrapped arrow body
    (`const clamp = (a, b) =>` / `  a + b;`) stays inside its own block.
    """
    n = len(source)
    i = scan_from

    if has_arg_list:
        # Same skip as _find_definition_open_brace: a default value cannot end the signature early.
        close_paren = _brace_scan_end(source, i - 1, "(", ")", lang)
        if close_paren >= n:
            return None
        i = close_paren

    state = "code"
    escape = False
    depth = 0
    bound = min(n, i + _BODYLESS_SCAN_BOUND)

    while i < bound:
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

        if lang:
            skip = _lang_skip(source, i, lang)
            if skip is not None:
                i = skip
                continue
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

        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                # A closer we never opened ends the statement: the definition ran into the block
                # around it rather than continuing.
                return i
        elif ch == ";" and depth == 0:
            return i + 1
        elif ch == "\n" and depth == 0:
            k = i - 1
            while k >= scan_from and source[k] in " \t\r":
                k -= 1
            if k < 0 or source[k] not in _CONTINUATION_TAIL:
                return i
        i += 1

    return None


def _find_earliest_genuine_definition(source: str, target_name: str, lang: str = "") -> Optional[tuple]:
    """
    Search every brace-language candidate pattern for `target_name` and
    return (match_start, open_brace_index, close_brace_end_index) for the
    EARLIEST genuine definition in the file — i.e. the earliest match for
    which _find_definition_open_brace actually confirms a "{" follows
    nearby, discarding call sites and brace-less arrow-function matches
    along the way. Returns None if no pattern yields a genuine definition
    anywhere in the source.

    HARDEN-4 D2: when no braced definition exists anywhere in the file, the
    brace-less heads are tried as a *second*, separate pass instead. Running
    them only as a fallback is what keeps this a pure addition: every block
    that is found today is still found, from the same place, because the
    brace-less pass can never out-rank a braced definition that already won.

    Shared by _extract_brace_block (the block-extraction path) and
    _find_block_start_line_fallback (get_context_lines' fallback path) so
    both give the same, correct answer about where a definition really is
    instead of two separately-maintained search strategies drifting apart
    — which is exactly how _find_block_start_line_fallback ended up with
    the same call-site false-positive bug _extract_brace_block had, except
    worse: it used `pattern.search()` (first match of the first pattern
    that matches ANYWHERE in the file, tried in a fixed pattern order) with
    no genuineness check and no "earliest wins" comparison at all, so a
    call site appearing anywhere could pre-empt patterns later in the list
    even when they'd have found the real, correctly-ordered definition.
    """
    patterns = _brace_candidate_patterns(target_name)
    best_start = None
    best_match_start = None
    best_open_brace = None

    for pat in patterns:
        for match in pat.finditer(source):
            has_arg_list = match.group(0).rstrip().endswith("(")
            open_idx = _find_definition_open_brace(source, match.end(), has_arg_list, lang)
            if open_idx is None:
                continue

            effective_start = _effective_match_start(source, match.start())
            start = _line_start_index(source, effective_start)
            if best_start is None or start < best_start:
                best_start = start
                best_match_start = effective_start
                best_open_brace = open_idx

    if best_start is not None:
        end = _brace_scan_end(source, best_open_brace, lang=lang)
        return (best_match_start, best_open_brace, end)

    return _find_earliest_bodyless_definition(source, target_name, lang)


def _find_earliest_bodyless_definition(source: str, target_name: str, lang: str = "") -> Optional[tuple]:
    """The earliest definition of `target_name` that has no braced body (HARDEN-4 D2), as
    `(match_start, None, end)` — the same tuple `_find_earliest_genuine_definition` returns, with
    `None` in the open-brace slot because there is none.

    Only reached when the braced search above found nothing at all, so it can only turn a "not
    found" into a found block; it never changes a block that was already being returned.
    """
    best_start = None
    best_match_start = None
    best_end = None

    for pat in _bodyless_candidate_patterns(target_name):
        for match in pat.finditer(source):
            has_arg_list = match.group(0).rstrip().endswith("(")
            end = _find_definition_end_bodyless(source, match.end(), has_arg_list, lang)
            if end is None:
                continue

            effective_start = _effective_match_start(source, match.start())
            start = _line_start_index(source, effective_start)
            if best_start is None or start < best_start:
                best_start = start
                best_match_start = effective_start
                best_end = end

    if best_start is None:
        return None
    return (best_match_start, None, best_end)


def _extract_brace_block(source: str, target_name: str, lang: str = "") -> str:
    found = _find_earliest_genuine_definition(source, target_name, lang)
    if found is None:
        return ""

    match_start, open_idx, end = found
    start = _line_start_index(source, match_start)
    return source[start:end]


# Prose strategy: finds a block by heading or by its first mention; fails open, like the code strategy.

_HEADING_RE = re.compile(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$")


def _iter_prose_headings(source: str) -> list[tuple[int, str, int]]:
    """Return ``[(level, heading_text, char_start_of_heading_line), ...]``
    for every ATX-style (``#``) markdown heading, in document order."""
    return [
        (len(m.group(1)), m.group(2).strip(), m.start())
        for m in _HEADING_RE.finditer(source)
    ]


def _extract_heading_section(source: str, query_norm: str) -> str:
    """Return the section for the heading matching *query_norm* (already
    lower-cased/stripped), from the heading line up to the next heading of
    equal-or-higher level (sub-headings stay inside the section), or end of
    document. Returns "" if no heading matches.
    """
    headings = _iter_prose_headings(source)
    if not headings:
        return ""

    match_idx: Optional[int] = None
    # Prefer an exact (case-insensitive) heading-text match...
    for i, (_level, text, _start) in enumerate(headings):
        if text.lower() == query_norm:
            match_idx = i
            break
    # Falls back to a substring match in either direction, e.g. "the storm" and "Chapter 3: The Storm".
    if match_idx is None:
        for i, (_level, text, _start) in enumerate(headings):
            low = text.lower()
            if query_norm in low or low in query_norm:
                match_idx = i
                break
    if match_idx is None:
        return ""

    level, _text, start = headings[match_idx]
    end = len(source)
    for nxt_level, _nxt_text, nxt_start in headings[match_idx + 1:]:
        if nxt_level <= level:
            end = nxt_start
            break

    return source[start:end].rstrip() + "\n"


def _split_prose_paragraphs(source: str) -> list[str]:
    """Split *source* into paragraphs on one-or-more blank lines."""
    return [p for p in re.split(r"\n[ \t]*\n", source) if p.strip()]


def _extract_entity_paragraphs(source: str, query_norm: str, max_paragraphs: int) -> str:
    """Return up to *max_paragraphs* paragraphs centred on the first
    paragraph containing *query_norm* (case-insensitive substring).
    Returns "" if no paragraph contains the query.
    """
    paragraphs = _split_prose_paragraphs(source)
    hit_idx: Optional[int] = None
    for i, para in enumerate(paragraphs):
        if query_norm in para.lower():
            hit_idx = i
            break
    if hit_idx is None:
        return ""

    before = (max_paragraphs - 1) // 2
    start_idx = max(0, hit_idx - before)
    end_idx = min(len(paragraphs), start_idx + max_paragraphs)
    start_idx = max(0, end_idx - max_paragraphs)  # re-anchor near doc edges

    selected = paragraphs[start_idx:end_idx]
    return "\n\n".join(p.strip() for p in selected).strip() + "\n"


def extract_prose_section(
    source: str, query: str, file_ext: str, max_paragraphs: int = 3,
) -> str:
    """
    Extract a section of prose (``.md`` / ``.txt``) matching *query* — the
    pull side of the creative-mode context broker (AUTO-CR-6). Mirrors
    :func:`extract_block`'s "not found → ''" contract so callers can treat
    code and prose targets identically.

    Strategy, in order:

    1. **Heading match.** If *query* matches a markdown heading (exact
       case-insensitive match preferred, substring as a fallback), return
       that heading's full section: from the heading line up to the next
       heading of equal-or-higher level, or end of file.
    2. **Entity match (fallback).** Return the paragraph containing the
       first occurrence of *query* (case-insensitive), plus a couple of
       neighbouring paragraphs for context, capped to *max_paragraphs*
       paragraphs total.

    Returns ``""`` when *file_ext* is not prose, *query* is empty/whitespace,
    or nothing matches.
    """
    ext = _normalize_ext(file_ext)
    if ext not in {".md", ".txt"}:
        return ""

    query_norm = (query or "").strip().lstrip("#").strip().lower()
    if not query_norm:
        return ""

    section = _extract_heading_section(source, query_norm)
    if section:
        return section

    return _extract_entity_paragraphs(source, query_norm, max_paragraphs)


# Public API

def _extract_block_named(source: str, target_name: str, ext: str) -> str:
    """The language-specific extraction for one exact *target_name*."""
    if ext == ".py":
        return _extract_python_block(source, target_name)
    # Default to brace-based strategy for JS/TS/Go/Java/C/C++/Rust/etc.
    return _extract_brace_block(source, target_name, _LANG_OF_EXT.get(ext, ""))


def extract_block(source: str, target_name: str, file_ext: str) -> str:
    """
    Extract a named code block from source text for any supported language.

    Returns:
        The full code block, or "" if the target is not found.
    """
    source = _no_bom(source)
    ext = _normalize_ext(file_ext)

    block = _extract_block_named(source, target_name, ext)
    if block or not target_name or "." not in target_name:
        return block

    # HARDEN-1 A4: a reference the caller hands us is very often dotted —
    # `module.func` from `find_references` on an imported module, `a.b.c` from
    # a chained call — while the definition it names is the *bare* symbol.
    # Looking the whole name up found nothing (that is the only way we get
    # here), so the last segment is tried: `module.func` → `func`,
    # `Class.method` → the method, which the Python finder already resolves
    # through its qualname when the class is visible. The whole name still
    # wins whenever it *is* a real definition (a `Class.method` qualname, a
    # JS `exports.h = function ...`), because it is tried first.
    return _extract_block_named(source, target_name.rsplit(".", 1)[-1].strip(), ext)


def extract_module_docstring(source: str, file_ext: str, max_chars: int = 500) -> str:
    """AUTO-H2-2: Return the file's top-of-module docstring, or "" if there
    isn't one.

    Why this exists: ``extract_block`` deliberately returns only the named
    symbol's own body — that's the right default for keeping Stage B's
    prompt small. But a module docstring commonly carries context the named
    symbol's body itself never states, most importantly "this code is
    intentionally bad" (a test fixture built to exercise a static analyzer,
    a documented negative/control case, a toy example). A reviewer — human
    or LLM — judging whether a symbol needs hardening cannot make that call
    correctly without seeing why the file exists in the first place, and
    right now nothing in Gate 1's pipeline surfaces that even when it's a
    single ``\"\"\"...\"\"\"`` block away from the code being reviewed.

    Only implements the Python path (``ast.get_docstring``) — the false
    positives that motivated this (AUTO-T8/AUTO-T9, see gate1_grounding.py)
    were both Python fixtures, and Python is where nearly all of this
    codebase's own "intentionally bad" fixtures live
    (tests/fixtures/collect_mini_repo/). Extend to other languages only if
    a real false positive of this shape shows up there — speculative
    coverage for a failure mode nobody has hit yet isn't worth the parser
    complexity for non-Python languages.
    """
    source = _no_bom(source)
    ext = _normalize_ext(file_ext)
    if ext != ".py":
        return ""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    doc = ast.get_docstring(tree, clean=True)
    if not doc:
        return ""
    doc = doc.strip()
    if len(doc) > max_chars:
        doc = doc[:max_chars].rstrip() + " …(truncated)"
    return doc


def _split_braced_names(inner: str, alias_sep: str) -> list[str]:
    """
    Split a comma-separated ``{a, b as c}``-style clause into bound names,
    taking the right-hand side of *alias_sep* when present.

    *alias_sep* is ``" as "`` for JS/TS ``import``/``require`` aliasing, or
    ``":"`` for destructured-require renaming (``const {a: b} = require(...)``).
    A TS ``type`` mark (``{ type Props }``) is no part of the name.
    """
    names: list[str] = []
    for part in inner.split(","):
        part = " ".join(part.split())
        if part.startswith("type "):
            part = part[5:].strip()
        if not part:
            continue
        if alias_sep in part:
            names.append(part.split(alias_sep, 1)[1].strip())
        else:
            names.append(part)
    return names


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
    source = _no_bom(source)
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

        # An import statement ends at its semicolon or line end, so a bare side-effect import does not swallow the next one (bugfix).
        for m in re.finditer(r"(?m)^[ \t]*import\s+(?:type\s+)?((?:\{[^}]*\}|[^;'\"`{}\n])+?)\s+from\s+['\"`]"
                             r"[^'\"`\n]+['\"`]", source):
            clause = m.group(1).strip()
            if clause.startswith("* as "):
                items.append(clause[5:].strip())
            elif clause.startswith("{"):
                inner = clause.strip("{} ").strip()
                items.extend(_split_braced_names(inner, " as "))
            elif "," in clause:
                default_part, rest = clause.split(",", 1)
                items.append(default_part.strip())
                inner = rest.strip()
                if inner.startswith("{") and inner.endswith("}"):
                    inner = inner[1:-1]
                items.extend(_split_braced_names(inner, " as "))
            else:
                items.append(clause)

        # require() bindings
        for m in re.finditer(r"(?m)^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\s*\(", source):
            items.append(m.group(1))

        # destructuring require
        for m in re.finditer(r"(?m)^\s*(?:const|let|var)\s*\{([^}]+)\}\s*=\s*require\s*\(", source):
            inner = m.group(1)
            items.extend(_split_braced_names(inner, ":"))

        return _unique_preserve_order(items)

    if ext == ".go":
        items = []
        for m in re.finditer(r'(?m)^\s*import\s+(?:([A-Za-z_][\w]*)\s+)?["`]([^"`]+)["`]', source):
            alias, path = m.group(1), m.group(2)
            if alias:
                items.append(alias)
            else:
                items.append(path.rstrip("/").split("/")[-1])

        # A group ends at a ")" that starts its line, so a ")" inside a comment does not cut the block.
        for block in re.finditer(r"(?ms)^\s*import\s*\((.*?)^[ \t]*\)", source):
            inner = block.group(1)
            for line in inner.splitlines():
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                m = re.match(r'(?:([A-Za-z_][\w]*|\.)\s+)?["`]([^"`]+)["`]', line)
                if m:
                    alias, path = m.group(1), m.group(2)
                    if alias in ("_", "."):
                        continue         # a blank or a dot import binds no name of its own
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

        # Rust use: the path identifiers, and for "x as y" the bound name only, not the original.
        for m in re.finditer(r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+([^;]+);", source):
            inner = re.sub(r"\b([A-Za-z_]\w*)\s+as\s+([A-Za-z_]\w*)", r"\2", m.group(1).strip())
            parts = re.findall(r"[A-Za-z_][\w]*", inner)
            items.extend(p for p in parts if p != "_")

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

    # Generic fallback for brace languages: identifiers or dotted names followed by "(", minus keywords.
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
    source = _no_bom(source)
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
                j = i
                while j >= 2 and re.match(r"^\s*@\S+", lines[j - 2]):
                    j -= 1
                return j
        return None

    # The earliest real definition is used, not the first pattern that matches, so a call site cannot pre-empt it (AUTO-BUG).
    found = _find_earliest_genuine_definition(source, target_name, _LANG_OF_EXT.get(ext, ""))
    if found is None:
        return None
    match_start, _open_idx, _end = found
    return source.count("\n", 0, match_start) + 1