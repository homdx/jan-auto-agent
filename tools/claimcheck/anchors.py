"""CC-1 — anchors: what in a claim names the repository, and does it exist.

A claim is *about our code* when something in it names the repository — a path,
a function, a commit, a ticket — and that name is there at the pinned commit.
`lenz_claim_filter.is_internal` guessed this from the text alone, with a regex;
it missed claims whose subject is plain prose and flagged world claims that
merely hold a slash (`dir/{a => b}.py` is git's rename output, not our file).
Three steps replace the guess:

* `extract_anchors(claim)` — pure, no I/O: the tokens that look like a name from
  the repository, with their spans;
* `resolve_anchors(anchors, view)` — each anchor looked up in a `RepoView`;
* `classify(claim, resolved)` — `code`, `world` or `mixed`.

An anchor that does NOT resolve is the most valuable kind: a function that is not
there is a claim that may simply be false. So an unresolved anchor still makes the
claim `code` when it is *shaped like ours* (a file under a directory the repository
has, a function of a module that is there). `resolve_anchors` records that place in
`ResolvedAnchor.path` — `found=False` with `path != ""` is a *dangling* anchor, and
CC-5 turns it into evidence ("`X` does not exist at <sha7>").
"""

from __future__ import annotations

import ast
import bisect
import builtins
import keyword
import os
import re
import threading
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from tools.arena import gitref
from tools.claimcheck.model import Anchor, RepoView, ResolvedAnchor

# ------------------------------------------------------------------ extraction

# any case (`Tests.MD`), and the other languages a target may hold: `App.java`, `run.js` gave no
# anchor, so a claim about the file went to the voters as a world claim with no evidence. A bare
# name of a suffix the repository has no file of still resolves to nothing (`Node.js`: world).
_SUFFIXES = ("(?i:py|pyi|md|rst|ini|cfg|conf|json|jsonl|csv|sh|bash|toml|yaml|yml|txt|xml|html|css|"
             "js|jsx|ts|tsx|mjs|java|kt|kts|gradle|go|rs|c|h|cc|cpp|hpp|rb|php|cs|swift|scala|sql|"
             "proto|properties|lock|ipynb|bat|ps1|mk|cmake)")
_HEX_CUE = re.compile(
    r"(?:(?<!\w)(?i:commits?|sha|at|HEAD)[:\s]\s*|(?<!\w)@)`?(?P<h>[0-9a-f]{7,40})\b")
_HEX_TICKS = re.compile(r"`(?P<h>[0-9a-f]{7,40})`")
_TICKET_ID = re.compile(r"\b(?:KC|FL|AR|AUTO|SLOW|GATE1|CC)(?:-[A-Z][A-Z0-9]{0,15}){0,2}-\d+\b")
_TICKET_NUM = re.compile(r"\b(?i:ticket)\s+\d{1,4}\b")
_NAME = r"[A-Za-z_]\w*(?:(?:::|\.)[A-Za-z_]\w*)*"
_TEST_PATH = re.compile(
    r"(?<![\w/.\-$@])(?P<p>(?:[\w.\-]+/)*[\w\-][\w.\-]*\.py)::(?P<n>" + _NAME + ")")
_TEST_MODULE = re.compile(r"(?<![\w/.\-$@])(?P<p>test_\w+)::(?P<n>" + _NAME + ")")
_PATH = re.compile(
    r"(?<![\w/.\-$@])(?P<p>(?:[\w.\-]+/)*[\w\-][\w.\-]*\.(?:" + _SUFFIXES + r")\b)"
    r"(?::\d+(?:-\d+)?)?(?![\w/])")
_TICKS = re.compile(r"`([^`\n]+)`")
_SYMBOL_IN_TICKS = re.compile(
    r"\s*(?P<n>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*(?P<call>\(.*\))?\s*", re.S)
_DEF_BEFORE = re.compile(r"\b(?:def|class)\s+$")
_CALL = re.compile(r"(?<![\w.])(?P<n>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\(\)")
_REF_ORIGIN = re.compile(r"(?<![\w/.\-])origin/(?P<n>\w[\w./\-]*\w)")
_REF_BRANCH = re.compile(r"\b(?i:branch(?:es)?)\s+`?(?P<n>\w[\w./\-]*\w|\w)`?")
_REF_ON = re.compile(r"\bon\s+`?(?P<n>main|master|HEAD)`?(?![\w/.\-])")
_REF_HEAD = re.compile(r"(?<![\w/.\-])HEAD(?:[~^]\d*)*(?![\w/.\-])")   # HEAD~1 is not HEAD
_NOT_BRANCH_NAMES = frozenset((
    "the a an of is was has had and or that this it to in on by with for as not than "
    "name names point points protection can will does do are were be been which").split())
# What a backticked word is that is NOT a name from our code: the language, the
# standard library, and the tools every report mentions.
_STDLIB = frozenset(sys.stdlib_module_names)
_BUILTINS = frozenset(dir(builtins))
_NOT_SYMBOLS = (frozenset(keyword.kwlist) | _BUILTINS | _STDLIB
                | frozenset(("pytest", "git", "pip", "python", "python3", "bash", "sh",
                             "self", "cls", "numpy", "pandas")))


def _bare_symbol_ok(name: str, called: bool) -> bool:
    """A single identifier is a symbol only inside backticks, shaped like a name
    from code (snake_case, CamelCase) — or written as a call, `run()`."""
    if name in _NOT_SYMBOLS or (name.startswith("__") and name.endswith("__")):
        return False
    if called:
        return True
    return "_" in name or bool(re.match(r"[A-Z][a-z0-9]", name)) or bool(re.search(r"[a-z][A-Z]", name))


def _is_sha(text: str) -> bool:
    return bool(re.search(r"\d", text)) and bool(re.search(r"[a-f]", text))


def _commit_spans(claim: str):
    for rx in (_HEX_CUE, _HEX_TICKS):
        for m in rx.finditer(claim):
            # a hash with no letter a-f is a number unless the claim says it is a commit:
            # one real prefix in forty is all digits ("Commit 480164324 ...")
            said = m.group(0).lstrip("@ ").lower().startswith(("commit", "sha")) or m.group(0).startswith("@")
            if said or _is_sha(m.group("h")):
                yield "commit", m.start("h"), m.end("h")


def _ticket_spans(claim: str):
    for rx in (_TICKET_ID, _TICKET_NUM):
        for m in rx.finditer(claim):
            yield "ticket", m.start(), m.end()


def _is_test_target(path: str, name: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    segments = re.split(r"::|\.", name)
    return (base.startswith("test_") or base.endswith("_test.py")
            or path.startswith("tests/") or "/tests/" in path
            or any(seg.lower().startswith("test") for seg in segments))


def _test_spans(claim: str):
    """`path.py::name` and `test_x::name`: one anchor, never split into a path and a
    symbol. A test when the file or the name says so, else a path-qualified symbol."""
    for rx in (_TEST_PATH, _TEST_MODULE):
        for m in rx.finditer(claim):
            kind = "test" if _is_test_target(m.group("p"), m.group("n")) else "symbol"
            yield kind, m.start(), m.end()


def _is_rename(claim: str, start: int, end: int) -> bool:
    """`a.py => b.py`, `{a => b}.py`: git's rename form, not a file of ours."""
    return (claim[max(0, start - 4):start].rstrip().endswith("=>")
            or claim[end:end + 4].lstrip().startswith("=>"))


#: a quoted path with a space in it: `dir with space/a b.py`, "a b.py" — the quotes are its ends (a single
#: quote is not one: "the file's config.py's" would be a path "s config.py")
_QUOTED_SPACED_PATH = re.compile(
    r"(?P<q>[`\"])(?P<p>(?:[\w.\- ]+/)*[\w\-][\w.\- ]*\.(?:" + _SUFFIXES + r"))(?::\d+(?:-\d+)?)?(?P=q)")


_SUFFIX_END = re.compile(r"\.(?:" + _SUFFIXES + r")$")


def _spaced_path(text: str) -> bool:
    """A quoted text that is one path with spaces, not a command that names a path: a space
    past the first `/` (`dir with space/a b.py`), no word an option, no word but the last a
    file. `python3 scripts/run_claim_check.sh` is a command and the path in it (CC-1's golden);
    `a b.py` alone is either, and stays the plain rule's."""
    words = text.split()
    return (text == text.strip() and "/" in text and " " in text.split("/", 1)[1]
            and not any(w.startswith("-") for w in words)
            and not any(_SUFFIX_END.search(w) for w in words[:-1]))


def _path_spans(claim: str):
    # first: bare, `dir with space/a b.py` was anchored as `b.py`, which does not exist, and the
    # voter was told so — a confident FALSE on a file that is there. Only a quoted one: unquoted,
    # where a path with spaces starts is anyone's guess
    for m in _QUOTED_SPACED_PATH.finditer(claim):
        if _spaced_path(m.group("p")):
            yield "path", m.start("p"), m.end("p")
    for m in _PATH.finditer(claim):
        if m.group("p").startswith("www.") or _is_rename(claim, m.start("p"), m.end()):
            continue   # a host name, or git's rename form
        yield "path", m.start("p"), m.end()


def _symbol_spans(claim: str):
    for m in _TICKS.finditer(claim):
        sym = _SYMBOL_IN_TICKS.fullmatch(m.group(1))
        if not sym:
            continue
        name, called = sym.group("n"), bool(sym.group("call"))
        if "." not in name and not _bare_symbol_ok(name, called):
            continue
        if "." in name and keyword.iskeyword(name.split(".", 1)[0]):
            continue
        start = m.start(1) + sym.start("n")
        yield "symbol", start, start + len(name)
    for m in _CALL.finditer(claim):
        name = m.group("n")
        if "." not in name and not _bare_symbol_ok(name, True):
            continue
        if _DEF_BEFORE.search(claim, 0, m.start("n")):
            continue    # `def f():` in a quoted piece of code is a definition, not a mention
        yield "symbol", m.start("n"), m.end("n")


def _ref_spans(claim: str):
    for m in _REF_ORIGIN.finditer(claim):
        yield "ref", m.start(), m.end()
    for m in _REF_BRANCH.finditer(claim):
        if m.group("n").lower() not in _NOT_BRANCH_NAMES:
            yield "ref", m.start("n"), m.end("n")
    for m in _REF_ON.finditer(claim):
        yield "ref", m.start("n"), m.end("n")
    for m in _REF_HEAD.finditer(claim):
        yield "ref", m.start(), m.end()


_FINDERS = (_commit_spans, _ticket_spans, _test_spans, _path_spans, _symbol_spans, _ref_spans)


def extract_anchors(claim: str) -> list:
    """The anchors of *claim*, in text order, without overlaps, with spans.

    Pure: no I/O, no clock. Kinds claim the text in a fixed priority — commit,
    ticket, test, path, symbol, ref — so a `path::test` is one anchor and `gates.py`
    is a path, not the dotted symbol `gates.py`. `claim[a.start:a.end] == a.text`."""
    starts: list = []   # the taken spans, sorted and disjoint: only the two neighbours
    ends: list = []     # of a new span can overlap it (a scan of all was quadratic)
    found: list = []
    for finder in _FINDERS:
        for kind, start, end in finder(claim):
            i = bisect.bisect_left(starts, start)
            if (start >= end or (i > 0 and ends[i - 1] > start)
                    or (i < len(starts) and starts[i] < end)):
                continue
            starts.insert(i, start)
            ends.insert(i, end)
            found.append(Anchor(kind, claim[start:end], start, end))
    found.sort(key=lambda a: (a.start, a.end))
    return [_with_wider(claim, a) if a.kind == "path" else a for a in found]


#: words a path's longer reading may take in front of it (`a long dir name/x.py`)
MAX_WIDER_WORDS = 3
_WORD_BEFORE = re.compile(r"[\w.\-/]+ $")


def _with_wider(claim: str, a: Anchor) -> Anchor:
    """*a* with its longer readings (`Anchor.wider`): the words before it, one space apart.

    A space in a file or directory name is ambiguous in a sentence: `my file.py`, `renamed é.py`
    and `my dir/mod.py` were anchored as `file.py`, `é.py`, `dir/mod.py`, and the voter was told the
    file does not exist (or the claim went to `world`). Which reading is meant only the repository
    can say, so extraction offers them and `_resolve_path` takes one that is a file. *quoted*: the
    reading is exactly the text between a pair of backticks or double quotes."""
    out, s = [], a.start
    for _ in range(MAX_WIDER_WORDS):
        m = _WORD_BEFORE.search(claim, max(0, s - 200), s)
        if not m:
            break
        s = m.start()
        quoted = s > 0 and claim[s - 1] in "`\"" and claim[a.end:a.end + 1] == claim[s - 1]
        out.append((s, claim[s:a.end], quoted))
    if not out:
        return a
    return Anchor(a.kind, a.text, a.start, a.end, wider=tuple(reversed(out)))


# ------------------------------------------------------------------ resolution

MAX_PY_BYTES = 400 * 1024     # a bigger .py file is not indexed (generated, vendored)
_INDEX_ATTR = "_cc_definition_index"


@dataclass(frozen=True)
class _Def:
    path: str
    qualname: str
    start: int   # decorators included
    end: int


def _assigned_names(node) -> list:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names: list = []
    for t in targets:
        for n in ([t] if isinstance(t, ast.Name) else getattr(t, "elts", ())):
            if isinstance(n, ast.Name):
                names.append(n.id)
    return names


def _definitions(text: str, path: str) -> list:
    """Every `def`, `async def` and `class` of one module, nested ones by dotted
    qualname, each with the span a reader would call the definition; and every name a module
    or a class body assigns (`MAX_RETRIES = 3`, `timeout: int = 30`), a constant being a
    definition too — a claim about `MAX_RETRIES` found nothing and went to the voters as a
    world claim, with no code in front of them. A function's locals are not definitions."""
    tree = ast.parse(text)
    out: list = []
    stack = [(tree, "", True)]     # node, qualname prefix, whether its assignments define names
    while stack:
        node, prefix, defining = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = prefix + child.name
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                out.append(_Def(path, qualname, start, child.end_lineno or child.lineno))
                stack.append((child, qualname + ".", isinstance(child, ast.ClassDef)))
            elif defining and isinstance(child, (ast.Assign, ast.AnnAssign)):
                for name in _assigned_names(child):
                    out.append(_Def(path, prefix + name, child.lineno, child.end_lineno or child.lineno))
            else:
                stack.append((child, prefix, defining))
    return out


class _Index:
    """The repository as the resolver needs it, built once per view."""

    def __init__(self, view: RepoView) -> None:
        self.files = sorted(view.files())
        self.by_qualname: dict = {}
        self.by_last: dict = {}
        self.module_of: dict = {}   # path -> tuple of dotted parts, "x/__init__.py" -> ("x",)
        self.line_count: dict = {}
        for path in self.files:
            if not path.endswith(".py"):
                continue
            parts = path[:-3].split("/")
            if parts[-1] == "__init__" and len(parts) > 1:
                parts = parts[:-1]
            self.module_of[path] = tuple(parts)
            try:
                text = view.read(path)
                if len(text.encode("utf-8", "surrogateescape")) > MAX_PY_BYTES:
                    continue
                self.line_count[path] = text.count("\n") + 1
                # bytes that were not UTF-8 reach us as surrogates (read_utf8), which `ast` refuses:
                # parse them as U+FFFD, line numbers are the same
                defs = _definitions(text.encode("utf-8", "surrogateescape").decode("utf-8", "replace"), path)
            except (OSError, ValueError, SyntaxError, RecursionError, MemoryError, UnicodeError):
                continue
            for d in defs:
                self.by_qualname.setdefault(d.qualname, []).append(d)
                self.by_last.setdefault(d.qualname.rsplit(".", 1)[-1], []).append(d)
        for table in (self.by_qualname, self.by_last):
            for defs in table.values():
                defs.sort(key=lambda d: (d.path, d.start))
        self.top_dirs = {p.split("/", 1)[0] for p in self.files if "/" in p}
        self.dirs = {p.rsplit("/", i)[0] for p in self.files for i in range(1, p.count("/") + 1)}
        self.file_set = set(self.files)
        self.basenames: dict = {}
        for path in self.files:
            self.basenames.setdefault(path.rsplit("/", 1)[-1], []).append(path)

    def modules(self, dotted: tuple) -> list:
        """Python files whose module path ends with *dotted* (`gates` -> every gates.py)."""
        n = len(dotted)
        return sorted(p for p, parts in self.module_of.items() if parts[-n:] == dotted)


#: one index build at a time: Gate 1's presence workers share one view of the target (CC-10), and
#: four threads that each missed the cache built the same five-second index four times
_INDEX_LOCK = threading.Lock()


def _index_of(view: RepoView) -> _Index:
    cached = getattr(view, _INDEX_ATTR, None)
    if isinstance(cached, _Index):
        return cached
    with _INDEX_LOCK:
        cached = getattr(view, _INDEX_ATTR, None)
        if isinstance(cached, _Index):
            return cached
        index = _Index(view)
        try:
            setattr(view, _INDEX_ATTR, index)    # a view is a pinned snapshot: one index per view
        except (AttributeError, TypeError):
            pass
        return index


def _found(anchor: Anchor, d: _Def, others: list) -> ResolvedAnchor:
    return ResolvedAnchor(
        anchor, True, path=d.path, qualname=d.qualname, lines=(d.start, d.end),
        candidates=tuple((o.path, o.qualname, o.start, o.end) for o in others[:2]))


def _split_lines(text: str) -> tuple:
    m = re.fullmatch(r"(.*?)(?::(\d+)(?:-(\d+))?)?", text)
    path, a, b = m.group(1), m.group(2), m.group(3)
    return path, ((int(a), int(b or a)) if a else (0, 0))


def _nearest_dir(index: _Index, path: str) -> str:
    """The deepest directory of *path* the repository has, "" when even the first
    component is not one of its directories."""
    parts = path.split("/")[:-1]
    if not parts or parts[0] not in index.top_dirs:
        return ""
    while parts and "/".join(parts) not in index.dirs:
        parts.pop()
    return "/".join(parts)


def _wider_file(anchor: Anchor, index: _Index, quoted: bool, exact: bool = False) -> Optional[ResolvedAnchor]:
    """The first longer reading of *anchor* (`Anchor.wider`) that is one file of the repository:
    the path itself, or (not *exact*) the one file it is the tail or the name of."""
    for start, text, q in anchor.wider:
        if q != quoted:
            continue
        raw, lines = _split_lines(text)
        path = raw[2:] if raw.startswith("./") else raw
        hit = path if path in index.file_set else None
        if hit is None and not exact:
            tails = ([p for p in index.files if p.endswith("/" + path)] if "/" in path
                     else list(index.basenames.get(path, ())))
            hit = tails[0] if len(tails) == 1 else None
        if hit:
            return ResolvedAnchor(Anchor("path", text, start, anchor.end), True, path=hit, lines=lines)
    return None


def _resolve_path(anchor: Anchor, index: _Index) -> ResolvedAnchor:
    """A path anchor; with longer readings (a space in the name), in this order: a quoted reading
    that is a file (`my file.py` in backticks), the plain reading as a path of the repository, an
    unquoted reading as a path of it (`my file.py defines …`), the plain reading as a file's tail or
    name, an unquoted reading as one. So a sentence's word is never glued to a path that is there."""
    if not anchor.wider:
        return _resolve_plain_path(anchor, index)
    wide = _wider_file(anchor, index, quoted=True)
    if wide is not None:
        return wide
    plain = _resolve_plain_path(anchor, index)
    raw = _split_lines(anchor.text)[0]
    raw = raw[2:] if raw.startswith("./") else raw
    if plain.found and (raw in index.file_set or raw in index.dirs):
        return plain
    wide = _wider_file(anchor, index, quoted=False, exact=True)
    if wide is not None:
        return wide
    if plain.found:
        return plain
    return _wider_file(anchor, index, quoted=False) or plain


def _resolve_plain_path(anchor: Anchor, index: _Index) -> ResolvedAnchor:
    raw, lines = _split_lines(anchor.text)
    path = raw[2:] if raw.startswith("./") else raw
    if path in index.file_set or path in index.dirs:
        return ResolvedAnchor(anchor, True, path=path, lines=lines)
    if "/" in path:
        tails = [p for p in index.files if p.endswith("/" + path)]   # `contest/gates.py`
    else:
        tails = list(index.basenames.get(path, ()))
    if len(tails) == 1:
        return ResolvedAnchor(anchor, True, path=tails[0], lines=lines)
    if tails:   # ambiguous: exists, but the name alone does not say which
        return ResolvedAnchor(anchor, False, path=tails[0], candidates=tuple(tails[:3]))
    if "/" in path:
        return ResolvedAnchor(anchor, False, path=_nearest_dir(index, path))
    suffix = path.rsplit(".", 1)[-1]
    ours = any(p.endswith("." + suffix) for p in index.files)   # a bare name of a kind we have
    return ResolvedAnchor(anchor, False, path="." if ours else "")


def _resolve_symbol(anchor: Anchor, index: _Index) -> ResolvedAnchor:
    text = anchor.text
    if "::" in text:
        left, right = text.split("::", 1)
        qualname = right.replace("::", ".")
        if left.endswith(".py"):
            files = ([left] if left in index.file_set else
                     [p for p in index.files if p.endswith("/" + left)])
        else:
            files = index.modules(tuple(left.split(".")))
        defs = [d for d in index.by_qualname.get(qualname, ()) if d.path in files]
        if defs:
            return _found(anchor, defs[0], defs[1:])
        if files:
            return ResolvedAnchor(anchor, False, path=files[0])
        return ResolvedAnchor(anchor, False, path=_nearest_dir(index, left))
    parts = text.split(".")
    if len(parts) == 1:   # a bare name: unique, or the first of several
        defs = index.by_last.get(text, [])
        if defs:
            return _found(anchor, defs[0], defs[1:])
        return ResolvedAnchor(anchor, False)
    exact = index.by_qualname.get(text, [])
    if exact:
        return _found(anchor, exact[0], exact[1:])
    nearest = ""
    for k in range(len(parts) - 1, 0, -1):          # module-qualified, longest module first
        files = index.modules(tuple(parts[:k]))
        if not files:
            continue
        qualname = ".".join(parts[k:])
        defs = [d for d in index.by_qualname.get(qualname, ()) if d.path in files]
        if defs:
            return _found(anchor, defs[0], defs[1:])
        nearest = nearest or files[0]
    files = index.modules(tuple(parts))             # the module itself
    if files:
        return ResolvedAnchor(anchor, True, path=files[0],
                              lines=(1, index.line_count.get(files[0], 1)))
    owner = index.by_qualname.get(".".join(parts[:-1]))    # `Store.compact`, class there, method not
    return ResolvedAnchor(anchor, False, path=nearest or (owner[0].path if owner else ""))


def _resolve_commit(anchor: Anchor, view: RepoView) -> ResolvedAnchor:
    try:
        sha = view.rev_parse(anchor.text) or ""
    except Exception:   # a view that cannot answer has not found it
        sha = ""
    return ResolvedAnchor(anchor, bool(sha), sha=sha)


def _resolve_ticket(anchor: Anchor, view: RepoView, index: _Index) -> ResolvedAnchor:
    ticket_id = anchor.text.split()[-1]             # "ticket 123" -> "123", "KC-34" -> "KC-34"
    try:
        path = view.ticket_file(ticket_id) or ""
    except Exception:
        path = ""
    if path:
        return ResolvedAnchor(anchor, True, path=path)
    ours = "-" in ticket_id or "epic-tasks" in index.top_dirs
    return ResolvedAnchor(anchor, False, path="epic-tasks" if ours else "")


#: an identifier written without backticks: snake_case or UPPER_SNAKE (one `_` at least, not only
#: at the ends), outside a path, a call, a dotted name and backticks — `retry_call never raises`
_BARE_IDENT = re.compile(r"(?<![\w.`/$@\-])([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)(?![\w`(/\-])")


def resolve_claim(claim: str, view: RepoView) -> list:
    """`resolve_anchors` of *claim*'s anchors, plus every identifier written without backticks
    that the repository defines (`retry_call`, `MAX_RETRIES`): a claim that named the repo's own
    function that way was a `world` claim, settled by the voters with no code in front of them
    (the legacy symbols rule had it as code). An identifier the repository does not define stays
    a word; a single lowercase word (`backoff`) stays one by ticket 255's rule, written either way."""
    anchors = extract_anchors(claim)
    resolved = resolve_anchors(anchors, view)
    taken = [(a.start, a.end) for a in anchors]
    extra = []
    index = None
    for m in _BARE_IDENT.finditer(claim):
        start, end = m.span(1)
        name = m.group(1)
        if any(start < e and s < end for s, e in taken) or name in _NOT_SYMBOLS or keyword.iskeyword(name):
            continue
        index = index or _index_of(view)
        got = _resolve_symbol(Anchor("symbol", name, start, end), index)
        if got.found:
            extra.append(got)
    if not extra:
        return resolved
    out = sorted(resolved + extra, key=lambda r: (r.anchor.start, r.anchor.end))
    return _prefer_named_file(out, index)


def resolve_anchors(anchors, view: RepoView) -> list:
    """Each anchor looked up in *view*; one `ResolvedAnchor` per anchor, same order.

    A file that does not parse, or is over `MAX_PY_BYTES`, is skipped when the
    definitions are indexed — never fatal. The index is built once per view."""
    anchors = list(anchors)
    index = _index_of(view) if any(a.kind != "commit" and a.kind != "ref" for a in anchors) else None
    out = []
    for a in anchors:
        if a.kind == "path":
            out.append(_resolve_path(a, index))
        elif a.kind in ("symbol", "test"):
            out.append(_resolve_symbol(a, index))
        elif a.kind == "ticket":
            out.append(_resolve_ticket(a, view, index))
        else:   # commit, ref
            out.append(_resolve_commit(a, view))
    return _prefer_named_file(out, index) if index is not None else out


def _prefer_named_file(out: list, index: _Index) -> list:
    """`X` in `tools/y.py`: a name defined in several files means the one the claim names.

    The first definition by path is the answer for a bare name on its own; when the claim
    also names a file that holds a definition of it, that file is the answer. "Of it" is by the
    name as written: `request_completion` in `tools/llm_stream.py` is that module's function, not
    the first by path, `tests/test_contest_cli.py`'s nested stub of the same name — the stub's
    qualname was the only one looked for in the named file, so the decoy won."""
    files = {r.path for r in out if r.anchor.kind == "path" and r.found and r.path in index.file_set}
    if not files:
        return out
    fixed = list(out)
    for i, r in enumerate(out):
        if r.anchor.kind not in ("symbol", "test") or not (r.found and r.qualname and r.candidates) \
                or r.path in files:
            continue
        last = r.qualname.rsplit(".", 1)[-1]
        written = r.anchor.text.split("::")[-1].replace("::", ".")
        defs = [d for d in index.by_last.get(last, ())
                if d.qualname == r.qualname or d.qualname == written or d.qualname.endswith("." + written)]
        # in the named file: the definition named exactly as written first, then any of that name
        in_file = [d for d in defs if d.path in files]
        pick = next((d for d in in_file if d.qualname == written), in_file[0] if in_file else None)
        if pick is not None:
            fixed[i] = _found(r.anchor, pick, [d for d in defs if d is not pick])
    return fixed


# ------------------------------------------------------------------ classification

_CODE_KINDS = frozenset(("path", "symbol", "test", "commit", "ticket"))

# A claim is `mixed` when, besides naming our code, it ASSERTS something about the
# outside world: a clause that goes on about a library ("..., which returns -1"),
# a flag's meaning, an exit code. Mentioning a library call in passing ("uses
# `time.time` as its default clock") is still a claim about our code, so a world
# fact counts only inside such a clause and only when it names an outside API.
_CLAUSE = re.compile(r",\s*(?:which|whose|so|and|because|since|while|whereas)\b|\b(?:because|whereas)\b",
                     re.I)
_WORLD_WORDS = re.compile(
    r"\bexit(?:s|ed)?\s+(?:with\s+)?(?:status|code)\b|\bexit codes?\b|\brc ?\d|unrecogni[sz]ed|"
    r"\bposix\b|\bplugin\b|\b(?:ensure_ascii|sort_keys|capture_output)\b", re.I)
_HEAD = re.compile(r"\s*([A-Za-z_]\w*)")


def _outside_apis(claim: str, own_heads: frozenset) -> bool:
    """A backticked name or call whose head is the standard library or a builtin
    (`subprocess.run`, `str.rfind`, `dict()`, `re.M`, `fnmatch`) and not ours."""
    spans = [m.group(1) for m in _TICKS.finditer(claim)] + [m.group("n") for m in _CALL.finditer(claim)]
    for token in spans:
        head = _HEAD.match(token)
        if head and head.group(1) in (_STDLIB | _BUILTINS) and head.group(1) not in own_heads \
                and not keyword.iskeyword(head.group(1)):
            return True
    return False


def asserts_world_fact(claim: str, resolved) -> bool:
    """True when *claim* states a fact about the world, not only about our code."""
    if not _CLAUSE.search(claim):
        return False
    own = frozenset(r.anchor.text.split(".")[0] for r in resolved if r.found)
    return bool(_WORLD_WORDS.search(claim)) or _outside_apis(claim, own)


def is_dangling(resolved) -> bool:
    """True when an anchor that names our code does not resolve though it points
    into the repository (a missing function of a module that is there)."""
    return any(r.anchor.kind in _CODE_KINDS and not r.found and r.path for r in resolved)


def classify(claim: str, resolved) -> str:
    """`code`, `world` or `mixed`.

    `code`: a path, symbol, test, commit or ticket anchor resolved — or failed to
    resolve but is shaped like ours (see `is_dangling`). `world`: no such anchor; a
    `ref` alone never makes a claim `code`. `mixed`: `code` plus a world fact."""
    resolved = list(resolved)
    ours = any(r.anchor.kind in _CODE_KINDS and (r.found or r.path) for r in resolved)
    if not ours:
        return "world"
    return "mixed" if asserts_world_fact(claim, resolved) else "code"


# ------------------------------------------------------------------ PathRepoView

GIT_TIMEOUT = 20.0   # seconds one `PathRepoView.git` call may take; a hung git raises GitRefError
_READ_ONLY_GIT = frozenset((
    "rev-parse rev-list log show diff diff-tree cat-file ls-tree ls-files blame grep "
    "merge-base describe name-rev show-ref for-each-ref shortlog status").split())
_FORBIDDEN_GIT_ARGS = ("--output", "--ext-diff", "--textconv", "--open-files-in-pager", "--exec-path",
                       "--git-dir", "--work-tree", "--config-env",
                       "--contents",   # blame --contents reads a file outside the repository
                       "--no-index")   # diff/grep --no-index read any path on disk, not the repository
#: a diff driver is named by the commit's .gitattributes and defined in the operator's config;
#: git runs its textconv / external diff by default, so the view turns both off for these.
_NO_FILTERS_GIT = frozenset(("show", "diff", "diff-tree", "log", "blame"))   # blame runs textconv too


#: directories of a plain tree (not a git checkout) that are not the project's own code
_VENDORED = frozenset((".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages", ".tox",
                       ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".eggs"))


class PathRepoView:
    """A `RepoView` over a directory; a pinned snapshot — the file list is read once.

    Works on a plain directory (an exported tree) as well as on a git checkout:
    `rev_parse` answers None and `git` refuses when there is no `.git`."""

    def __init__(self, root) -> None:
        self.root = Path(root)
        self._files: Optional[list] = None

    def _safe(self, path: str) -> Optional[Path]:
        """*path* under the root, or None when it names something outside it."""
        if not path or path.startswith("/") or ".." in Path(path).parts:
            return None
        full = self.root / path
        real_root = os.path.realpath(self.root)
        real = os.path.realpath(full)
        return full if real == real_root or real.startswith(real_root + os.sep) else None

    def exists(self, path: str) -> bool:
        full = self._safe(path)
        return full is not None and full.exists()

    def read(self, path: str) -> str:
        full = self._safe(path)
        if full is None or not full.is_file():
            raise FileNotFoundError(path)
        return gitref.read_utf8(full)

    def files(self) -> list:
        """The files of the tree: a git checkout's tracked and untracked-but-not-ignored ones
        (`git ls-files`), a plain directory's minus the vendored trees (`_VENDORED`); in both, a
        symlink to another file of the tree is left out.

        It was every file under the root but `.git`: Gate 1 runs on the live working directory, and
        a cited `request_completion` resolved to a stub in `.regression_tests/` (a mirror of
        `tests/`), `Settings.load_config` to `.venv/…/site-packages/` — the first chunk the voters
        read was the decoy, and a 20,000-file `.venv` was indexed for every run."""
        if self._files is None:
            listed = self._git_files()
            found = listed if listed is not None else self._walk()
            self._files = sorted(self._without_mirrors(found))
        return list(self._files)

    def _git_files(self) -> Optional[list]:
        if not (self.root / ".git").exists():
            return None
        try:
            out = gitref.git(self.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                             strip=False, timeout=120)
        except gitref.GitRefError:
            return None
        names = [n for n in dict.fromkeys(out.split("\0")) if n]
        # a committed node_modules or virtualenv is still not the project's code
        venvs = tuple(n[:-len("pyvenv.cfg")] for n in names if n == "pyvenv.cfg" or n.endswith("/pyvenv.cfg"))
        return [n for n in names if os.path.isfile(os.path.join(self.root, n))
                and not _VENDORED.intersection(n.split("/")[:-1])
                and not any(n.startswith(v) for v in venvs)]

    def _walk(self) -> list:
        found = []
        for folder, dirs, names in os.walk(self.root):
            dirs[:] = [d for d in dirs if d not in _VENDORED
                       and not os.path.isfile(os.path.join(folder, d, "pyvenv.cfg"))]
            rel = os.path.relpath(folder, self.root)
            for name in names:
                if name == ".git":   # a worktree's or a submodule's `.git` is a file, not a directory
                    continue
                path = name if rel == "." else f"{rel}/{name}".replace(os.sep, "/")
                if os.path.isfile(os.path.join(folder, name)):
                    found.append(path)
        return found

    def _without_mirrors(self, paths: list) -> list:
        """*paths* minus each symlink whose target is another of *paths* (`.smoke_tests/x.py` ->
        `tests/x.py`): the same definition twice, and the mirror's path sorted first."""
        listed = set(paths)
        real_root = os.path.realpath(self.root)
        out = []
        for p in paths:
            full = os.path.join(self.root, p)
            if os.path.islink(full):
                real = os.path.realpath(full)
                if real.startswith(real_root + os.sep) and \
                        os.path.relpath(real, real_root).replace(os.sep, "/") in listed:
                    continue
            out.append(p)
        return out

    def rev_parse(self, rev: str) -> Optional[str]:
        if not rev or rev.startswith("-") or not (self.root / ".git").exists():
            return None
        try:
            return gitref.git(self.root, "rev-parse", "--verify", "--quiet", rev + "^{commit}") or None
        except gitref.GitRefError:
            return None

    def ticket_file(self, ticket_id: str) -> Optional[str]:
        """`epic-tasks/NN-*.md` for a ticket: by the number after the dash of the id
        (leading zeros ignored), and for an id with a prefix (`KC-76`) first by the id
        written into the file name (`123-kc76-…md`), because there the number is the
        file's, not the id's."""
        number = re.search(r"(\d+)\s*$", ticket_id)
        if not number:
            return None
        n = int(number.group(1))
        names = [p for p in self.files() if p.startswith("epic-tasks/") and p.count("/") == 1
                 and p.endswith(".md")]
        by_number = [p for p in names if re.match(r"(\d+)-", p[len("epic-tasks/"):])
                     and int(re.match(r"(\d+)-", p[len("epic-tasks/"):]).group(1)) == n]
        if "-" not in ticket_id:
            return by_number[0] if by_number else None
        slug = "-?".join(re.escape(part) for part in ticket_id.lower().split("-"))   # kc76 and cc-1
        # the epic's own ticket folders too: CC-9 is docs/claim-check/tickets/291-cc-9-….md, and
        # looked up in epic-tasks/ alone it fell back to 09-v6-… — an unrelated ticket, shown to
        # the voter as CC-9, whose verbatim quote then verified
        tickets = [p for p in self.files() if re.fullmatch(r"docs/[^/]+/tickets/[^/]+\.md", p)]
        by_slug = [p for p in names + tickets if re.search(rf"(?<![a-z0-9]){slug}(?![0-9])", p.lower())]
        if by_slug:
            return by_slug[0]
        # by its number alone only when the file names the id (`07-x.md` holding `# KC-7`): for a
        # prefixed id the number in a file name is the file's (AUTO-3 is not 03-…)
        named = re.compile(rf"(?<![A-Za-z0-9]){slug}(?![0-9])", re.I)
        for p in by_number:
            try:
                if named.search(self.read(p)):
                    return p
            except (OSError, UnicodeDecodeError):
                continue
        return None

    def git(self, *args: str) -> str:
        """`git <args>` in the root, read-only: a short allow-list of subcommands
        and no option that writes a file or starts a program."""
        if not args or args[0] not in _READ_ONLY_GIT:
            raise ValueError(f"git {args[0] if args else ''}: not a read-only subcommand")
        for i, arg in enumerate(args):
            if (arg.startswith(_FORBIDDEN_GIT_ARGS) or arg.startswith("-O")
                    or (arg == "-c" and i + 1 < len(args) and "=" in args[i + 1])):
                raise ValueError(f"git {args[0]} {arg}: refused, it writes or runs a program")
        if args[0] in _NO_FILTERS_GIT:
            args = (args[0], "--no-textconv", "--no-ext-diff", *args[1:])
        # the operator's global and system config are not read: `color.ui=always` or
        # `diff.noprefix` there changed the text a voter is shown (CC-4)
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        return gitref.git(self.root, *args, env=env, strip=False, timeout=GIT_TIMEOUT)
