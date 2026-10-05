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
            ["git", *args], cwd=str(repo), input=stdin, capture_output=True,
            text=True, env=env,
            # Bug 174: not the locale's strict codec — a latin-1 commit subject
            # or ticket blob was a UnicodeDecodeError traceback. surrogateescape
            # keeps every byte, so a blob read here and fed back as *stdin*
            # (`hash-object`) is the same blob; `printable` is for the screen.
            encoding="utf-8", errors="surrogateescape",
        )
    except OSError as err:  # no git binary at all
        raise GitRefError(printable(f"git {' '.join(args)}: {err}")) from err
    if proc.returncode != 0:
        first = (printable(proc.stderr).strip().splitlines()
                 or [f"exit {proc.returncode}"])[0]
        raise GitRefError(printable(f"git {' '.join(args)}: {first}"))
    return proc.stdout.strip() if strip else proc.stdout


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
