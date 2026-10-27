"""tools/contest/backend.py — KC-34: one agent session, behind one protocol.

``runner.py`` used to know ``KiloClient`` and ``EventTap`` by name: ``run_agent``
took ``client`` and ``tap``, ``run_round`` built a ``KiloClient`` and a
``EventTap`` per agent and called ``tap.stop()``/``tap.join(2.0)`` itself. That
made the backend part of the round — an operator with an OpenRouter key and no
``kilo`` binary could not run a round at all, because there was nowhere to put
a non-Kilo session.

This module is the seam. A :class:`ContestBackend` owns the whole lifecycle of
one session — create, prompt, wait for idle, abort on stall, close — and
nothing else: the worktree and the harvest stay in ``runner.py``, and the gate
(``policy.py``) stays in ``runner.py`` too, because ``on_permission`` is the
runner's closure all the way down. Two backends implement it:

  * :class:`KiloBackend` — today's path, ``KiloClient`` + ``EventTap`` over
    ``kilo serve``. The kilo round is byte-identical in behaviour.
  * :class:`OpenRouterBackend` — a subprocess agent loop that speaks an
    OpenAI-compatible ``/chat/completions`` directly (OpenRouter, kenari, any
    OpenAI-compatible gateway) inside the worktree. The session is the process;
    ``prompt`` writes to its stdin, ``wait_idle`` reads its stdout until the
    IDLE sentinel, ``abort`` sends SIGTERM.

``runner.py`` only sees the protocol, so adding a backend means a class here
and a choice in ``cli.cmd_run`` — never a fork of ``runner.py``.

The two things this module deliberately does not abstract:

  * **the model call is not one primitive.** A Kilo session and a subprocess
    agent loop are different animals, and ``wait_idle`` is where they meet:
    ``IdleResult`` and ``SessionRef`` are shared (they live in ``kilo_client``
    and import nothing Kilo-specific), the way a wait ends is not.
  * **``on_question`` is a Kilo-shaped callback.** The runner passes it to
    every backend, and :class:`OpenRouterBackend` never calls it: a bare
    ``/chat/completions`` has no "ask the operator a question" event, so
    ``max_questions_per_turn`` has no edge to count on this backend.
    ``on_permission`` *is* called — the agent loop turns every ``bash`` call
    into the same ``permission.asked`` shape ``policy.decide`` reads
    (``permission: "bash"``, ``patterns: []``, ``metadata.command``), so the
    three-layer gate, ``deny_commands`` and the forbidden-sibling-worktree
    check enforce this backend exactly as they enforce a Kilo session, and
    ``tool_parts`` returns the loop's own recent tool calls rather than
    ``[]``. See ``tools/contest/policy.py:_mechanical`` for why a bash call
    with no outside path is decided mechanically and costs no gate call.

Everything here degrades rather than raises, except ``create_session`` and
``prompt``: a non-retryable backend failure becomes
:class:`ContestBackendError`, and a retryable one comes back as
``IdleResult(status="error")`` whose payload carries
``data.isRetryable = True`` for the runner's existing ``_retryable()`` check.

``python3 -m tools.contest.backend --agent-loop`` is the subprocess: it is
this file's own tail, so a backend needs no second file to spawn. Standard
library plus ``tools.contest.kilo_client``; the agent loop is ``urllib``.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import json
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from tools.contest.kilo_client import (
    EventTap,
    IdleResult,
    KiloClient,
    KiloHttpError,
    KiloServer,
    SessionRef,
    _deadline_grant,
    _context_limit_of,
    _permission_answer,
)

__all__ = [
    "BACKENDS",
    "AGENT_LOOP_MODULE",
    "ContestBackend",
    "ContestBackendError",
    "KiloBackend",
    "KiloLimitKept",
    "KiloLimitRefused",
    "KiloTapReconnectError",
    "KILO_PROJECT_FILE",
    "KILO_PROJECT_FILE_NAMES",
    "OpenRouterBackend",
    "drop_stale_kilo_file",
    "tracked_kilo_files",
]

_LOG = logging.getLogger(__name__)

#: The two values ``[contest] backend =`` accepts.
BACKENDS = ("kilo", "openrouter")

#: The subprocess entry point ``OpenRouterBackend.spawn_agent`` runs: this file.
AGENT_LOOP_MODULE = "tools.contest.backend"

#: The stdin/stdout record that ends a turn, written when the model has no tool calls left.
IDLE_SENTINEL = "idle"

#: Bash timeout for one agent command; longer than turn_timeout_sec would be pointless.
_AGENT_BASH_TIMEOUT = 300.0

#: ``CONTEST_AGENT_BASH_TIMEOUT`` in the agent's environment overrides the above.
_AGENT_BASH_TIMEOUT_ENV = "CONTEST_AGENT_BASH_TIMEOUT"


# Protocol

class ContestBackendError(Exception):
    """A backend failure that cannot be retried (mirrors KiloHttpError's role).

    Raised by :meth:`ContestBackend.create_session` and
    :meth:`ContestBackend.prompt` when the session cannot be created or the
    turn cannot be sent. ``run_agent`` turns this into
    ``AgentState.ERROR`` with the same line it already writes for a refused
    ``POST /session``. A failure that is retryable does not raise — it comes
    back as ``IdleResult(status="error")`` with ``data.isRetryable`` set, so
    the runner's ``_retryable()`` decides.
    """


class KiloLimitRefused(ContestBackendError):
    """The workspace will not take the model's ``limit`` at all.

    Raised by :meth:`KiloBackend.set_model_limit` when the push would rewrite a
    tracked project file into the agent's diff, and in two cases: the checkout
    already tracks one, so an ignore rule would not help; or ``git`` did not
    answer at all (Round 151) — a timeout, an index it refuses to read — so the
    check cannot tell the two apart, and a push taken on a blind index is the
    one a tracked file survives in the diff unseen.

    Round 148: the only reason ``set_model_limit`` can refuse, and the only one
    that will refuse again the same way on the next call. The runner remembers
    it (`push_refused`) so the check is not re-run for the same size, and a
    watch that was standing by that refusal stays armed — a window that was
    never handed to Kilo is exactly what the watch is for. Every other failure
    is a plain :class:`ContestBackendError`: it is retryable, and the runner
    does not remember it."""


class KiloTapReconnectError(ContestBackendError):
    """Round 151: the limit did go over, but the workspace's event stream did
    not come back.

    A `PATCH /config` on 7.6.2 disposes the directory's instance and ends its
    ``/event`` stream, so the tap has to be reopened for the next turn to be
    heard at all (`KiloBackend._reconnect_tap`). That reopen can fail — the log
    directory cannot be made, the log cannot be opened — and it is a different
    failure from the push: the server answered the patch and now sizes the
    session by the window the caller asked for, so the caller must remember the
    window as handed over and keep its in-turn watch off. The two failures are
    otherwise identical to a caller that catches only
    :class:`ContestBackendError`, and today the reconnect's error is read as
    "the window was not handed to Kilo" — which un-remembers a window Kilo does
    hold and re-patches, and reloads Kilo, for every later prompt.
    """


class KiloLimitKept(ContestBackendError):
    """Round 153: the patch landed, and the server kept its own window anyway.

    Live, 7.6.2: a model whose ``limit`` the server's own config content carries
    (the round's spawn ``KILO_CONFIG_CONTENT`` — or the operator's) keeps it
    field by field. A ``PATCH /config`` for a smaller one answers 200, writes
    ``.kilo/kilo.jsonc``, disposes the instance and reopens the stream — and the
    next ``GET /provider`` still reports the content's limit. The window never
    went over, even though the server said 200 and this round's stream is fine,
    which is why a caller that trusts the status code stands its in-turn watch
    down for a turn Kilo sizes by the old, larger window: the refusal the memory
    just recorded.

    ``kept`` is the ``limit.context`` the read-back reported, ``None`` when the
    read-back could not be read at all: the caller cannot tell which window Kilo
    sizes by and must keep the watch armed either way. It is remembered like a
    :class:`KiloLimitRefused` — the server's own content does not change inside
    a round, so the same size is not re-patched, and reloaded, every prompt.

    Unlike :class:`KiloLimitRefused` the file *was* written: the caller drops it
    on the way out with the limit it pushed.
    """

    def __init__(self, message: str, *, kept: int | None = None) -> None:
        super().__init__(message)
        self.kept = kept


@runtime_checkable
class ContestBackend(Protocol):
    """One agent session on one backend.

    A backend owns the full lifecycle of one session: create, prompt, wait for
    the model to go idle, abort on stall, close. It does NOT own the worktree
    or the harvest — those stay in ``runner.py`` — and it does not decide a
    permission: ``on_permission`` is the runner's closure, which is what the
    policy actually enforces.

    Every method raises :class:`ContestBackendError` on a non-retryable
    failure. Retryable failures never raise: ``wait_idle`` returns
    ``IdleResult(status="error")`` with ``error["data"]["isRetryable"] = True``
    in the dict the runner's existing ``_retryable()`` check reads (KC-19).
    """

    def wait_ready(self) -> None:
        """Let this backend's transport settle before the first prompt.

        A Kilo event published before the SSE stream is open is lost, and the
        first one is the session's own, so ``KiloBackend`` polls the tap's
        socket here. A freshly-spawned subprocess has nothing to race — its
        stdout buffer holds everything it writes before it is read — so
        :class:`OpenRouterBackend` returns at once.
        """
        ...

    def create_session(self, provider_id: str, model_id: str, *,
                       rules: list, title: str,
                       agent: str | None = None,
                       variant: str | None = None) -> SessionRef: ...

    def prompt(self, session: SessionRef, text: str) -> None: ...

    def compact(self, session: SessionRef) -> None:
        """KC-67: shrink this session's own history server-side, before the next
        prompt — ``POST /session/{id}/summarize``, KC-10's call.

        A backend that has no session on a server to compact does not raise: it
        returns without changing anything, and the runner sends the prompt that
        was coming anyway. A server that refuses the call raises
        :class:`ContestBackendError` like :meth:`prompt`; the runner treats both
        as "no compact", never as a failed round.
        """
        ...

    def mark(self) -> int | None:
        """KC-63: a position in this backend's event stream, taken before a
        prompt; ``None`` when the backend has no shared stream."""
        ...

    def abort(self, session: SessionRef) -> None:
        """Ask the session to stop its turn.

        Idempotent and quiet: a session that is already gone is not an error.
        ``run_agent`` calls this on a stall and on Ctrl-C, and
        :meth:`wait_idle` calls it on a timeout.
        """
        ...

    def interrupt(self, session: SessionRef | None = None) -> None:
        """End an in-flight :meth:`wait_idle` immediately.

        Not an abort: ``abort`` asks the *session* to stop and may take a
        moment to arrive, while this ends the *wait* now. ``KiloBackend``
        stops the event tap (the wait then wakes on ``tap.closed``);
        :class:`OpenRouterBackend` SIGTERMs the agent. *session* is
        ``None`` only from the round's Ctrl-C path, where every live backend
        is interrupted at once and the session may not exist yet.
        """
        ...

    def interrupted(self) -> bool:
        """Whether :meth:`interrupt` has been asked for this backend."""
        ...

    def wait_idle(self, session: SessionRef, timeout: float, *,
                  idle_event_timeout: float | None = None,
                  on_permission: Callable[[dict], tuple],
                  on_question: Callable[[dict], None],
                  on_deadline: Callable[[float], float | None] | None = None,
                  since: int | None = None,
                  max_retry_wait: float | None = None,
                  quota_re: "re.Pattern | None" = None,
                  max_retry_attempts: int | None = None,
                  quiet_after: float | None = None) -> IdleResult:
        """Block until this session goes idle, answering on the way.

        ``timeout`` bounds the whole wait, ``idle_event_timeout`` the silence.
        ``on_permission(event) -> (reply, message)`` decides a
        ``permission.asked`` event; ``on_question(event) -> None`` observes a
        ``question.asked`` one. Both are required — the runner always passes
        them, even to a backend that never calls one of them
        (:class:`OpenRouterBackend` never calls ``on_question``).

        A third element of an ``on_permission`` answer (KC-66) is the seconds
        the handler asks back for the gate's own waits, which run inside the
        loop and would otherwise come out of the agent's turn: the deadline
        moves by that much. Omitted, as by every pre-KC-66 handler, nothing
        moves and the wait is byte for byte what it was.

        ``on_deadline(elapsed) -> seconds | None`` (KC-36) is asked once at
        ``timeout``, before the abort: a positive number extends the deadline
        by that much, anything else aborts exactly as without it. ``None`` —
        the default, and every pre-KC-36 caller — asks nothing and aborts at
        ``timeout``.

        ``since`` (KC-63) is a :meth:`mark` taken right before the prompt:
        events before it belong to an earlier turn and never end this wait.

        ``max_retry_wait`` and ``quota_re`` (KC-61) are passed to
        :meth:`KiloClient.wait_idle` when they are not ``None``: a ``session.status``
        retry scheduled further out than the bound ends the wait as
        ``status="error"`` with ``error["name"] == "ProviderQuota"`` instead of
        waiting the silence clock out. :class:`OpenRouterBackend` takes both and
        ignores them — a bare ``/chat/completions`` has no retry status, so its
        wait is unchanged with either set.

        ``max_retry_attempts`` (KC-64) is passed to :meth:`KiloClient.wait_idle`
        when it is truthy: ``status="error"`` with
        ``error["name"] == "ProviderUnavailable"`` once the provider has failed
        that many retries in a row with no assistant output in between.
        :class:`OpenRouterBackend` takes it and ignores it too — its provider
        errors reach the runner as a plain ``session.error``.

        ``quiet_after`` (KC-73) is passed to :meth:`KiloClient.wait_idle` when it
        is not ``None``: a session that sends nothing in that many seconds ends
        the wait as ``status="quiet"``, not aborted. The runner arms it only on a
        backend whose :meth:`mark` is not ``None``; :class:`OpenRouterBackend`
        takes it and ignores it.
        """
        ...

    def tool_parts(self, session: SessionRef) -> list:
        """The recent ``tool`` parts, newest last.

        The gate reads the last few of these to see what the agent has been
        doing rather than one call out of context. :class:`OpenRouterBackend`
        returns its own tool calls, in :meth:`ContestBackend.wait_idle`'s
        ``state`` shape, not ``[]``.
        """
        ...

    def session_info(self, session: SessionRef) -> dict:
        """``cost`` and ``tokens`` of the finished session; ``{}`` when unknown."""
        ...

    def messages(self, session: SessionRef) -> list:
        """The session's messages, in order; ``[]`` when there are none."""
        ...

    def close(self) -> None:
        """Per-agent cleanup. Idempotent, called once per agent, after every run.

        Not a no-op: this is where a backend gives back the transport it holds
        for the round. ``KiloBackend`` stops and joins its event tap
        (``run_round`` used to do this itself); ``OpenRouterBackend``
        terminates its agent. A server process is not a session, so neither
        closes one.
        """
        ...


# Kilo

#: Round 148: the project config a PATCH /config writes; it outlives the turn that wrote it.
KILO_PROJECT_FILE = ".kilo/kilo.jsonc"

#: Project config files a PATCH /config rewrites; only tracked copies block the push (round 151).
KILO_PROJECT_FILE_NAMES = (".kilo/kilo.jsonc", ".kilo/kilo.json")

#: Ignore file, kept in the workspace, that hides the project config files (round 151).
_KILO_IGNORE_PATH = (".kilo", ".gitignore")

#: Contents of the ignore file: the project config names and its own name (round 151).
_KILO_IGNORE_LINES = ("kilo.jsonc", "kilo.json", ".gitignore")

#: Seconds the guards wait for git; a git that cannot read its index must refuse (round 151).
_KILO_GIT_TIMEOUT = 10.0



def _git(directory, *args, timeout: float = 30.0) -> tuple:
    """One `git` in *directory*: ``(ran, stdout.strip())``.

    ``(False, "")`` on any failure — not a checkout, no `git`, a timeout, a
    refused tree — so a caller can tell "git answered no" from "git never
    answered". Never raises: this is a workspace check, not a round step.
    """
    try:
        out = subprocess.run(["git", "-C", str(directory), *args],
                             capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    return out.returncode == 0, out.stdout.strip()


def _tracked_project_files(directory) -> tuple:
    """Round 151: ``(files, answered)`` — the project files *directory* tracks,
    and whether `git` answered at all.

    *files* is a subset of :data:`KILO_PROJECT_FILE_NAMES`, by name: the two
    paths a `PATCH /config` rewrites, and the only ones that can land in the
    agent's diff. A tracked ``.kilo/rules/x.md`` is not reported.

    *answered* is False only when `git` said nothing — no `git`, a timeout, an
    index it cannot read. Silence is not "nothing is tracked", and a caller
    that protects a file must refuse on it. A directory that is not a checkout
    *does* answer ("not a git repository") and is reported answered with no
    files: there is no diff there to protect.
    """
    try:
        proc = subprocess.run(["git", "-C", str(directory), "ls-files", ".kilo"],
                              capture_output=True, text=True,
                              timeout=_KILO_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return [], False
    if proc.returncode == 0:
        tracked = [line.strip() for line in (proc.stdout or "").splitlines()
                   if line.strip()]
        return [p for p in tracked if p in KILO_PROJECT_FILE_NAMES], True
    text = ((proc.stderr or "") + (proc.stdout or "")).lower()
    if "not a git repository" in text:
        return [], True
    return [], False


def _jsonc_value(text: str):
    """`json.loads` over a ``.jsonc`` document: comments and trailing commas stripped.

    ``None`` when it is not a JSON object at all — the file is then unattributable,
    and the caller keeps it rather than deleting a file it cannot read.
    """
    out, quote, i, n = [], None, 0, len(text)
    while i < n:
        ch = text[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(n, i + 2)
            continue
        out.append(ch)
        i += 1
    try:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))
    except (ValueError, TypeError):
        return None


def _holds_limit(node, limit: dict) -> bool:
    """Whether *node* still carries *limit*, nested anywhere.

    A sub-dict match, not equality: Kilo may keep other config next to the
    limit it was patched with, and the limit object itself may carry keys the
    patch did not send. A limit that no longer matches is not this round's, and
    that is what keeps an agent-authored file alive to the harvest.
    """
    if isinstance(node, dict):
        if all(node.get(key) == value for key, value in limit.items()):
            return True
        return any(_holds_limit(value, limit) for value in node.values())
    if isinstance(node, list):
        return any(_holds_limit(value, limit) for value in node)
    return False


def _file_carries_limit(target: Path, limit: dict) -> bool:
    """Whether *target* still holds the limit this round pushed, as read back."""
    if not isinstance(limit, dict) or not limit:
        return False
    try:
        return _holds_limit(_jsonc_value(target.read_text(encoding="utf-8")), limit)
    except OSError:
        return False


def tracked_kilo_files(directory) -> list:
    """The project-file names the checkout of *directory* already tracks.

    Round 151: only the two names a `PATCH /config` rewrites are reported, so a
    tracked ``.kilo/rules/x.md`` no longer blocks the push — the untracked file
    is kept out of the diff by `.kilo/.gitignore`, and only a tracked one would
    land in it.

    Raises :class:`KiloLimitRefused` when `git` did not answer (no `git`, a
    timeout, an index it cannot read) rather than returning ``[]``: silence is
    "unknown", and a push taken on an unknown index rewrites a tracked
    ``.kilo/kilo.jsonc`` into the agent's diff unseen — the whole point of the
    check. ``[]`` means `git` answered and named no project file.
    """
    files, answered = _tracked_project_files(directory)
    if not answered:
        raise KiloLimitRefused(
            f"git did not answer in {directory} — the push would be blind, and a "
            "tracked .kilo/kilo.jsonc there would land in the agent's diff")
    return files


def drop_stale_kilo_file(directory, expected_limit: dict | None = None,
                         *, assume_stale: bool = False) -> bool:
    """Round 148/151: delete the *untracked* project file this round wrote.

    ``KILO_PROJECT_FILE`` outlives the turn that wrote it, and a respawned
    server (`--resume`, `scripts/revive_round.py`) finds it on spawn — so the
    round deletes its own before that server starts and again when the run ends.
    Kilo marks the file `configProtected`, which is why the deletion checks the
    index first: only a file the checkout does not track is this round's, and
    only that goes.

    Round 151: untracked is not the same as this round's. One the agent wrote on
    purpose is, and it has to survive to the harvest — the gate saw the edit,
    and a deletion at the end of the run would erase what the round asked for.
    So a file goes only when it is exactly what the runner pushed for this
    workspace: *expected_limit* is the ``limit`` `set_model_limit` sent, and one
    that no longer carries it is left alone. ``expected_limit=None`` — the run
    pushed nothing — keeps every untracked file, which is the agent's.
    *assume_stale* is the pre-spawn call, where this run has pushed nothing yet,
    so an untracked project file there is a leftover of an earlier attempt and
    goes without a content compare.

    The `.kilo/.gitignore` `set_model_limit` wrote goes with the file it hides
    (`_drop_kilo_ignore`), and so does an empty `.kilo/`. One the agent wrote on
    purpose keeps its ignore, because that is what keeps *it* out of
    `git add -A` for the rest of the round.

    True when one was removed; False when there was nothing to do — no file, a
    tracked one, an agent's, a directory `git` will not answer. Never raises: a
    stale file is a nuisance, not a round.
    """
    files, answered = _tracked_project_files(directory)
    if not answered or files:
        return False
    for name in KILO_PROJECT_FILE_NAMES:
        try:
            target = Path(directory) / name
            if not target.is_file():
                continue
            if expected_limit is not None:
                if not _file_carries_limit(target, expected_limit):
                    continue
            elif not assume_stale:
                continue
            target.unlink()
            _drop_kilo_ignore(directory)
            return True
        except OSError:
            return False
    return False


def _kilo_ignore_path(directory) -> Path:
    return Path(directory) / _KILO_IGNORE_PATH[0] / _KILO_IGNORE_PATH[1]


def _exclude_kilo_dir(directory: str) -> None:
    """The project-file names ignored *in the workspace itself*, once per directory.

    Round 148 wrote ``.kilo/`` to the checkout's ``info/exclude``, and a linked
    worktree's ``git rev-parse --git-path info/exclude`` answers the repository's
    *common* exclude — the file every worktree shares, the operator's own
    checkout included, not the worktree's own. A round would then leave ``.kilo/``
    in the operator's real exclude forever, an entry no round wrote it for.

    Round 151: there is no exclude that is ours alone. Per-worktree
    ``<common>/worktrees/<name>/info/exclude`` is read by no one — checked with
    ``check-ignore -v`` on git 2.34.1, where the worktree's
    ``--git-path info/exclude`` still resolves the common file — so a linked
    worktree gets no exclude edit at all, which left the workspace's
    ``.kilo/kilo.jsonc`` untracked but visible: a ``git add -A`` in the agent's
    own turn would have staged it into the agent's diff, and it would have been
    *tracked* afterwards, which is what `tracked_kilo_files` refuses forever.
    A rule file the agent adds in ``.kilo/`` stays committable either way, and
    this does not touch the agent's tree unless it was empty of our own:
    ``.kilo/.gitignore`` with exactly the two names a ``PATCH /config`` writes
    and its own name, so ``git status`` and ``git add -A`` see none of ours.

    A ``.kilo/.gitignore`` that is already there with other content is the
    agent's, and a warning is logged instead of an edit — a write to it would
    land in the agent's diff. The file goes with the project file in
    `drop_stale_kilo_file`. Never raises: an unignored project file is a nuisance,
    not a round.

    Except Kilo's own. Live, 7.6.2: an instance that opens a workspace with a
    ``.kilo/`` and no ``.kilo/.gitignore`` writes one — ``node_modules``,
    ``package.json``, the lock files, ``.gitignore``, ``agent-manager.json`` —
    and never rewrites one that is there. It names neither project file, so a
    ``.kilo/`` the agent made (``.kilo/rules/``) or a reload after a patch leaves
    Kilo's file in place and ``kilo.jsonc`` in plain view. A file that ignores
    itself and is not tracked is never in the agent's diff, whoever wrote it, so
    the missing names are appended to it; one that is tracked, or does not hide
    itself, is still the agent's and is still only warned about.
    """
    # Checked on every push, since the agent or a leg can remove the file behind any cache (round 155, round 148).
    ok, inside = _git(directory, "rev-parse", "--is-inside-work-tree",
                      timeout=_KILO_GIT_TIMEOUT)
    if not ok or inside != "true":
        return
    try:
        path = _kilo_ignore_path(directory)
        if path.exists():
            text = path.read_text(encoding="utf-8")
            lines = {line.strip() for line in text.splitlines()}
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            text, lines = "", set()
        missing = [line for line in _KILO_IGNORE_LINES if line not in lines]
        if missing:
            if lines:
                if not _hides_itself_untracked(directory, lines):
                    _LOG.warning("%s: .kilo/.gitignore is the agent's and ignores none of %s "
                                 "— the window may show in the diff",
                                 directory, ", ".join(missing))
                    return
                with path.open("a", encoding="utf-8") as fh:
                    if text and not text.endswith("\n"):
                        fh.write("\n")
                    fh.write("".join(line + "\n" for line in missing))
            else:
                path.write_text("".join(line + "\n" for line in _KILO_IGNORE_LINES),
                                encoding="utf-8")
    except OSError:
        return


def _hides_itself_untracked(directory, lines: set) -> bool:
    """Whether a ``.kilo/.gitignore`` with *lines* can never reach the agent's diff:
    it ignores itself and the checkout does not track it. A `git` that does not
    answer is a no — the file is then left as it is."""
    if ".gitignore" not in lines:
        return False
    ok, out = _git(directory, "ls-files", "--", "/".join(_KILO_IGNORE_PATH),
                   timeout=_KILO_GIT_TIMEOUT)
    return ok and not out


def _drop_kilo_ignore(directory) -> None:
    """The ignore file `set_model_limit` wrote, when the project file goes.

    Only ours goes: one that carries anything besides `_KILO_IGNORE_LINES` is the
    agent's, and an agent's file is never deleted here. An empty ``.kilo/`` goes
    too, whether or not it still held the file — a directory the guard created
    for a patch it has just taken back is the round's, not the agent's.
    """
    try:
        path = _kilo_ignore_path(directory)
        if path.is_file() and \
                {line.strip() for line in path.read_text(encoding="utf-8").splitlines()} \
                == set(_KILO_IGNORE_LINES):
            path.unlink()
        parent = path.parent
        if parent.is_dir():
            parent.rmdir()
    except OSError:
        pass


#: Least time the old tap gets to exit after its stop; a zero join left two taps on one log (round 160).
_OLD_TAP_JOIN_SEC = 2.0


class KiloBackend:
    """:class:`ContestBackend` backed by a Kilo server — today's default.

    One ``KiloClient`` and one ``EventTap`` per backend, per agent session.
    Every method delegates as is; nothing about the kilo round changes. The
    tap is owned here, which is what lets ``run_round`` stop reaching into it:
    the per-agent ``tap.stop()``/``tap.join(2.0)`` used to sit in
    ``run_round``'s ``work()`` ``finally`` and now sits in :meth:`close`.
    """

    def __init__(self, server: KiloServer, directory: str, *,
                 events_log: str | None = None, client: KiloClient | None = None,
                 tap: EventTap | None = None) -> None:
        if client is None:
            client = KiloClient(server, directory)
        tap_args = None
        if tap is None:
            base = getattr(server, "base_url", None)
            if not isinstance(base, str) or not base:
                raise ContestBackendError(
                    f"server has no base_url: {type(server).__name__!r}")
            if not events_log:
                raise ContestBackendError("KiloBackend needs an events_log path for its tap")
            tap = EventTap(base, directory, events_log).start()
            # Arguments to rebuild this backend's tap after a reconnect; set only when it built the tap (round 145, round 151).
            tap_args = (base, directory, events_log)
        self._client = client
        self._tap = tap
        self._directory = directory
        self._tap_args = tap_args
        self._interrupted = False
        # Held by the tap reconnect and by interrupt, so two readers never share a stream (round 151).
        self._tap_lock = threading.Lock()

    # ── the protocol ────────────────────────────────────────────────────────

    def wait_ready(self) -> None:
        _wait_for_stream(self._tap)

    def create_session(self, provider_id: str, model_id: str, *,
                       rules: list, title: str,
                       agent: str | None = None,
                       variant: str | None = None) -> SessionRef:
        try:
            return self._client.create_session(provider_id, model_id, rules=rules,
                                               title=title, agent=agent, variant=variant)
        except KiloHttpError as exc:
            raise ContestBackendError(str(exc)) from exc

    def prompt(self, session: SessionRef, text: str) -> None:
        try:
            self._client.prompt(session, text)
        except KiloHttpError as exc:
            raise ContestBackendError(str(exc)) from exc

    def compact(self, session: SessionRef) -> None:
        try:
            self._client.compact(session)
        except KiloHttpError as exc:
            raise ContestBackendError(str(exc)) from exc

    def mark(self) -> int | None:
        return self._tap.mark()

    def set_model_limit(self, provider_id: str, model_id: str, limit: dict) -> None:
        """Round 145: the model's ``limit`` for this workspace, at once (see
        `KiloClient.set_model_limit`). Kilo writes it to ``.kilo/kilo.jsonc`` in
        the workspace, so ``.kilo/`` goes into the checkout's ``info/exclude``
        first — never a file in the agent's tree, its diff or
        its commit.

        Round 148: not when the checkout already **tracks** ``.kilo/``. There
        ``info/exclude`` does nothing — the patch would rewrite a tracked file
        and land in the agent's diff. The push is then refused with a warning
        *and* :class:`KiloLimitRefused`: a silent return would look to the
        caller like a window Kilo now holds, and the runner's in-turn watch —
        the fallback for exactly a window that was not handed over — would stand
        down for a turn Kilo still sizes by its own 131 072. The refusal is
        structural and cannot change between calls, so the caller remembers it.

        Round 153: a 200 is not the answer, either. After the reconnect the
        model is read back from ``GET /provider`` and compared with what was
        sent; a limit the server keeps of its own is
        :class:`KiloLimitKept`, the same "the window never went over" case a
        caller has to remember with its watch still armed.

        Raises `ContestBackendError`, the tracked case as
        `KiloLimitRefused`, the reconnect case — any failure of it, round
        161 — as `KiloTapReconnectError`, the read-back case as
        `KiloLimitKept`; the caller logs and goes on, with its watch still
        armed — or remembered as pushed, in the one case that is this round's
        stream and not its window."""
        tracked = tracked_kilo_files(self._directory)
        if tracked:
            shown = ", ".join(tracked[:3]) + (" …" if len(tracked) > 3 else "")
            _LOG.warning("%s: the workspace tracks %s — the remembered window is "
                         "not handed to Kilo: the patch would land in the agent's diff",
                         self._directory, shown)
            raise KiloLimitRefused(
                f"the workspace tracks {shown} — the patch would rewrite it and land "
                "in the agent's diff")
        _exclude_kilo_dir(self._directory)
        try:
            self._client.set_model_limit(provider_id, model_id, limit)
        except KiloHttpError as exc:
            raise ContestBackendError(str(exc)) from exc
        # After the patch lands, a tap failure is reported as KiloTapReconnectError (round 151).
        with self._tap_lock:
            before = self._tap
        try:
            self._reconnect_tap()
        except KiloTapReconnectError:
            raise
        except Exception as exc:  # noqa: BLE001 — round 161: the window went over
            self._give_up_stale_tap(before, exc)
            raise KiloTapReconnectError(
                f"the event stream for {self._directory} could not be reopened after "
                f"the config reload: {exc}") from exc
        self._readback_limit(provider_id, model_id, limit)

    def _give_up_stale_tap(self, before: EventTap, error: Exception) -> None:
        """Round 161: a reconnect that broke anywhere but `_start_tap`.

        `_start_tap` wraps its own failure, but the old tap's `stop()` or
        `join()` raising (or anything else between the reload and the swap)
        came out of `set_model_limit` raw: the runner read it as a window that
        never went over, re-sent the PATCH — and reloaded Kilo — every turn, and
        the next wait sat out its silence clock, because the reconnect's first
        wait had consumed the old tap's one ``tap.closed``. When nothing new was
        published the old tap is stopped best-effort and that end recorded
        again, as 160 does for the double start failure; a tap already swapped
        in is live and gets no second ``tap.closed``. Never raises.
        """
        with self._tap_lock:
            if self._tap is not before:
                return
        try:
            before.stop()
        except Exception:  # noqa: BLE001 — the tap is given up either way
            pass
        try:
            # memory only: the next caller's wait on it wakes at once
            before._close_with("the event stream could not be reopened after the "
                               f"config reload: {error}")
        except Exception:  # noqa: BLE001
            _LOG.warning("%s: the old event tap could not record its end — the "
                         "next wait on it runs to its silence clock", self._directory)

    def _readback_limit(self, provider_id: str, model_id: str, limit: dict) -> None:
        """Round 153: prove the push took, by reading the model back.

        Live, 7.6.2: ``limit.context`` the server's own config content carries
        outranks a ``PATCH /config``, field by field — the patch answers 200 and
        writes the file, and the next ``GET /provider`` still reports it.
        ``limit.context`` is compared always and ``limit.input`` when the push
        sent one; ``output`` is Kilo's own reserve and stays out of it. Anything
        the read-back reports that is not this limit — a different number, no
        limit, an offer it would not read — is a window that never went over, so
        :class:`KiloLimitKept` keeps the caller's in-turn watch armed.

        A reconnect failure above never reaches here: the stream is this
        round's problem, and the window is not.
        """
        seen = self._client.model_limits(provider_id, model_id)
        want_context = _context_limit_of((limit or {}).get("context"))
        want_input = _context_limit_of((limit or {}).get("input"))
        if seen is None:
            raise KiloLimitKept(
                f"the read-back for {provider_id}/{model_id} did not come back with a "
                "limit — the offer shows none for the model, or it would not answer")
        got_context = _context_limit_of(seen.get("context"))
        got_input = _context_limit_of(seen.get("input"))
        if got_context != want_context or (want_input is not None
                                           and got_input != want_input):
            raise KiloLimitKept(
                f"the read-back reports {got_context:,} for {provider_id}/{model_id} "
                f"where the patch asked for {want_context:,}", kept=got_context)

    def _start_tap(self, base: str, directory: str, log: str) -> EventTap:
        """Round 151: a new `EventTap` on this backend's own log, retried once.

        Both attempts failing raises :class:`KiloTapReconnectError`,
        never a bare `OSError`: the caller is otherwise about to wait on a
        stream nobody reads, and that reads to the runner as a window that never
        went over rather than as a broken stream.
        """
        error: Exception | None = None
        for attempt in (1, 2):
            try:
                return EventTap(base, directory, log).start()
            except Exception as exc:  # noqa: BLE001 — a tap that cannot start is no tap
                error = exc
                _LOG.warning("%s: event tap start attempt %d failed: %s",
                             directory, attempt, exc)
        raise KiloTapReconnectError(
            f"the event stream for {directory} could not be reopened after the "
            f"config reload: {error}") from error

    def _reconnect_tap(self, settle: float = 5.0) -> None:
        """Round 145: the workspace's event stream after a config reload.

        Live, 7.6.2: a `PATCH /config` disposes the directory's instance and the
        `/event` stream for it ends (``tap.closed: stream ended``); the old tap
        never hears another event, so every later wait sits out its silence
        clock (live: a 34-second compact waited 600 s). The old tap is given up
        to *settle* seconds to see its end, stopped, and a new one is started
        on the same log and waits for ``server.connected``. Marks are per tap:
        the caller takes its next mark after this returns. A tap this backend
        did not build (a test's) is left alone.

        Round 151: a new tap that cannot be started is retried once and then
        raises :class:`KiloTapReconnectError`. Every wait shares the one
        deadline of *settle* seconds and logs its timeout, so a reload that does
        not end the stream cannot hold the round for three times *settle*.
        `interrupt` holds the same lock: a Ctrl-C mid-swap stops the tap being
        replaced, and a swap that publishes into an already-interrupted backend
        stops its own tap straight away. Round 159: that lock is held only to
        read and to publish `self._tap`, never across a wait or a start.

        Round 160: the old tap is stopped and joined **before** the new one
        starts — one writer on ``events.jsonl`` at a time. 151 started the new
        tap first, and a stream the reload did not end (or a join cut to zero by
        a spent deadline) left both taps appending the same events to the log
        the legs' pytest report and the idle watch read back. Nothing is lost by
        the order: after a reload Kilo has ended the old stream already, so the
        old tap was never a live fallback. The join gets at least
        `_OLD_TAP_JOIN_SEC`, since `stop` shuts the socket and the reader is gone
        within it; one that still runs is logged.
        """
        if self._tap_args is None:
            return
        deadline = time.monotonic() + max(0.0, float(settle))
        base, directory, log = self._tap_args
        # The lock covers only reading and publishing the tap, so an interrupt never waits on a reload (round 159).
        with self._tap_lock:
            old = self._tap
            if self._interrupted:
                # A Ctrl-C before the reconnect opens no new stream for a backend being given up.
                old.stop()
                return
        left = max(0.0, deadline - time.monotonic())
        saw_end = False
        try:
            saw_end = old.wait(lambda e: e.get("type") == "tap.closed", left) is not None
        except Exception:  # noqa: BLE001 — a tap that cannot wait is stopped anyway
            pass
        if not saw_end:
            _LOG.warning("%s: the old event stream did not end within %.0fs of the "
                         "config reload — stopping it", directory, settle)
        old.stop()
        if not old.join(max(_OLD_TAP_JOIN_SEC, deadline - time.monotonic())):
            _LOG.warning("%s: the old event tap is still running %.0fs after its stop "
                         "— the new tap shares %s with it", directory,
                         _OLD_TAP_JOIN_SEC, log)
        try:
            new_tap = self._start_tap(base, directory, log)
        except KiloTapReconnectError:
            # Re-record the closed tap so the next wait on it still wakes at once (round 151).
            old._close_with("the event stream could not be reopened after "
                            "the config reload")
            raise
        with self._tap_lock:
            self._tap = new_tap
            interrupted = self._interrupted
            if interrupted:
                # An interrupt during the start stopped the old tap, so the new one is stopped here too.
                new_tap.stop()
        if interrupted:
            # stopped above: when this returns, no tap thread of ours runs on
            new_tap.join(_OLD_TAP_JOIN_SEC)
            return
        left = max(0.0, deadline - time.monotonic())
        connected = False
        try:
            seen = new_tap.wait(lambda e: e.get("type") in ("server.connected", "tap.closed"),
                                left)
        except Exception:  # noqa: BLE001 — the stream is open; the event is a courtesy
            seen = None
        if seen is not None and seen.get("type") == "tap.closed":
            # An interrupt during this wait consumed the close signal, so record it again.
            new_tap._close_with((seen.get("properties") or {}).get("error") or "stopped")
            return
        connected = seen is not None
        if not connected:
            _LOG.warning("%s: the reconnected event stream did not report "
                         "server.connected within %.0fs", directory, settle)

    def abort(self, session: SessionRef) -> None:
        try:
            self._client.abort(session)
        except KiloHttpError as exc:
            _LOG.warning("abort(%r) failed: %s", getattr(session, "id", session), exc)

    def interrupt(self, session: SessionRef | None = None) -> None:
        # The same lock as _reconnect_tap, so the tap being stopped is the one that is or will be published (round 151).
        with self._tap_lock:
            self._interrupted = True
            self._tap.stop()

    def interrupted(self) -> bool:
        return self._interrupted

    def wait_idle(self, session: SessionRef, timeout: float, *,
                  idle_event_timeout: float | None = None,
                  on_permission: Callable[[dict], tuple],
                  on_question: Callable[[dict], None],
                  on_deadline: Callable[[float], float | None] | None = None,
                  since: int | None = None,
                  max_retry_wait: float | None = None,
                  quota_re: "re.Pattern | None" = None,
                  max_retry_attempts: int | None = None,
                  quiet_after: float | None = None) -> IdleResult:
        # The callback is sent only when armed, so an older client gets the same call (KC-36).
        client_kwargs = {"idle_event_timeout": idle_event_timeout,
                         "on_permission": on_permission, "on_question": on_question}
        if on_deadline is not None:
            client_kwargs["on_deadline"] = on_deadline
        if since is not None:
            client_kwargs["since"] = since
        # The retry bound and quota phrases are sent only when armed, for older clients (KC-61).
        if max_retry_wait is not None:
            client_kwargs["max_retry_wait"] = max_retry_wait
        if quota_re is not None:
            client_kwargs["quota_re"] = quota_re
        # A limit of 0 is off and is not sent, so an older client gets the same call (KC-64).
        if max_retry_attempts:
            client_kwargs["max_retry_attempts"] = max_retry_attempts
        # KC-73: the same — armed only after an overflow, never on a turn
        if quiet_after is not None:
            client_kwargs["quiet_after"] = quiet_after
        return self._client.wait_idle(self._tap, session, timeout, **client_kwargs)

    def tool_parts(self, session: SessionRef) -> list:
        return self._client.tool_parts(session)

    def session_info(self, session: SessionRef) -> dict:
        return self._client.session_info(session)

    def messages(self, session: SessionRef) -> list:
        return self._client.messages(session)

    def close(self) -> None:
        with self._tap_lock:
            self._tap.stop()
            self._tap.join(2.0)


#: Seconds to wait for a tap's reader to open its stream; an early event would be lost (FL-1).
STREAM_CONNECT_TIMEOUT_S = 30.0


def _wait_for_stream(tap: EventTap, timeout: float = STREAM_CONNECT_TIMEOUT_S) -> bool:
    """Give the tap's reader time to connect; True when the stream is open.

    Returns False when the wait ran out — the caller is then about to publish
    into a stream nobody is reading, which is worth saying out loud. It used to
    return `None` either way, so a tap that never connected looked exactly like
    one that connected instantly, and the loss surfaced much later as "the
    model never answered".

    `hello_probe` is the sharpest case: a probe whose events are dropped reads
    as "this variant did not answer", so an agent silently runs at a lower
    reasoning variant than it could. Nothing raises here — a tap that is slow
    to connect is not a reason to fail a round — but it no longer passes
    unremarked.
    """
    deadline = time.monotonic() + timeout
    while getattr(tap, "_socket", None) is None and time.monotonic() < deadline:
        if tap.join(0.02):
            return False  # the reader already ended: the wait will see tap.closed
    connected = getattr(tap, "_socket", None) is not None
    if not connected:
        _LOG.warning(
            "event stream for %s did not open within %.0fs — events published "
            "now are lost, and a turn that emits into it reads as silent",
            getattr(tap, "directory", "?"), timeout,
        )
    return connected


# OpenRouter agent loop (a subprocess)

#: Queue item for the end of the agent's stdout; None is reserved for "nothing in time".
_EOF = object()


class _AgentRecord:
    """One subprocess agent: the process, its two pipes, its transcript."""

    def __init__(self, directory: str, session_id: str, provider_id: str,
                 model_id: str, agent: str | None,
                 proc: subprocess.Popen | None, stdin, stdout) -> None:
        self.proc = proc
        self.stdin = stdin
        self.stdout = stdout
        self.directory = directory
        self.session_id = session_id
        self.provider_id = provider_id
        self.model_id = model_id
        self.agent = agent
        self.parts: list = []
        self.messages: list = []
        self.input_tokens = 0
        self.output_tokens = 0
        # One line per agent stdout line, or None at exit; a queue, so wait_idle can time out.
        self.lines: "queue.Queue" = queue.Queue()
        # AUDIT3-9b: the last lines the agent wrote to stderr, kept for diagnosis.
        self.stderr_tail: "collections.deque" = collections.deque(maxlen=200)
        self.reader: "threading.Thread | None" = None


class OpenRouterBackend:
    """:class:`ContestBackend` that drives a model through an
    OpenAI-compatible ``/chat/completions`` endpoint, inside a subprocess.

    ``api_key``, ``base_url`` and the per-agent model are the three fields
    ``[contest_openrouter_llm]`` / ``AgentSpec`` supply; the model id is not a
    constructor argument, because a backend may serve more than one agent in a
    round and :meth:`create_session` is what carries the agent's own model.

    The agent loop is ``python3 -m tools.contest.backend --agent-loop`` — this
    file's own tail. It keeps the conversation, runs ``bash``/``read``/``write``
    tool calls in *directory* (the worktree), and speaks JSON lines:

        stdin  → {"type": "prompt", "text": …}
                 {"type": "permission.reply", "id": …, "reply": "once"|"reject"}
        stdout ← {"type": "permission.asked", "properties": {…}}
                 {"type": "tool", "state": {…}}
                 {"type": "message", "parts": […]}
                 {"type": "usage", "input": n, "output": m}
                 {"type": "error", "data": {…}}
                 {"type": "idle"}

    ``rules`` from :meth:`create_session` are accepted and ignored: the loop
    has no server to enforce them against, and the gate that actually decides
    anything is the runner's ``on_permission`` closure, which this backend
    feeds the same ``permission.asked`` shape a Kilo session would emit.

    One reader thread per agent pumps its stdout into a queue, so
    :meth:`wait_idle` can time out while a line is still on the wire: a
    blocking ``readline()`` would sit past ``timeout`` waiting for a silent
    agent.

    *spawn_agent* replaces the ``Popen`` — tests pass a fake so nothing real is
    ever spawned.
    """

    def __init__(self, api_key: str, base_url: str, directory: str, *,
                 timeout: float = 300.0, max_steps: int = 24,
                 spawn_agent=None, extra_env: dict | None = None) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ContestBackendError("OpenRouterBackend needs a base_url")
        self._api_key = api_key if isinstance(api_key, str) else ""
        self._base_url = base_url.rstrip("/")
        self._directory = os.path.abspath(str(directory))
        self._timeout = float(timeout)
        self._max_steps = int(max_steps)
        self._spawn_agent = spawn_agent
        # Round environment, applied after os.environ so pytest sees the worker count (KC-65).
        self._extra_env = dict(extra_env) if extra_env else None
        self._records: dict = {}
        self._interrupted = False

    # ── the protocol ────────────────────────────────────────────────────────

    def wait_ready(self) -> None:
        return None

    def create_session(self, provider_id: str, model_id: str, *,
                       rules: list, title: str,
                       agent: str | None = None,
                       variant: str | None = None) -> SessionRef:
        # AUDIT-18: the variant reaches the model too, as OpenRouter's
        # ``reasoning.effort`` (see `_reasoning_effort`); it used to be kept on
        # the ref only, so the round record named a variant the run never used.
        sid = f"openrouter-{model_id or title or 'agent'}"
        record = self._spawn_one(sid, provider_id, model_id, agent, variant=variant or None)
        self._records[sid] = record
        return SessionRef(id=sid, provider_id=provider_id, model_id=model_id,
                          directory=self._directory, agent=agent, variant=variant or None)

    def prompt(self, session: SessionRef, text: str) -> None:
        record = self._record(session)
        self._write(record, {"type": "prompt", "text": text})
        record.messages.append({"info": {"role": "user",
                                         "sessionID": session.id,
                                         "time": time.time()},
                                "parts": [{"type": "text", "text": text}]})

    def compact(self, session: SessionRef) -> None:
        # No compaction: the subprocess agent keeps its whole transcript and reports no token fill (KC-67).
        return None

    def mark(self) -> int | None:
        # Each session reads its own stdout, so no other turn's events reach a wait (KC-63).
        return None

    def abort(self, session: SessionRef) -> None:
        self._terminate(self._record(session))

    def interrupt(self, session: SessionRef | None = None) -> None:
        self._interrupted = True
        if session is None:
            for record in self._records.values():
                self._terminate(record)
            return
        self._terminate(self._record(session))

    def interrupted(self) -> bool:
        return self._interrupted

    def wait_idle(self, session: SessionRef, timeout: float, *,
                  idle_event_timeout: float | None = None,
                  on_permission: Callable[[dict], tuple],
                  on_question: Callable[[dict], None],
                  on_deadline: Callable[[float], float | None] | None = None,
                  since: int | None = None,
                  max_retry_wait: float | None = None,
                  quota_re: "re.Pattern | None" = None,
                  max_retry_attempts: int | None = None,
                  quiet_after: float | None = None) -> IdleResult:
        """Read the agent's stdout until ``idle``, the timeout, or the timeout's
        silence window.

        ``on_permission`` decides every ``permission.asked`` the agent emits for
        a ``bash`` call — the gate runs here, the same as on a Kilo session.
        ``on_question`` is never called: a bare ``/chat/completions`` has no
        question event, so there is nothing to observe (the runner's
        ``max_questions_per_turn`` edge has no counterpart on this backend).

        ``on_deadline`` (KC-36) is asked the same way on a Kilo session: at the
        turn deadline, before ``abort``, with the elapsed seconds — a positive
        return pushes it back, anything else aborts. The worktree churn that
        earns the extension is the round's, and it is this backend's too.
        ``since`` (KC-63) is accepted and ignored: :meth:`mark` is ``None``.
        ``max_retry_wait`` and ``quota_re`` (KC-61) are accepted and ignored too:
        this backend has no Kilo retry status to judge, and a provider 429
        reaches the runner as a plain ``session.error``.
        ``max_retry_attempts`` (KC-64) is ignored for the same reason: this
        backend has no Kilo retry counter, so a provider that keeps failing
        reaches the runner as a ``session.error`` and keeps KC-19's retry path.
        ``quiet_after`` (KC-73) is ignored: the runner never arms it here, since
        :meth:`mark` is ``None`` and a finished subprocess turn has nothing left
        running.

        A provider error comes back as ``IdleResult(status="error")`` with the
        agent's payload, ``data.isRetryable`` set for a 429 or a 5xx, so the
        runner's existing retry decides. The loop's own crash is ``"closed"`` —
        the session is gone, the worktree is not.
        """
        started = time.monotonic()
        deadline = started + max(0.0, float(timeout))
        silence = float(idle_event_timeout) if idle_event_timeout is not None else None
        if silence is not None and silence <= 0:
            silence = None
        last_seen = started
        permissions: list = []
        questions: list = []
        record = self._record(session)

        while True:
            now = time.monotonic()
            overall_left = deadline - now
            if silence is not None:
                left = min(overall_left, silence - (now - last_seen))
            else:
                left = overall_left
            if left <= 0:
                # The turn deadline asks first; the silence clock never does (KC-36).
                quiet = silence is not None and (silence - (now - last_seen)) <= 0
                if not quiet and overall_left <= 0 and on_deadline is not None:
                    grant = _deadline_grant(on_deadline, time.monotonic() - started)
                    # A grant that leaves the deadline in the past aborts, rather than re-asking in a hot loop.
                    if grant is not None and deadline + grant > time.monotonic():
                        deadline += grant
                        continue
                self.abort(session)
                return IdleResult(status="timeout", elapsed=time.monotonic() - started,
                                  permissions=permissions, questions=questions)

            line = self._next_line(record, left)
            if line is None:
                continue  # quiet for a while; the deadline decides, not a sleep
            if line == "":
                # EOF: the agent died without saying idle. The worktree stands.
                code = None if record.proc is None else record.proc.poll()
                self._terminate(record)
                return IdleResult(
                    status="closed",
                    error=f"agent stream ended (exit {code})",
                    elapsed=time.monotonic() - started,
                    permissions=permissions, questions=questions)

            last_seen = time.monotonic()
            event = _loads(line)
            if event is None:
                continue
            kind = event.get("type")
            if kind == "permission.asked":
                try:
                    reply, message, granted = _permission_answer(on_permission(event))
                except Exception as exc:  # noqa: BLE001 — a broken policy is a reject
                    reply, message = "reject", f"policy failed: {type(exc).__name__}"
                    granted = 0.0
                if granted > 0:
                    # The gate's waits and the round-wide suite waits are given back to the turn (KC-66, KC-58).
                    deadline += granted
                # KC-58: never silent while the runner held the permission
                last_seen = time.monotonic()
                props = event.get("properties") or {}
                self._write(record, {"type": "permission.reply", "id": props.get("id"),
                                     "reply": reply, "message": message})
                permissions.append(event)
                continue
            if kind == "question.asked":
                questions.append(event)
                on_question(event)
                continue
            if kind == "tool":
                record.parts.append(event)
                continue
            if kind == "message":
                parts = event.get("parts") if isinstance(event.get("parts"), list) else []
                record.messages.append({"info": {"role": "assistant",
                                                 "sessionID": session.id,
                                                 "time": time.time()},
                                        "parts": parts})
                continue
            if kind == "usage":
                record.input_tokens += int(event.get("input") or 0)
                record.output_tokens += int(event.get("output") or 0)
                continue
            if kind == "error":
                return IdleResult(status="error", error=event.get("data"),
                                  elapsed=time.monotonic() - started,
                                  permissions=permissions, questions=questions)
            if kind == IDLE_SENTINEL:
                return IdleResult(status="idle", elapsed=time.monotonic() - started,
                                  permissions=permissions, questions=questions)

    def tool_parts(self, session: SessionRef) -> list:
        return list(self._record(session).parts)

    def session_info(self, session: SessionRef) -> dict:
        record = self._record(session)
        total = record.input_tokens + record.output_tokens
        return {"cost": 0.0,
                "tokens": {"total": total,
                           "input": record.input_tokens,
                           "output": record.output_tokens}}

    def messages(self, session: SessionRef) -> list:
        return list(self._record(session).messages)

    def close(self) -> None:
        for record in self._records.values():
            self._terminate(record)
        self._records.clear()

    # ── internals ───────────────────────────────────────────────────────────

    def _record(self, session: SessionRef) -> _AgentRecord:
        try:
            return self._records[session.id]
        except KeyError as exc:
            raise ContestBackendError(
                f"no session {session.id!r} on this backend") from exc

    def _spawn_one(self, sid: str, provider_id: str, model_id: str,
                   agent: str | None, *, variant: str | None = None) -> _AgentRecord:
        if self._spawn_agent is not None:
            spawned = self._spawn_agent(self._base_url, model_id, self._directory,
                                        self._api_key, self._timeout, self._max_steps)
            proc = getattr(spawned, "proc", None)
            if proc is None and hasattr(spawned, "poll"):
                # a fake that is the process itself, not a holder of one
                proc = spawned
            record = _AgentRecord(self._directory, sid, provider_id, model_id, agent,
                                  proc,
                                  getattr(spawned, "stdin", None),
                                  getattr(spawned, "stdout", None))
            thread = threading.Thread(target=self._read_thread, args=(record,), daemon=True,
                                      name="contest-agent-read")
            record.reader = thread
            thread.start()
            return record
        argv = [sys.executable, "-m", AGENT_LOOP_MODULE, "--agent-loop",
                "--base-url", self._base_url, "--model", str(model_id),
                "--directory", self._directory, "--max-steps", str(self._max_steps),
                "--request-timeout", f"{self._timeout:g}"]
        if variant:
            argv += ["--variant", str(variant)]
        env = dict(os.environ)
        if self._extra_env:
            env.update(self._extra_env)
        env["CONTEST_AGENT_API_KEY"] = self._api_key
        env[_AGENT_BASH_TIMEOUT_ENV] = f"{self._timeout:g}"
        proc = subprocess.Popen(argv, cwd=self._directory, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.PIPE, text=True, bufsize=1)
        record = _AgentRecord(self._directory, sid, provider_id, model_id, agent,
                              proc, proc.stdin, proc.stdout)
        thread = threading.Thread(target=self._read_thread, args=(record,), daemon=True,
                                  name="contest-agent-read")
        record.reader = thread
        thread.start()
        # AUDIT3-9b: stderr is a pipe too, and nothing read it: once the agent
        # had written a pipe's worth (~64 KiB of warnings or a traceback) its
        # next write blocked forever and the turn hung until the silence clock.
        threading.Thread(target=self._drain_stderr, args=(record, getattr(proc, "stderr", None)),
                         daemon=True, name="contest-agent-stderr").start()
        return record

    @staticmethod
    def _drain_stderr(record: _AgentRecord, stream) -> None:
        """Read the agent's stderr to its end, keeping the last lines."""
        if stream is None:
            return
        try:
            for line in stream:
                record.stderr_tail.append(line.rstrip("\r\n"))
        except (OSError, ValueError):
            pass

    @staticmethod
    def _write(record: _AgentRecord, payload: dict) -> None:
        stream = record.stdin
        if stream is None:
            return
        try:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stream.flush()
        except (OSError, ValueError, AttributeError):
            pass  # the agent is gone; the wait will see the EOF

    @staticmethod
    def _next_line(record: _AgentRecord, timeout: float):
        """One line from the agent's stdout, or ``None`` on a deadline.

        ``""`` means the process is gone. ``None`` means nothing came in time
        — ``wait_idle`` rechecks the deadline on the next iteration, which is
        what keeps a silent agent from holding a turn past ``timeout``. The
        queue carries ``_EOF`` for the end, not ``None``, so those two meanings
        do not collide: ``wait_idle`` must tell "quiet" from "gone".
        """
        try:
            item = record.lines.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        return "" if item is _EOF else item

    @staticmethod
    def _read_thread(record: _AgentRecord) -> None:
        """Pump the agent's stdout into its queue, one line at a time.

        A daemon thread: the pipe closes when ``terminate`` ends the process,
        ``readline()`` then returns ``""`` and this appends ``_EOF``, so a
        waiter in ``wait_idle`` wakes on an EOF instead of running its own
        deadline. ``stderr`` is left alone — the agent writes its protocol to
        stdout only, and a stderr dump is not part of the wire.
        """
        stream = record.stdout
        try:
            while True:
                line = stream.readline()
                if line == "":
                    break
                record.lines.put(line.rstrip("\r\n"))
        except (OSError, ValueError, AttributeError):
            pass
        finally:
            record.lines.put(_EOF)

    def _terminate(self, record: _AgentRecord) -> None:
        proc = record.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except (OSError, ValueError):
            return
        deadline = time.monotonic() + 5.0
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc.poll() is None:
            try:
                proc.kill()
            except (OSError, ValueError):
                pass


# The agent loop run by the OpenRouter subprocess

_AGENT_SYSTEM = """\
You implement one ticket in this directory. It is the whole of your working
tree; anything outside it is refused by a reviewer.

Work until you are done: read the ticket it points at, change the code it
names, add the test it asks for, commit once, and record it. Then say what you
did and stop.

Rules that are checked mechanically, so do not waste turns on them:

- Never `git push`. Never leave the working tree.
- One commit, on top of the tree you were given.
- A test ships with the change.
- Do not touch `epic-tasks/`.
- `CollectBridge._shrink` must be byte-identical when you finish.

A rejected command is final for that command: do not retry it, change tack.
"""

#: One bash call the agent loop asks about, in the shape policy.py already reads.
_BASH_PERMISSION = "bash"


def _bash_timeout() -> float:
    """The per-command budget, from the environment, else the default."""
    raw = os.environ.get(_AGENT_BASH_TIMEOUT_ENV, "")
    try:
        value = float(raw) if raw else _AGENT_BASH_TIMEOUT
    except ValueError:
        value = _AGENT_BASH_TIMEOUT
    return max(1.0, value)


def _agent_tools() -> list:
    """The three tools the loop offers, in the shape /chat/completions wants."""
    def schema(props: dict, required: list) -> dict:
        return {"type": "object", "properties": props, "required": required}

    return [
        {"type": "function", "function": {
            "name": "bash",
            "description": "Run one shell command in the working tree. Each call "
                           "is decided by a reviewer before it runs.",
            "parameters": schema({"command": {"type": "string",
                                              "description": "the command, one line"}},
                                 ["command"])}},
        {"type": "function", "function": {
            "name": "read",
            "description": "Read one file of the working tree, by relative path.",
            "parameters": schema({"path": {"type": "string"}}, ["path"])}},
        {"type": "function", "function": {
            "name": "write",
            "description": "Write one file of the working tree, by relative path. "
                           "Creates parent directories.",
            "parameters": schema({"path": {"type": "string"},
                                  "content": {"type": "string"}}, ["path", "content"])}},
    ]


@contextlib.contextmanager
def _sigint_held():
    """Hold a SIGINT that arrives inside the block; yields a list, non-empty if one did.

    The caller re-raises it (``signal.raise_signal``) once it can clean up, so the
    handler that was in place still runs, just later. Off the main thread, where
    no handler can be set, or with SIGINT ignored, nothing is held.
    """
    held: list = []
    if threading.current_thread() is not threading.main_thread():
        yield held
        return
    previous = signal.getsignal(signal.SIGINT)
    if previous in (signal.SIG_IGN, None):
        yield held
        return
    signal.signal(signal.SIGINT, lambda signum, frame: held.append(signum))
    try:
        yield held
    finally:
        signal.signal(signal.SIGINT, previous)


def _run_bash_group(command: str, cwd: str, timeout: float) -> tuple:
    """``bash -c command`` in its own process group: ``(returncode, stdout, stderr)``.

    AUDIT-6: ``subprocess.run(..., capture_output=True, timeout=...)`` kills
    only ``bash`` on a timeout and then waits for the pipes to close — and a
    background child (``sleep 999 &``, a dev server) still holds them, so the
    "timeout" waited as long as that child lived; with no timeout hit, a child
    that kept the pipes open held the turn just the same. Output now goes to
    temporary files, which nobody waits on, and the call returns when ``bash``
    exits. On a timeout the whole process group is killed, then
    ``TimeoutExpired`` is raised. A child that ``bash`` left running after a
    normal exit is left alone, as before (a server the agent started on
    purpose); it just no longer blocks the call.
    """
    import tempfile

    posix = os.name != "nt"
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        proc = None
        try:
            # CTRL-C-START: bash may already run while Popen has not returned (or
            # `try:` is not reached yet); a Ctrl-C there escaped the kill below and
            # left the command running. It is held until `proc` is known.
            with _sigint_held() as held:
                proc = subprocess.Popen(["bash", "-c", command], cwd=cwd,
                                        stdout=out_f, stderr=err_f,
                                        stdin=subprocess.DEVNULL,
                                        start_new_session=posix)
            if held:
                signal.raise_signal(signal.SIGINT)
            proc.wait(timeout=timeout)
        except BaseException:
            # CC review fixes 49: not only a timeout — a Ctrl-C (KeyboardInterrupt)
            # or any other exit of the wait kills the group too. In its own session
            # the command no longer gets the terminal's SIGINT, so it kept running
            # after the round was stopped, with no timeout left to end it.
            if proc is None:
                raise
            try:
                if posix:
                    os.killpg(proc.pid, 9)
                else:
                    proc.kill()
            except OSError:
                pass
            proc.wait()
            raise
        out_f.seek(0)
        err_f.seek(0)
        out = out_f.read().decode("utf-8", "replace")
        err = err_f.read().decode("utf-8", "replace")
    return proc.returncode, out, err


#: Kilo-style variant names → OpenRouter ``reasoning.effort`` (AUDIT-18).
_EFFORT_OF_VARIANT = {"minimal": "minimal", "low": "low", "medium": "medium",
                      "high": "high", "xhigh": "xhigh", "max": "xhigh"}


def _reasoning_effort(variant) -> "str | None":
    """The ``reasoning.effort`` a session's *variant* asks for, or None.

    ``None`` / empty / ``highest`` (resolved away before an openrouter round
    starts) send nothing, as before. A name this table does not know is not
    guessed at: it is reported on stderr and nothing is sent, so the round's
    log says the variant was not applied rather than the run silently using
    the provider default.
    """
    if not isinstance(variant, str) or not variant.strip():
        return None
    name = variant.strip().lower()
    if name == "highest":
        return None
    effort = _EFFORT_OF_VARIANT.get(name)
    if effort is None:
        print(f"contest agent: reasoning variant {variant!r} has no OpenRouter "
              f"reasoning.effort — the provider default is used", file=sys.stderr)
    return effort


class _Agent:
    """The loop: read a prompt, call the model, run its tool calls, say ``idle``."""

    def __init__(self, base_url: str, api_key: str, model: str, directory: str, *,
                 max_steps: int = 24, request_timeout: float = 300.0,
                 variant: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._effort = _reasoning_effort(variant)
        self._directory = directory
        self._max_steps = max(1, int(max_steps))
        self._timeout = float(request_timeout)
        self._messages: list = [{"role": "system", "content": _AGENT_SYSTEM}]

    # ── the wire ────────────────────────────────────────────────────────────

    def _say(self, payload: dict) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _ask(self) -> dict | None:
        """One line from stdin, or None at EOF (the runner went away)."""
        try:
            line = sys.stdin.readline()
        except (OSError, ValueError):
            return None
        if line == "":
            return None
        return _loads(line)

    def _say_permission(self, call_id: str, name: str, args: dict) -> tuple:
        """Ask the runner about one tool call; return ``(reply, message)``.

        ``permission`` is ``bash`` for a bash call and ``name`` for a file
        call, with the path in ``patterns`` — the same three sources
        ``policy._extract_paths`` reads, so the mechanical layer decides a
        write inside the worktree on its own and never costs a gate call.
        """
        pid = f"{call_id}-{name}"
        if name == "bash":
            props = {"id": pid, "sessionID": self._model,
                     "permission": _BASH_PERMISSION, "patterns": [], "always": [],
                     "metadata": {"command": str(args.get("command") or "")},
                     "tool": {"messageID": call_id, "callID": call_id}}
        else:
            path = str(args.get("path") or "")
            props = {"id": pid, "sessionID": self._model, "permission": name,
                     "patterns": [path], "always": [path],
                     "metadata": {"directories": [path] if path else []},
                     "tool": {"messageID": call_id, "callID": call_id}}
        self._say({"type": "permission.asked", "properties": props})
        reply, message = "reject", "no reply from the runner"
        while True:
            line = self._ask()
            if line is None:
                return "reject", "the runner went away"
            if line.get("type") == "permission.reply":
                reply = line.get("reply") if line.get("reply") in ("once", "reject") else "reject"
                message = str(line.get("message") or "")
                break
            if line.get("type") == "abort":
                return "reject", "aborted"
        return reply, message

    # ── the model ───────────────────────────────────────────────────────────

    def _call(self) -> tuple | None:
        """One /chat/completions call: ``(assistant message, usage)`` or None."""
        import urllib.request

        url = self._base_url + "/chat/completions"
        payload = {"model": self._model, "messages": self._messages,
                   "tools": _agent_tools(), "tool_choice": "auto",
                   "temperature": 0.2, "max_tokens": 4096}
        if self._effort:
            payload["reasoning"] = {"effort": self._effort}
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 — a failed call is a turn, not a crash
            status = getattr(exc, "code", None)
            retryable = isinstance(status, int) and (status == 429 or 500 <= status < 600)
            text = f"{type(exc).__name__}: {exc}"
            self._say({"type": "error",
                       "data": {"name": "AgentError", "message": text,
                                "isRetryable": retryable,
                                "metadata": {"code": str(status or "0"),
                                             "status": str(status or 0)}}})
            return None
        if not isinstance(body, dict):
            self._say({"type": "error",
                       "data": {"name": "AgentError",
                                "message": f"reply is not an object: {type(body).__name__}",
                                "isRetryable": False}})
            return None
        choices = body.get("choices") if isinstance(body.get("choices"), list) else []
        message = (choices[0].get("message") if choices and isinstance(choices[0], dict)
                   else None)
        if not isinstance(message, dict):
            self._say({"type": "error",
                       "data": {"name": "AgentError",
                                "message": "reply has no choices[0].message",
                                "isRetryable": False}})
            return None
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return message, {"input": int(usage.get("prompt_tokens") or 0),
                         "output": int(usage.get("completion_tokens") or 0)}

    # ── the tools ───────────────────────────────────────────────────────────

    def _tool_result(self, name: str, call_id: str, args: dict) -> tuple:
        """One tool call: ``(status, output text)``. Never raises."""
        if name == "bash":
            command = str(args.get("command") or "")
            if not command:
                return "error", "bash: no command"
            reply, message = self._say_permission(call_id, "bash", args)
            if reply != "once":
                return "error", (message or "the reviewer refused this command")
            try:
                returncode, out, err = _run_bash_group(command, self._directory,
                                                       _bash_timeout())
            except subprocess.TimeoutExpired:
                return "error", f"timed out after {_bash_timeout():g}s: {command}"
            except (OSError, ValueError) as exc:
                return "error", f"{type(exc).__name__}: {exc}"
            text = out or ""
            if err:
                text += ("\n" if text else "") + "stderr:\n" + err
            if returncode:
                text = f"exit {returncode}\n" + text.strip()
            return ("completed" if returncode == 0 else "error"), text

        path = str(args.get("path") or "")
        if not path or path.startswith("/") or ".." in path.split("/"):
            return "error", f"{name}: path must be inside the working tree: {path!r}"
        reply, message = self._say_permission(call_id, name, args)
        if reply != "once":
            return "error", (message or "the reviewer refused this path")
        target = os.path.join(self._directory, path)
        try:
            if name == "write":
                parent = os.path.dirname(target)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(target, "w", encoding="utf-8") as fh:
                    fh.write(str(args.get("content") or ""))
                return "completed", f"wrote {path}"
            if name == "read":
                with open(target, "r", encoding="utf-8", errors="replace") as fh:
                    return "completed", fh.read()
        except (OSError, ValueError) as exc:
            return "error", f"{type(exc).__name__}: {exc}"
        return "error", f"unknown tool {name!r}"

    # ── the loop ────────────────────────────────────────────────────────────

    def run(self) -> int:
        """Read prompts from stdin forever; say ``idle`` after each turn."""
        while True:
            line = self._ask()
            if line is None:
                return 0
            kind = line.get("type")
            if kind == "prompt":
                self._messages.append({"role": "user",
                                       "content": str(line.get("text") or "")})
                if not self._turn():
                    return 0
            elif kind == "abort":
                return 0

    def _turn(self) -> bool:
        """One prompt: model calls until there is nothing left to run."""
        for step in range(self._max_steps):
            result = self._call()
            if result is None:
                return False
            message, usage = result
            self._messages.append(dict(message))
            self._say({"type": "usage", **usage})
            if message.get("role"):
                self._say({"type": "message", "parts": [
                    {"type": "text", "text": str(message.get("content") or "")}]})
            calls = message.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                self._say({"type": IDLE_SENTINEL})
                return True
            for index, call in enumerate(calls):
                function = call.get("function") if isinstance(call, dict) else None
                if not isinstance(function, dict):
                    continue
                name = str(function.get("name") or "")
                try:
                    args = json.loads(function.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except (json.JSONDecodeError, ValueError):
                    args = {}
                call_id = str(call.get("id") or f"{step}.{index}")
                status, output = self._tool_result(name, call_id, args)
                self._say({"type": "tool", "tool": name,
                           "state": {"status": status, "input": args,
                                     "output": str(output)}})
                self._messages.append({"role": "tool", "tool_call_id": call_id,
                                       "content": str(output)})
        self._say({"type": "error",
                   "data": {"name": "AgentError",
                            "message": f"no idle after {self._max_steps} steps",
                            "isRetryable": False}})
        return True


def _loads(line) -> dict | None:
    """One JSON line as a dict, else None — a malformed line is not a turn."""
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def agent_loop(argv=None) -> int:
    """``python3 -m tools.contest.backend --agent-loop …`` — the subprocess."""
    parser = argparse.ArgumentParser(prog="tools.contest.backend --agent-loop")
    parser.add_argument("--agent-loop", action="store_true")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--request-timeout", type=float, default=300.0)
    parser.add_argument("--variant", default=None)
    args = parser.parse_args(argv)
    _Agent(args.base_url, os.environ.get("CONTEST_AGENT_API_KEY", ""), args.model,
           args.directory, max_steps=args.max_steps,
           request_timeout=args.request_timeout, variant=args.variant).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(agent_loop())
