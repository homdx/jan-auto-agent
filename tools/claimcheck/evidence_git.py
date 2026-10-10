"""CC-4 — git evidence: a commit, a diff range, a ticket's text, a file's history.

docs/claim-check/tickets/270-cc-4-git-evidence.md. Half of a review's code claims are
about history ("commit a73e389 fixed only the ticket text", "the fix landed but
`_declared_paths` still reads one line"); their evidence is a commit's diff, the diff
between two refs, or the ticket the claim cites. `git_chunks` turns the resolved anchors
of one claim into those chunks:

* a found **commit**: a header chunk (`git:<sha7>:header` — full sha, author, date,
  subject, body cut at `MAX_BODY` chars, `--stat`), then the commit's hunks that touch
  a path or symbol the claim names, else those of the `LARGEST_FILES` files with the
  largest change (`git:<sha7>:<path>:<newstart>-<newend>`). A merge is diffed against
  its first parent, the root commit against the empty tree;
* **base and head** both given: the hunks of `git diff base head -- <path>` for each
  path the claim anchors (`diff:<b7>..<h7>:<path>:<a>-<b>`), a note for an anchored path
  the range leaves alone; for a claim naming no path the `--stat` and the hunks holding
  a claim keyword;
* a found **ticket** (or a found path `epic-tasks/NN-*.md`): its title line, its
  `**Status:**` line and its text up to the end of the first `##` section, within the
  budget (`ticket:<id>`, kind `ticket`);
* a found **path** when the claim talks about history (`fixed`, `changed`, `introduced`,
  `added`, `removed`, `regress…`): `git log -n 5` of the path (`gitlog:<path>`).

A hunk keeps the diff's own lines — `-old`, `+new`, ` context` — behind a gutter with the
new side's line number (blank for a removed line), so `+    check=False` is a verbatim
quote of the chunk and `18|` says where it is in the head file.

Every read goes through `view.git(...)` with `show`, `diff` and `log` only (the views
add `--no-textconv --no-ext-diff` and ignore the operator's global config); each call
has `GIT_TIMEOUT` seconds. Nothing raises on a bad rev, a missing path, a binary file, a
hung or failing git: the answer is a `note` chunk (`note:git:<what>`) that says so —
an absence is evidence for CC-5. Same repository, same inputs: byte-identical chunks.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Optional

from tools.claimcheck.evidence_source import keyword_tokens
from tools.claimcheck.model import Chunk

GIT_TIMEOUT = 20.0               # seconds one view.git call may take; then a note
MAX_BODY = 1200                  # chars of a commit message body in the header chunk
LARGEST_FILES = 3                # files shown for a commit claim that names none of its files
MAX_HUNKS = 8                    # hunks per commit, per anchored path of a range, per keyword search
MAX_DIFF_BYTES = 200 * 1024      # a bigger patch keeps only the hunks with a claim keyword
MAX_FILE_LINES = 20000           # a file whose diff changes more lines is not fetched at all
MAX_FETCH_LINES = 40000          # changed lines fetched for one call; the files past it are a note
HISTORY_LOG = 5                  # commits listed for a path the claim talks history about
RADIUS = 3                       # lines kept around a keyword line of a hunk over the budget
_HEAD_SHARE = 0.4                # of a cut hunk's budget, at most, for its head
_MARK = 40                       # chars a cut marker takes, budgeted up front
LONG_LINE = 400                  # chars kept of one diff line that alone is longer than a chunk

#: the diff's text must not depend on the repository's config: rename detection on,
#: the standard prefixes, no colour; a patch also three lines of context and one algorithm
#: (`--unified` turns the patch on, so `--numstat` / `--stat` calls leave those out).
_DIFF_OPTS = ("--no-color", "-M", "--src-prefix=a/", "--dst-prefix=b/", "--no-relative")
_PATCH_OPTS = ("--unified=3", "--diff-algorithm=myers", "--indent-heuristic")
_HISTORY = re.compile(r"\b(?:fix(?:es|ed|ing)?|chang(?:e|es|ed|ing)|introduc(?:e|es|ed|ing)"
                      r"|add(?:s|ed|ing)?|remov(?:e|es|ed|ing)|regress\w*)\b", re.I)
_TICKET_PATH = re.compile(r"^epic-tasks/\d+-[^/]*\.md$")
_HUNK_AT = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NL = re.compile(r"\r\n|\r|\n")
_STATUS = re.compile(r"^\s*\*\*Status:\*\*")
_C_ESC = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


class _GitFailed(Exception):
    """A view.git call that did not answer: its message is the note's text."""


def _git(view, *args: str) -> str:
    """`view.git(*args)` with `GIT_TIMEOUT` seconds; `_GitFailed` for an error or a timeout.

    The call runs in a daemon thread, so a view whose git hangs (or a spy that sleeps)
    costs this claim a note, not the run; the views kill their own git at the same bound."""
    box: dict = {}

    def call() -> None:
        try:
            box["out"] = view.git(*args)
        except Exception as err:   # any view, any failure: a note, never a traceback
            box["err"] = err

    worker = threading.Thread(target=call, name="claimcheck-git", daemon=True)
    worker.start()
    worker.join(GIT_TIMEOUT)
    if worker.is_alive():
        raise _GitFailed(f"git {args[0]} timed out after {GIT_TIMEOUT:g} s")
    if "err" in box:
        first = (str(box["err"]).strip().splitlines() or [type(box["err"]).__name__])[0]
        raise _GitFailed(first[:300])
    out = box.get("out")
    return out if isinstance(out, str) else ""


def _note(what: str, text: str, why: str) -> Chunk:
    return Chunk(id=f"note:git:{what}", kind="note", path="", start=0, end=0, text=text, why=why)


def _unquote(path: str) -> str:
    """A path as git prints it (`"a b/\\303\\251.py"` when it has a byte outside ASCII) → the path."""
    path = path.rstrip("\t")
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        return path
    raw, body, i = bytearray(), path[1:-1], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567" and re.match(r"[0-7]{3}", body[i + 1:i + 4]):
                raw.append(int(body[i + 1:i + 4], 8) & 0xFF)
                i += 4
                continue
            if nxt in _C_ESC:
                raw.append(_C_ESC[nxt])
                i += 2
                continue
        raw.extend(ch.encode("utf-8", "surrogateescape"))
        i += 1
    return raw.decode("utf-8", "surrogateescape")


def _side(token: str, prefix: str) -> str:
    token = _unquote(token)
    return token[len(prefix):] if token.startswith(prefix) else token


# ------------------------------------------------------------------ hunks

@dataclass
class Hunk:
    """One `@@` hunk of a unified diff (or a file's header alone: a rename, a binary file)."""

    path: str                     # the new side's path ("" never; the old one for a deletion)
    old_path: str
    header: list                  # the file's header lines, `diff --git` to `+++`
    at: str = ""                  # the `@@ … @@` line; "" for a header-only entry
    lines: list = field(default_factory=list)
    old_start: int = 0
    old_count: int = 0
    new_start: int = 0
    new_count: int = 0
    binary: bool = False
    keys: list = field(default_factory=list)
    hits: int = 0
    order: int = 0

    @property
    def new_end(self) -> int:
        return self.new_start + max(self.new_count, 1) - 1 if self.at else 0

    @property
    def span(self) -> tuple:
        return (self.new_start, self.new_end) if self.at else (0, 0)

    def text(self, max_chars: int = 2400) -> str:
        """The file header, the `@@` line and the hunk's lines behind a new-side gutter.

        Longer than *max_chars*: the hunk's head, then `RADIUS` lines around each line
        holding a claim keyword, the gaps marked — a cut at the head alone would drop
        the very line the claim is about."""
        width = len(str(max(self.new_end, 1)))
        out = [_long(h, max_chars) for h in list(self.header) + ([self.at] if self.at else [])]
        rows, new = [], self.new_start
        for line in self.lines:
            if line.startswith(("+", " ")):
                rows.append(_long(f" {new:>{width}}| {line}", max_chars))
                new += 1
            else:                     # "-" and "\ No newline at end of file"
                rows.append(_long(f" {'':>{width}}| {line}", max_chars))
        cost = [len(r) + 1 for r in rows]
        budget = max_chars - sum(len(h) + 1 for h in out)
        if sum(cost) <= budget:
            return "\n".join(out + rows)
        budget -= _MARK               # the marker after the last shown line
        shown, spent = set(), 0
        for i, c in enumerate(cost):  # the head: what the hunk starts with
            if spent + c > budget * _HEAD_SHARE and i:
                break
            shown.add(i)
            spent += c
        for i, line in enumerate(self.lines):
            if not any(k in line for k in self.keys):
                continue
            win = [j for j in range(max(0, i - RADIUS), min(len(rows), i + RADIUS + 1)) if j not in shown]
            need = sum(cost[j] for j in win) + _MARK
            if win and spent + need <= budget:
                shown.update(win)
                spent += need
        at = 0
        for i in sorted(shown):
            if i > at:
                out.append(_cut(i - at))
            out.append(rows[i])
            at = i + 1
        if at < len(rows):
            out.append(_cut(len(rows) - at))
        return "\n".join(out)


def _cut(n: int) -> str:
    return f"# … {n} diff line{'s' if n != 1 else ''} cut"


def _long(line: str, max_chars: int) -> str:
    """A line that alone would not fit: its head and a marker (a minified file, a data blob)."""
    keep = min(LONG_LINE, max(40, max_chars // 2))
    if len(line) <= keep:
        return line
    return line[:keep] + f" … [{len(line) - keep} chars cut]"


def _header_paths(header: list) -> tuple:
    """(old, new) of one file of a patch, from its rename / `---` / `+++` / `diff --git` lines."""
    old = new = ""
    for line in header:
        if line.startswith("rename from "):
            old = _unquote(line[len("rename from "):])
        elif line.startswith("rename to "):
            new = _unquote(line[len("rename to "):])
        elif line.startswith("--- ") and not old:
            old = "" if line[4:].rstrip("\t") == "/dev/null" else _side(line[4:], "a/")
        elif line.startswith("+++ ") and not new:
            new = "" if line[4:].rstrip("\t") == "/dev/null" else _side(line[4:], "b/")
    if (not old and not new) and header and header[0].startswith("diff --git "):
        rest = header[0][len("diff --git "):]
        quoted = re.findall(r'"(?:[^"\\]|\\.)*"', rest)
        if len(quoted) == 2:
            old, new = _side(quoted[0], "a/"), _side(quoted[1], "b/")
        elif len(rest) >= 5 and (len(rest) - 5) % 2 == 0:   # "a/P b/P": a binary file, a mode change
            half = (len(rest) - 5) // 2
            old = new = rest[2:2 + half]
    return old or new, new or old


def hunks_of(patch_text: str, *, keywords=(), paths=None) -> list:
    """The hunks of a unified diff, ranked by claim keyword hits (ties: patch order).

    Each hunk keeps its file's header. A file with no `@@` (a rename alone, a binary
    file, a mode change) is one header-only entry. *paths*: only the files whose old
    or new path is in it."""
    want = set(paths) if paths is not None else None
    keys = [k for k in dict.fromkeys(keywords) if k]
    out: list = []
    header: list = []
    current: Optional[Hunk] = None
    file_hunks: list = []

    def close_file() -> None:
        nonlocal header, file_hunks, current
        while len(header) > 1 and header[-1] == "":
            header.pop()              # the patch's final newline, after a header-only file
        if header:
            old, new = _header_paths(header)
            binary = any(h.startswith(("Binary files ", "GIT binary patch")) for h in header)
            if not file_hunks:
                file_hunks = [Hunk(path=new, old_path=old, header=header, binary=binary)]
            for h in file_hunks:
                h.path, h.old_path, h.binary = new, old, binary
                if want is None or h.path in want or h.old_path in want:
                    out.append(h)
        header, file_hunks, current = [], [], None

    for line in (patch_text or "").split("\n"):
        line = line[:-1] if line.endswith("\r") else line
        if line.startswith("diff --git "):
            close_file()
            header = [line]
            continue
        if not header:
            continue
        m = _HUNK_AT.match(line)
        if m:
            current = Hunk(path="", old_path="", header=header, at=line,
                           old_start=int(m.group(1)), old_count=int(m.group(2) or 1),
                           new_start=int(m.group(3)), new_count=int(m.group(4) or 1))
            file_hunks.append(current)
        elif current is not None:
            current.lines.append(line)
        else:
            header.append(line)
    close_file()
    if out and out[-1].lines and out[-1].lines[-1] == "":
        out[-1].lines.pop()           # the patch's final newline is not a context line
    for i, h in enumerate(out):
        body = "\n".join([h.at, *h.lines]) if h.at else "\n".join(h.header)
        h.order, h.keys = i, keys
        h.hits = sum(1 for k in keys if k in body)
    return sorted(out, key=lambda h: (-h.hits, h.order))


# ------------------------------------------------------------------ helpers

def _numstat(view, revs: list, paths=()) -> list:
    """[(added, deleted, old, new)] of a diff; added/deleted are None for a binary file."""
    out = _git(view, *revs[:1], "--numstat", "-z", *_DIFF_OPTS, *revs[1:], "--", *paths)
    parts, rows, i = out.split("\0"), [], 0
    while i < len(parts):
        rec = parts[i]
        i += 1
        if not rec.strip("\n"):
            continue
        fields = rec.lstrip("\n").split("\t", 2)
        if len(fields) != 3:
            continue
        a, d, p = fields
        if not (a == d == "-" or (a.isdigit() and d.isdigit())):
            continue
        if p == "" and i + 1 < len(parts):
            old, new = parts[i], parts[i + 1]
            i += 2
        else:
            old = new = p
        num = (None, None) if a == "-" else (int(a), int(d))
        rows.append((*num, old, new))
    return rows


def _diff_cmd(view, sha: str) -> list:
    """The command prefix and revs that diff commit *sha* against its first parent
    (the root commit: against the empty tree, which `show --root` does)."""
    parents = _git(view, "log", "-1", "--no-show-signature", "--format=%P", sha).split()
    if parents:
        return ["diff", parents[0], sha]
    return ["show", "--root", "--format=", "--no-show-signature", sha]


def _anchored_paths(resolved) -> list:
    """The files the claim names: path anchors (found, or as written) and the files of
    its symbols and tests, in claim order, each once."""
    out: list = []
    for r in resolved:
        kind = r.anchor.kind
        if kind == "path":
            for p in (r.path if r.found else "", re.sub(r":[\d\-]+$", "", r.anchor.text)):
                if p and p not in out:
                    out.append(p)
        elif kind in ("symbol", "test") and r.path and r.path not in out and "." in r.path.rsplit("/", 1)[-1]:
            out.append(r.path)
    return out


def _keywords(resolved, claim: str) -> list:
    keys = keyword_tokens(claim)
    for r in resolved:
        if r.anchor.kind in ("symbol", "test"):
            last = (r.qualname or r.anchor.text).rsplit(".", 1)[-1]
            if last and last not in keys:
                keys.append(last)
    return keys


def _render(hunks: list, prefix: str, max_chars: int, why: str) -> list:
    out = []
    for h in hunks:
        a, b = h.span
        out.append(Chunk(id=f"{prefix}:{h.path}:{a}-{b}", kind="git", path=h.path, start=a, end=b,
                         text=h.text(max_chars), why=why))
    return out


def _fetch_hunks(view, cmd: list, rows: list, files: list, keys: list, prefix: str,
                 max_chars: int, why: str, keyword_only: bool = False) -> list:
    """The hunks of *files* (rows from `_numstat`): binary and over-`MAX_FILE_LINES` files
    are notes; a patch over `MAX_DIFF_BYTES` keeps only its keyword hunks."""
    chunks, fetch, total, left_out = [], [], 0, []
    for a, d, old, new in rows:
        if new not in files and old not in files:
            continue
        if a is None:   # a keyword search over every file says nothing of the binary ones
            if not keyword_only:
                chunks.append(_note(f"binary:{new}", f"binary file: {new}", why))
        elif a + d > MAX_FILE_LINES:
            if not keyword_only:
                chunks.append(_note(f"huge:{new}", f"diff too large: {new} (+{a} -{d} lines); see --stat", why))
        elif total + a + d > MAX_FETCH_LINES:
            left_out.append(new)
        else:
            total += a + d
            fetch.append(new)
            if old != new:
                fetch.append(old)
    if left_out:
        chunks.append(_note(f"unread:{prefix}", f"{len(left_out)} more changed file(s) not read "
                            f"(over {MAX_FETCH_LINES} changed lines): {', '.join(left_out[:5])}", why))
    if not fetch:
        return chunks
    patch = _git(view, cmd[0], *_DIFF_OPTS, *_PATCH_OPTS, *cmd[1:], "--", *dict.fromkeys(fetch))
    hunks = [h for h in hunks_of(patch, keywords=keys, paths=fetch) if not h.binary]
    if keyword_only or len(patch.encode("utf-8", "surrogateescape")) > MAX_DIFF_BYTES:
        hunks = [h for h in hunks if h.hits]
    return chunks + _render(hunks[:MAX_HUNKS], prefix, max_chars, why)


def _stat(view, cmd: list) -> str:
    return _git(view, cmd[0], "--stat=1000,1000", "--summary", *_DIFF_OPTS, *cmd[1:]).strip("\n")


# ------------------------------------------------------------------ commit

def _commit_chunks(r, view, resolved, keys: list, max_chars: int) -> list:
    sha, why = r.sha, f"commit {r.anchor.text}"
    sha7 = sha[:7]
    head = _git(view, "show", "-s", "--no-color", "--no-show-signature",
                "--format=%H%x00%an <%ae>%x00%aI%x00%s%x00%b", sha)
    full, author, date, subject, body = (head.split("\0") + [""] * 5)[:5]
    body = body.strip("\n")
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY].rstrip() + f"\n[… {len(body) - MAX_BODY} chars of the message cut]"
    cmd = _diff_cmd(view, sha)
    stat = _stat(view, cmd)
    text = f"commit {full.strip()}\nAuthor: {author}\nDate:   {date}\n\n    {subject}\n"
    if body:
        text += "\n" + "\n".join(("    " + b) if b else "" for b in body.split("\n")) + "\n"
    text += "\n" + (stat if stat else "(no changes)")
    out = [Chunk(id=f"git:{sha7}:header", kind="git", path="", start=0, end=0, text=text, why=why)]
    if not stat:
        return out
    rows = _numstat(view, cmd)
    named = set(_anchored_paths(resolved))
    files = [new for a, d, old, new in rows if new in named or old in named]
    if not files:   # the claim names none of the commit's files: the largest changes
        ranked = sorted(rows, key=lambda row: (-((row[0] or 0) + (row[1] or 0)), row[3]))
        files = [row[3] for row in ranked[:LARGEST_FILES]]
    return out + _fetch_hunks(view, cmd, rows, files, keys, f"git:{sha7}", max_chars, why)


# ------------------------------------------------------------------ range

def _range_chunks(view, base: str, head: str, resolved, keys: list, max_chars: int) -> list:
    def sha_of(rev: str) -> str:
        try:
            return view.rev_parse(rev) or ""
        except Exception:
            return ""

    b_sha, h_sha = sha_of(base), sha_of(head)
    missing = [(rev, s) for rev, s in ((base, b_sha), (head, h_sha)) if not s]
    if missing:
        return [_note(rev, f"commit {rev} is not in this repository", f"range {base}..{head}")
                for rev, _ in missing]
    prefix, why = f"diff:{b_sha[:7]}..{h_sha[:7]}", f"range {base}..{head}"
    cmd = ["diff", b_sha, h_sha]
    paths = _anchored_paths(resolved)
    rows = _numstat(view, cmd)
    if not paths:
        stat = _stat(view, cmd)
        out = [Chunk(id=f"{prefix}:stat", kind="git", path="", start=0, end=0,
                     text=f"git diff --stat {b_sha[:7]} {h_sha[:7]}\n" + (stat or "(no changes)"), why=why)]
        return out + _fetch_hunks(view, cmd, rows, [row[3] for row in rows], keys, prefix, max_chars, why,
                                  keyword_only=True)
    out = []
    for p in paths:
        if not any(p in (old, new) for _, _, old, new in rows):
            out.append(_note(f"unchanged:{p}", f"{p} is unchanged between {b_sha[:7]} and {h_sha[:7]}", why))
            continue
        out += _fetch_hunks(view, cmd, rows, [p], keys, prefix, max_chars, why)
    return out


# ------------------------------------------------------------------ ticket, history

def _ticket_id(r) -> str:
    if r.anchor.kind == "ticket":
        return r.anchor.text.split()[-1]
    return r.path


def ticket_chunk(resolved, view, *, max_chunk_chars: int = 2400) -> Optional[Chunk]:
    """The ticket a found `ticket` anchor (or a found path `epic-tasks/NN-*.md`) names:
    its title line, its `**Status:**` line and its text to the end of the first `##`
    section, cut at the budget (title and status are always kept); None when there is
    no such file."""
    r = resolved
    path = r.path if r.found else ""
    if r.anchor.kind == "ticket" and not path:
        try:
            path = view.ticket_file(_ticket_id(r)) or ""
        except Exception:
            path = ""
    if not path:
        return None
    try:
        text = view.read(path)
    except Exception:
        return None
    lines = _NL.split(text)
    if lines and lines[-1] == "":
        lines.pop()
    title = next((i for i, l in enumerate(lines) if l.startswith("# ")), None)
    status = next((i for i, l in enumerate(lines) if _STATUS.match(l)), None)
    sections = [i for i, l in enumerate(lines) if l.startswith("## ")]
    stop = sections[1] if len(sections) > 1 else len(lines)
    keep = sorted({i for i in (title, status) if i is not None})
    width = len(str(max(stop, 1)))
    row = lambda i: _long(f" {i + 1:>{width}}| {lines[i]}", max_chunk_chars)
    budget = max_chunk_chars - sum(len(row(i)) + 1 for i in keep) - 60
    shown = set(keep)
    for i in range(stop):
        if i in shown:
            continue
        cost = len(row(i)) + 1
        if cost > budget:
            break
        shown.add(i)
        budget -= cost
    out, at = [], 0
    for i in sorted(shown):
        if i > at:
            n = i - at
            out.append(f"# … {n} line{'s' if n != 1 else ''} omitted ({at + 1}-{i})")
        out.append(row(i))
        at = i + 1
    if at < len(lines):
        out.append(f"# … the rest of the ticket omitted ({at + 1}-{len(lines)})")
    last = max(shown) + 1 if shown else 0
    return Chunk(id=f"ticket:{_ticket_id(r)}", kind="ticket", path=path, start=1 if shown else 0,
                 end=last, text="\n".join(out), why=f"ticket {r.anchor.text}")


def _history_chunk(view, path: str, why: str) -> Optional[Chunk]:
    log = _git(view, "log", f"-n{HISTORY_LOG}", "--no-color", "--no-show-signature",
               "--date=short", "--format=%h %ad %s", "--", path).strip("\n")
    if not log:
        return None
    return Chunk(id=f"gitlog:{path}", kind="git", path=path, start=0, end=0,
                 text=f"git log -n {HISTORY_LOG} -- {path}\n{log}", why=why)


# ------------------------------------------------------------------ entry point

def git_chunks(resolved, view, *, base: Optional[str] = None, head: Optional[str] = None,
               claim: str = "", max_chunk_chars: int = 2400) -> list:
    """The git evidence for one claim's resolved anchors (see the module docstring):
    the range's chunks first when *base* and *head* are both given, then per anchor in
    claim order. Never raises for the repository's sake; each failure is a note chunk."""
    resolved = list(resolved)
    keys = _keywords(resolved, claim)
    out: list = []

    def guarded(what: str, why: str, make) -> None:
        try:
            got = make()
        except _GitFailed as err:
            out.append(_note(what, str(err), why))
            return
        except Exception as err:   # a git output this module did not expect: a note, not a crash
            out.append(_note(what, f"git evidence failed: {type(err).__name__}: {str(err)[:200]}", why))
            return
        if got is None:
            return
        out.extend(got if isinstance(got, list) else [got])

    if base and head:
        guarded(f"diff:{base}..{head}", f"range {base}..{head}",
                lambda: _range_chunks(view, base, head, resolved, keys, max_chunk_chars))
    history = bool(_HISTORY.search(claim or ""))
    for r in resolved:
        kind = r.anchor.kind
        if kind == "commit":
            if not r.found:
                out.append(_note(r.anchor.text, f"commit {r.anchor.text} is not in this repository",
                                 f"commit {r.anchor.text}"))
            else:
                guarded(r.anchor.text, f"commit {r.anchor.text}",
                        lambda r=r: _commit_chunks(r, view, resolved, keys, max_chunk_chars))
        elif kind == "ticket" and r.found:
            guarded(f"ticket:{r.anchor.text}", f"ticket {r.anchor.text}",
                    lambda r=r: ticket_chunk(r, view, max_chunk_chars=max_chunk_chars))
        elif kind == "path" and r.found:
            if _TICKET_PATH.match(r.path):
                guarded(f"ticket:{r.path}", f"ticket {r.path}",
                        lambda r=r: ticket_chunk(r, view, max_chunk_chars=max_chunk_chars))
            if history:
                guarded(f"log:{r.path}", f"history of {r.path}",
                        lambda r=r: _history_chunk(view, r.path, f"history of {r.anchor.text}"))
    seen, unique = set(), []
    for c in out:
        if c.id not in seen:
            seen.add(c.id)
            unique.append(c)
    return unique
