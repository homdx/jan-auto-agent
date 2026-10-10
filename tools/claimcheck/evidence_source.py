"""CC-3 — source evidence: symbol bodies, keyword windows, collect facts (docs/claim-check/
tickets/265-cc-3-source-evidence.md).

A voter deciding "`_declared_paths` reads one line" needs that function's text with the
file's own line numbers, and around it no more than it takes to know who calls it and
what the contract says. This module turns the anchors CC-1 resolved into those chunks:

* `source_chunks` — per found anchor, the definition (decorators and docstring included),
  a test with the same-file fixtures it uses, a path's import block and definition list,
  a cited `file.py:12-30` with three lines around it. A body over `max_chunk_chars` is
  cut to its signature and docstring plus the **keyword windows** of the claim, the rest
  replaced by `# … N lines omitted (a-b)` markers. Ranges of one file that overlap or
  touch are merged, so the same line is never handed out twice.
* `keyword_windows` / `keyword_tokens` — the claim's code-looking tokens found in a
  body, ±3 lines each, touching windows merged, at most five.
* `collect_chunks` — the collect model's facts on the anchor's module (callers, calls,
  contracts, risk, fail-open), one chunk per kind; nothing at all when the model is
  absent.

Properties the ticket pins: deterministic (same inputs, the same bytes — nothing iterates
a set into the output); line numbers are the file's (1-based, inclusive, the gutter
`" 120| "` prints exactly `start..end`); a file over `MAX_READ_BYTES` is never read whole
— only the anchor's lines are streamed off disk; non-UTF-8 bytes come out as U+FFFD;
a `found=False` anchor gives nothing (CC-5 writes the "does not exist" note).

Line splitting follows Python's parser (`\\r\\n`, `\\r`, `\\n`), not `str.splitlines`,
which also breaks on form feeds and U+2028 and would shift every number after one.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field, replace
from typing import Optional

from tools.claimcheck.model import Chunk

MAX_READ_BYTES = 400 * 1024      # a bigger file is read for the anchor's lines only
RADIUS = 3                       # lines around a keyword hit and around a cited range
MAX_WINDOWS = 5
MAX_TOKENS = 40
MAX_CLAIM_SCAN = 4000            # a claim is scanned this far for tokens (bounded work)
MAX_LITERAL = 40
NOISE_SHARE = 0.25               # a token on more than this share of a body's lines ...
NOISE_MIN_LINES = 5              # ... (and on more than this many) finds nothing in particular
BIG_FILE_HEAD = 40               # lines shown of an over-cap file named without lines
MAX_INDEX_ENTRIES = 80           # top-level definitions listed for a path anchor
MAX_FIXTURES = 8                 # same-file fixtures followed from one test, transitively
COLLECT_LIMIT = 8
_HEADER_SHARE = 0.6              # of max_chunk_chars, at most, for a signature and docstring
_PATH_SHARE = 0.25               # ... for a named file's import block, and again for its definition list
_MARKER_ALLOWANCE = 60           # chars an omission marker takes, budgeted up front

_NL = re.compile(r"\r\n|\r|\n")
_LINE_END = ("\r\n", "\n", "\r")


# ------------------------------------------------------------------ keyword tokens

# Every pattern below is linear: bounded or non-nested quantifiers, no alternation that
# can re-match the same span (CC-1's lesson: extraction is run on hostile text).
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_BACKTICK = re.compile(r"`([^`\n]{1,80})`")
_DQ = re.compile(r'"([^"\n]{1,%d})"' % MAX_LITERAL)
_SQ = re.compile(r"(?<![A-Za-z0-9_])'([^'\n]{1,%d})'(?![A-Za-z0-9_])" % MAX_LITERAL)
_KWARG = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=(?:[A-Za-z0-9_.]+|\"[^\"\n]{0,40}\"|'[^'\n]{0,40}')")
_OP = re.compile(r"==|!=|<=|>=|(?<![<>=!-])[<>](?![<>=])")
_NUM = re.compile(r"(?<![A-Za-z0-9_.-])\d+(?:\.\d+)?(?![A-Za-z0-9_.])"     # 180, 0.5; not utf-8's 8
                  r"|(?<![A-Za-z0-9_.])-\d+(?:\.\d+)?(?![A-Za-z0-9_.])")   # -1


def _code_word(word: str, claim: str, end: int) -> bool:
    """A word the way code writes it: `snake_case`, `dotted.name`, or a call `name(`."""
    if "." in word:
        return any(len(part) > 1 for part in word.split("."))   # not "e.g", "i.e"
    if "_" in word:
        return any(ch.isalnum() for ch in word)                 # not a bare "_"
    return end < len(claim) and claim[end] == "("


def keyword_tokens(claim: str) -> list:
    """The claim's code-looking tokens, in the order they first appear, each once.

    Identifiers with `_` or `.` (and, for a dotted one, its last part: the body says
    `def load_timeout`, not `config.load_timeout`), call names, backticked text, string
    literals up to 40 characters, `name=value`, the comparison operators, numbers."""
    text = (claim or "")[:MAX_CLAIM_SCAN]
    found: list = []   # (position, order, token)

    def add(pos: int, token: str, order: int = 0) -> None:
        token = token.strip()
        if token:
            found.append((pos, order, token))

    for m in _BACKTICK.finditer(text):
        add(m.start(1), m.group(1))
    for m in _WORD.finditer(text):
        word = m.group(0)
        if _code_word(word, text, m.end()):
            add(m.start(), word)
            last = word.rsplit(".", 1)[-1]
            if "." in word and len(last) > 2:
                add(m.start(), last, 1)
    for rx in (_DQ, _SQ):
        for m in rx.finditer(text):
            add(m.start(1), m.group(1))
    for rx in (_KWARG, _OP, _NUM):
        for m in rx.finditer(text):
            add(m.start(), m.group(0))
    out: list = []
    seen: set = set()
    for _, _, token in sorted(found):
        if token not in seen:
            seen.add(token)
            out.append(token)
            if len(out) == MAX_TOKENS:
                break
    return out


def _windows(lines: list, claim: str, budget: Optional[int], width: int,
             radius: int = RADIUS, max_windows: int = MAX_WINDOWS) -> list:
    """Windows (0-based, inclusive) over *lines*; see `keyword_windows`."""
    tokens = keyword_tokens(claim)
    if not tokens or not lines:
        return []
    hits = {t: [i for i, line in enumerate(lines) if t in line] for t in tokens}
    noise = max(NOISE_MIN_LINES, len(lines) * NOISE_SHARE)
    hits = {t: at for t, at in hits.items() if len(at) <= noise}   # `0` on every `STEP_0xx` line
    score: dict = {}
    for t, at in hits.items():
        for i in at:
            score[i] = score.get(i, 0.0) + 1.0 / len(at)   # a rare token weighs more
    prefix = [0]                  # chars of lines[:k], so a window's cost is O(1), not O(its length)
    for line in lines:
        prefix.append(prefix[-1] + _line_chars(line, width))

    def chars(a: int, b: int) -> int:
        return prefix[b + 1] - prefix[a]

    windows: list = []
    used = 0
    for i in sorted(score, key=lambda i: (-score[i], i)):
        a, b = max(0, i - radius), min(len(lines) - 1, i + radius)
        merged = [(a, b)] + [w for w in windows if w[0] <= b + 1 and a <= w[1] + 1]
        a, b = min(w[0] for w in merged), max(w[1] for w in merged)
        rest = [w for w in windows if w not in merged]
        if len(rest) + 1 > max_windows:
            continue
        cost = chars(a, b) - sum(chars(x, y) for x, y in merged[1:])
        if budget is not None and used + cost > budget:
            continue
        used += cost
        windows = rest + [(a, b)]
    return sorted(windows)


def keyword_windows(text: str, claim: str, *, budget: Optional[int] = None) -> list:
    """Line windows `(start, end)` of *text* (1-based, inclusive) around the claim's tokens.

    Each hit gives ±3 lines; windows that overlap or touch merge; at most five, the lines
    where the rarest tokens meet first; *budget* caps their characters (gutter included).
    A token on more than a quarter of the lines (and more than five) is ignored as noise.
    A claim with no code-looking token gives `[]`."""
    lines = _split(text)
    return [(a + 1, b + 1) for a, b in _windows(lines, claim, budget, len(str(len(lines))))]


# ------------------------------------------------------------------ reading a file

def _split(text: str) -> list:
    lines = _NL.split(text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _clean(text: str) -> str:
    """Surrogate-escaped bytes (how `PathRepoView.read` keeps invalid UTF-8) -> U+FFFD."""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _on_disk(view, path: str) -> Optional[str]:
    """The file under a directory-backed view's root, when the view shows it."""
    root = getattr(view, "root", None)
    if root is None:
        return None
    try:
        if not view.exists(path):       # the view's own hiding rules (`.git`, `.collect/`)
            return None
    except (OSError, ValueError):
        return None
    real_root = os.path.realpath(str(root))
    full = os.path.realpath(os.path.join(real_root, path))
    if not full.startswith(real_root + os.sep) or not os.path.isfile(full):
        return None
    return full


def _size(view, path: str) -> Optional[int]:
    sizer = getattr(view, "size", None)
    if callable(sizer):
        try:
            return int(sizer(path))
        except (OSError, ValueError, TypeError):
            return None
    full = _on_disk(view, path)
    return os.path.getsize(full) if full else None


def _stream(view, path: str, start: int, end: int) -> list:
    """Lines *start*..*end* (1-based) of an over-cap file, read line by line, never whole."""
    reader = getattr(view, "read_lines", None)
    if callable(reader):
        return [_clean(line) for line in reader(path, start, end)]
    full = _on_disk(view, path)
    if full is None:
        return []
    out = []
    with open(full, "r", encoding="utf-8", errors="replace", newline="") as fh:
        for n, line in enumerate(fh, 1):   # newline="": "\r", "\n" and "\r\n" all end a line
            if n > end:
                break
            if n >= start:
                for ending in _LINE_END:
                    if line.endswith(ending):
                        line = line[: -len(ending)]
                        break
                out.append(line)
    return out


@dataclass
class _File:
    path: str
    lines: Optional[list]      # None: over the cap, read by `_stream` only
    tree: Optional[ast.AST] = None
    nodes: list = field(default_factory=list)   # (qualname, start, node), the resolver's walk


def _load(view, path: str, cache: dict) -> Optional[_File]:
    if path in cache:
        return cache[path]
    f: Optional[_File] = None
    size = _size(view, path)
    if size is not None and size > MAX_READ_BYTES:
        f = _File(path, None)
    else:
        try:
            text = view.read(path)
        except (OSError, ValueError, UnicodeError):
            text = None
        if text is not None:
            if len(text) > MAX_READ_BYTES:      # a view that cannot tell the size up front
                f = _File(path, None)
            else:
                f = _File(path, [_clean(line) for line in _split(text)])
                if path.endswith(".py"):
                    try:
                        f.tree = ast.parse(text)
                    except (SyntaxError, ValueError, RecursionError, MemoryError):
                        f.tree = None
                    if f.tree is not None:
                        f.nodes = _def_nodes(f.tree)
    cache[path] = f
    return f


def _def_nodes(tree: ast.AST) -> list:
    """Every def / class with its dotted qualname and decorator-inclusive start — the same
    walk as CC-1's `_definitions`, so a resolved anchor's (qualname, start) finds its node."""
    out = []
    stack = [(tree, "")]
    while stack:
        node, prefix = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = prefix + child.name
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                out.append((qualname, start, child))
                stack.append((child, qualname + "."))
            else:
                stack.append((child, prefix))
    out.sort(key=lambda t: (t[1], t[0]))
    return out


def _node_at(f: _File, qualname: str, start: int):
    for q, s, node in f.nodes:
        if q == qualname and s == start:
            return node
    return None


def _header_end(node, start: int) -> int:
    """The last line of a definition's signature and docstring."""
    if node is None or not getattr(node, "body", None):
        return start
    first = node.body[0]
    if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)):
        return max(start, first.end_lineno or first.lineno)
    return max(start, first.lineno - 1)


# ------------------------------------------------------------------ pieces and chunks

def _line_chars(line: str, width: int) -> int:
    return len(line) + width + 4      # " 120| " gutter and the newline


def _span_chars(lines: list, a: int, b: int, width: int) -> int:
    return sum(_line_chars(lines[i], width) for i in range(a, b + 1))


@dataclass
class _Piece:
    """Lines of one file to show: the span it covers and the ranges actually printed."""

    path: str
    start: int
    end: int
    shown: list                     # [(a, b)] 1-based inclusive, inside start..end
    whys: list
    lines: dict = field(default_factory=dict)   # line number -> text, for every shown line
    index: tuple = ()               # "#   name  line" rows, printed after the first range

    def touches(self, other: "_Piece") -> bool:
        return (self.path == other.path and other.start <= self.end + 1
                and self.start <= other.end + 1)

    def absorb(self, other: "_Piece") -> None:
        self.start, self.end = min(self.start, other.start), max(self.end, other.end)
        self.shown = _merge_ranges(self.shown + other.shown)
        self.lines.update(other.lines)
        self.whys += [w for w in other.whys if w not in self.whys]
        self.index = self.index or other.index

    def render(self) -> Chunk:
        width = len(str(self.end))
        out = []
        at = self.start
        for k, (a, b) in enumerate(self.shown):
            if a > at:
                out.append(_marker(at, a - 1))
            out.extend(f" {n:>{width}}| {self.lines[n]}" for n in range(a, b + 1))
            if k == 0 and self.index:
                out.extend(self.index)
            at = b + 1
        if at <= self.end:
            out.append(_marker(at, self.end))
        return Chunk(id=f"src:{self.path}:{self.start}-{self.end}", kind="source", path=self.path,
                     start=self.start, end=self.end, text="\n".join(out), why=", ".join(self.whys))


def _marker(a: int, b: int) -> str:
    n = b - a + 1
    return f"# … {n} line{'s' if n != 1 else ''} omitted ({a}-{b})"


def _merge_ranges(ranges: list) -> list:
    out: list = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _piece(f: _File, start: int, end: int, shown: list, why: str, index: tuple = ()) -> _Piece:
    shown = _merge_ranges(shown)
    lines = {n: f.lines[n - 1] for a, b in shown for n in range(a, b + 1)}
    return _Piece(f.path, start, end, shown, [why], lines, index)


def _fit_head(lines: list, a: int, b: int, width: int, budget: int) -> int:
    """The last line of a..b (0-based) that keeps the head within *budget*; at least *a*."""
    used = 0
    for i in range(a, b + 1):
        used += _line_chars(lines[i], width)
        if used > budget and i > a:
            return i - 1
    return b


def _condensed(f: _File, start: int, end: int, head_end: int, claim: str, why: str,
               max_chars: int, index: tuple = ()) -> _Piece:
    """start..end (1-based) whole when it fits, else its head (signature, docstring) and the
    keyword windows of the rest within the budget, omission markers between."""
    lines = f.lines
    width = len(str(end))
    s, e = start - 1, end - 1
    index_chars = sum(len(row) + 1 for row in index)
    if _span_chars(lines, s, e, width) + index_chars <= max_chars:
        return _piece(f, start, end, [(start, end)], why, index)
    h = min(max(s, head_end - 1), e)
    h = _fit_head(lines, s, h, width, int(max_chars * _HEADER_SHARE) - index_chars)
    budget = max_chars - _span_chars(lines, s, h, width) - index_chars - 2 * _MARKER_ALLOWANCE
    shown = [(start, h + 1)]
    if h < e and budget > 0:
        body = lines[h + 1: e + 1]
        for a, b in _windows(body, claim, budget, width):
            shown.append((h + 2 + a, h + 2 + b))
    return _piece(f, start, end, shown, why, index)


def _definition_piece(f: _File, qualname: str, start: int, end: int, claim: str, why: str,
                      max_chars: int) -> Optional[_Piece]:
    n = len(f.lines)
    start, end = max(1, start), min(end, n)
    if start > end:
        return None
    node = _node_at(f, qualname, start)
    return _condensed(f, start, end, _header_end(node, start), claim, why, max_chars)


def _import_head_end(f: _File) -> int:
    """The last line of the module's leading docstring / import block, 0 when it has none."""
    if f.tree is None:
        return 0
    end = 0
    for stmt in f.tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            end = stmt.end_lineno or stmt.lineno
        elif isinstance(stmt, ast.Expr) and isinstance(getattr(stmt, "value", None), ast.Constant) \
                and isinstance(stmt.value.value, str) and end == 0 and stmt is f.tree.body[0]:
            end = stmt.end_lineno or stmt.lineno
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            break
    return end


def _definition_index(f: _File, budget: int) -> tuple:
    """`#   name  line` rows of the top-level definitions, as many as *budget* chars hold."""
    if f.tree is None:
        return ()
    rows = [(stmt.lineno, stmt.name) for stmt in f.tree.body
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if not rows:
        return ()
    out = ["# definitions (name  line):"]
    used = len(out[0]) + 1
    for line, name in rows[:MAX_INDEX_ENTRIES]:
        row = f"#   {name}  {line}"
        if used + len(row) + 1 > budget:
            break
        out.append(row)
        used += len(row) + 1
    if len(out) - 1 < len(rows):
        out.append(f"#   … {len(rows) - (len(out) - 1)} more")
    return tuple(out) if len(out) > 2 else ()


def _path_piece(f: _File, claim: str, why: str, max_chars: int) -> Optional[_Piece]:
    """A file named without a symbol: the whole file when it fits, else its import block and
    definition list (a quarter of the budget each, at most) and the claim's keyword windows
    in the rest — the windows are what the claim is about, so they get the most room."""
    n = len(f.lines)
    if n == 0:
        return None
    width = len(str(n))
    if _span_chars(f.lines, 0, n - 1, width) <= max_chars:
        return _piece(f, 1, n, [(1, n)], why)
    head = _import_head_end(f) if f.path.endswith(".py") else min(n, 10)
    head = _fit_head(f.lines, 0, max(head, 1) - 1, width, int(max_chars * _PATH_SHARE)) + 1
    index = _definition_index(f, int(max_chars * _PATH_SHARE))
    return _condensed(f, 1, n, head, claim, why, max_chars, index)


def _range_piece(f: _File, a: int, b: int, claim: str, why: str, max_chars: int) -> Optional[_Piece]:
    n = len(f.lines)
    if b < a:
        a, b = b, a
    if a > n:
        return None     # the cited lines are not in the file: no evidence to give
    start, end = max(1, a - RADIUS), min(n, b + RADIUS)
    return _condensed(f, start, end, start, claim, why, max_chars)


def _big_piece(view, f: _File, a: int, b: int, why: str, max_chars: int) -> Optional[_Piece]:
    """An over-cap file: lines a..b streamed off disk, cut at the budget."""
    got = _stream(view, f.path, a, b)
    if not got:
        return None
    end = a + len(got) - 1
    width = len(str(end))
    keep, used = [], 0
    for line in got:
        used += _line_chars(line, width)
        if used > max_chars - _MARKER_ALLOWANCE and keep:
            break
        keep.append(line)
    shown_end = a + len(keep) - 1
    lines = {a + i: line for i, line in enumerate(keep)}
    return _Piece(f.path, a, end, [(a, shown_end)], [why], lines)


# ------------------------------------------------------------------ tests and fixtures

def _fixture_name(node) -> Optional[str]:
    """The name a `@pytest.fixture` / `@fixture(name=...)` function is used by, else None."""
    for dec in node.decorator_list:
        target, call = dec, None
        if isinstance(dec, ast.Call):
            target, call = dec.func, dec
        name = (target.attr if isinstance(target, ast.Attribute)
                else target.id if isinstance(target, ast.Name) else "")
        if name != "fixture":
            continue
        if call is not None:
            for kw in call.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    return kw.value.value
        return node.name
    return None


def _used_fixtures(node) -> list:
    """Argument names, then `@pytest.mark.usefixtures("x")` names, in source order."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    a = node.args
    names = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs if x.arg not in ("self", "cls")]
    for dec in node.decorator_list:
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "usefixtures":
            names += [x.value for x in dec.args if isinstance(x, ast.Constant) and isinstance(x.value, str)]
    return names


def _fixture_pieces(f: _File, test_qualname: str, test_node, claim: str, max_chars: int) -> list:
    fixtures: dict = {}   # name -> [(qualname, start, node)]
    for q, s, node in f.nodes:
        name = _fixture_name(node) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        if name:
            fixtures.setdefault(name, []).append((q, s, node))
    if not fixtures:
        return []
    scope = test_qualname.rsplit(".", 1)[0] + "." if "." in test_qualname else ""

    def pick(name: str):
        defs = fixtures.get(name, [])
        for q, s, node in defs:                    # the test's own class overrides the module's
            if scope and q == scope + node.name:
                return q, s, node
        for q, s, node in defs:
            if "." not in q:
                return q, s, node
        return defs[0] if defs else None

    out, seen, queue = [], set(), list(_used_fixtures(test_node))
    while queue and len(seen) < MAX_FIXTURES:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        found = pick(name)
        if found is None:
            continue
        q, s, node = found
        piece = _definition_piece(f, q, s, node.end_lineno or node.lineno, claim, f"fixture {name}", max_chars)
        if piece is not None:
            out.append(piece)
        queue += [n for n in _used_fixtures(node) if n not in seen]
    return out


# ------------------------------------------------------------------ public

def _pieces_for(ra, view, cache: dict, claim: str, max_chars: int) -> list:
    path = ra.path
    if not ra.found or not path:
        return []
    f = _load(view, path, cache)
    if f is None:
        return []
    why = ra.anchor.text
    a, b = ra.lines if ra.lines else (0, 0)
    kind = ra.anchor.kind
    if f.lines is None:                       # over the cap: the anchor's lines only, streamed
        if a > 0:
            lo = max(1, a - RADIUS) if kind == "path" else a
            hi = (b + RADIUS) if kind == "path" else b
            if ra.qualname == "" and kind != "path":
                lo, hi = 1, BIG_FILE_HEAD     # a module named as a symbol
            piece = _big_piece(view, f, lo, hi, why, max_chars)
        else:
            piece = _big_piece(view, f, 1, BIG_FILE_HEAD, why, max_chars)
        return [piece] if piece else []
    if kind in ("symbol", "test") and ra.qualname:
        pieces = []
        piece = _definition_piece(f, ra.qualname, a, b, claim, why, max_chars)
        if piece is not None:
            pieces.append(piece)
        for cand in ra.candidates:            # a property and its setter: the same name twice in one file
            if (isinstance(cand, tuple) and len(cand) == 4 and cand[0] == path
                    and cand[1] == ra.qualname):
                other = _definition_piece(f, cand[1], cand[2], cand[3], claim, why, max_chars)
                if other is not None:
                    pieces.append(other)
        node = _node_at(f, ra.qualname, a)
        if node is not None and (kind == "test" or ra.qualname.rsplit(".", 1)[-1].startswith("test")):
            pieces += _fixture_pieces(f, ra.qualname, node, claim, max_chars)
        return pieces
    if kind == "path" and a > 0:
        piece = _range_piece(f, a, b, claim, why, max_chars)
        return [piece] if piece else []
    if kind in ("path", "symbol", "test"):    # a file named alone, or a module named as a symbol
        piece = _path_piece(f, claim, why, max_chars)
        return [piece] if piece else []
    return []                                 # commit, ticket, ref: CC-4's


def _in_named_file(resolved) -> list:
    """A symbol the claim places in a file it also names (`_commits_above` in
    `tools/contest/workspace.py`) is that file's definition: when CC-1 picked another
    file's, a candidate in the named file replaces it. Only a candidate CC-1 found —
    nothing is looked up here."""
    named = {ra.path for ra in resolved if ra.found and ra.anchor.kind == "path"}
    out = []
    for ra in resolved:
        if (ra.found and ra.anchor.kind in ("symbol", "test") and ra.qualname
                and named and ra.path not in named):
            for cand in ra.candidates:
                if isinstance(cand, tuple) and len(cand) == 4 and cand[0] in named:
                    others = ((ra.path, ra.qualname) + tuple(ra.lines),) + tuple(
                        c for c in ra.candidates if c is not cand)
                    ra = replace(ra, path=cand[0], qualname=cand[1], lines=(cand[2], cand[3]),
                                 candidates=others)
                    break
        out.append(ra)
    return out


def _with_symbol(resolved) -> set:
    """Files a found symbol or test of the claim is in: a path anchor on one of them adds
    nothing the definition does not (the ticket's "path without a symbol")."""
    return {ra.path for ra in resolved
            if ra.found and ra.anchor.kind in ("symbol", "test") and ra.qualname}


def source_chunks(resolved, view, model=None, *, claim: str = "", max_chunk_chars: int = 2400) -> list:
    """The source chunks for the found anchors of *resolved*, in anchor order, then the
    collect chunks when *model* is given (see the module docstring)."""
    resolved = _in_named_file(list(resolved))
    covered = _with_symbol(resolved)
    cache: dict = {}
    pieces: list = []
    by_path: dict = {}     # path -> its pieces, in order: a merge looks only at its own file
    for ra in resolved:
        if ra.anchor.kind == "path" and ra.lines == (0, 0) and ra.path in covered:
            continue
        for piece in _pieces_for(ra, view, cache, claim, max_chunk_chars):
            same = by_path.setdefault(piece.path, [])
            overlapping = [p for p in same if p.touches(piece)]
            if not overlapping:
                pieces.append(piece)
                same.append(piece)
                continue
            keep = overlapping[0]              # the earliest keeps its place in the order
            keep.absorb(piece)
            for other in overlapping[1:]:      # a piece that bridges two merges them all
                keep.absorb(other)
                pieces.remove(other)
                same.remove(other)
    chunks = [p.render() for p in pieces]
    if model is not None:
        chunks += collect_chunks(resolved, model)
    return chunks


def _query(fn, *args, **kwargs) -> list:
    """A collect query that answers nothing rather than raising (a partial artifact)."""
    try:
        got = fn(*args, **kwargs)
    except Exception:   # noqa: BLE001 — the ticket's contract: no collect chunk, no error
        return []
    if got is None:
        return []
    return list(got) if isinstance(got, (list, tuple)) else [got]


def _contract_line(c) -> str:
    name, desc = getattr(c, "name", ""), getattr(c, "description", "")
    return f"contract: {name}: {desc}" if name and desc else f"contract: {name or desc}"


def _risk_line(r) -> str:
    fields = [("score", "score"), ("blast_radius", "blast_radius"), ("loc", "loc"),
              ("unguarded", "unguarded_count"), ("undocumented_fail_open", "undocumented_fail_open_count"),
              ("zero_coverage", "zero_coverage")]
    parts = [f"{label} {getattr(r, attr)}" for label, attr in fields if hasattr(r, attr)]
    return "risk: " + ", ".join(parts)


def _fail_open_line(e) -> str:
    line = f"fail_open: {getattr(e, 'location', '')} {getattr(e, 'exception_type', '')}".rstrip()
    rationale = getattr(e, "rationale", None)
    return f"{line} — {rationale}" if rationale else line


def collect_chunks(resolved, model) -> list:
    """The collect model's facts on each found anchor's module, one chunk per fact kind:
    `collect:callers|calls|contracts|risk|fail_open:<path>`, kind `collect`. An absent
    model (or none) gives `[]` and no error."""
    if model is None or not getattr(model, "available", False):
        return []
    out, seen = [], set()
    for ra in resolved:
        if not ra.found or not ra.path.endswith(".py"):
            continue
        path, why = ra.path, ra.anchor.text
        names = [f"{path}:{ra.qualname}", path] if ra.qualname else [path]
        contracts = []
        for name in names:
            for c in _query(model.contracts_for, name):
                if c not in contracts:
                    contracts.append(c)
        facts = (
            ("callers", [f"caller: {p}" for p in _query(model.callers_of, path, limit=COLLECT_LIMIT)]),
            ("calls", [f"calls: {p}" for p in _query(model.calls_into, path, limit=COLLECT_LIMIT)]),
            ("contracts", sorted(_contract_line(c) for c in contracts)),
            ("risk", [_risk_line(r) for r in _query(model.risk_for, path)]),
            ("fail_open", sorted(_fail_open_line(e) for e in _query(model.fail_open_for, path))),
        )
        for kind, rows in facts:
            cid = f"collect:{kind}:{path}"
            if not rows or cid in seen:
                continue
            seen.add(cid)
            out.append(Chunk(id=cid, kind="collect", path=path, start=0, end=0,
                             text="\n".join(rows), why=why))
    return out
