"""CC-5 — the pack: rank, merge, trim, render, and find a quote (docs/claim-check/tickets/275-…).

CC-3 and CC-4 each hand back as much evidence as they can find. A voter cannot read all
of it (a reasoning model given too much spends its budget before it writes a verdict), so
this module decides what survives, in what order and in what text:

* `build_pack` — collects `source_chunks` and `git_chunks`, adds a **note** for every
  anchor that did not resolve (the missing code is evidence too), merges `src:` chunks of
  one file whose lines overlap or touch, ranks, trims to a `PackBudget`, and returns a
  `Pack` whose chunks are in rank order. It never raises: a provider or a view that fails
  is a note, not a traceback.
* `assemble_pack` — the half of `build_pack` that needs no provider: merge, rank, trim of
  chunks the caller already holds. `build_pack` ends in it; the tests drive it directly.
* `render_pack` — the exact text the voter reads, in a stable format a golden file pins.
* `find_quote` / `find_all_quote` — which chunk (the first, or every one) a quote is
  *verbatim* from. CC-6's `verify_quotes` is built on `find_all_quote`, and this is the only
  place the rule lives (see its docstring).
* `PackBudget` — the limits, read from `[claim_vote]` with defaults.

Decisions the ticket left open, made here and pinned by a test each:

* **Notes** (kind `note`, any id: `note:dangling:…`, `note:ambiguous:…`, CC-4's `note:git:…`)
  all rank as a dangling note does, 110: a voter must know a commit is missing or a git call
  failed as much as it must know a function is gone.
* **A git hunk of a file the claim does not name** (the three largest files of a commit that
  names none of its files, a keyword hunk of a range) scores 70, between a hunk of a named
  file (90) and a source file named alone (60); the table has no row for it.
* **The keyword bonus counts a keyword in a chunk's *text*, not its gutter**: a claim that
  mentions `120` must not match every chunk whose line numbers pass 120.
* **The budget is the rendered text.** `PackBudget.chars` covers the title line, every
  `[[id]]  (kind)` label, every chunk's text and the trailer, so `len(pack.render())` stays
  within it (but for a top chunk that must still be shown on a budget too small for its
  label and one line).
* **Trim stops at the first chunk that does not fit** (the ticket: "until the next one would
  pass either limit"); only the top-ranked chunk is cut to fit rather than dropped.

Everything is deterministic: the same inputs give the same bytes (nothing iterates a set
into the output; ties break by id).
"""

from __future__ import annotations

import ast
import re
import threading
from dataclasses import dataclass, replace
from typing import Optional

from tools.claimcheck.evidence_git import git_chunks
from tools.claimcheck.evidence_source import (
    MAX_READ_BYTES, _clean, _marker, _size, _split, keyword_tokens, source_chunks,
)
from tools.claimcheck.model import Chunk, Pack

MIN_QUOTE = 8             # a quote shorter than this (after normalising) proves nothing
MAX_LINE = 2000           # a line longer than this is cut, with a marker line after it
MAX_NAMES = 10            # names listed in a note ("defines: …")
BONUS_PER_KEYWORD = 10
BONUS_CAP = 40
VIEW_TIMEOUT = 20.0       # seconds one view call of this module may take (patched in tests)

#: Base score by kind (the ticket's table). See the module docstring for the 70.
SCORE_NOTE = 110
SCORE_SYMBOL_SOURCE = 100
SCORE_ANCHORED_HUNK = 90
SCORE_OTHER_HUNK = 70
SCORE_PATH_SOURCE = 60
SCORE_TICKET = 50
SCORE_COLLECT = 40
SCORE_GIT_OTHER = 30      # a log, a header, a stat, anything unknown

_TRAILER_RESERVE = 100    # chars kept free for the trailer line (worst case ~60) and its newline
_MIN_TOP = 80             # the top chunk is shown in at least this many chars, budget or not
_CUT_MARKER_MAX = 24      # `# … 999999999 chars cut`

_CHUNK_GUTTER = re.compile(r"^ *\d*\| ?")                 # ` 120| ` and, for a `-` line, `    | `
_QUOTE_GUTTER = re.compile(r"^(?: *\d+\| ?|  +\| ?)")     # what a voter copies along with a line
_SHOWN = re.compile(r"^ *(\d+)\|(?: |$)")
_OMITTED = re.compile(r"^# … (\d+) lines? omitted \((\d+)-(\d+)\)$")
#: every marker line a chunk can carry: CC-3's and CC-4's, and this module's own
_MARKER_LINE = re.compile(r"^# … \d+ (?:diff )?(?:lines?|chars?) (?:omitted(?: \(\d+-\d+\))?|cut)$")
_INDEX_HEAD = "# definitions (name  line):"
_INDEX_ROW = re.compile(r"^#   (\S.*?)  \d+$")
_INDEX_MORE = re.compile(r"^#   … (\d+) more$")
_SPACE = re.compile(r"\s+")
_LINES = re.compile(r"\r\n|\r|\n")


# ------------------------------------------------------------------ the budget

@dataclass(frozen=True)
class PackBudget:
    """What one claim's pack may hold: characters of rendered text, chunks, and characters
    of one chunk's text."""

    chars: int = 6000
    chunks: int = 6
    per_chunk: int = 2400

    @classmethod
    def from_config(cls, parser) -> "PackBudget":
        """`[claim_vote] pack_chars`, `pack_chunks`, `pack_chunk_chars` of *parser* (a
        `ConfigParser`); a key that is absent, not a positive integer, or a parser that
        is not one gives the default."""
        base = cls()

        def read(key: str, default: int) -> int:
            try:
                value = parser.get("claim_vote", key, fallback=None)
                number = int(str(value).strip())
            except Exception:   # noqa: BLE001 — a bad or missing config is the default, never an error
                return default
            return number if number > 0 else default

        return cls(chars=read("pack_chars", base.chars),
                   chunks=read("pack_chunks", base.chunks),
                   per_chunk=read("pack_chunk_chars", base.per_chunk))


# ------------------------------------------------------------------ the view, guarded

def _guarded(fn, *args):
    """`fn(*args)` in a daemon thread for at most `VIEW_TIMEOUT` s; (True, value), or
    (False, None) for an exception or a timeout. A hung git costs the claim a note."""
    box: dict = {}

    def call() -> None:
        try:
            box["out"] = fn(*args)
        except Exception as err:   # noqa: BLE001 — any view, any failure
            box["err"] = err

    worker = threading.Thread(target=call, name="claimcheck-pack", daemon=True)
    worker.start()
    worker.join(VIEW_TIMEOUT)
    if worker.is_alive() or "err" in box:
        return False, None
    return True, box.get("out")


def _pack_sha(view, head: Optional[str]) -> str:
    """The commit the pack is for: the view's own `sha` when it has one, else `HEAD` of the
    view, else *head* (resolved when the view can, as given when not), else ''."""
    own = getattr(view, "sha", None)
    if isinstance(own, str) and own:
        return own
    rev_parse = getattr(view, "rev_parse", None)
    if callable(rev_parse):
        for rev in ("HEAD", head):
            if not rev:
                continue
            ok, got = _guarded(rev_parse, rev)
            if ok and isinstance(got, str) and got:
                return got
    return head if isinstance(head, str) and head else ""


def _read(view, path: str) -> Optional[str]:
    """A file's text, or None when it is gone, unreadable, or over the cap (never read whole)."""
    try:
        size = _size(view, path)
        if size is not None and size > MAX_READ_BYTES:
            return None
        text = view.read(path)
    except Exception:   # noqa: BLE001 — a read that fails leaves the chunk as it is
        return None
    if not isinstance(text, str) or len(text) > MAX_READ_BYTES:
        return None
    return text


# ------------------------------------------------------------------ chunk text

def _has_gutter(chunk: Chunk) -> bool:
    """A file chunk or a git hunk: its lines carry a `NNN|` number. Header, stat, log, ticket,
    note and collect chunks are plain text."""
    return chunk.kind == "source" or (chunk.kind == "git" and chunk.start > 0)


def _content(chunk: Chunk) -> list:
    """The chunk's lines without gutters; a marker line is None (it is not a quoted line).
    A diff's own `+`/`-`/` ` stay: `+    check=False` is verbatim from the chunk."""
    gutter = _has_gutter(chunk)
    out: list = []
    for line in chunk.text.split("\n"):
        if _MARKER_LINE.match(line):
            out.append(None)
            continue
        if gutter:
            m = _CHUNK_GUTTER.match(line)
            if m:
                line = line[m.end():]
        out.append(line)
    return out


def _norm(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def _segments(chunk: Chunk) -> list:
    """The chunk's text as normalised runs of consecutive lines: a marker ends a run, because
    the lines on either side of an omission are not next to each other in the file."""
    runs: list = []
    current: list = []
    for line in _content(chunk):
        if line is None:
            if current:
                runs.append(current)
                current = []
        else:
            current.append(line)
    if current:
        runs.append(current)
    return [_norm(" ".join(run)) for run in runs]


def normalise_quote(quote: str) -> str:
    """A quote the way `find_quote` compares it: the `NNN|` gutter stripped from each line
    (a diff's own `+`/`-` is not a gutter), runs of whitespace one space, trimmed. Case stays."""
    lines = []
    for line in _LINES.split(quote):
        m = _QUOTE_GUTTER.match(line)
        lines.append(line[m.end():] if m else line)
    return _norm(" ".join(lines))


def find_all_quote(pack: Pack, quote: str) -> list:
    """The ids of every chunk of *pack* that *quote* is a verbatim quote of, in pack (rank)
    order; [] when none is.

    The rule — the only place it lives; CC-6's `verify_quotes` calls it:

    * both sides are normalised the same way: the gutter (`120|`, or the blank one of a
      removed diff line) is dropped, runs of whitespace become one space, the ends are
      trimmed; the comparison is case-sensitive;
    * the quote must lie inside **one** chunk, and inside one run of its lines: a quote that
      runs across two chunks, or across an omission marker, is not a quote of either;
    * a marker line (`# … 5 lines omitted (10-14)`) is not a quoted line;
    * a quote shorter than `MIN_QUOTE` characters after normalising proves nothing: [].

    One line can sit in two chunks (a function's `src:` chunk and the `git:` hunk that
    changed it): both are listed, so a voter that names the lower-ranked one is still right.
    """
    if not isinstance(quote, str):
        return []
    needle = normalise_quote(quote)
    if len(needle) < MIN_QUOTE:
        return []
    return [chunk.id for chunk in pack.chunks
            if any(needle in run for run in _segments(chunk))]


def find_quote(pack: Pack, quote: str) -> Optional[str]:
    """The id of the first chunk, in pack (rank) order, that *quote* is a verbatim quote of,
    else None: the first element of `find_all_quote`, whose docstring holds the rule."""
    found = find_all_quote(pack, quote)
    return found[0] if found else None


# ------------------------------------------------------------------ render

def _header(sha: str) -> str:
    return f"EVIDENCE for the claim, at commit {sha[:7]}" if sha else "EVIDENCE for the claim"


def _label(chunk: Chunk) -> str:
    return f"[[{chunk.id}]]  ({chunk.kind})"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _trailer(pack: Pack) -> str:
    parts = []
    if pack.omitted:
        parts.append(f"{_plural(pack.omitted, 'chunk')} omitted")
    if pack.cut:
        parts.append(f"{_plural(pack.cut, 'chunk')} cut")
    return f"(evidence trimmed: {', '.join(parts)})" if parts else "(evidence trimmed)"


def render_pack(pack: Pack) -> str:
    """The text the voter reads: a title line with the commit, then per chunk its
    `[[id]]  (kind)` label and its text as it is, then a trailer when anything was trimmed.
    An empty pack is one fixed sentence."""
    if not pack.chunks:
        return "EVIDENCE: none found for this claim."
    lines = [_header(pack.sha)]
    for chunk in pack.chunks:
        lines.append(_label(chunk))
        lines.append(chunk.text)
    if pack.truncated:
        lines.append(_trailer(pack))
    return "\n".join(lines)


# ------------------------------------------------------------------ cutting a chunk

def _cap_lines(chunk: Chunk) -> tuple:
    """(chunk, True when a line was cut): a line over `MAX_LINE` characters (past its gutter)
    keeps its first `MAX_LINE` and a `# … N chars cut` line follows it; the numbering of the
    lines after it is untouched."""
    gutter = _has_gutter(chunk)
    out: list = []
    changed = False
    for line in chunk.text.split("\n"):
        m = _CHUNK_GUTTER.match(line) if gutter else None
        head = m.end() if m else 0
        if len(line) - head > MAX_LINE:
            out.append(line[: head + MAX_LINE])
            out.append(f"# … {len(line) - head - MAX_LINE} chars cut")
            changed = True
        else:
            out.append(line)
    return (replace(chunk, text="\n".join(out)), True) if changed else (chunk, False)


def _tail_markers(chunk: Chunk, lines: list) -> list:
    """`markers[k]`: the marker that follows the first *k* lines of the chunk when the rest
    is cut. A file chunk's says which file lines went (`# … 12 lines omitted (31-42)`, as
    CC-3's do), a hunk's how many diff lines (`# … 9 diff lines cut`, as CC-4's)."""
    total = len(lines)
    out = []
    if chunk.kind == "source":
        last = chunk.start - 1
        for k in range(total):
            a, b = last + 1, chunk.end
            out.append(_marker(a, b) if a <= b else f"# … {_plural(total - k, 'line')} omitted")
            m = _SHOWN.match(lines[k])
            if m:
                last = max(last, int(m.group(1)))
            else:
                o = _OMITTED.match(lines[k])
                if o:
                    last = max(last, int(o.group(3)))
        return out
    word = "diff line" if (chunk.kind == "git" and chunk.start > 0) else "line"
    verb = "cut" if word == "diff line" else "omitted"
    return [f"# … {_plural(total - k, word)} {verb}" for k in range(total)]


def _fit(chunk: Chunk, limit: int) -> tuple:
    """(chunk, True when cut): the chunk within *limit* characters of text. Whole lines from the
    top, then a marker; when not even one line and the marker fit, the first line's head."""
    text = chunk.text
    if len(text) <= limit:
        return chunk, False
    lines = text.split("\n")
    markers = _tail_markers(chunk, lines)
    used = [0]
    for line in lines:
        used.append(used[-1] + len(line) + 1)
    for k in range(len(lines) - 1, 0, -1):
        if used[k] + len(markers[k]) <= limit:
            return replace(chunk, text="\n".join(lines[:k] + [markers[k]])), True
    room = limit - _CUT_MARKER_MAX - 1
    if room > 0:
        head = lines[0][:room]
        return replace(chunk, text=f"{head}\n# … {len(text) - len(head)} chars cut"), True
    return replace(chunk, text=f"# … {len(text)} chars cut"), True


# ------------------------------------------------------------------ merging source chunks

def _merge_group(group: list, view) -> Optional[Chunk]:
    """One `src:` chunk for chunks of one file whose spans overlap or touch: the union of the
    lines they show, text rebuilt from the file's own lines with the same gutter, a marker for
    every stretch of the span none of them shows. None when the file cannot be read back."""
    text = _read(view, group[0].path) if view is not None else None
    if text is None:
        return None
    lines = [_clean(line) for line in _split(text)]
    start, end = min(c.start for c in group), max(c.end for c in group)
    if start < 1 or end > len(lines):
        return None
    shown: set = set()
    index: list = []
    for c in group:
        rows = []
        for line in c.text.split("\n"):
            m = _SHOWN.match(line)
            if m:
                shown.add(int(m.group(1)))
            elif not _OMITTED.match(line) and line:
                rows.append(line)        # the definition list a path chunk carries
        index = index or rows
    shown = {n for n in shown if start <= n <= end}
    if not shown:
        return None
    runs: list = []
    for n in sorted(shown):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    width = len(str(end))
    out: list = []
    at = start
    for k, (a, b) in enumerate(runs):
        if a > at:
            out.append(_marker(at, a - 1))
        out.extend(f" {n:>{width}}| {lines[n - 1]}" for n in range(a, b + 1))
        if k == 0 and index:
            out.extend(index)
        at = b + 1
    if at <= end:
        out.append(_marker(at, end))
    whys: list = []
    for c in group:
        for why in (c.why or "").split(", "):
            if why and why not in whys:
                whys.append(why)
    path = group[0].path
    return Chunk(id=f"src:{path}:{start}-{end}", kind="source", path=path, start=start, end=end,
                 text="\n".join(out), why=", ".join(whys))


def _merge_sources(chunks: list, view) -> list:
    """Chunks of one file (`src:` only: two hunks of a file are two diffs, and a git chunk never
    merges with a source chunk) whose line ranges overlap or touch become one. The earliest
    keeps its place. A group whose file cannot be read back stays as it was."""
    spans: dict = {}
    for i, c in enumerate(chunks):
        if c.kind == "source" and c.id.startswith("src:") and c.start > 0 and c.end >= c.start:
            spans.setdefault(c.path, []).append(i)
    replaced: dict = {}   # index of the first member -> merged chunk
    dropped: set = set()
    for path in sorted(spans):
        order = sorted(spans[path], key=lambda i: (chunks[i].start, chunks[i].end, i))
        groups: list = []
        for i in order:
            if groups and chunks[i].start <= groups[-1][1] + 1:
                groups[-1][0].append(i)
                groups[-1][1] = max(groups[-1][1], chunks[i].end)
            else:
                groups.append([[i], chunks[i].end])
        for members, _ in groups:
            if len(members) < 2:
                continue
            merged = _merge_group([chunks[i] for i in members], view)
            if merged is None:
                continue
            first = min(members)
            replaced[first] = merged
            dropped.update(i for i in members if i != first)
    return [replaced.get(i, c) for i, c in enumerate(chunks) if i not in dropped]


# ------------------------------------------------------------------ notes for anchors that did not resolve

class _Reader:
    """Files of the view, each read at most once per pack, never raising."""

    def __init__(self, view) -> None:
        self.view = view
        self._text: dict = {}
        self._files: Optional[list] = None

    def text(self, path: str) -> Optional[str]:
        if path not in self._text:
            self._text[path] = _read(self.view, path) if self.view is not None else None
        return self._text[path]

    def entries(self, directory: str) -> list:
        """What is directly under *directory* (files, and `sub/` for a directory), sorted."""
        if self._files is None:
            lister = getattr(self.view, "files", None)
            ok, got = _guarded(lister) if callable(lister) else (False, None)
            self._files = [p for p in got if isinstance(p, str)] if ok and isinstance(got, list) else []
        prefix = directory.rstrip("/") + "/"
        names = set()
        for p in self._files:
            if p.startswith(prefix):
                rest = p[len(prefix):]
                names.add(rest.split("/", 1)[0] + ("/" if "/" in rest else ""))
        return sorted(names)

    def exists(self, path: str) -> bool:
        check = getattr(self.view, "exists", None)
        if not callable(check):
            return False
        ok, got = _guarded(check, path)
        return bool(ok and got)


def _listed(names: list, more: int = 0) -> str:
    shown = ", ".join(f"`{n}`" for n in names[:MAX_NAMES])
    extra = max(0, len(names) - MAX_NAMES) + more
    return f"{shown}, … {extra} more" if extra else shown


def _index_names(chunks: list, path: str) -> Optional[tuple]:
    """(names, more) from the definition list a source chunk of *path* already carries (CC-3's
    `# definitions (name  line):` rows), else None: the file is then read, not these chunks."""
    for c in chunks:
        if c.kind != "source" or c.path != path or _INDEX_HEAD not in c.text:
            continue
        names: list = []
        more = 0
        for line in c.text.split("\n"):
            row = _INDEX_ROW.match(line)
            if row:
                names.append(row.group(1))
            else:
                tail = _INDEX_MORE.match(line)
                if tail:
                    more = int(tail.group(1))
        if names:
            return names, more
    return None


def _definitions(text: str) -> Optional[tuple]:
    """(top-level names in file order, the parsed tree) of a Python module, None when it does not parse."""
    try:
        tree = ast.parse(text)
    except Exception:   # noqa: BLE001 — SyntaxError, ValueError, RecursionError, MemoryError
        return None
    names = [s.name for s in tree.body
             if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    return names, tree


def _owner_members(tree, anchor_text: str) -> Optional[tuple]:
    """For a dotted anchor `Owner.name` whose `Owner` is a class of the file: (Owner, its
    methods) — where a renamed method is to be seen."""
    qualname = anchor_text.split("::", 1)[1].replace("::", ".") if "::" in anchor_text else anchor_text
    parts = qualname.split(".")
    if len(parts) < 2:
        return None
    owner = parts[-2]
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == owner:
            members = [m.name for m in node.body
                       if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
            return (owner, members) if members else None
    return None


def _note(note_id: str, text: str, why: str) -> Chunk:
    return Chunk(id=note_id, kind="note", path="", start=0, end=0, text=text, why=why)


def _dangling_text(ra, sha: str, chunks: list, reader: _Reader) -> Optional[str]:
    """What a voter is told about an anchor that is not there: what the repository has instead."""
    at = f" at {sha[:7]}" if sha else " in the repository"
    head = f"`{ra.anchor.text}` does not exist{at}"
    path = ra.path
    if path == ".":
        return f"{head}; no file of that name is in the repository."
    names: Optional[list] = None
    more = 0
    tree = None
    carried = _index_names(chunks, path)      # CC-3 already listed this file's definitions
    if carried:
        is_file = True
        names, more = carried
    else:
        text = reader.text(path)
        is_file = text is not None
        if is_file and path.endswith(".py"):
            parsed = _definitions(text)
            if parsed:
                names, tree = parsed
    if is_file:
        out = f"{head}; the file `{path}` exists"
        if names:
            out += f" and defines: {_listed(names, more)}"
            owner = _owner_members(tree, ra.anchor.text) if tree is not None and ra.anchor.kind != "path" else None
            if owner:
                out += f"; `{owner[0]}` has: {_listed(owner[1])}"
        return out + "."
    listing = reader.entries(path)
    if listing:
        return f"{head}; the directory `{path}` exists and holds: {_listed(listing)}."
    if reader.exists(path):
        return f"{head}; `{path}` exists."
    return head + "."


def _dangling_notes(resolved: list, sha: str, chunks: list, view) -> list:
    """A note chunk for every anchor of ours (path, symbol, test) that did not resolve though it
    points into the repository: `note:dangling:<text>` names what is there instead, so a voter
    can see a rename. A file name that fits several files is `note:ambiguous:<text>`."""
    reader = _Reader(view)
    out: list = []
    seen: set = set()
    for ra in resolved:
        kind = ra.anchor.kind
        if ra.found or kind not in ("path", "symbol", "test"):
            continue
        if kind == "path" and ra.candidates:
            cid = f"note:ambiguous:{ra.anchor.text}"
            names = ", ".join(f"`{p}`" for p in ra.candidates if isinstance(p, str))
            text = (f"`{ra.anchor.text}` is ambiguous: it fits several files ({names}); "
                    f"the claim does not say which.")
        elif ra.path or kind == "path":
            # a file written out in the claim that is not there, nor is its directory: the claim
            # is about code the repository does not have, and a voter must be told so
            cid = f"note:dangling:{ra.anchor.text}"
            text = _dangling_text(ra if ra.path else replace(ra, path="."), sha, chunks, reader)
        else:
            continue
        if cid not in seen and text:
            seen.add(cid)
            out.append(_note(cid, text, ra.anchor.text))
    return out


# ------------------------------------------------------------------ rank

def _named_paths(resolved: list) -> set:
    """The files the claim names: path anchors (found, or as written) and the files of its
    symbols and tests — the same notion CC-4 uses to pick a commit's hunks."""
    out: set = set()
    for r in resolved:
        kind = r.anchor.kind
        if kind == "path":
            for p in (r.path if r.found else "", re.sub(r":[\d\-]+$", "", r.anchor.text)):
                if p:
                    out.add(p)
        elif kind in ("symbol", "test") and r.path and "." in r.path.rsplit("/", 1)[-1]:
            out.add(r.path)
    return out


def _base_score(chunk: Chunk, symbols: set, named: set) -> int:
    kind = chunk.kind
    if kind == "note":
        return SCORE_NOTE
    if kind == "source":
        asked_by = {w for w in (chunk.why or "").split(", ") if w}
        return SCORE_SYMBOL_SOURCE if asked_by & symbols else SCORE_PATH_SOURCE
    if kind == "git":
        if chunk.start > 0:
            return SCORE_ANCHORED_HUNK if chunk.path in named else SCORE_OTHER_HUNK
        return SCORE_GIT_OTHER
    if kind == "ticket":
        return SCORE_TICKET
    if kind == "collect":
        return SCORE_COLLECT
    return SCORE_GIT_OTHER


def _score(chunk: Chunk, tokens: list, symbols: set, named: set) -> int:
    text = "\n".join(line for line in _content(chunk) if line is not None)
    hits = sum(1 for t in tokens if t in text)
    return _base_score(chunk, symbols, named) + min(BONUS_CAP, BONUS_PER_KEYWORD * hits)


def _rank(chunks: list, claim: str, resolved: list) -> list:
    """Highest score first; equal scores by id. The score is the kind's base plus 10 per distinct
    keyword of the claim in the chunk's text, at most 40."""
    tokens = keyword_tokens(claim)
    symbols = {r.anchor.text for r in resolved
               if r.found and r.anchor.kind in ("symbol", "test") and r.qualname}
    named = _named_paths(resolved)
    scored = [(-_score(c, tokens, symbols, named), c.id, c) for c in chunks]
    scored.sort(key=lambda t: (t[0], t[1]))
    return [c for _, _, c in scored]


# ------------------------------------------------------------------ trim

def _trim(ranked: list, budget: PackBudget, sha: str) -> tuple:
    """(kept, omitted, cut). Rank order; each chunk first goes within `per_chunk` and `MAX_LINE`;
    taken while it fits both the chunk count and the characters left (label, text and newlines
    counted; the title and the trailer reserved), stopping at the first that does not. The top
    chunk is never dropped: it is cut to what the budget holds (at least `_MIN_TOP`)."""
    max_chunks = max(1, budget.chunks)
    per = max(1, budget.per_chunk)
    room = max(0, budget.chars - len(_header(sha)) - _TRAILER_RESERVE)
    kept: list = []
    used = 0
    cut = 0
    omitted = 0
    for i, chunk in enumerate(ranked):
        if len(kept) >= max_chunks:
            omitted = len(ranked) - i
            break
        chunk, a = _cap_lines(chunk)
        chunk, b = _fit(chunk, per)
        was_cut = a or b
        cost = len(_label(chunk)) + 2 + len(chunk.text)
        if used + cost > room:
            if kept:
                omitted = len(ranked) - i
                break
            chunk, c = _fit(chunk, max(room - len(_label(chunk)) - 2, _MIN_TOP))
            was_cut = was_cut or c
            cost = len(_label(chunk)) + 2 + len(chunk.text)
        kept.append(chunk)
        used += cost
        cut += 1 if was_cut else 0
    return kept, omitted, cut


# ------------------------------------------------------------------ the entry points

def _dedupe(chunks: list) -> list:
    seen: set = set()
    out = []
    for c in chunks:
        if c.id not in seen:
            seen.add(c.id)
            out.append(c)
    return out


def assemble_pack(claim: str, resolved, chunks, view=None, *, sha: str = "",
                  budget: PackBudget = PackBudget()) -> Pack:
    """Merge, rank and trim *chunks* the caller already holds into a `Pack`: what `build_pack`
    does after it has asked the providers. *view* is only for reading a file back to merge
    two chunks of it; without one (or when it cannot) the chunks stay as they are."""
    claim = claim if isinstance(claim, str) else ""
    resolved = [r for r in (resolved or []) if r is not None]
    merged = _merge_sources(_dedupe([c for c in (chunks or []) if isinstance(c, Chunk)]), view)
    ranked = _rank(_dedupe(merged), claim, resolved)
    kept, omitted, cut = _trim(ranked, budget, sha)
    return Pack(claim, sha, tuple(kept), bool(omitted or cut), omitted, cut)


def _ask(name: str, make, out: list) -> None:
    """One provider's chunks onto *out*; a provider that raises (they promise not to) is a note."""
    try:
        got = make()
    except Exception as err:   # noqa: BLE001 — build_pack never raises
        out.append(_note(f"note:pack:{name}", f"{name} evidence could not be read "
                         f"({type(err).__name__}).", name))
        return
    out.extend(c for c in (got or []) if isinstance(c, Chunk))


def build_pack(claim: str, resolved, view, *, model=None, base: Optional[str] = None,
               head: Optional[str] = None, budget: PackBudget = PackBudget()) -> Pack:
    """The evidence pack of one claim: CC-3's source chunks and CC-4's git chunks for the
    found anchors, a note for each anchor that did not resolve, merged, ranked and trimmed to
    *budget*; the chunks are in rank order (see the module docstring).

    No anchors (a `world` claim; the caller should not ask) gives the empty pack. Never
    raises, whatever a provider or the view does: a failure is a note chunk, and the pack of a
    build that itself fails is one note, `truncated=False`."""
    claim = claim if isinstance(claim, str) else ""
    sha = ""
    try:
        resolved = [r for r in (resolved or []) if r is not None]
        sha = _pack_sha(view, head)
        if not resolved:
            return Pack(claim, sha, (), False)
        per = max(1, budget.per_chunk)
        got: list = []
        _ask("source", lambda: source_chunks(resolved, view, model, claim=claim, max_chunk_chars=per), got)
        _ask("git", lambda: git_chunks(resolved, view, base=base, head=head, claim=claim,
                                       max_chunk_chars=per), got)
        got = _dedupe(got)
        notes = _dangling_notes(resolved, sha, got, view)
        return assemble_pack(claim, resolved, got + notes, view, sha=sha, budget=budget)
    except Exception as err:   # noqa: BLE001 — the contract: a pack, not a traceback
        note = _note("note:pack:build", f"the evidence could not be assembled ({type(err).__name__}).", "pack")
        return Pack(claim, sha, (note,), False)
