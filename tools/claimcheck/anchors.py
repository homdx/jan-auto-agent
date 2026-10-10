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
import builtins
import keyword
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from tools.arena import gitref
from tools.claimcheck.model import Anchor, RepoView, ResolvedAnchor

# ------------------------------------------------------------------ extraction

_SUFFIXES = "py|md|ini|json|csv|sh|toml|yaml|yml|txt"
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
_REF_HEAD = re.compile(r"(?<![\w/.\-])HEAD(?![\w/.\-])")
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
            if _is_sha(m.group("h")):
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


def _path_spans(claim: str):
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
    taken: list = []
    found: list = []
    for finder in _FINDERS:
        for kind, start, end in finder(claim):
            if start >= end or any(start < b and a < end for a, b in taken):
                continue
            taken.append((start, end))
            found.append(Anchor(kind, claim[start:end], start, end))
    found.sort(key=lambda a: (a.start, a.end))
    return found


# ------------------------------------------------------------------ resolution

MAX_PY_BYTES = 400 * 1024     # a bigger .py file is not indexed (generated, vendored)
_INDEX_ATTR = "_cc_definition_index"


@dataclass(frozen=True)
class _Def:
    path: str
    qualname: str
    start: int   # decorators included
    end: int


def _definitions(text: str, path: str) -> list:
    """Every `def`, `async def` and `class` of one module, nested ones by dotted
    qualname, each with the span a reader would call the definition."""
    tree = ast.parse(text)
    out: list = []
    stack = [(tree, "")]
    while stack:
        node, prefix = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = prefix + child.name
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                out.append(_Def(path, qualname, start, child.end_lineno or child.lineno))
                stack.append((child, qualname + "."))
            else:
                stack.append((child, prefix))
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
                defs = _definitions(text, path)
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


def _index_of(view: RepoView) -> _Index:
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


def _resolve_path(anchor: Anchor, index: _Index) -> ResolvedAnchor:
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
    return out


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

_READ_ONLY_GIT = frozenset((
    "rev-parse rev-list log show diff diff-tree cat-file ls-tree ls-files blame grep "
    "merge-base describe name-rev show-ref for-each-ref shortlog status").split())
_FORBIDDEN_GIT_ARGS = ("--output", "--ext-diff", "--open-files-in-pager", "--exec-path")


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
        if self._files is None:
            found = []
            for folder, dirs, names in os.walk(self.root):
                dirs[:] = [d for d in dirs if d != ".git"]
                rel = os.path.relpath(folder, self.root)
                for name in names:
                    path = name if rel == "." else f"{rel}/{name}".replace(os.sep, "/")
                    if os.path.isfile(os.path.join(folder, name)):
                        found.append(path)
            self._files = sorted(found)
        return list(self._files)

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
        if "-" in ticket_id:
            slug = ticket_id.lower().replace("-", "")
            by_slug = [p for p in names if re.search(rf"(?<![a-z0-9]){re.escape(slug)}(?![0-9])", p.lower())]
            if by_slug:
                return by_slug[0]
        return by_number[0] if by_number else None

    def git(self, *args: str) -> str:
        """`git <args>` in the root, read-only: a short allow-list of subcommands
        and no option that writes a file or starts a program."""
        if not args or args[0] not in _READ_ONLY_GIT:
            raise ValueError(f"git {args[0] if args else ''}: not a read-only subcommand")
        for arg in args:
            if arg.startswith(_FORBIDDEN_GIT_ARGS) or arg == "-O":
                raise ValueError(f"git {args[0]} {arg}: refused, it writes or runs a program")
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"}
        return gitref.git(self.root, *args, env=env, strip=False)
