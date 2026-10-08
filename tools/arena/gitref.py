"""tools/arena/gitref.py — AR-3: one file committed onto a ref, plumbing only.

`arena run start NN` needs a base that holds the round's ticket without the
operator's checkout ever moving: no `checkout`, `switch`, `stash`, `reset`,
`add` or porcelain `commit` anywhere in arena. So the commit is built from
plumbing in a throw-away index (`GIT_INDEX_FILE=<tmp>`): the parent's tree is
read into it, the one blob is added, the tree is written and committed, and the
ref is pointed at the result. The operator's `HEAD`, `.git/index` and working
tree are never read for writing, so `git status` is identical before and after.

Every git failure is a `GitRefError` whose message is one line — the git
command and its first stderr line — which the CLI hands to `output.refuse`.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


class GitRefError(Exception):
    """A git command failed; the message is the one line the CLI prints."""


def git(repo: Path, *args: str, env: Optional[dict] = None,
        stdin: Optional[str] = None, strip: bool = True) -> str:
    """Run `git <args>` in *repo*; its stdout (stripped unless *strip* is
    False — a file's text keeps its bytes), or `GitRefError`."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, env=env,
            # Bug 174: not the locale's strict codec — a latin-1 commit subject
            # or ticket blob was a UnicodeDecodeError traceback. surrogateescape
            # keeps every byte, so a blob read here and fed back as *stdin*
            # (`hash-object`) is the same blob; `printable` is for the screen.
            # Bug 185: bytes in, bytes out — text mode's universal newlines
            # turned a CRLF ticket's `\r\n` into `\n`, so the blob read back
            # was never the blob on the branch.
            input=None if stdin is None else stdin.encode("utf-8", "surrogateescape"),
        )
    except OSError as err:  # no git binary at all
        raise GitRefError(printable(f"git {' '.join(args)}: {err}")) from err
    out = proc.stdout.decode("utf-8", "surrogateescape")
    if proc.returncode != 0:
        err_text = proc.stderr.decode("utf-8", "surrogateescape")
        first = (printable(err_text).strip().splitlines()
                 or [f"exit {proc.returncode}"])[0]
        raise GitRefError(printable(f"git {' '.join(args)}: {first}"))
    return out.strip() if strip else out


def ls_tree_names(repo: Path, ref: str, folder: str, recursive: bool = False) -> list[str]:
    r"""The file names under *folder* in *ref*'s tree, quoted names included.

    Bug 206: `ls-tree --name-only` C-quotes a name holding a non-ASCII letter, a
    `"` or a `\` (`"epic-tasks/\320\260.md"`), and no ticket pattern knows such a
    string — a hand-named ticket was invisible on a branch or a base. `-z` turns
    the names into NUL-separated bytes and turns the quoting off, so the names
    come back as git holds them. `strip=False`: the NULs are the separators, and
    a stripped result would keep them anyway but loses any leading blank.
    `GitRefError` when *ref* cannot be read, as `git` does — the caller decides
    whether that is `[]` or a refusal.
    """
    extra = ["-r"] if recursive else []
    raw = git(repo, "ls-tree", *extra, "-z", "--name-only", ref, folder, strip=False)
    return [name for name in raw.split("\0") if name]


def status_paths(repo: Path, pathspec: Optional[str] = None) -> list[str]:
    """The untracked and modified paths under *pathspec*, by their real names.

    Bug 209: `git()` strips the whole output, so the first `status --porcelain`
    line lost its leading space (` M epic-tasks/01-a.md` → `M epic-tasks/…`) and
    `line[3:]` cut a letter — the refusal named `pic-tasks/01-a.md`. `-z` gives
    NUL-separated records, `XY<space>path`, so the path comes from the record, a
    non-ASCII name is its own name and a rename is its new name. `GitRefError`
    when git cannot answer.
    """
    args = ["status", "--porcelain", "-z", "--untracked-files=all"]
    if pathspec:
        args += ["--", pathspec]
    raw = git(repo, *args, strip=False)
    out: list[str] = []
    renamed = False
    for record in raw.split("\0"):
        if not record:
            continue
        if renamed:
            # `-z` puts the OLD name second in an `R ` / `C ` pair — `old -> new`
            # over a tab becomes `new\0old`, so the name worth keeping was first.
            renamed = False
            continue
        if len(record) < 3 or record[2] != " ":
            continue  # not `XY<space>path`: an unreadable record, skipped
        out.append(record[3:])
        renamed = record[0] in ("R", "C")
    return out


def printable(text: str) -> str:
    """*text* (from `git` or `read_utf8`) with each undecodable byte as U+FFFD,
    safe to print or to search; never feed it back to git as content."""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def read_utf8(path: Path) -> str:
    """A file's text the way `git` returns a blob: invalid UTF-8 bytes kept as
    surrogates, so the text hashes and commits back to the same bytes."""
    return Path(path).read_bytes().decode("utf-8", "surrogateescape")


def _index_with_file(repo: Path, parent: str, path_in_repo: str, content: str,
                     env: dict) -> str:
    """Steps 1–4 in the index *env* names: the parent's tree plus the file; the tree sha."""
    git(repo, "read-tree", parent, env=env)
    blob = git(repo, "hash-object", "-w", "--stdin", env=env, stdin=content)
    git(repo, "update-index", "--add", "--cacheinfo",
        f"100644,{blob},{path_in_repo}", env=env)
    return git(repo, "write-tree", env=env)


def tree_with_file(repo: Path, parent: str, path_in_repo: str, content: str) -> str:
    """The tree sha *parent*'s tree would have with *content* at *path_in_repo*."""
    with tempfile.TemporaryDirectory(prefix="arena-index-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        return _index_with_file(Path(repo), parent, path_in_repo, content, env)


def commit_file_on(repo: Path, parent: str, path_in_repo: str, content: str,
                   message: str, ref: str) -> str:
    """Commit *content* at *path_in_repo* on top of *parent*, point *ref* at it.

    Returns the new commit's sha. Built in a temporary index, so the
    operator's own index, HEAD and working tree are untouched.
    """
    repo = Path(repo)
    with tempfile.TemporaryDirectory(prefix="arena-index-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        tree = _index_with_file(repo, parent, path_in_repo, content, env)
        sha = git(repo, "commit-tree", tree, "-p", parent, "-m", message, env=env)
    git(repo, "update-ref", ref, sha)
    return sha
