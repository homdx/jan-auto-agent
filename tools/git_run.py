"""tools/git_run.py — FL-2: the one bounded `.git/index.lock` retry, for every git call.

git holds `.git/index.lock` for the whole of any command that writes the index —
`add`, `commit`, `checkout -B`, `clean -fdx`, `worktree add` — and a second git that finds it there does not wait: it exits 128 with
`Unable to create '<path>/.git/index.lock': File exists`. That is a *transient*:
the holder is a moment from releasing it. FL-1 (round 84) made `GitManager._run`
wait for it — 8 attempts, 0.25 s apart — and it fixed the one caller the stress
run happened to hit. Everything else in the tree was written before it and still
took the first 128 as final, which is how a round of N agents against one
repository lost a workspace to `worktree add` / `checkout -B` / `clean -fdx`, how
`gates.git` read a collision as an empty diff, and how a failed `git status` read
as a clean tree.

The ladder lives here, once, so every caller has the same bound instead of six
copies of it. It matches only that one signature, so a *stale* lock left by a
crashed git costs about two seconds and then surfaces the same error unchanged —
and that error already names stale locks as the likely cause. Read-only
subcommands go through the same call; the signature simply never matches, so no
caller needs a whitelist of which git writes the index.

This module runs git and reports: it returns the `CompletedProcess` and never
raises on a non-zero exit, so each caller still maps the result to its own
failure — `WorkspaceError`, `RuntimeError`, `GitError`, `None`, a sentinel. It
never waits on a stale lock beyond the ladder and never deletes `.git/index.lock`
on a caller's behalf.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "LOCK_BACKOFF_S",
    "LOCK_CONTENTION_RE",
    "LOCK_RETRIES",
    "run_git",
]

#: The one signature that is a transient — git's own "another git has the index"
#: message, on stderr with exit 128. A stale lock left by a crashed git matches it
#: too, which is exactly why the ladder below is bounded and short: it ends by
#: raising that error rather than waiting forever.
LOCK_CONTENTION_RE = re.compile(
    r"Unable to create '.*\.lock': File exists", re.IGNORECASE,
)

#: 8 attempts 0.25 s apart. A held lock that a neighbour releases in half a second
#: is inside the ladder; a stale one costs about two seconds and then raises.
LOCK_RETRIES = 8
LOCK_BACKOFF_S = 0.25

def _backoff(seconds: float) -> None:
    """The default wait between two attempts — the seam tests stand in for."""
    time.sleep(seconds)


def _attempted(text: str) -> bool:
    """Whether *text* is git's held-index message."""
    return bool(LOCK_CONTENTION_RE.search(text))


def run_git(
    cmd: Sequence[str],
    cwd: Optional[str | Path] = None,
    *,
    encoding: Optional[str] = None,
    errors: Optional[str] = None,
    timeout: Optional[float] = None,
    retries: int = LOCK_RETRIES,
    backoff_s: float = LOCK_BACKOFF_S,
    sleep: Optional[Callable[[float], None]] = None,
) -> subprocess.CompletedProcess:
    """Run *cmd* — the full git argv — and return its `CompletedProcess`.

    Only one failure repeats: a non-zero exit whose output is git's held-index
    message. Everything else — an ordinary bad ref, a path that is not a
    repository, a timeout, a missing git binary — comes back on the first attempt,
    so a real failure never pays the ladder.

    *cmd* is the whole argv, so callers keep their own `-C`, `-c`, `--porcelain`
    and friends unchanged; *cwd* is passed to `subprocess.run` as before.
    *encoding* / *errors* are passed through untouched: each caller decodes its
    own output the way it always did (`delta_validator` in particular relies on a
    non-decodable byte sequence raising, so that a binary file at HEAD reads as
    "no baseline"). *timeout* is ``None`` unless the caller had one before — the
    ladder adds no deadline to a call that never had one. *retries=1* is a single
    attempt with no waiting at all.

    *sleep* is the wait between two attempts, the module's ``_backoff`` unless
    given: a named seam so a test can release the lock *in* the backoff and count the attempts
    instead of racing a timer thread against the ladder (FL-1's rule), and so
    `GitManager` keeps waiting through its own ``_backoff``.

    Raises
    ------
    OSError, subprocess.TimeoutExpired
        An attempt that could not be made or did not finish; never swallowed.
    """
    proc: Optional[subprocess.CompletedProcess] = None
    for attempt in range(max(1, retries)):
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding=encoding,
            errors=errors,
            timeout=timeout,
        )
        if (
            proc.returncode == 0
            or attempt == retries - 1
            or not _attempted(proc.stderr or "")
        ):
            return proc
        logger.debug(
            "git %s: index held by another git — retry %d/%d in %.2fs",
            " ".join(str(c) for c in cmd),
            attempt + 1,
            retries - 1,
            backoff_s,
        )
        (sleep or _backoff)(backoff_s)
    assert proc is not None  # pragma: no cover
    return proc
