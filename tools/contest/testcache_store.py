"""Test-run cache, store half (ticket 212): the tree fingerprint and the round's append-only run cache.

This module is dependency-free on purpose.  It never parses a shell command or
a pytest summary (that is ``testcache.py``); it takes the opaque ``key``
string and the already-built ``summary`` dict from its caller.  Two jobs:

* ``tree_fingerprint(worktree)`` -- one hash that answers "is this tree
  byte-for-byte what it was when the earlier run happened?".
* ``TestRunCache(path)`` -- one JSON-lines file per round
  (``contest-out/NN/test-cache.jsonl``) that ten agents append to at once.

The cache is never on the round's critical path: every failure here turns
into ``None`` / a logged warning, and the caller then runs the command as it
would without the cache.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Every git call gets this long; a hung git must not hang the policy.
GIT_TIMEOUT_S = 20
# An untracked file bigger than this is hashed by (size, mtime-free) name only:
# the fingerprint stays cheap, and a build artefact that large is not source.
MAX_UNTRACKED_BYTES = 5 * 1024 * 1024
# Path components whose changes never alter what pytest would report.
_IGNORED_PARTS = ("runs", ".pytest_cache", "__pycache__")
# git pathspecs for the tracked diff: the same exclusions, git-side.
_DIFF_EXCLUDES = (
    ":(exclude,glob)runs/**",
    ":(exclude,glob)**/runs/**",
    ":(exclude,glob).pytest_cache/**",
    ":(exclude,glob)**/.pytest_cache/**",
    ":(exclude,glob)**/__pycache__/**",
)


def _git(worktree: str | Path, *args: str) -> bytes:
    """Run one read-only git command in *worktree*; raise on any failure.

    ``GIT_OPTIONAL_LOCKS=0`` stops git from opportunistically refreshing (and
    so rewriting) the index, which keeps the call free of side effects.
    """
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    proc = subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True, timeout=GIT_TIMEOUT_S, env=env, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr[:200]!r}")
    return proc.stdout


def _ignored(rel: str) -> bool:
    """True when a repo-relative path lies under runs/, .pytest_cache or __pycache__."""
    parts = rel.split("/")
    if parts[0] == "runs":
        return True
    return any(p in (".pytest_cache", "__pycache__") for p in parts)


def _hash_untracked(h: "hashlib._Hash", root: Path, rel: str) -> None:
    """Fold one untracked file (name + content, or link text) into *h*."""
    path = root / rel
    h.update(b"U\0" + rel.encode("utf-8", "surrogateescape") + b"\0")
    if path.is_symlink():
        # The link text is the content; never follow it out of the tree.
        h.update(b"L\0" + os.readlink(path).encode("utf-8", "surrogateescape"))
        return
    if not path.is_file():
        h.update(b"N\0")          # vanished or special file
        return
    size = path.stat().st_size
    if size > MAX_UNTRACKED_BYTES:
        h.update(b"B\0" + str(size).encode())
        return
    h.update(b"F\0" + hashlib.sha256(path.read_bytes()).digest())


def tree_fingerprint(worktree: str | Path) -> str | None:
    """sha256 over HEAD, the tracked diff and the untracked files' contents.

    ``None`` on any failure (not a git repo, git missing, timeout, no commit
    yet): then nothing is cached or served.  Read-only: no ``git add``.
    """
    try:
        root = Path(worktree)
        head = _git(root, "rev-parse", "HEAD").strip()
        diff = _git(root, "diff", "HEAD", "--binary", "--no-ext-diff",
                    "--no-textconv", "--no-color", "--", ".", *_DIFF_EXCLUDES)
        listing = _git(root, "ls-files", "-o", "--exclude-standard", "-z")
        h = hashlib.sha256()
        h.update(b"H\0" + head + b"\0D\0")
        h.update(hashlib.sha256(diff).digest())
        names = sorted(
            n.decode("utf-8", "surrogateescape")
            for n in listing.split(b"\0") if n
        )
        for rel in names:
            if not _ignored(rel):
                _hash_untracked(h, root, rel)
        return h.hexdigest()
    except Exception as exc:  # noqa: BLE001 -- the cache must never break a round
        log.debug("tree_fingerprint(%s) failed: %s", worktree, exc)
        return None


class TestRunCache:
    """The round's append-only run cache: one JSON line per run or classification.

    Two kinds of line share the file and never see each other:
    run lines ``{t, key, fp, summary, rc, wall_s, agent}`` and classification
    lines ``{kind: "classify", command, parsed}``.
    """

    __test__ = False  # the name starts with "Test"; keep pytest from collecting it

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # -- writing ---------------------------------------------------------
    def _append(self, obj: dict) -> None:
        """Append *obj* as one line under an exclusive flock; never raises."""
        try:
            line = (json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n").encode()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                size = os.fstat(fd).st_size
                # A crashed writer may have left a torn last line without its
                # newline; start a fresh line so ours is not glued onto it.
                if size and os.pread(fd, 1, size - 1) != b"\n":
                    line = b"\n" + line
                os.write(fd, line)          # one write: whole line or nothing
            finally:
                os.close(fd)                # closing releases the flock
        except Exception as exc:  # noqa: BLE001
            log.warning("test cache %s: append failed: %s", self.path, exc)

    def record(self, key: str, fingerprint: str, summary: dict,
               rc: int | None, wall_s: float, agent: str) -> None:
        """Remember one finished run under (*key*, *fingerprint*)."""
        self._append({"t": time.time(), "key": key, "fp": fingerprint,
                      "summary": summary, "rc": rc, "wall_s": wall_s,
                      "agent": agent})

    def record_classification(self, command: str, parsed_key_or_none: str | None) -> None:
        """Remember the gate model's answer for *command* (``None`` = not pytest)."""
        self._append({"t": time.time(), "kind": "classify",
                      "command": command, "parsed": parsed_key_or_none})

    # -- reading ---------------------------------------------------------
    def _entries(self):
        """Yield every well-formed JSON object line; skip torn or garbled ones."""
        try:
            data = self.path.read_bytes()
        except OSError:
            return
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj

    def lookup(self, key: str, fingerprint: str) -> dict | None:
        """Newest run entry with this key and fingerprint, or ``None``."""
        found = None
        for obj in self._entries():
            if obj.get("kind") == "classify":
                continue
            if obj.get("key") == key and obj.get("fp") == fingerprint:
                found = obj          # later lines are newer
        return found

    def classification(self, command: str) -> tuple[bool, str | None]:
        """``(known, parsed)`` for *command*; ``(False, None)`` if never asked."""
        known, parsed = False, None
        for obj in self._entries():
            if obj.get("kind") == "classify" and obj.get("command") == command:
                known, parsed = True, obj.get("parsed")
        return known, parsed
