"""tools/contest/runner.py — KC-6: prompt → wait → harvest → rework, in the same
session, for N agents at once, resumable.

`scripts/kilo_hello.py --append-model` (PROBE.md §4) is this loop for one agent
and two turns: prompt, `wait_idle`, check the disk, prompt again into the *same*
session, `wait_idle`, check again. This module is that loop with the probe's
flag replaced by the policy (KC-3), the disk check replaced by the harvest
(KC-5), the second prompt replaced by `rework_message`, and N of them in a pool.

Per agent, `run_agent` walks the states in order:

    CREATED → PROMPTED → WAITING → HARVESTING → READY
                 ▲                     │
                 └──── REWORK ◄────────┤  (attempts left)
                                       └→ GAVE_UP
    WAITING → (retry) → PROMPTED  (retryable error, retries left — bounded)
    WAITING → STALLED  (turn timeout, silence, a third question — abort sent)
    WAITING → ERROR    (session.error, the stream closed, retries exhausted)
    CREATED → ERROR    (POST /session refused)
    WAITING → PROMPTED (context overflow with uncommitted work: a fresh session, KC-54)
    WAITING → STALLED  (context overflow with nothing left to carry on — KC-54)

After every transition `on_transition(run)` fires — `run_round` writes
`state.json` there — and one line per turn goes to `out_dir/<agent>/turns.jsonl`.
Every artifact write is fail-open: a log line or a state file that cannot be
written is a warning, never an exception into a round.

Stall detection: KC-12 (round 51) gives `wait_idle` an `idle_event_timeout=`,
so this module only passes the round's `idle_event_timeout_sec` (KC-2's
`[contest]` key) and reads the result. The primitive owns the clock — the last
event *of this session* on its stream, in `time.monotonic()` — and sends the
`abort` itself. A silence stall and the overall `turn_timeout_sec` both come
back as `IdleResult.status == "timeout"`; `elapsed` tells them apart. KC-47
(round 91) widens the silence window while the session still has a `bash` call
in flight and puts that call in `IdleResult.open_tool`, which this module puts
in `last_error` — `no event for Ns during bash: <command>` — so a stall report
says the agent was running its tests. The third-question edge is the runner's
own, still: `stall()` aborts and interrupts
the backend, and the wait wakes on the closed stream.

The round narrates itself (KC-18, round 57): every `transition` is one INFO
line on `tools.contest.runner` — `<agent>: <STATE> …` with the attempt, the
harvest's codes, the error or the commit — and `run_round` keeps a heartbeat
thread that logs, every `progress_every_sec` seconds (`[contest]`, 0 = off),
the round's age and each agent's state and time in it. `cli.main` routes the
logger to stderr; a caller that never configured logging hears nothing.

The harvest's tests: `run_round(..., run_tests=True)` (KC-16, round 55) makes
the four pytest roots the round's judge — a tree that breaks `tests/` is
REWORK with the pytest tail in the rework prompt, instead of READY. The roots
are slow (about 105 s here for one suite), so they run one worktree at a time
under `_TEST_RUNS_LOCK` while the rest of the round stays parallel.

A STALLED or ERROR turn with a commit on its branch is harvested too (KC-21,
round 60): the turn goes to the terminal state with `harvest` recorded on it
and `run.commit` set, so `export_patches` names it by the terminal state
(`<agent>.STALLED.patch`) instead of dropping it; a READY verdict finishes the
run as READY with the note `<sha12> after <the turn's error>`, and a REWORK
verdict keeps the state the turn earned — no rework prompt, the session is gone.
A branch with no commit above the base keeps today's path byte for byte: no
harvest, no pytest, `commit: null`.

A `session.error` that is a context overflow (KC-54, round 98) is neither
retried nor scored as-is: a prompt into a full session overflows again, so the
worktree's uncommitted edits are carried into a *fresh* session with
`round_prompt(dirty=)` — KC-22's `--resume` prompt, which is what a session
that has never seen the ticket needs — inside the attempt's continue budget.
The turn that overflowed is recorded with the old `session_id` before the
replacement is made. A clean overflow, or one that has spent the budget, is a
`STALLED` turn rather than an `ERROR` and still falls through to KC-21: a
commit above the base is harvested there and can still end `READY`. Every
non-overflow `session.error` keeps today's path byte for byte.

KC-45 (round 89) makes the provider's own name for a dropped stream
retryable: ``interrupted the response stream`` and ``upstream unavailable``
match the message pattern, so the round 86 payload goes to a
``RETRY_PROMPT`` re-prompt instead of an ``ERROR`` after one turn, and
``_retry_reason`` drops the pair of quotes Kilo wraps it in. Round 92's
agnes-3-0-flash ended ERROR on the same drop in other words — `interrupted the
response before it finished. send the request again as a new request` — so the
pattern is the stem, ``interrupted the response``, not the round 86 wording. A `provider
rejected the request` is still permanent on the session's *first* call —
refused once, refused again — but retryable once the session has had an
assistant reply that finished, where it is the free tier refusing under load.

KC-48 (round 92) closes the gap that left 36 python processes in round 86: a
`bash` call that returned had started four `pytest -n=8` suites with thirty-two
xdist workers, and the runner put the agent in `STALLED` while the suites ran
on, reparented to `systemd --user`, until the box ran out of cores. Nothing
looked at processes before — `finish()` transitioned and returned. Now every
terminal state reaps the worktree: every process whose `/proc/<pid>/cwd` is the
worktree or under it, and whose uid is ours, gets `SIGTERM`, and whatever is
still running after `REAP_GRACE_SEC` gets `SIGKILL`. The runner and its
ancestors are never candidates, however their cwd reads. The reap is recorded on
the agent as `reaped: [{pid, cmd}]` — absent when nothing was left behind — and
logged as one line. A STALLED or ERROR turn with a commit is reaped before
its salvage harvest reads the tree. The round's end runs the same sweep over
every worktree once more, Ctrl-C included, so a process started by a tool call
that returned after the agent's own reap is not left behind either. The
runner's own children — OpenRouter's agent loop, the harvest's judge — are the
runner's to end, and are never recorded as the agent's leftovers. The reap is
fail-open: a `/proc` entry that vanishes mid-scan is skipped, and a `/proc` that
is not there at all is one `WARNING` and an empty reap, never an exception into
a round.

KC-59 makes the scratch dir per agent: the round's `tmp_roots` globs stay in
force, and each agent additionally gets `<tmp_root>/<agent>/`, created at round
start by `workspace.ensure_agent_tmp_dirs` and named in its prompt. The other
agents' dirs are on this agent's `forbidden`, so a write into one of them is a
mechanical reject rather than a gate call — the same geometry rule that keeps a
sibling worktree forbidden (KC-46).

KC-62 (round 107) makes Kilo's own store a retryable error. Four of the five
live agents ended `ERROR` within two minutes on `Failed to execute statement` —
the round's `kilo serve` refusing a write in the SQLite every Kilo process of
the user shares, not a provider and not a model. `_LOCAL_STORE_RE` matches the
store's texts, `run_agent` retries them in the *same* session with
`RETRY_PROMPT(reason="kilo store error")` on their own budget
(`max_local_store_retries`, `local_store_retry_backoff_sec`, doubled per retry
and jittered ±30 % so four agents that hit the lock in the same second retry at
different times), and the provider's `max_error_retries` is not touched — a 429
and a store error in one turn each spend their own counter. The texts are not
added to `_RETRYABLE_MSG_RE`, because that rule is also the gate's, and the
provider's transients keep their own budget.

The work of a spent budget is kept rather than dropped (until KC-41 lands the
deadline commit): an `ERROR` on a store error whose tree is still dirty is
written to `state.json` with `resumable: true`, and `_plan` restarts such an
agent on `--resume` the way it restarts a mid-flight one — harvest when there is
a commit to score, otherwise a fresh session in the same worktree with
`dirty_on_resume`. A plain `ERROR` and an old `state.json` that has no key both
read `False`, so nothing that ended for a reason outside the store is restarted
on a resume that did not mean to.

KC-67 keeps a context overflow for 7 days, in `context_memory.py`, because the
provider's answer names the size Kilo does not know: round 113 overflowed the
same model at the same 262 144 tokens three rounds in a row, and KC-54 still
ends those STALLED. Before every prompt that goes into a session that already
holds a conversation — a rework, a continue, a retry — this module asks for the
model's size. Kilo's own `limit.context`, when intake has it, ends the question
there, because Kilo compacts those on its own; otherwise the smallest size
remembered for that provider and model is the number, and a session that holds
`compact_at_percent` of it is compacted first — one `POST /session/{id}/summarize`,
then the prompt. Every failure degrades to "no remembered size", which is today's
prompt, and every overflow is recorded whether or not the round ends STALLED.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import signal
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from tools.backoff import save_state
from tools.contest import context_memory
from tools.contest.backend import ContestBackend, ContestBackendError
from tools.contest.gates import declared_files, git
from tools.contest.harvest import harvest, rework_message
from tools.contest.kilo_client import (
    AGENT_TEST_TIMEOUT_MS,
    SessionRef,
    kilo_neighbours,
)
from tools.contest.policy import HARD_DENYLIST, Policy, PolicyContext
from tools.contest.roster import AgentSpec, ContestConfig
from tools.contest.workspace import (
    Workspace,
    agent_tmp_dir,
    agent_tmp_dirs,
    agent_tmp_globs,
)
from tools.git_run import run_git

__all__ = [
    "AgentRun",
    "AgentState",
    "RoundState",
    "round_prompt",
    "run_agent",
    "run_round",
    "WORKERS_FILE",
    "agent_pytest_workers",
    "agent_tmp_path",
    "core_count",
    "prompt_workers_note",
    "read_pytest_workers",
    "refresh_pytest_workers",
    "round_live_agents",
    "write_pytest_workers",
]

_log = logging.getLogger(__name__)

#: How many of the session's latest tool parts the gate sees.
RECENT_TOOLS = 8

class _TestRunsLock:
    """The round's pytest lock, plus who is inside it and who is queued behind.

    `_inner` is the mutual exclusion — one worktree's roots at a time. The
    bookkeeping is a dict under its own guard, so `turn["harvest"]` can carry
    `waited`, the heartbeat can say `HARVESTING (queued 12m, 2 ahead)` for a run
    that is still waiting and `HARVESTING (tests 3m)` for the one that holds the
    roots, and the round's table can total the wait per agent. `time.monotonic`
    is what is read, so a test can patch the clock and see the wait without a
    real sleep. Everything here is fail-open: an untracked agent is simply not
    in the queue.
    """

    def __init__(self) -> None:
        self._inner = threading.Lock()
        self._guard = threading.Lock()
        # agent -> {"waiting": t, "running": t}: when it asked for the lock and,
        # once granted, when it got it. Absent once it has exited.
        self._entries: dict = {}

    def enter(self, agent: str = "") -> float:
        """Ask for the lock; the returned value is how long the wait took."""
        key = self._key(agent)
        with self._guard:
            self._entries.setdefault(key, {})["waiting"] = time.monotonic()
        self._inner.acquire()
        with self._guard:
            asked = self._entries[key].get("waiting")
            got = time.monotonic()
            self._entries[key]["running"] = got
        return max(0.0, got - (asked if asked is not None else got))

    def exit(self, agent: str = "") -> None:
        """Leave the queue and give the lock back."""
        with self._guard:
            self._entries.pop(self._key(agent), None)
        self._inner.release()

    def ahead(self, agent: str = "") -> int:
        """How many are in front of *agent*: the holder plus who asked before it."""
        with self._guard:
            return self._in_front(agent)

    def status(self, agent: str = "") -> tuple | None:
        """`("queued", waited, ahead)` or `("running", elapsed)`, else None."""
        with self._guard:
            entry = self._entries.get(self._key(agent))
            if not entry:
                return None
            now = time.monotonic()
            if "running" in entry:
                return ("running", max(0.0, now - entry["running"]), 0)
            return ("queued", max(0.0, now - entry.get("waiting", now)), self._in_front(agent))

    def _key(self, agent: str) -> str:
        """The queue's name for this agent; an unnamed one is still ordered."""
        return agent or "unnamed"

    def _in_front(self, agent: str) -> int:
        """The queue's depth ahead of *agent*; `self` is held, read under the guard."""
        key = self._key(agent)
        mine = self._entries.get(key)
        if mine is None:
            # Has not asked yet, so everyone in the queue asked before it will.
            return len(self._entries)
        my_since = mine.get("waiting") or 0.0
        count = 0
        for name, entry in self._entries.items():
            if name == key:
                continue
            if "running" in entry:
                count += 1                      # the one that holds the roots
            elif (entry.get("waiting") or 0.0) <= my_since:
                count += 1                      # asked before this one did
        return count


#: The four pytest roots run one worktree at a time. The judge machine takes
#: about 20 minutes for eight parallel suites and about 105 s for one, so two
#: agents harvesting at once must not fan the roots out; only the test run is
#: slow, so the lock is held for the whole `harvest` call — its mechanical part
#: is a handful of `git` calls and costs nothing next to the roots.
_TEST_RUNS_LOCK = _TestRunsLock()

#: KC-27: the heartbeat's git calls per worktree — a short budget, because a
#: tick that waits on a held index is late, not failed.
_TICK_GIT_TIMEOUT_S = 5.0
_TICK_GIT_RETRIES = 1

#: KC-48: how long a process in a finished worktree gets between the TERM and
#: the KILL. Five seconds is enough for a suite's own cleanup to run, and
#: pointless to wait out if it will not — the round 86 suites ran for about a
#: minute past their agent's own STALLED. A module constant, patched in tests,
#: because no test wants to spend five seconds on the grace.
REAP_GRACE_SEC = 5.0

#: KC-48: the proc root the reap scans. A constant, not a literal inside the
#: walkers, so a test can point the reap at a directory that is not there.
_PROC_ROOT = "/proc"

#: KC-48: how long the reap waits for a SIGKILLed process to be gone. KILL is
#: not ignorable, so only a process in uninterruptible sleep gets near it.
_REAP_KILL_WAIT_SEC = 2.0

#: KC-48: how much of a reaped process's command line `state.json` keeps.
_REAP_CMD_CHARS = 120

# KC-48: guards the read-modify-write on `run.reaped`. An agent's own reap runs
# in its pool worker while the round's end sweep runs in the main thread, so the
# record must not be rebuilt from a torn read by two threads at once.
_REAPED_LOCK = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# KC-65: the agents' pytest worker count
# ─────────────────────────────────────────────────────────────────────────────

#: KC-65: the round's record of the pytest-xdist workers its agents' `-n auto`
#: should use. `cmd_run` writes it once with the start value, `run_round.save`
#: rewrites it after every transition, and the plugin
#: (`tools/contest/pytest_plugin/contest_pytest_workers.py`) reads it at each
#: pytest start. This file is the source of the count — nothing else in the tree
#: decides it, so the count the file holds and the count a prompt names cannot
#: drift apart.
WORKERS_FILE = "pytest-workers"

#: KC-65: the last lines of every prompt the runner sends. `{n}` is the count
#: the agents' `-n auto` reads at that moment. Last in the message on purpose:
#: KC-22's "only the first prompt carries it" and every existing prompt test
#: that checks the start of a message stay as they are.
_WORKERS_NOTE = (
    "\n\nThe box is shared: right now `-n auto` gives your pytest {n} workers.\n"
    "Do not pass a larger `-n`; a run is slower when many agents test at once.\n"
)


def core_count() -> int:
    """KC-65: the cores the box offers, as `os.cpu_count` reports them.

    One function, because the count is capped at the cores in several places and
    a test steers it by monkeypatching `os.cpu_count`. `None` or `0` is 1 — a
    cap of zero would floor the whole round at no workers at all.
    """
    return max(1, int(os.cpu_count() or 1))


def agent_pytest_workers(live: int, cores: int, config: ContestConfig) -> int:
    """KC-65: the workers one agent's `-n auto` gets while *live* agents hold a
    pool slot on *cores* cores.

    A fixed `pytest_workers_per_agent` above 0 wins, and it is not capped: the
    operator asked for that number. Otherwise the count follows the crowd — the
    last agent left gets the whole box, up to `pytest_workers_few_agents` live
    agents get `pytest_workers_few`, and above that `pytest_workers_min`. The
    auto counts are capped at the cores and floored at 1, so ten agents ask for
    no more than the box has, and nobody gets one worker, where a full suite
    runs for over half an hour and an agent's own `--timeout` fires on healthy
    code.
    """
    cores = max(1, int(cores or 1))
    per_agent = int(getattr(config, "pytest_workers_per_agent", 0) or 0)
    if per_agent > 0:
        return max(1, per_agent)
    few_agents = max(1, int(getattr(config, "pytest_workers_few_agents", 0) or 0))
    if live <= 1:
        count = cores
    elif live <= few_agents:
        count = int(getattr(config, "pytest_workers_few", 4) or 4)
    else:
        count = int(getattr(config, "pytest_workers_min", 2) or 2)
    return max(1, min(count, cores))


def round_live_agents(config: ContestConfig, resume: "RoundState | None") -> int:
    """KC-65: how many agents hold a pool slot when the round starts.

    A `Workspace` carries no state, so the count comes from the runs: on
    `--resume` they are the agents of `state.json` minus the ones already
    terminal (a resumed round whose eight agents already ended has two live, not
    eight), and they may outnumber the names on the command line. Without a
    resume, `len(config.agents)` — one workspace per agent.
    """
    if resume is not None:
        return sum(1 for run in resume.agents if not run.terminal)
    return len(config.agents)


def read_pytest_workers(path) -> "int | None":
    """The count *path* holds, or ``None`` when it cannot be read.

    ``None`` is the runner's "no guess": the prompt then carries no worker line,
    and the agents' pytest falls back to xdist's own answer. An unreadable file
    is not a round.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 1 else None


def write_pytest_workers(path, value: int) -> None:
    """Atomic: a temp file beside the target, then `os.replace`.

    The file is read by a pytest that may start at any moment and rewritten
    after every transition, so a torn write would hand a run a count that is not
    a number. `os.replace` means the reader sees the old value or the new one,
    never a half of either.
    """
    target = Path(path)
    temp = target.with_name(target.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        handle.write(f"{value}\n")
    os.replace(temp, target)


def refresh_pytest_workers(out_dir, config: ContestConfig, state: "RoundState",
                           memo: dict) -> None:
    """KC-65: follow the round with the worker file, after every transition.

    `live` is the agents that hold a pool slot now — neither `CREATED` (queued,
    it holds no slot) nor terminal (it gave the slot up). `HARVESTING` still
    counts: it may go back to work, and a harvest is a pytest run.

    A fixed `pytest_workers_per_agent` is written once by the CLI and never
    touched here, so the round cannot move a number the operator pinned. The
    write is atomic and only happens when the number changed — *memo* keeps the
    last value written, so a save that sees the same count does no I/O. A write
    that fails is one log line per round and never a stop: the agents' pytest
    then falls back to xdist's own answer and the round goes on.
    """
    if int(getattr(config, "pytest_workers_per_agent", 0) or 0) > 0:
        return
    try:
        live = sum(1 for run in state.agents
                   if run.state is not AgentState.CREATED and not run.terminal)
        value = agent_pytest_workers(live, core_count(), config)
        if "value" not in memo:
            memo["value"] = read_pytest_workers(out_dir / WORKERS_FILE)
        if memo["value"] == value:
            return
        memo["value"] = value
        write_pytest_workers(out_dir / WORKERS_FILE, value)
    except OSError as exc:
        if not memo.get("warned"):
            memo["warned"] = True
            _log.warning("could not write %s: %s: %s", out_dir / WORKERS_FILE,
                         type(exc).__name__, exc)


def prompt_workers_note(out_dir, config: ContestConfig) -> str:
    """KC-65: the worker count of this moment, as the last lines of a prompt.

    "" when the file cannot be read — no line, no guess — and "" when a fixed
    `pytest_workers_per_agent` already equals every core the box has, where the
    note would say nothing the agent does not already know.
    """
    per_agent = int(getattr(config, "pytest_workers_per_agent", 0) or 0)
    if per_agent > 0 and per_agent == core_count():
        return ""
    value = read_pytest_workers(out_dir / WORKERS_FILE)
    if value is None:
        return ""
    return _WORKERS_NOTE.format(n=value)


def agent_tmp_path(config: ContestConfig, round_no: "str | int") -> "Path | None":
    """KC-65: the round's own share of `[contest] agent_tmpdir`, or ``None``.

    ``<agent_tmpdir>/contest-<NN>`` — one dir per round, so a round never
    deletes another round's scratch and the parent is never touched. ``None``
    when the key is unset or empty, which is the round's inherited ``$TMPDIR``
    and needs no dir of its own.
    """
    root = str(getattr(config, "agent_tmpdir", "") or "").strip()
    if not root:
        return None
    return Path(os.path.expanduser(root)).resolve() / f"contest-{round_no}"


#: The prompt sent after a retryable session error (KC-19).  The
#: ``{reason}`` placeholder is filled with ``_brief(data["message"])``.
RETRY_PROMPT = (
    "The provider dropped the connection mid-turn ({reason}). "
    "Your worktree and this conversation are intact — continue from where you "
    "were; do not start over."
)

_RETRYABLE_CODES = frozenset({
    "ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "EPIPE", "UND_ERR_SOCKET",
})
_RETRYABLE_MSG_RE = re.compile(
    r"429|502|503|504|overloaded|rate limit|timeout"
    r"|interrupted the response|upstream unavailable", re.IGNORECASE
)

#: KC-62: Kilo's own store refusing a write — the round server's SQLite, shared
#: with every other Kilo process of the same user. Not the provider, not the
#: model: the session is intact and the same turn can go on. These texts are
#: deliberately not in `_RETRYABLE_MSG_RE`: they need their own budget, and
#: `_retryable` is also the gate-side rule for the provider's transients.
_LOCAL_STORE_RE = re.compile(
    r"Failed to execute statement|Failed query:|database is locked|database is busy"
    r"|SQLITE_BUSY|SQLITE_LOCKED|disk I/O error", re.IGNORECASE
)


def _quota_re(config) -> "re.Pattern | None":
    """KC-61: ``[contest] quota_patterns``, ``|``-separated, as one
    case-insensitive regex of literal phrases; ``None`` when the key is empty.

    Every phrase is ``re.escape``d, so the join can never be a malformed
    pattern: an absent or empty key is "no quota phrases", which is today's
    behaviour, never a round-killing ``re.error``.
    """
    raw = getattr(config, "quota_patterns", "") or ""
    phrases = [p.strip() for p in str(raw).split("|") if p.strip()]
    return re.compile("|".join(map(re.escape, phrases)), re.IGNORECASE) if phrases else None


def _is_quota(error, quota_re) -> bool:
    """KC-61: a ``ProviderQuota`` from ``wait_idle``, or any ``session.error``
    whose text is a quota.

    The first is how a Kilo session ends when the provider names a retry
    fourteen hours out; the second covers a provider that reports the same
    thing as a plain error. *error* may already be a string — the intake probe
    answers with the text it read, not a payload — in which case it is the text
    itself. Anything without the phrases in ``quota_patterns`` is not a quota,
    so a transient ``temporarily unavailable`` keeps KC-19's retry path.
    """
    if isinstance(error, dict) and error.get("name") == "ProviderQuota":
        return True
    if quota_re is None:
        return False
    text = error if isinstance(error, str) else _error_message(error)
    return quota_re.search(text) is not None

#: KC-45 §2/§2a: the provider's refusal of the request itself — no status code,
#: no retry flag, no socket error. Refused on the session's *first* call, it is
#: refused again when resent (§2). Once the session has had an assistant reply
#: that finished, the model id and the request fields have just worked, and the
#: refusal is the free tier refusing under load — the same class as the 429 two
#: lines above it (§2a).
#: A gateway's 403 ``request was blocked by a gateway or proxy`` is the same
#: class (round 104: the three ``vercel_8080`` agents got it after ~30 min of
#: work): retried only once the session has answered.
_PROVIDER_REJECTED_RE = re.compile(
    r"provider rejected the request|blocked by a gateway or proxy", re.IGNORECASE)

#: KC-54: a context overflow's payload — the provider's name for it, or either
#: spelling of the message it carries. The name is in the pattern as well as
#: the messages, so a payload that is just the string
#: ``"ContextOverflowError"`` matches too.
_OVERFLOW_RE = re.compile(
    r"ContextOverflowError|maximum context length|context_length_exceeded", re.IGNORECASE
)

#: KC-56: a reply cut off at ``finish: "length"`` whose tokens reach this share
#: of the model's ``limit.context`` filled the context window rather than the
#: output budget. One constant for the runner: KC-39 §7's check before a rework
#: into a full session is the same 90 %, and reads it from here.
CONTEXT_FULL_SHARE = 0.9

#: KC-67: the prompt kinds that go into a session which already holds a
#: conversation — the kinds the runner may compact first. The first prompt of a
#: run and the first prompt of the fresh session an overflow's work continued in
#: both start empty, so a fill for either would be a read of the new session,
#: not the one that overflowed.
_CONTEXT_GATE_KINDS = ("rework", "continue", "retry")

#: KC-56: the continue sent into the same session when the last reply hit the
#: *output* limit before any text or tool call — not `continue_message`, which
#: is about uncommitted files, and the tree may well be clean.
CUT_OFF_MESSAGE = (
    "Your last reply hit the output limit before any text or tool call. Think "
    "less, and make the next step a tool call. Your worktree and this "
    "conversation are intact — continue from where you were; do not start over."
)


def _retry_backoff(retry: int, base, cap) -> int:
    """The wait before the *retry*-th retry: *base* doubled on each retry,
    never longer than *cap* seconds (``0`` = no cap).

    With 15 and 60: 15, 30, 60, 60, … — a long budget of retries stays a
    minute apart instead of doubling into hours (round 104).
    """
    backoff = int(base) * (2 ** (int(retry) - 1))
    cap = int(cap or 0)
    return min(backoff, cap) if cap > 0 else backoff


def _error_message(error) -> str:
    """The message of a ``session.error`` payload: ``data.message``, else a
    top-level ``message``, else ``""``."""
    if not isinstance(error, dict):
        return ""
    data = error.get("data")
    if isinstance(data, dict) and isinstance(data.get("message"), str):
        return data["message"]
    if isinstance(error.get("message"), str):
        return error["message"]
    return ""


def _is_local_store(error) -> bool:
    """KC-62: True when *error*'s message is a Kilo-local store failure.

    Round 107's four `Failed to execute statement` payloads carry the text in
    `data.message`, which is where `_error_message` reads it. A payload that is
    not a dict, or a message that matches none of the phrases, is not one —
    every `_retryable` failure keeps today's path.
    """
    return bool(_LOCAL_STORE_RE.search(_error_message(error)))


def _rejected_request(error) -> bool:
    """KC-45 §2a: *error* is the provider refusing the request itself — the
    one payload whose retry depends on the session's transcript."""
    return _PROVIDER_REJECTED_RE.search(_error_message(error)) is not None


def _retryable(error, finished: int = 0) -> bool:
    """True when *error* is a retryable provider error (KC-19, KC-45).

    Retryable when ``data.isRetryable`` is truthy, or ``data.metadata.code``
    is one of the known transient socket codes, or the message matches a
    status-code, overload or dropped-stream pattern — the two KC-45 additions
    being ``interrupted the response`` (``… stream`` in round 86, ``… before it
    finished`` in round 92), the provider's own name for the connection it cut
    mid-turn, and ``upstream unavailable``, Kilo's text for
    the same class on the stream log.

    ``provider rejected the request`` needs *finished*, the count of the
    session's assistant messages that carry a ``finish`` (KC-45 §2a, the caller
    counts them off ``KiloClient.messages``): zero means the refusal happened
    before any answer, so the request is refused again when resent (§2); one or
    more means the model id and the request fields have just worked, and the
    refusal is the free tier refusing under load (§2a). *finished* is ``0`` by
    default, which is §2's behaviour for every caller that supplies nothing.

    A payload that is not a dict, or has no recognizable retryable signal, is
    not retryable.
    """
    if not isinstance(error, dict):
        return False
    data = error.get("data") or {}
    if isinstance(data, dict) and data.get("isRetryable"):
        return True
    metadata = data.get("metadata") if isinstance(data, dict) else None
    if isinstance(metadata, dict) and metadata.get("code") in _RETRYABLE_CODES:
        return True
    msg = _error_message(error)
    if _RETRYABLE_MSG_RE.search(msg):
        return True
    if _PROVIDER_REJECTED_RE.search(msg):
        # fail-open: a count that cannot be trusted is no count, which is §2's
        # answer — an unreadable transcript cannot prove the session answered
        return isinstance(finished, int) and not isinstance(finished, bool) and finished > 0
    return False


def _is_overflow(error) -> bool:
    """True when *error* is a context overflow (KC-54).

    An overflow has filled the session's context, so a prompt into the *same*
    session overflows again: the runner opens a fresh session for the work
    instead of retrying, and a clean tree is a stall rather than a crash.
    True when ``name`` is ``"ContextOverflowError"``, or when ``data.message``
    (or a top-level ``message``) carries ``"maximum context length"`` or
    ``"context_length_exceeded"``. A plain string matches on any of those
    three. A payload that is not a dict, or carries none of them, is not an
    overflow — ``None`` and ``{}`` are False.
    """
    parts = []
    if isinstance(error, str):
        parts.append(error)
    elif isinstance(error, dict):
        name = error.get("name")
        if isinstance(name, str):
            parts.append(name)
        data = error.get("data")
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            parts.append(data["message"])
        if isinstance(error.get("message"), str):
            parts.append(error["message"])
    return _OVERFLOW_RE.search(" ".join(parts)) is not None


def _is_provider_unavailable(error) -> bool:
    """KC-64: wait_idle ended the turn after too many provider retries.

    Never retried by the runner: Kilo already tried ``provider_retry_max_attempts``
    times, and the runner's ``max_error_retries`` would spend two more full
    rounds of the same against a provider that is not answering. True only for
    the payload ``wait_idle`` builds itself — ``name == "ProviderUnavailable"``;
    anything else, including a string or a payload that is not a dict, is False.
    """
    return isinstance(error, dict) and error.get("name") == "ProviderUnavailable"


#: KC-64: bynara answers an exhausted plan with the same "temporarily
#: unavailable" 503 as a real outage (KC-61, 2026-09-25), so the end says
#: where to look instead of guessing which one it was.
_PLAN_HINT = " (can be an exhausted plan — check the provider's site)"


def _tokens_used(tokens: dict) -> int:
    """``input + cache.read + reasoning + output`` of one message's ``tokens``."""
    def num(value) -> int:
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    cache = tokens.get("cache")
    read = num(cache.get("read")) if isinstance(cache, dict) else 0
    return int(num(tokens.get("input")) + read + num(tokens.get("reasoning"))
               + num(tokens.get("output")))


def _cut_off(backend: ContestBackend, session: SessionRef,
             context_limit: int | None = None) -> str | None:
    """KC-56: why the turn that just went idle was cut off, or ``None``.

    Reads the session's last assistant message. ``finish == "length"`` is a
    reply the provider stopped at a token limit, not a model that decided it
    was done: ``"context"`` when its tokens (`_tokens_used`) are at or above
    `CONTEXT_FULL_SHARE` of *context_limit* — the window is full and a prompt
    into this session overflows — else ``"output"``, the reply's own budget. An
    unknown *context_limit* (``None``, 0) is ``"output"``. Any other finish, no
    assistant message, or a ``messages()`` that raises is ``None``: fail-open,
    today's path.
    """
    try:
        messages = backend.messages(session)
    except Exception:  # noqa: BLE001 — a transcript that cannot be read cuts nothing off
        return None
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        info = message.get("info") if isinstance(message, dict) else None
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        if info.get("finish") != "length":
            return None
        tokens = info.get("tokens")
        used = _tokens_used(tokens) if isinstance(tokens, dict) else 0
        if context_limit and used >= CONTEXT_FULL_SHARE * context_limit:
            return "context"
        return "output"
    return None


def _context_budget(spec, records) -> tuple:
    """KC-67: ``(size, source)`` for *spec*'s model, from Kilo's own limit first.

    ``("N", "kilo")`` when intake put the model's own ``limit.context`` on the
    spec: then Kilo compacts the session on its own and the runner does nothing
    more with the number, which is why the two sources are told apart. Otherwise
    the smallest size remembered for that provider and model, ``"remembered"`` —
    the models the provider declares no limit for, the ones that overflowed.
    ``("none", "none")`` when there is nothing at all, which is today's prompt
    and today's overflow.
    """
    limit = getattr(spec, "context_limit", None)
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        return int(limit), "kilo"
    size = context_memory.smallest_size(records, spec.provider_id, spec.model_id)
    if size is not None:
        return int(size), "remembered"
    return None, "none"


def _context_tokens(backend: ContestBackend, session: SessionRef) -> int:
    """KC-67: the context of the last reply that still went through, in tokens.

    ``input + cache.read + reasoning + output`` of the session's last assistant
    message that reports any — the same sum KC-56's cut-off reads, so the runner
    and Kilo size the session the same way. A message that reports none is
    skipped: Kilo keeps the message it opened for the reply an overflow refused,
    all zeros (round 114, sn68-var1), and that is not the last reply that went
    through. ``0`` for every failure: a transcript that cannot be read, or one
    with no such message yet, is no fill, not an error, and no fill never
    compacts anything.
    """
    try:
        messages = backend.messages(session)
    except Exception:  # noqa: BLE001 — a transcript that cannot be read is no fill
        return 0
    if not isinstance(messages, list):
        return 0
    for message in reversed(messages):
        info = message.get("info") if isinstance(message, dict) else None
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        tokens = info.get("tokens")
        used = _tokens_used(tokens) if isinstance(tokens, dict) else 0
        if used > 0:
            return used
    return 0


def _summary_tokens(backend: ContestBackend, session: SessionRef) -> int | None:
    """KC-67: the size of the summary a compact left, in tokens, else ``None``.

    Kilo writes the compact as an assistant message flagged ``summary``; its
    ``output`` is the summary the next prompt carries instead of the history,
    so it is what the context shrank to — before the next prompt's own text.
    ``None`` when there is no such message or it reports nothing: the console
    then says the size is not known yet, and the next turn's fill shows it.
    """
    try:
        messages = backend.messages(session)
    except Exception:  # noqa: BLE001 — a transcript that cannot be read is no size
        return None
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        info = message.get("info") if isinstance(message, dict) else None
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        if not info.get("summary"):
            return None
        tokens = info.get("tokens")
        output = tokens.get("output") if isinstance(tokens, dict) else None
        if isinstance(output, int) and not isinstance(output, bool) and output > 0:
            return output
        return None
    return None


def _finished_replies(backend: ContestBackend, session: SessionRef) -> int:
    """KC-45 §2a: how many of *session*'s assistant messages carry a ``finish``.

    ``0`` for every failure — a transcript that cannot be read, or one that is
    not a list, is treated as one with no finished reply, which is §2's
    first-call behaviour: the refusal is not retried. Any ``finish`` counts,
    not only ``"stop"``: a reply cut off at a token limit still answered the
    request the provider now calls bad.
    """
    try:
        messages = backend.messages(session)
    except Exception:  # noqa: BLE001 — a transcript that cannot be read has no replies
        return 0
    if not isinstance(messages, list):
        return 0
    finished = 0
    for message in messages:
        info = message.get("info") if isinstance(message, dict) else None
        if isinstance(info, dict) and info.get("role") == "assistant" and info.get("finish"):
            finished += 1
    return finished


def _unquote(text: str) -> str:
    """*text* with one layer of matching surrounding quotes removed (KC-45 §3).

    Kilo wraps some provider messages in a literal pair of quotes — the round 86
    dropped-stream payload arrives as ``"the model's provider interrupted the
    response stream"`` — and ``RETRY_PROMPT`` puts the reason in a sentence of
    its own, so the extra layer only reads as noise. One layer only, and only
    when both ends are the same quote: an unbalanced message is unchanged.
    """
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return text[1:-1].strip()
    return text


def _retry_reason(error) -> str:
    """The brief reason string for ``RETRY_PROMPT`` from a retryable payload.

    ``data.message`` first, then the payload itself. The message has one layer
    of surrounding quotes removed (KC-45 §3): ``RETRY_PROMPT`` already puts the
    reason in its own sentence, so Kilo's extra ``"…"`` only reads as noise.
    """
    if isinstance(error, dict):
        data = error.get("data") or {}
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            return _brief(_unquote(data["message"]))
    return _brief(error)


def _budget_left(config) -> float:
    """`harvest_budget_sec` as a non-negative float; missing or bad is no budget."""
    try:
        return max(0.0, float(getattr(config, "harvest_budget_sec", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def _harvest(ws, ticket_path, run_tests, config=None):
    """`harvest` for one worktree, with the pytest roots serialized round-wide.

    KC-57: `config.harvest_budget_sec` bounds the roots' wall time, and the lock
    is entered under the agent's name so the turn can carry how long it waited
    and how many were ahead. Fail-open throughout: an absent or broken lock is
    "no queue to stand in", so the roots still run and the harvest still ends.
    """
    budget = _budget_left(config)
    if not run_tests:
        return harvest(ws, ticket_path, budget_sec=budget)
    waiter = getattr(ws, "agent", "")
    ahead = 0
    waited = 0.0
    try:
        ahead = int(_TEST_RUNS_LOCK.ahead(waiter))
    except (AttributeError, TypeError, ValueError):
        ahead = 0
    entered = False
    try:
        waited = float(_TEST_RUNS_LOCK.enter(waiter))
        entered = True
    except (AttributeError, TypeError, ValueError):
        waited = 0.0
    try:
        return harvest(ws, ticket_path, run_tests=True,
                       budget_sec=budget, waited=waited, ahead=ahead)
    finally:
        if entered:
            try:
                _TEST_RUNS_LOCK.exit(waiter)
            except Exception:  # noqa: BLE001 — the harvest is over either way
                pass


def _commits_above(ws: Workspace) -> int:
    """The commits on the branch above its base — `judge_worktree`'s `commits`.

    KC-21's gate: a turn that died with one under it has work to be scored. Zero
    both when the branch is at the base and when the count cannot be read — a
    worktree with nothing on it keeps the old path, with no harvest and no pytest.
    """
    count = git(ws.path, "rev-list", "--count", f"{ws.base_sha}..HEAD")
    return int(count) if count.isdigit() else 0


def _head_sha(ws: Workspace) -> str | None:
    """HEAD of *ws* as a full sha, or None when it cannot be read."""
    return git(ws.path, "rev-parse", "HEAD") or None


def _split_numstat(line: str) -> tuple[int, int, str]:
    """`(added, deleted, path)` of one `git diff --numstat` line.

    `(0, 0, "")` for anything that is not a numstat line: a binary file is a
    `-` where a number belongs, a rename arrives as `path => path`, and an
    empty output has no lines at all.
    """
    parts = line.split("\t")
    if len(parts) != 3:
        return 0, 0, ""
    # a binary file is `-` where a number belongs: the path still counts as a
    # touched file, its lines do not
    try:
        added, deleted = int(parts[0]), int(parts[1])
    except ValueError:
        added, deleted = 0, 0
    return added, deleted, parts[2].strip().rsplit(" => ", 1)[-1]


def _line_count(path: Path) -> int:
    """The lines of one file, `0` when it cannot be read.

    `--numstat` has no view of an untracked file, so a sample counts one from
    the disk itself.
    """
    try:
        with Path(path).open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _churn(ws: Workspace) -> tuple[int, int]:
    """`(files touched, lines changed)` in *ws*, committed and not. Never raises.

    KC-36: the sample that decides whether a turn's deadline is extended. The
    union of

      * `git status --porcelain --untracked-files=all` — the paths, and for
        each untracked file its own line count, which no `--numstat` can see;
      * `git diff --numstat` — added plus deleted of the uncommitted edits;
      * `git diff --numstat <base_sha>..HEAD` — what the agent already
        committed on this branch;

    minus everything under `.smoke_tests/` (`sync_test_tiers.py` links, not
    work). `--no-optional-locks` so a sample never takes `index.lock` from the
    agent's own `git commit`.

    Read-only and never raises: any git failure, a missing worktree and a
    binary `-` in a `--numstat` line all count `0` for that path, and a tree
    that cannot be read at all is `(0, 0)` rather than an exception into a
    round.
    """
    def out(*args: str) -> str:
        try:
            r = run_git(["git", "--no-optional-locks", *args], cwd=ws.path,
                        timeout=_TICK_GIT_TIMEOUT_S, retries=_TICK_GIT_RETRIES)
        except Exception:  # noqa: BLE001 — a sample must never raise
            return ""
        return r.stdout if r.returncode == 0 else ""

    paths: set[str] = set()
    lines = 0
    try:
        for raw in out("status", "--porcelain", "--untracked-files=all").splitlines():
            if len(raw) <= 3:
                continue
            name = raw[3:].rsplit(" -> ", 1)[-1].strip()
            if not name or name.startswith(".smoke_tests/"):
                continue
            paths.add(name)
            if raw[:2] == "??":
                lines += _line_count(Path(ws.path) / name)
        for raw in out("diff", "--numstat").splitlines():
            added, deleted, name = _split_numstat(raw)
            if not name or name.startswith(".smoke_tests/"):
                continue
            paths.add(name)
            lines += added + deleted
        for raw in out("diff", "--numstat", f"{ws.base_sha}..HEAD").splitlines():
            added, deleted, name = _split_numstat(raw)
            if not name or name.startswith(".smoke_tests/"):
                continue
            paths.add(name)
            lines += added + deleted
    except Exception:  # noqa: BLE001 — see the docstring
        return 0, 0
    return len(paths), lines


def _worktree_files(ws: Workspace) -> int:
    """The number of distinct paths changed in *ws*'s worktree (KC-27).

    The files half of :func:`_churn`: one walker, shared with the turn's
    deadline, instead of a second one for the heartbeat. Same contract as
    KC-27 — the union of `git status --porcelain --untracked-files=all` and
    `git diff <base_sha>..HEAD`, minus `.smoke_tests/`, read-only, never
    raising, `--no-optional-locks` so it never takes `index.lock` from under
    the agent's own `git commit`.
    """
    return _churn(ws)[0]


class AgentState(str, Enum):
    """One agent's position in the loop. The last four are terminal."""

    CREATED = "CREATED"
    PROMPTED = "PROMPTED"
    WAITING = "WAITING"
    HARVESTING = "HARVESTING"
    REWORK = "REWORK"
    READY = "READY"
    GAVE_UP = "GAVE_UP"
    STALLED = "STALLED"
    ERROR = "ERROR"

    @property
    def terminal(self) -> bool:
        return self in (AgentState.READY, AgentState.GAVE_UP, AgentState.STALLED, AgentState.ERROR)


def _counters() -> dict:
    return {"asked": 0, "allowed": 0, "rejected": 0, "gated": 0, "gate_failed": 0}


@dataclass
class AgentRun:
    """One agent's run: mutable, and JSON-round-trippable through `to_dict`/`from_dict`.

    `attempt` is 0 for the first turn and grows by one per rework. `turns` holds
    one dict per turn: `kind` (`initial`/`continue`/`rework`/`retry`), `attempt`,
    `sent_at`, `idle_at`, `idle_status`, `stale_events` (KC-63, only when a
    wait skipped an earlier turn's events), and `harvest` = `{"verdict", "reasons": [codes]}`
    once the turn was scored. `permissions` counts what the policy was asked
    and how it answered; `questions` counts the questions over the whole run.
    `reaped` (KC-48) is what the worktree was still running when the run ended.
    """

    agent: AgentSpec
    workspace: Workspace
    session_id: str | None = None
    state: AgentState = AgentState.CREATED
    attempt: int = 0
    turns: list = field(default_factory=list)
    permissions: dict = field(default_factory=_counters)
    questions: int = 0
    last_error: str | None = None
    #: KC-62: the run ended `ERROR` on Kilo's own store and its worktree still
    #: holds uncommitted work, so `--resume` may restart it. `False` for every
    #: other terminal state, and for a `state.json` written before the key.
    #: KC-41 supersedes: the deadline commit replaces this with work committed
    #: on the spot, and the flag goes away with it.
    resumable: bool = False
    commit: str | None = None
    cost: float | None = None
    tokens: dict | None = None
    #: KC-48: `[{"pid": int, "cmd": str}]` of what the reap found in the
    #: worktree at the terminal state, absent from `state.json` when the agent
    #: left nothing running.
    reaped: list | None = None

    @property
    def terminal(self) -> bool:
        return self.state.terminal

    def to_dict(self) -> dict:
        data = asdict(self)
        data["workspace"]["path"] = str(self.workspace.path)
        data["state"] = self.state.value
        if not data.get("reaped"):
            data.pop("reaped", None)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AgentRun":
        ws = dict(data["workspace"])
        ws["path"] = Path(ws["path"])
        run = cls(agent=AgentSpec(**data["agent"]), workspace=Workspace(**ws))
        for name in ("session_id", "attempt", "turns", "permissions", "questions",
                     "last_error", "resumable", "commit", "cost", "tokens", "reaped"):
            if name in data:
                setattr(run, name, data[name])
        run.state = AgentState(data.get("state", "CREATED"))
        return run

    def last_reason(self) -> str:
        """`last_error`, else the last scored turn's verdict and reason codes."""
        if self.last_error:
            return self.last_error
        for turn in reversed(self.turns):
            h = turn.get("harvest")
            if h:
                return " ".join([h.get("verdict", "")] + [str(c) for c in h.get("reasons", [])]).strip()
        return ""

    def test_wait(self) -> float:
        """KC-57: the seconds this run spent queued for the round's pytest lock.

        The sum of `waited` over its turns — one harvest per turn, each either
        straight through the lock or behind the harvest before it. Turns that
        carry no harvest, and rows written before `waited` existed, count as 0.
        """
        total = 0.0
        for turn in self.turns:
            h = turn.get("harvest") or {}
            try:
                total += max(0.0, float(h.get("waited") or 0))
            except (TypeError, ValueError):
                continue
        return round(total, 1)


@dataclass
class RoundState:
    """One round: what `out_dir/state.json` holds, and what `--resume` reads back."""

    round_no: int
    ticket: str
    base_sha: str
    started_at: float
    agents: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"round_no": self.round_no, "ticket": self.ticket, "base_sha": self.base_sha,
                "started_at": self.started_at, "agents": [run.to_dict() for run in self.agents]}

    @classmethod
    def from_dict(cls, data: dict) -> "RoundState":
        return cls(round_no=int(data["round_no"]), ticket=str(data["ticket"]),
                   base_sha=str(data["base_sha"]), started_at=float(data["started_at"]),
                   agents=[AgentRun.from_dict(a) for a in data.get("agents", [])])

    def table_rows(self) -> list:
        """One dict per agent — the SUMMARY's inputs; KC-7 renders them."""
        return [{
            "name": run.agent.name,
            "model": run.agent.model,
            "state": run.state.value,
            "attempts": run.attempt,
            "turns": len(run.turns),
            "permissions": dict(run.permissions),
            "questions": run.questions,
            "cost": run.cost,
            "tokens": run.tokens,
            "commit": run.commit,
            "last_reason": run.last_reason(),
            "test_wait": run.test_wait(),
        } for run in self.agents]


# ─────────────────────────────────────────────────────────────────────────────
# the prompt
# ─────────────────────────────────────────────────────────────────────────────

# KC-47 §5: `AGENT_TEST_TIMEOUT_MS` is the figure `_PROMPT` tells the agent to
# give its own test run. It is the `kilo_client` constant, so the number the
# prompt asks for and the number the silence clock reasons about cannot drift
# apart. It must stay below `turn_timeout_sec` — `tests/test_contest_runner.py`
# asserts it does, against the committed `contest.ini`.

#: `docs/collect-epics/RUN-THE-EPIC-COMPETITION.md` §Stage 1, "PROMPT STARTS …
#: PROMPT ENDS", with the blockquote markers dropped and `<YOUR NAME>` as
#: `{name}`. A module string on purpose: the runbook is documentation and may
#: drift; the text the agents are scored against must not.
_PROMPT = """\
You are implementing one ticket from an epic round. Every agent in this round
is implementing the same ticket against the same starting tree; the best
implementation is merged and becomes the base for the next round. You are
being scored on the implementation, not on speed.

Get your ticket:

```bash
python3 scripts/next_task.py --tasks epic-tasks/ --progress runs/{name}/PROGRESS.csv
```

The command prints a ticket: a description of a defect in the code named on
its `**File:**` / `**Symbol:**` lines. The ticket file itself already exists
in `epic-tasks/` — do **not** create, edit or rewrite it; it is your task
description, not your deliverable. Your deliverable is a change to the code
it names, plus a test. Implement **exactly that ticket**. Then record it:

```bash
python3 scripts/append_task.py --progress runs/{name}/PROGRESS.csv \\
    --ticket <the NN-*.md you were given> --outcome DONE --commit <sha> \\
    --note "one line: what changed + the test that covers it"
```

Stop after **one** ticket. Do not call `next_task.py` again — the next round
is handed out separately, from a different tree.

The ground rules are printed inside the ticket. Three of them settle the round
on their own, so read them before you start:

- `CollectBridge._shrink` must be **byte-identical** when you are done. New
  work runs *before* it, never instead of it. This is checked mechanically.
- **One local commit.** Never `git push`.
- **A test ships with the change**, and it must fail without the change.

Two more that are checked by reading your diff:

- Everything you add is **fail-open**: an absent collect model, a malformed
  config key or a broken artifact degrades to "no collect data" and never
  raises into a run.
- **Stay on the ticket.** Touching files the ticket does not name counts
  against you unless you say why in the commit message.

Never point any command at a live provider config. If a step needs one, copy
`agents_128k.ini` to a scratch path and stub every `base_url` first.

Running the test suite on this machine can take up to 20 minutes under load:
give that `bash` call a `timeout` of at least {test_timeout_ms} ms.

When you are done, report: the commit sha, each Acceptance checkbox and
whether you met it, and anything in the ticket you found to be wrong about the
live code — each ticket names the commit it was written against in its
`**Status:**` line (the original 24 used `68b78a0`); the code is the
authority, not the ticket.

Your starting tree is commit {base_sha}; your one commit goes on top of it.
Any command that reaches outside your worktree is decided by a reviewer, and a
rejection is final for that command — do not retry it.
"""


#: KC-59: the sentence appended when the round derived a scratch dir for the
#: agent — ``<tmp_root>/<agent>/``, the one place outside the worktree the
#: policy lets it write and no other agent's. ``{tmp_dir}`` is that dir; an
#: empty *tmp_dir* appends nothing, so a prompt without one is today's text.
_SCRATCH_DIR_NOTE = (
    "\n\nYour scratch dir is {tmp_dir} — put temporary files there. The policy "
    "allows that dir and not the other agents'."
)


def round_prompt(agent_name: str, ticket_path: Path, base_sha: str, *, dirty: str = "",
                 tmp_dir: str = "") -> str:
    """The runbook's prompt for *agent_name*, plus the base sha and the permission rule.

    The ticket is not repeated: the session reads it from its own worktree via
    `next_task.py`, so *ticket_path* is accepted for the caller's clarity only.
    When *tmp_dir* is non-empty (KC-59), the sentence that names the agent's own
    scratch dir is appended first; when *dirty* is non-empty (a `--resume` into a
    worktree that still holds uncommitted work, KC-22), the `continue_message`
    paragraph is appended after it so the fresh session learns of the work on its
    first prompt — every existing caller passes neither and gets the unchanged
    text.
    """
    del ticket_path
    text = _PROMPT.format(name=agent_name, base_sha=base_sha,
                          test_timeout_ms=AGENT_TEST_TIMEOUT_MS)
    if tmp_dir:
        text = text + _SCRATCH_DIR_NOTE.format(tmp_dir=tmp_dir)
    if dirty:
        text = text + "\n\n" + continue_message(dirty)
    return text


#: How many `git status` lines `continue_message` lists before it says "and N more":
#: a tree with thousands of untracked files must not turn a nudge into a megabyte prompt.
_DIRTY_LINES_SHOWN = 40


def continue_message(dirty: str) -> str:
    """The nudge sent when a turn ends `idle` with uncommitted work (KC-22).

    The session already holds the ticket and the preceding `round_prompt`, so
    neither is repeated: just the `git status --porcelain` lines, indented, and
    the instruction to finish in this same worktree. *dirty* is the porcelain
    output (KC-22's `git status` lines) — already excluding `runs/`.
    """
    lines = dirty.splitlines()
    indented = "\n".join("  " + ln for ln in lines[:_DIRTY_LINES_SHOWN])
    if len(lines) > _DIRTY_LINES_SHOWN:
        indented += f"\n  ... and {len(lines) - _DIRTY_LINES_SHOWN} more"
    return (
        "Your turn ended before anything was committed. The worktree still "
        "holds your uncommitted work:\n"
        + indented + "\n"
        "Finish the ticket in this same worktree: one commit, the test, "
        "append_task.py with the commit's sha. Do not start over and do not "
        "discard these files."
    )


class TreeReadError(RuntimeError):
    """`git status` could not be read for a worktree — a read error, not a tree.

    FL-2: a status that exits non-zero (it lost the index lock, or the path is
    not a repository at all) used to come back as ``""``, which every caller
    read as "the tree is clean". Deciding "no uncommitted work" from a command
    that did not run is what the KC-31/KC-41 path is built on: a turn scored
    clean is a turn that is not harvested, and an agent's work becomes zero
    entries.
    """


def _dirty_tree(ws: Workspace) -> str:
    """The uncommitted work of *ws* as `git status --porcelain
    --untracked-files=all`, with `runs/` excluded — `""` only when the tree is
    clean.

    `runs/<agent>/PROGRESS.csv` rows live under `runs/` and must not count as
    the agent's work.

    Raises :class:`TreeReadError` when git cannot answer (FL-2) — the callers
    catch it, in `_plan` and in the KC-22 branch of `run_agent`, and degrade to
    "no nudge" with a warning. The command still goes through
    `tools.git_run.run_git`, so a transient held index is waited out first: only
    a status that has genuinely failed is a read error.

    Not `gates.git`: that strips its output, and the first porcelain line of a
    tree with unstaged edits starts with a space that is part of the status.
    """
    try:
        # --no-optional-locks: status only reads, so it never takes the index
        # lock an agent's own `git add` in this worktree may need at that moment
        r = run_git(["git", "--no-optional-locks", "status", "--porcelain",
                     "--untracked-files=all"], cwd=ws.path)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TreeReadError(f"git status in {ws.path} did not run: {exc}") from exc
    if r.returncode != 0:
        raise TreeReadError(
            f"git status in {ws.path} exited {r.returncode}: "
            f"{r.stderr.strip() or r.stdout.strip()}"
        )
    lines = []
    for ln in r.stdout.splitlines():
        if not ln.strip():
            continue
        body = ln[3:] if len(ln) > 3 else ln  # drop the two status chars + space
        name = body.rsplit(" -> ", 1)[-1]
        if name.startswith("runs/"):
            continue
        lines.append(ln)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# artifacts — every write fail-open
# ─────────────────────────────────────────────────────────────────────────────

def _append_jsonl(path: Path, entry: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 — a log line is not a round
        _log.warning("could not append %s: %s: %s", path, type(exc).__name__, exc)


def _write_json(path: Path, data) -> None:
    try:
        save_state(data, path)  # tmp + fsync + rename, so a reader never sees a torn file
    except Exception as exc:  # noqa: BLE001 — see the module docstring
        _log.warning("could not write %s: %s: %s", path, type(exc).__name__, exc)


def _brief(value) -> str:
    """A one-line, bounded rendering of a server payload for `last_error`."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return " ".join(text.split())[:300]


def _abort_quietly(backend: ContestBackend, session: SessionRef) -> None:
    try:
        backend.abort(session)
    except Exception as exc:  # noqa: BLE001 — the session may already be gone
        _log.warning("abort(%s) failed: %s: %s", session.id, type(exc).__name__, exc)


# ─────────────────────────────────────────────────────────────────────────────
# the wait, with the round's stall edge
# ─────────────────────────────────────────────────────────────────────────────

def _no_idle_error(config: ContestConfig, elapsed: float,
                   clock: "_TurnClock | None") -> str:
    """`no idle after …` for the turn that never idled.

    Today's string, unchanged, when no extension clock was armed — that is
    `turn_extend_sec = 0` and every `state.json` written before KC-36. With
    one, the same sentence names the sample the deadline refused: the files
    and lines on disk when it gave up, and how long they had not moved.
    """
    if clock is None or clock.on_deadline is None:
        return f"no idle after {config.turn_timeout_sec}s"
    files, lines = clock.last_sample
    quiet_for = max(0.0, elapsed - clock.last_change_at)
    return (f"no idle after {_age(elapsed)} ({files} files, {lines} lines, "
            f"unchanged for {_age(quiet_for)})")


@dataclass
class _TurnClock:
    """One turn's deadline, and what the runner needs to tell about it.

    KC-36: ``on_deadline`` is what ``wait_idle`` asks at the deadline,
    ``extensions`` the grants it earned (one entry per grant, recorded in the
    turn), ``last_sample`` the churn it refused, and ``granted`` the churn the
    deadline has already moved — both the cap the next extension is clipped to
    and the heartbeat's ``+Nm``. ``prev_sample`` is the churn the last grant
    was based on, the baseline the next sample grows against.

    KC-66: ``gate_added`` and ``gate_attempts`` are the gate's half, kept out
    of ``granted`` on purpose. The gate's waits are recovery, not progress, so
    a turn that hit a busy gate key still has its full churn room left, and the
    two are added up when the turn is recorded.
    """

    on_deadline: Callable[[float], float | None] | None
    extensions: list = field(default_factory=list)
    prev_sample: tuple[int, int] = (0, 0)
    last_sample: tuple[int, int] = (0, 0)
    last_change_at: float = 0.0
    granted: float = 0.0
    #: KC-66: the seconds this turn got back for the gate's own waits, and the
    #: tries those waits cost.
    gate_added: float = 0.0
    gate_attempts: int = 0

    def grant_gate(self, seconds: float, tries: int) -> float:
        """KC-66: the seconds a gate's waits are granted back to this turn.

        The gate runs inside ``wait_idle``'s loop, so its 429 would otherwise
        come straight out of the agent's turn. Whatever it asks is granted, and
        it is kept out of ``granted`` — see the class docstring — so a turn
        that spent time on the gate's rate limit still earns its churn
        extensions. Returns the same seconds, which is what the deadline moves
        by and what the turn records. Fail open: a malformed amount is 0.0.
        """
        try:
            asked = max(0.0, float(seconds))
            count = int(tries)
        except (TypeError, ValueError):
            return 0.0
        if asked <= 0 or count <= 0:
            return 0.0
        self.gate_added += asked
        self.gate_attempts += count
        return asked


def _turn_deadline(run: AgentRun, config: ContestConfig) -> _TurnClock:
    """The clock for one turn: extend on churn, cap at `turn_max_sec`.

    ``turn_extend_sec = 0`` returns a clock with ``on_deadline`` still
    ``None`` — `wait_idle` is asked nothing and the turn is today's path byte
    for byte. Otherwise every deadline asks for a fresh sample of the worktree
    this *run* owns: `run.workspace.path`, keyed by `run.agent.name`, never by
    `model_id`, so `hy3-var1` and `hy3-var2` on one model extend on their own
    churn.

    Progress is the sample growing strictly in either number — files alone is
    too coarse, because an agent that writes a file in its first minute and
    then loops shows the same count forever, so `lines` is what usually moves.
    On progress the deadline is pushed by `turn_extend_sec`, clipped to
    `turn_max_sec`, and the sample becomes the new previous; on no progress —
    and at the cap, whatever the churn says — it aborts as today.
    """
    extend = float(getattr(config, "turn_extend_sec", 0) or 0)
    clock = _TurnClock(on_deadline=None)
    # `turn_extend_sec = 0` arms nothing and reads no git — today's path, for
    # real, not just in the shape of the call.
    if extend <= 0:
        return clock
    sample = _churn(run.workspace)
    clock.prev_sample = sample
    clock.last_sample = sample
    ws, spec = run.workspace, run.agent
    nominal = float(config.turn_timeout_sec or 0)
    cap = float(getattr(config, "turn_max_sec", 0) or 0)
    ceiling = cap if cap > 0 else nominal

    def on_deadline(elapsed: float) -> float | None:
        current = _churn(ws)
        clock.last_sample = current
        previous = clock.prev_sample
        if not (current[0] > previous[0] or current[1] > previous[1]):
            return None
        grant = min(extend, ceiling - nominal - clock.granted)
        if grant <= 0:
            return None
        clock.granted += grant
        clock.prev_sample = current
        clock.last_change_at = float(elapsed)
        clock.extensions.append({"at": round(float(elapsed), 1), "files": current[0],
                                 "lines": current[1], "granted": int(grant)})
        try:
            _log.info("%s: WAITING — +%s at %s (%d files, %d lines)",
                      spec.name, _age(grant), _age(float(elapsed)),
                      current[0], current[1])
        except Exception:  # noqa: BLE001 — the grant stands, the line is not a round
            pass
        return grant

    clock.on_deadline = on_deadline
    return clock


def _wait_turn(backend: ContestBackend, session: SessionRef, config: ContestConfig,
               *, on_permission, on_question,
               on_deadline: Callable[[float], float | None] | None = None,
               since: int | None = None):
    """`backend.wait_idle` for one turn, with the round's stall edge wired in.

    Returns the `IdleResult`. `idle_event_timeout` is the round's
    `idle_event_timeout_sec`: a session silent for that long is aborted by the
    backend and comes back as `status="timeout"`, at an `elapsed` well under
    `turn_timeout_sec` — which is how the runner names it a silence stall
    rather than a turn timeout. Zero or unset disables the clock, which is
    `wait_idle`'s behaviour without the argument.

    *on_deadline* (KC-36) is the turn's own churn clock, one per turn, built
    by `run_agent`. When it is `None` — `turn_extend_sec = 0`, or a backend
    with no worktree to read — the kwarg is not passed at all, and the call is
    byte for byte today's.

    *since* (KC-63) is the backend's mark taken right before this turn's
    prompt; `None` (a backend without one) is not passed at all.
    """
    silence = float(config.idle_event_timeout_sec or 0)
    wait_kwargs = {
        "idle_event_timeout": silence or None,
        "on_permission": on_permission,
        "on_question": on_question,
    }
    if on_deadline is not None:
        wait_kwargs["on_deadline"] = on_deadline
    if since is not None:
        wait_kwargs["since"] = since
    # KC-61: a retry scheduled further out than the bound is a quota reset, not
    # a blip — the agent ends `ERROR provider_quota` at once instead of waiting
    # for the silence clock. 0 arms nothing, which is today's behaviour.
    max_retry_wait = float(getattr(config, "provider_retry_max_wait_sec", 0) or 0)
    if max_retry_wait > 0:
        wait_kwargs["max_retry_wait"] = max_retry_wait
    quota_re = _quota_re(config)
    if quota_re is not None:
        wait_kwargs["quota_re"] = quota_re
    # KC-64: N retries in a row, with no model output between them, end the
    # turn instead of Kilo's own retry loop resetting the silence clock for an
    # hour. 0, a missing key or a negative value arms nothing — today's
    # behaviour, bounded only by the turn deadline.
    attempts = int(getattr(config, "provider_retry_max_attempts", 0) or 0)
    if attempts > 0:
        wait_kwargs["max_retry_attempts"] = attempts
    return backend.wait_idle(session, float(config.turn_timeout_sec), **wait_kwargs)


def _compact_session(backend: ContestBackend, session: SessionRef, config: ContestConfig,
                     on_permission, on_question) -> bool:
    """KC-67: one `POST /session/{id}/summarize`, then the idle it ends in.

    True only when the session went idle after the compact — the only answer
    that means the history is smaller and the prompt that was coming is safe.
    Every other outcome is False, and False is today's path: the prompt goes
    out as it stands, and KC-54 still decides the overflow it may cause.

    A backend without a compact (a subprocess agent, whose session has no
    server-side history to shrink) returns False at once. A server that refuses
    the call, a wait that ends in anything but `idle` — a refused compact, a
    timeout on a session that will not answer, or a raise from either — is
    False too, with a warning and nothing else: a compact that fails must never
    end a round, and the caller re-reads no state from it.
    """
    compact = getattr(backend, "compact", None)
    if not callable(compact):
        return False
    try:
        mark_fn = getattr(backend, "mark", None)
        since = mark_fn() if callable(mark_fn) else None
        compact(session)
        idle = _wait_turn(backend, session, config, on_permission=on_permission,
                          on_question=on_question, since=since)
        if getattr(idle, "status", None) != "idle":
            _log.info("compact of %s ended %s, the prompt goes out as it stands",
                      getattr(session, "id", session), getattr(idle, "status", ""))
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — a failed compact is the prompt as today
        _log.warning("compact of %s: %s", getattr(session, "id", session), _brief(str(exc)))
        return False


# ─────────────────────────────────────────────────────────────────────────────
# KC-48: the reap — an ended agent leaves no process in its worktree
# ─────────────────────────────────────────────────────────────────────────────

def _stat_fields(proc_root: str, pid: int):
    """`(state, ppid)` off `/proc/<pid>/stat`, or `None` when it cannot be read.

    The comm (field two) may itself hold `)` and spaces, so the walk starts
    after the last `)`: `state` is first, `ppid` second.
    """
    try:
        with open(f"{proc_root}/{pid}/stat", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return None
    tail = raw.rsplit(")", 1)
    if len(tail) != 2:
        return None
    fields = tail[1].split()
    if len(fields) < 2 or not fields[1].isdigit():
        return None
    return fields[0], int(fields[1])


def _ancestor_pids(proc_root: str) -> set:
    """Our pid and every pid above it in the parent chain — never candidates.

    Read off `/proc`, so a chain that has already been reaped simply stops
    short. This is what keeps a round started from inside a worktree from
    signalling itself, and it costs one `stat` read per level of a chain that is
    a dozen long at most.
    """
    own = os.getpid()
    chain = {own}
    pid = own
    for _ in range(128):   # a cycle, not a chain, if we ever come back to ourselves
        info = _stat_fields(proc_root, pid)
        if info is None or info[1] in chain:
            break
        chain.add(info[1])
        pid = info[1]
    return chain


def _proc_uid(proc_root: str, pid: int):
    """The real uid that owns *pid*, or `None` when it cannot be read.

    Three sources, in order, because no single one exists everywhere: the owner
    of the `/proc/<pid>` directory itself — one `stat`, and it holds the real uid
    even on a stripped-down `/proc` that ships no `uid` file, which is this box
    (57 entries per pid, none of them `uid`) — then the dedicated
    `/proc/<pid>/uid`, then the `Uid:` line of `/proc/<pid>/status`. Every branch
    falls through rather than returning `None`, so one missing file does not skip
    the sources that are still there. Reading the uid off `/proc` at all is what
    keeps the reap from signalling a process the runner does not own: a `None`
    here means *skip this pid*, never *assume it is ours*.
    """
    try:
        return int(os.stat(f"{proc_root}/{pid}").st_uid)
    except OSError:
        pass
    try:
        with open(f"{proc_root}/{pid}/uid", encoding="utf-8") as fh:
            field = fh.read().split()
        if field and field[0].isdigit():
            return int(field[0])
    except OSError:
        pass
    try:
        with open(f"{proc_root}/{pid}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    field = line.split(None, 1)[1].split()
                    if field and field[0].isdigit():
                        return int(field[0])
    except OSError:
        pass
    return None


def _proc_cwd(proc_root: str, pid: int) -> Path | None:
    """The resolved cwd of *pid*, or `None` when the entry is gone or unreadable.

    Gone covers the entry vanishing between `os.listdir` and this read, and a
    cwd whose directory was already deleted: `/proc` keeps the link, reading it
    still works, and `resolve(strict=False)` returns the vanished path instead of
    raising.
    """
    try:
        link = os.readlink(f"{proc_root}/{pid}/cwd")
    except OSError:
        return None
    return Path(link).resolve(strict=False)


def _proc_cmd(proc_root: str, pid: int) -> str:
    """The process's command line, NULs and newlines as spaces, `""` when unreadable.

    A kernel thread has no cmdline at all; `""` is what the record then holds,
    and the log line says `(no command)` for it. Newlines are folded to spaces
    because a `python3 -c` payload carries them and the record has to stay on
    one log line and one `state.json` field.
    """
    try:
        with open(f"{proc_root}/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    text = raw.replace(b"\0", b" ").decode("utf-8", "replace")
    return re.sub(r"\s+", " ", text).strip()


def _alive(proc_root: str, pid: int) -> bool:
    """Whether *pid* still has a live task behind it — a zombie does not.

    A zombie is already dead: its parent only has to wait on it, and a KILL
    would reach nothing.
    """
    info = _stat_fields(proc_root, pid)
    return info is not None and info[0] not in ("Z", "X")


def _wait_for_exit(proc_root: str, pids: list, grace: float) -> list:
    """The pids of *pids* still running *grace* seconds after their TERM.

    Polls instead of sleeping the grace out, so a TERM that is honoured is not
    followed by a pointless wait for the rest of it. A grace of 0 returns at
    once — how a test reaches the KILL half of the reap without spending five
    seconds on the grace.
    """
    deadline = time.monotonic() + grace
    alive = [pid for pid in pids if _alive(proc_root, pid)]
    while alive and time.monotonic() < deadline:
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        alive = [pid for pid in alive if _alive(proc_root, pid)]
    return alive


def _signal_pid(pid: int, sig: int) -> None:
    """`os.kill` with the races of a concurrent scan folded away.

    `ESRCH` — the process exited between the scan and the signal — is the
    expected answer here, not a failure. Anything else is a WARNING and the reap
    goes on: one stubborn process must not keep the rest of the tree running.
    """
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass
    except OSError as exc:
        _log.warning("could not signal %d with %d: %s: %s", pid, sig, type(exc).__name__, exc)


def _reap_summary(reaped: list) -> str:
    """`(pytest -n=8 … ×4, …)` — the distinct commands, counted when repeated.

    The round 86 shape was four identical suites with thirty-two workers each, so
    the line names the shape of what was left behind rather than forty identical
    clauses, and caps itself so one agent cannot turn the log into a wall.
    """
    counts: dict = {}
    for item in reaped:
        cmd = item.get("cmd") or "(no command)"
        counts[cmd] = counts.get(cmd, 0) + 1
    parts = [f"{cmd} ×{n}" if n > 1 else cmd for cmd, n in counts.items()]
    text = ", ".join(parts)
    return text if len(text) <= 200 else text[:200] + "…"


def _grace(grace: float | None) -> float:
    """The grace to wait: *grace*, else `REAP_GRACE_SEC` read at the call.

    Read at the call, not bound as a default, so patching `REAP_GRACE_SEC` is
    enough to shorten the grace — which is how a test reaches the KILL half of
    the reap without spending five seconds on it.
    """
    return float(REAP_GRACE_SEC) if grace is None else float(grace)


def _reap_worktree(worktree, *, name: str, grace: float | None = None) -> list:
    """KC-48: TERM, then KILL, whatever is still standing in *worktree*.

    Candidates are the processes whose resolved `/proc/<pid>/cwd` is *worktree*
    or under it, and whose uid is ours; the runner, its ancestors and its own
    children are never candidates, however their cwd reads. A child of the
    runner is the runner's to end, not the agent's leftover: `OpenRouterBackend`
    spawns its agent loop with `cwd=` the worktree and `close()` ends it after
    this reap, and the harvest's judge runs there too. What the agent's own
    calls started is one level further down, or reparented to `systemd --user`. Everything is read through `/proc`, and
    everything that cannot be read is skipped rather than raised: a pid that
    exits between `os.listdir` and the read is already reaped, and a `/proc`
    that is not there at all is one WARNING and an empty reap.

    Returns `[{"pid": int, "cmd": first 120 chars}]`, `[]` when nothing was left
    behind. Never raises into a round.
    """
    proc_root = _PROC_ROOT
    try:
        entries = os.listdir(proc_root)
    except OSError as exc:
        _log.warning("%s: no reap of %s — %s is not readable (%s: %s)",
                     name, worktree, proc_root, type(exc).__name__, exc)
        return []
    try:
        ours = os.getuid()
    except (AttributeError, OSError):
        _log.warning("%s: no reap of %s — the runner's uid could not be read", name, worktree)
        return []
    try:
        root = Path(worktree).resolve(strict=False)
        excluded = _ancestor_pids(proc_root)
        me = os.getpid()
        found = []
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid in excluded:
                continue
            info = _stat_fields(proc_root, pid)
            if info is None or info[1] == me:
                continue
            cwd = _proc_cwd(proc_root, pid)
            if cwd is None or not cwd.is_relative_to(root):
                continue
            if _proc_uid(proc_root, pid) != ours:
                continue
            # Already dead: its parent only has to wait on it, and signalling or
            # recording it would say we reaped something we never touched.
            if not _alive(proc_root, pid):
                continue
            found.append((pid, _proc_cmd(proc_root, pid)[:_REAP_CMD_CHARS]))
    except OSError as exc:
        _log.warning("%s: reap of %s could not be scanned — %s: %s",
                     name, worktree, type(exc).__name__, exc)
        return []
    except Exception as exc:  # noqa: BLE001 — a reap is best effort, never a round-killing one
        _log.warning("%s: reap of %s could not be scanned — %s: %s",
                     name, worktree, type(exc).__name__, exc)
        return []
    if not found:
        return []
    for pid, _cmd in found:
        _signal_pid(pid, signal.SIGTERM)
    killed = _wait_for_exit(proc_root, [pid for pid, _ in found], _grace(grace))
    for pid in killed:
        _signal_pid(pid, signal.SIGKILL)
    # `kill` returns before the process is gone — the KILL is delivered, not yet
    # acted on — so the reap waits for it: whoever reads the tree next (the
    # salvage harvest, the export) must not find the stray still running. KILL
    # cannot be ignored, so this ends in milliseconds; the cap is for a process
    # stuck in uninterruptible sleep, which is logged rather than waited out.
    stuck = _wait_for_exit(proc_root, killed, _REAP_KILL_WAIT_SEC)
    if stuck:
        _log.warning("%s: %d reaped %s still running %gs after SIGKILL: %s", name,
                     len(stuck), "process" if len(stuck) == 1 else "processes",
                     _REAP_KILL_WAIT_SEC, stuck)
    reaped = [{"pid": pid, "cmd": cmd} for pid, cmd in found]
    _log.info("%s: reaped %d %s left in the worktree (%s)", name, len(reaped),
              "process" if len(reaped) == 1 else "processes", _reap_summary(reaped))
    return reaped


def _record_reaped(run: AgentRun, name: str, worktree, grace: float | None = None) -> list:
    """Reap *worktree* and put what came back on `run.reaped` for `state.json`.

    Merges rather than replaces: a second sweep of a tree the first already
    cleared finds nothing and leaves the record as it was, and a sweep that finds
    something the first sweep missed adds it without repeating what is already
    recorded. `state.json` then says what the agent left running.
    """
    reaped = _reap_worktree(worktree, name=name, grace=grace)
    if not reaped:
        return reaped
    with _REAPED_LOCK:          # the round's sweep and a worker's own reap may race
        have = {item.get("pid") for item in run.reaped or ()}
        run.reaped = list(run.reaped or []) + [item for item in reaped if item["pid"] not in have]
    return reaped


def _reap_round_worktrees(runs: list, grace: float | None = None) -> int:
    """The round's end sweep: every agent's worktree, once, in agent order.

    An agent whose own reap already ran finds an empty tree here, which is the
    common case — the sweep exists for the gap after it, the tool call that
    returned after its agent had ended. Returns how many processes the round was
    left with, so the caller knows whether `state.json` has changed.

    On Ctrl-C this covers the agents still mid-flight too: their sessions are
    being aborted and the round is exiting, so nothing in their trees is a live
    tool call any more — a suite left running there would outlive the round
    exactly as round 86's did. Their state is not touched: they stay mid-flight
    for `--resume`, which starts them again in a tree with nothing running.
    """
    total = 0
    for run in runs:
        total += len(_record_reaped(run, run.agent.name, run.workspace.path, grace))
    return total


# ─────────────────────────────────────────────────────────────────────────────
# one agent
# ─────────────────────────────────────────────────────────────────────────────

def run_agent(run: AgentRun, *, backend: ContestBackend, policy: Policy,
              config: ContestConfig, ticket_path: Path, out_dir: Path,
              on_transition: Callable[[AgentRun], None],
              run_tests: bool = False, agent_tmp: Path | None = None) -> AgentRun:
    """Drive *run* to a terminal state — single-threaded, one session for every
    turn; a context overflow (KC-54) or a reply cut off by a full context window
    (KC-56) opens a fresh one, which are the only second ``POST /session`` this
    function makes.

    *backend* is a `ContestBackend` (KC-34): everything this function needs of the
    session — create, prompt, wait, abort, tool history, close — goes through
    it, so a Kilo server and an OpenRouter subprocess are the same round. This
    function never touches a client or a tap.

    `on_transition(run)` is called after every state change. In `finally`, on a
    terminal state, the worktree is reaped first (KC-48 — whatever the agent's
    calls left standing is TERMed, then KILLed after the grace, and recorded on
    `run.reaped`), then the session's `cost` and `tokens` are read and its
    messages are written to `out_dir/<agent>.session.json`.

    *run_tests* passes the harvest's `run_tests` through to it after every turn:
    the four pytest roots are then the round's judge (KC-16) and run one
    worktree at a time, instead of the ticket's own self-check being the only
    evidence. Off by default, so every earlier call of this function is unchanged.

    *agent_tmp* is the round's share of `[contest] agent_tmpdir` (KC-65) — the
    dir the CLI pointed every agent's `$TMPDIR` at. Its `/*` glob joins the
    policy's `tmp_roots`, because the round moved the agents' shell onto a dir
    the policy has never seen, and without it an agent's first `tmp_path`
    fixture is a gate call it did not cost under `/tmp`. `None` is today's
    `/tmp` and adds nothing.

    ``run.agent.context_limit`` — the model's ``limit.context``, which intake
    puts on the spec from ``GET /provider`` (KC-56) — tells a reply cut off by
    a full context window from one cut off by its output budget. ``None`` is an
    unknown limit: every cut-off is then an output one.
    """
    out_dir = Path(out_dir)
    ws, spec = run.workspace, run.agent
    agent_dir = out_dir / spec.name
    # KC-59: the scratch dir is per agent. This agent's own dir is allowed and
    # named in its prompt; the other agents' dirs are forbidden ground for it, so
    # a write into one of them is a mechanical reject rather than a gate call.
    # `getattr` for both: a config without a `tmp_roots` key degrades to "no
    # scratch dir" instead of raising into a round.
    tmp_roots = getattr(config, "tmp_roots", ()) or ()
    scratch_dir = agent_tmp_dir(tmp_roots, spec.name)
    scratch_arg = str(scratch_dir) if scratch_dir is not None else ""
    scratch_others = tuple(
        d for d in agent_tmp_dirs(tmp_roots, config.agents) if d.name != spec.name
    )
    # KC-65: the round's own $TMPDIR is scratch for this agent too — the glob
    # below, so a tmp_path fixture is not a gate call it did not cost under /tmp.
    agent_tmp_glob = f"{agent_tmp}/*" if agent_tmp is not None else ""
    session: SessionRef | None = None
    stalled: list = []          # the reason, once the runner's stall edge fired
    time_up: "threading.Timer | None" = None   # the agent's hard limit, `agent_max_sec`
    questions_this_turn = [0]
    try:
        ticket_files = declared_files(ticket_path)
    except OSError:
        ticket_files = ()

    def transition(state: AgentState, error: str | None = None, *, note: str | None = None) -> None:
        run.state = state
        if error is not None:
            run.last_error = error
        detail = error if error is not None else note
        _log.info("%s: %s%s", spec.name, state.value, f" — {detail}" if detail else "")
        on_transition(run)

    def stall(reason: str) -> None:
        """Abort the session and end the wait; it wakes on the closed stream."""
        if stalled:
            return
        stalled.append(reason)
        _abort_quietly(backend, session)
        backend.interrupt(session)

    def on_permission(event: dict) -> tuple:
        props = event.get("properties") or {}
        run.permissions["asked"] += 1
        try:
            recent = tuple(backend.tool_parts(session)[-RECENT_TOOLS:])
        except Exception:  # noqa: BLE001 — a history that cannot be read is an empty one
            recent = ()
        spent = run.permissions["gated"] + run.permissions["gate_failed"]
        ctx = PolicyContext(
            worktree=ws.path,
            # the round's globs, this agent's own scratch dir (KC-59), and the
            # round's $TMPDIR (KC-65)
            tmp_roots=tuple(tmp_roots) + agent_tmp_globs(tmp_roots, spec.name)
                      + ((agent_tmp_glob,) if agent_tmp_glob else ()),
            # the rounds folder: this round's other worktrees and every earlier
            # round's, plus the other agents' scratch dirs. The policy lets this
            # agent's own worktree through (KC-46)
            forbidden=tuple(HARD_DENYLIST) + (ws.path.parent,) + scratch_others,
            ticket_title=Path(ticket_path).name,
            ticket_files=tuple(ticket_files),
            recent_tools=recent,
            gate_budget_left=max(0, int(config.gate_max_calls_per_session) - spent),
        )
        decision = policy.decide(event, ctx)
        policy.record(decision, event, agent_dir / "decisions.jsonl")
        run.permissions["allowed" if decision.reply == "once" else "rejected"] += 1
        if decision.layer == "gate":
            run.permissions["gated"] += 1
        elif decision.layer in ("gate-failed", "budget"):
            run.permissions["gate_failed"] += 1
        _log.info("%s: permission %s -> %s (%s)", spec.name, props.get("permission"),
                  decision.reply, decision.layer)
        # KC-66: the gate waited inside this wait_idle loop, so the seconds it
        # spent are granted back to this turn's deadline and counted on its
        # clock — the agent never pays for the gate's 429. `back` is what the
        # deadline moves by, so the turn records exactly what it was given.
        back = 0.0
        tries = int(getattr(decision, "gate_attempts", 1) or 1)
        turn_clock = getattr(run, "_turn_clock", None)
        if turn_clock is not None:
            back = turn_clock.grant_gate(
                float(getattr(decision, "gate_added_sec", 0.0) or 0.0), tries)
        if back > 0:
            _log.info("%s: gate %d tries, +%gs to the turn", spec.name, tries, back)
        return decision.reply, decision.reason, back

    def on_question(event: dict) -> None:
        del event  # rejected by wait_idle regardless; only the count matters here
        run.questions += 1
        questions_this_turn[0] += 1
        if questions_this_turn[0] >= int(config.max_questions_per_turn):
            stall(f"{questions_this_turn[0]} questions in one turn")

    def finish(state: AgentState, error: str | None = None, *, note: str | None = None) -> AgentRun:
        transition(state, error, note=note)
        return run

    def fresh_session(turn: dict, dirty: str) -> str | None:
        """Go on in a new session: the error line if ``POST /session`` failed.

        KC-54's swap, shared by KC-56's context cut-off. The full session is
        replaced, never prompted again; the new one has never seen the ticket
        or the round prompt, so its first prompt is `round_prompt(dirty=)`
        (KC-22's `--resume` shape), without the paragraph when *dirty* is
        empty. *turn* — the one that filled the old session — is recorded with
        that session's id, because `finally` records only the last session,
        and with ``new_session`` once there is one. `run.attempt` is left
        alone; the swap spends one of `max_continues_per_attempt`.
        """
        nonlocal session, continue_text, continue_used
        turn["session_id"] = run.session_id
        try:
            session = backend.create_session(
                spec.provider_id, spec.model_id, rules=config.session_rules(),
                title=ws.branch, agent=spec.kilo_agent, variant=spec.variant)
        except (ContestBackendError, ValueError) as exc:
            run.turns.append(turn)
            _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
            return f"POST /session failed: {_brief(str(exc))}"
        turn["new_session"] = session.id
        run.turns.append(turn)
        _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
        run.session_id = session.id
        continue_text = round_prompt(spec.name, ticket_path, ws.base_sha, dirty=dirty,
                                     tmp_dir=scratch_arg)
        continue_used += 1
        return None

    def _wait_backoff(seconds: float) -> bool:
        """Sleep *seconds* between two retries into the same session.

        The wait the provider's own retry had inline: a `Timer` sets an `Event`
        after *seconds*, and the loop checks it every 200 ms, so a stall that
        fires inside the wait — or a `backend.interrupted()` from Ctrl-C —
        wakes it at once. KC-62's local-store branch shares it, so the two
        budgets stand down on the same signal. Returns False when such a wake
        happened, True when the whole backoff ran or when *seconds* is 0 or
        less — nothing was waited, nothing was cut short.
        """
        seconds = float(seconds)
        if seconds <= 0:
            return not stalled and not backend.interrupted()
        event = threading.Event()
        timer = threading.Timer(seconds, event.set)
        timer.daemon = True
        timer.start()
        try:
            while not event.is_set():
                event.wait(timeout=0.2)
                if stalled or backend.interrupted():
                    break
        finally:
            timer.cancel()
        return not stalled and not backend.interrupted()

    def _memory() -> list:
        """KC-67: the context overflows this round still remembers — read
        fail-open, so a memory that is missing, unreadable or broken is no
        memory and nothing else, never a failed round."""
        try:
            return context_memory.load(
                context_memory.memory_path(config, out_dir),
                days=context_memory.days_of(config))
        except Exception as exc:  # noqa: BLE001 — no memory, never a raise
            _log.warning("%s: context memory: %s", spec.name, _brief(str(exc)))
            return []

    def _context_gate(turn: dict, kind: str) -> bool:
        """KC-67: what this session holds before the prompt, and the compact it
        earns. ``True`` only when one happened, which is the only case where the
        caller needs a fresh mark for its wait.

        The size is Kilo's own ``limit.context`` when intake has it — then the
        runner does nothing more with it, because Kilo compacts those models on
        its own — else the smallest one remembered for this provider and model,
        else nothing at all. The fill is the last reply that still went through
        over that size, and a compact happens only for a remembered size and
        only for a prompt that goes into a session which already holds a
        conversation: a fresh session holds nothing to compact away.

        The four fields are written whether or not there is a size, so a turn
        always says what it knew: ``context_size``, ``context_source``,
        ``fill`` and ``compacted``.
        """
        size, source, fill = None, "none", None
        try:
            size, source = _context_budget(spec, _memory())
            tokens = _context_tokens(backend, session)
            if size:
                fill = tokens * 100.0 / float(size)
        except Exception as exc:  # noqa: BLE001 — no size, the prompt as today
            _log.warning("%s: context: %s", spec.name, _brief(str(exc)))
            size, source, fill = None, "none", None
        turn["context_size"] = size
        turn["context_source"] = source
        turn["fill"] = round(fill, 1) if isinstance(fill, float) else None
        turn["compacted"] = False
        if kind not in _CONTEXT_GATE_KINDS:
            return False
        percent = context_memory.compact_at_percent(config)
        if source != "remembered" or not size or percent <= 0:
            return False
        if fill is None:
            return False
        if fill < percent:
            # one console line per prompt of a model the memory sizes: the
            # operator sees the fill grow towards the compact, not only the compact
            _log.info("%s: context %s tokens = %.1f%% of the %s remembered, "
                      "below %g%% — no compact before %s",
                      spec.name, f"{tokens:,}", fill, f"{size:,}", percent, kind)
            return False
        _log.info("%s: context %s tokens = %.1f%% of the %s remembered, at %g%% — "
                  "compacting before %s",
                  spec.name, f"{tokens:,}", fill, f"{size:,}", percent, kind)
        started = time.monotonic()
        if not _compact_session(backend, session, config, on_permission, on_question):
            _log.info("%s: compact did not finish — the %s prompt goes out as it stands",
                      spec.name, kind)
            return False
        took = time.monotonic() - started
        after = _summary_tokens(backend, session)
        turn["compacted"] = True
        turn["context_before"] = tokens
        turn["context_after"] = after
        if after is not None and after < tokens:
            _log.info("%s: compact finished in %.0f s: context %s -> %s tokens "
                      "(-%s, -%.0f%%) — the round continues with the %s prompt",
                      spec.name, took, f"{tokens:,}", f"{after:,}",
                      f"{tokens - after:,}", (tokens - after) * 100.0 / tokens, kind)
        else:
            _log.info("%s: compact finished in %.0f s: context %s tokens before, the "
                      "size after is not reported yet — the round continues with the "
                      "%s prompt; the next turn's fill shows it",
                      spec.name, took, f"{tokens:,}", kind)
        return True

    def _remember_overflow(error) -> None:
        """KC-67: one line in the shared memory, for the next round.

        The provider's own numbers — the limit it named, the prompt it named,
        the last reply that still went through — plus the round and the agent
        that produced them, so a remembered size always says where it came from.
        The record is added before the decision KC-54 makes of this overflow, so
        it is remembered whether the round ends STALLED or READY. A write that
        fails is a warning and nothing else: the overflow still ends the turn
        the way it does today, and the line is for the next session, not this
        one.
        """
        try:
            limit, prompt = context_memory.parse_overflow(_error_message(error))
            record = context_memory.OverflowRecord(
                at=time.time(),
                round=str(out_dir.name or ""),
                agent=spec.name,
                provider=spec.provider_id,
                model=spec.model_id,
                limit=limit,
                last_ok=_context_tokens(backend, session),
                prompt=prompt,
            )
            if not context_memory.add(context_memory.memory_path(config, out_dir),
                                      record, days=context_memory.days_of(config)):
                _log.warning("%s: context memory: not written", spec.name)
        except Exception as exc:  # noqa: BLE001 — the overflow still ends the turn
            _log.warning("%s: context memory: %s", spec.name, _brief(str(exc)))

    try:
        # ── CREATED: one session, kept for every turn ─────────────────────
        try:
            session = backend.create_session(
                spec.provider_id, spec.model_id, rules=config.session_rules(),
                title=ws.branch, agent=spec.kilo_agent, variant=spec.variant)
        except (ContestBackendError, ValueError) as exc:
            return finish(AgentState.ERROR, f"POST /session failed: {_brief(str(exc))}")
        run.session_id = session.id
        # The agent's hard limit: `agent_max_sec` from here, whatever the turns,
        # retries and extensions add up to. At the limit the session is aborted
        # the way a stall is, and the tree is left as it stands for the scoring.
        # 0 = no limit.
        agent_max = float(getattr(config, "agent_max_sec", 0) or 0)
        if agent_max > 0:
            time_up = threading.Timer(
                agent_max, stall, args=(f"time up: {_age(agent_max)} for the agent",))
            time_up.daemon = True
            time_up.start()

        rework_text = None
        retry_text = None
        continue_text = None
        continue_used = 0
        retries_used = 0
        local_retries = 0
        while True:
            if stalled:
                # the hard limit fired between two turns: no new prompt
                return finish(AgentState.STALLED, stalled[0])
            # ── PROMPTED ───────────────────────────────────────────────────
            if retry_text is not None:
                kind, text = "retry", retry_text
                retry_text = None
            elif rework_text is not None:
                kind, text = "rework", rework_text
                rework_text = None
            elif continue_text is not None:
                kind, text = "continue", continue_text
                continue_text = None
            else:
                kind = "initial"
                dirty = getattr(run, "dirty_on_resume", "") or ""
                if dirty:
                    # KC-22, `--resume` into a worktree that still holds the
                    # work: the fresh session learns of it on its first prompt.
                    text = round_prompt(spec.name, ticket_path, ws.base_sha, dirty=dirty,
                                        tmp_dir=scratch_arg)
                    run.dirty_on_resume = ""  # only the first prompt carries it
                else:
                    text = round_prompt(spec.name, ticket_path, ws.base_sha, tmp_dir=scratch_arg)
            turn = {"kind": kind, "attempt": run.attempt, "sent_at": time.time()}
            if kind == "continue":
                note = (f"attempt {run.attempt} (continue {continue_used} of "
                        f"{int(config.max_continues_per_attempt)})")
            else:
                note = f"attempt {run.attempt} ({kind})"
            transition(AgentState.PROMPTED, note=note)
            # KC-63: the mark goes right before the POST, so no event of this
            # turn can come before it and every event of an earlier one does
            mark_fn = getattr(backend, "mark", None)
            since = mark_fn() if callable(mark_fn) else None
            # KC-67: the session may already hold more than the model can take —
            # the size came from the memory, not from Kilo, so Kilo will not
            # compact it for us. A mark is re-taken when a compact did happen,
            # so the compact's own idle is not this turn's idle.
            if _context_gate(turn, kind):
                since = mark_fn() if callable(mark_fn) else None
            try:
                # KC-65: the worker count of this moment, as the last lines of
                # the message. This one send carries the first prompt, a
                # `continue`, a `REWORK` and a `--resume` prompt alike, so a
                # nudged agent reads the count it starts with, not the count it
                # had an hour ago.
                backend.prompt(session, text + prompt_workers_note(out_dir, config))
            except ContestBackendError as exc:
                return finish(AgentState.ERROR, f"prompt failed: {_brief(str(exc))}")

            # ── WAITING ────────────────────────────────────────────────────
            transition(AgentState.WAITING)
            questions_this_turn[0] = 0
            # KC-36: this turn's own deadline, decided from its own worktree.
            # It sits on the run only so the heartbeat can read it mid-wait.
            clock = _turn_deadline(run, config)
            run._turn_clock = clock
            try:
                idle = _wait_turn(backend, session, config,
                                  on_permission=on_permission, on_question=on_question,
                                  on_deadline=clock.on_deadline, since=since)
            finally:
                run._turn_clock = None
            turn["idle_at"] = time.time()
            turn["idle_status"] = idle.status
            stale = int(getattr(idle, "stale_skipped", 0) or 0)
            if stale:
                # KC-63: events of an earlier turn this wait did not read as its own
                turn["stale_events"] = stale
            if clock.extensions:
                turn["extensions"] = clock.extensions
            if clock.gate_added > 0:
                # KC-66: what the gate's 429 cost this turn, and how hard it tried
                turn["gate_added_sec"] = int(clock.gate_added)
                turn["gate_attempts"] = int(clock.gate_attempts)
            if stalled:
                turn["idle_status"] = "stalled"
                error, state = stalled[0], AgentState.STALLED
            elif idle.status == "timeout":
                # KC-12 sends the silence stall back as "timeout" too, so the
                # label comes from elapsed: under the overall deadline means
                # the silence window fired, at it means the turn never idled.
                # KC-36 moves that deadline when the churn earned it.
                # KC-47 widens that window while a `bash` call is still
                # running, and names the call in the stall when the widened
                # bound is what fired, so the next reader does not have to dig
                # in events.jsonl to learn the agent was running its tests.
                # KC-66: the gate's waits moved the deadline too, so a silence
                # inside that time is still a silence stall.
                silence = float(config.idle_event_timeout_sec or 0)
                limit = float(config.turn_timeout_sec) + clock.granted + clock.gate_added
                quiet = 0 < silence and idle.elapsed < limit
                if quiet:
                    turn["idle_status"] = "stalled"
                    open_tool = getattr(idle, "open_tool", None) or {}
                    if open_tool:
                        error = (f"no event for {silence:g}s during "
                                 f"{open_tool.get('tool', 'bash')}: "
                                 f"{open_tool.get('command') or '(no command)'}")
                    else:
                        error = f"no event for {silence:g}s"
                else:
                    error = _no_idle_error(config, idle.elapsed, clock)
                state = AgentState.STALLED
            elif idle.status == "error":
                overflow = _is_overflow(idle.error)
                if overflow:
                    _remember_overflow(idle.error)
                    # KC-54: the overflow has filled this session's context, so
                    # a prompt into it overflows again — never RETRY_PROMPT here,
                    # even when the provider flags the error retryable. The work
                    # is not lost, though: uncommitted edits go on in a *fresh*
                    # session, which has never seen the ticket or the round
                    # prompt, so it gets `round_prompt(dirty=)` (KC-22's
                    # `--resume` shape) rather than `continue_message`.
                    budget = int(config.max_continues_per_attempt)
                    dirty = ""
                    if _commits_above(ws) == 0:
                        try:
                            dirty = _dirty_tree(ws)
                        except TreeReadError as exc:
                            # FL-2: a status that could not be read is not "no
                            # uncommitted work" — say so, and let the stall
                            # below carry the "no work" reason rather than
                            # raising into the round.
                            _log.warning("%s: tree unreadable — %s", spec.name,
                                         _brief(str(exc)))
                    if dirty and 0 < budget and continue_used < budget:
                        failed = fresh_session(turn, dirty)
                        if failed:
                            return finish(AgentState.ERROR, failed)
                        continue
                    # A clean overflow is a model that spent its whole context
                    # reading and produced nothing — a stall, not a crash. Do
                    # not `return`: fall through to the KC-21 harvest below,
                    # which scores a commit the model made and then overflowed
                    # and can still end READY.
                    error = "context overflow" + ("" if dirty else " with no uncommitted work")
                    state = AgentState.STALLED
                # KC-61: the quota check comes first. `_RETRYABLE_MSG_RE`
                # matches `429`, so a daily limit that resets at midnight would
                # otherwise spend `max_error_retries` against a key that is
                # empty until then and end `after 2 retries: session.error:`
                # instead of naming the reason. The KC-21 harvest below still
                # runs, so an agent that committed before its key ran dry is
                # scored.
                if not overflow and _is_quota(idle.error, _quota_re(config)):
                    data = (idle.error or {}).get("data") or {}
                    at = data.get("retryAt")
                    when = (time.strftime(" (retry at %Y-%m-%d %H:%M UTC)",
                                          time.gmtime(float(at) / 1000))
                            if isinstance(at, (int, float))
                            and not isinstance(at, bool) else "")
                    text = _brief(_error_message(idle.error))
                    reason = text + when if text else when.strip()
                    error = f"provider_quota: {reason}".rstrip()
                    state = AgentState.ERROR
                else:
                    # KC-64: never retried. `_RETRYABLE_MSG_RE` matches the provider's
                    # "upstream unavailable" and "503" texts, so without this the
                    # spent provider gets `max_error_retries` more rounds on top of
                    # the `provider_retry_max_attempts` Kilo already spent.
                    down = _is_provider_unavailable(idle.error)
                    # KC-62: Kilo's own store refusing a write is neither the provider
                    # nor the model: the session is intact and the same turn goes on.
                    # It keeps its own budget and its own backoff — the provider's
                    # `retries_used` is not touched, so a 429 and a store error in one
                    # turn each spend their own counter. `max_local_store_retries = 0`
                    # turns it off, exactly as `max_error_retries = 0` does.
                    local_budget = int(getattr(config, "max_local_store_retries", 5) or 0)
                    if (not overflow and not down and _is_local_store(idle.error)
                            and local_retries < local_budget):
                        run.turns.append(turn)
                        _append_jsonl(agent_dir / "turns.jsonl",
                                      {"agent": spec.name, **turn, "cause": "local_store"})
                        local_retries += 1
                        # doubled per retry, then jittered ±30 %: four agents that hit
                        # the locked database in the same second must not retry in the
                        # same second again, and lock it back.
                        base = (float(getattr(config, "local_store_retry_backoff_sec", 10))
                                * (2 ** (local_retries - 1)))
                        backoff = base * random.uniform(0.7, 1.3)
                        _log.info("%s: kilo store error — retry %d/%d in %.0fs", spec.name,
                                  local_retries, local_budget, backoff)
                        if not _wait_backoff(backoff):
                            if stalled:
                                turn_r = {"kind": "retry", "attempt": run.attempt,
                                          "sent_at": time.time(), "idle_at": time.time(),
                                          "idle_status": "stalled"}
                                run.turns.append(turn_r)
                                _append_jsonl(agent_dir / "turns.jsonl",
                                              {"agent": spec.name, **turn_r})
                                return finish(AgentState.STALLED, stalled[0])
                        retry_text = RETRY_PROMPT.format(reason="kilo store error")
                        continue
                    # KC-45 §2a: KC-19's rules decide most `session.error`s. A
                    # mid-session `provider rejected the request` needs the count of
                    # the replies that already finished, so the transcript is read
                    # for that payload only — fail-open to 0, which is §2's answer.
                    # An overflow, a spent budget or any other error never reads it.
                    retryable = False
                    if not overflow and not down and retries_used < int(config.max_error_retries):
                        retryable = _retryable(idle.error)
                        if not retryable and _rejected_request(idle.error):
                            retryable = _retryable(
                                idle.error, finished=_finished_replies(backend, session))
                    if retryable:
                        run.turns.append(turn)
                        _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                        retries_used += 1
                        backoff = _retry_backoff(retries_used, config.error_retry_backoff_sec,
                                                 getattr(config, "error_retry_max_backoff_sec", 0))
                        _log.info("%s: retry %d/%d in %ds — %s", spec.name,
                                  retries_used, int(config.max_error_retries),
                                  backoff, _retry_reason(idle.error))
                        if not _wait_backoff(backoff):
                            if stalled:
                                turn_r = {"kind": "retry", "attempt": run.attempt,
                                          "sent_at": time.time(), "idle_at": time.time(),
                                          "idle_status": "stalled"}
                                run.turns.append(turn_r)
                                _append_jsonl(agent_dir / "turns.jsonl",
                                              {"agent": spec.name, **turn_r})
                                return finish(AgentState.STALLED, stalled[0])
                        retry_text = RETRY_PROMPT.format(reason=_retry_reason(idle.error))
                        continue
                    if down:
                        # KC-64: the provider's text is the reason, and the hint
                        # sits after `_brief`'s cut, never inside it — an exhausted
                        # plan reads the same as an outage, so the next reader has
                        # to look at the provider's site.
                        data = (idle.error or {}).get("data") or {}
                        error = (f"provider_unavailable after {data.get('attempts')} retries: "
                                 f"{_brief(data.get('message') or '')}"
                                 f"{_PLAN_HINT}")
                        state = AgentState.ERROR
                    elif not overflow:
                        # an overflow already set its own `error` and `state` above;
                        # this is the fallback for every other `session.error`
                        error = f"session.error: {_brief(idle.error)}"
                        if retries_used:
                            error = f"after {retries_used} retries: {error}"
                        # KC-62: the budget is spent — say how hard the runner tried
                        # the store, so the next reader does not read it as a model.
                        if local_retries and _is_local_store(idle.error):
                            error = f"after {local_retries} kilo store retries: {error}"
                        state = AgentState.ERROR
            elif idle.status == "closed":
                error, state = f"event stream closed: {_brief(idle.error)}", AgentState.ERROR
            elif idle.status == "idle":
                # A turn that ended idle is a good answer: the error budget
                # counts refusals in a row, so it starts over here — KC-62's
                # store budget too.
                retries_used = 0
                local_retries = 0
                # KC-22: a turn that ended idle with edits in the tree but no
                # commit is a model that has not handed in yet, not one that
                # handed in a wrong entry. Nudge it on in this same session —
                # do not harvest, which would fail every hard gate by
                # construction and burn the pytest roots on an unfinished tree.
                # A clean tree (the model did nothing) is *not* a continue: it
                # falls through to today's path (HARVESTING → REWORK/GAVE_UP),
                # which is the right answer for "you did nothing" — unless the
                # last reply was cut off at a token limit (KC-56). A rework
                # resets the counter; it is exhausted here only when the branch
                # still has no commit after the last nudge.
                budget = int(config.max_continues_per_attempt)
                if 0 < budget and continue_used < budget:
                    dirty = ""
                    if _commits_above(ws) == 0:
                        try:
                            dirty = _dirty_tree(ws)
                        except TreeReadError as exc:
                            # FL-2: a status that could not be read is not "no
                            # uncommitted work" — say so, and let the turn fall
                            # through to the harvest, which reads git itself.
                            _log.warning("%s: tree unreadable — %s", spec.name,
                                         _brief(str(exc)))
                    # KC-56: a reply cut off at `finish: "length"` is a model
                    # stopped mid-thought, not one that did nothing — clean
                    # tree or not, it goes on instead of being harvested.
                    cut = _cut_off(backend, session, spec.context_limit)
                    if cut is not None:
                        turn["cut_off"] = cut
                    if cut == "context":
                        # The window is full, so a continue here is the 4-second
                        # `ContextOverflowError` of round 74. A deliberate
                        # exception to KC-54's clean-overflow stall: the provider
                        # cut a reply off, it did not reject a prompt.
                        failed = fresh_session(turn, dirty)
                        if failed:
                            return finish(AgentState.ERROR, failed)
                        continue
                    if cut == "output" or dirty:
                        # the current turn keeps its own kind (initial/rework);
                        # the continue becomes the *next* PROMPTED turn, whose
                        # PROMPTED transition (with "(continue N of M)") runs at
                        # the top of the loop. Record this idle turn as it stands.
                        continue_text = CUT_OFF_MESSAGE if cut else continue_message(dirty)
                        continue_used += 1
                        run.turns.append(turn)
                        _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                        continue
                error = state = None
            else:
                error = state = None
            if state is not None:
                # KC-21: a turn that died with a commit under it has finished work,
                # so it is scored once before the terminal state lands — exactly as
                # the HARVESTING step and `_plan`'s resume branch score it. The
                # session is gone, the worktree is not; the verdict decides whether
                # the run counts as READY or stays in the state the turn earned.
                note = None
                if state in (AgentState.STALLED, AgentState.ERROR):
                    above = _commits_above(ws)
                    if above:
                        # KC-48: the session is gone, so what its calls left
                        # running goes before the harvest reads the tree and runs
                        # its suites next to it; `finally` reaps again, for what
                        # came after.
                        _record_reaped(run, spec.name, ws.path)
                        verdict = _harvest(ws, ticket_path, run_tests, config)
                        turn["harvest"] = {
                            "verdict": verdict.verdict,
                            "reasons": [r.code for r in verdict.reasons],
                            "elapsed": round(verdict.elapsed, 1),
                            "waited": round(verdict.waited, 1),
                        }
                        run.commit = None
                        if above == 1:
                            # the claim's sha once the harvest resolved it, else the
                            # one commit on the branch — `None` when there are two
                            # (amend them into one) or none (no harvest was run)
                            run.commit = verdict.commit or _head_sha(ws)
                        if verdict.verdict == "READY":
                            note = f"{(run.commit or '')[:12]} after {error}"
                            state = AgentState.READY
                            error = None
                if state is AgentState.ERROR and _is_local_store(idle.error):
                    # KC-62: the session is gone, the worktree is not — and with no
                    # commit under it, no commit for `_plan` to restart from either.
                    # The flag is what makes `--resume` start the agent again in this
                    # tree instead of leaving the turn dropped.
                    try:
                        if _dirty_tree(ws):
                            # KC-41 supersedes: the deadline commit lands the work
                            # here on the spot, and the flag goes away with it.
                            run.resumable = True
                    except TreeReadError as exc:
                        # FL-2: a status that could not be read is not a clean tree —
                        # no flag, and a warning so nobody reads it as one.
                        _log.warning("%s: tree unreadable — %s", spec.name,
                                     _brief(str(exc)))
                run.turns.append(turn)
                _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                return finish(state, error, note=note)

            # ── HARVESTING ─────────────────────────────────────────────────
            transition(AgentState.HARVESTING, note="tests on" if run_tests else "tests off")
            verdict = _harvest(ws, ticket_path, run_tests, config)
            turn["harvest"] = {"verdict": verdict.verdict,
                               "reasons": [r.code for r in verdict.reasons],
                               "elapsed": round(verdict.elapsed, 1),
                               "waited": round(verdict.waited, 1)}
            run.turns.append(turn)
            _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
            run.commit = verdict.commit
            label = "tests" if run_tests else "harvest"
            elapsed_str = f"({label} {_age(verdict.elapsed)})"
            if verdict.verdict == "READY":
                return finish(AgentState.READY, note=f"{(run.commit or '')[:12]} {elapsed_str}")
            if run.attempt >= int(config.max_rework):
                return finish(AgentState.GAVE_UP, "REWORK after the last attempt: "
                              + ", ".join(r.code for r in verdict.reasons if r.blocking))
            run.attempt += 1
            # KC-22: a rework resets the per-attempt continue counter; KC-62's local
            # store budget is per turn, not per attempt, so it resets there too.
            continue_used = 0
            local_retries = 0
            transition(AgentState.REWORK, note=f"attempt {run.attempt} {elapsed_str} — "
                       + ", ".join(r.code for r in verdict.reasons))
            rework_text = rework_message(verdict, run.attempt, int(config.max_rework))
    finally:
        if time_up is not None:
            time_up.cancel()
        if run.terminal:
            # KC-48: the turn is over — the session aborted, the stream closed,
            # or the state is whatever the run earned — so nothing the agent's
            # calls left standing may keep running in the tree the harvest and
            # the export are about to read.
            _record_reaped(run, spec.name, ws.path)
        if session is not None and run.terminal:
            _record_session(run, backend, session, out_dir)


def _record_session(run: AgentRun, backend: ContestBackend, session: SessionRef, out_dir: Path) -> None:
    """Cost, tokens and the transcript of a finished session. Fail-open."""
    try:
        info = backend.session_info(session)
        run.cost = info.get("cost")
        run.tokens = info.get("tokens")
    except Exception as exc:  # noqa: BLE001 — the server may be gone
        _log.warning("session_info(%s) failed: %s: %s", session.id, type(exc).__name__, exc)
    try:
        _write_json(out_dir / f"{run.agent.name}.session.json", backend.messages(session))
    except Exception as exc:  # noqa: BLE001
        _log.warning("messages(%s) failed: %s: %s", session.id, type(exc).__name__, exc)


# ─────────────────────────────────────────────────────────────────────────────
# the round
# ─────────────────────────────────────────────────────────────────────────────

class _Stopped(Exception):
    """Raised inside a worker's `on_transition` once Ctrl-C asked the round to stop."""


def _plan(config: ContestConfig, workspaces: list, ticket_path: Path,
          resume: RoundState | None, run_tests: bool = False) -> list:
    """The `AgentRun` per workspace: fresh, carried over, or restarted for resume.

    *run_tests* applies to the mid-flight harvest too: a resume must not call a
    tree READY that the round's own pytest roots would have rejected.
    """
    specs = {spec.name: spec for spec in config.agents}
    prior = {run.agent.name: run for run in resume.agents} if resume is not None else {}
    runs = []
    for ws in workspaces:
        run = prior.get(ws.agent)
        if run is None:
            run = AgentRun(agent=specs.get(ws.agent) or AgentSpec(ws.agent, "", ""), workspace=ws)
        elif not run.terminal or run.resumable:
            # mid-flight when the round died — or `ERROR` on Kilo's own store with a
            # dirty tree (KC-62): the session is gone, the worktree is not
            run.resumable = False      # spent on this restart, so a later plain ERROR is not
            run.workspace = ws
            verdict = _harvest(ws, ticket_path, run_tests, config)
            if verdict.verdict == "READY":
                run.state, run.commit = AgentState.READY, verdict.commit
            else:
                run.state, run.session_id, run.attempt = AgentState.CREATED, None, 0
                # KC-22, `--resume` into a worktree that still holds the agent's
                # uncommitted work: the fresh session learns of it on its first
                # prompt. A clean tree (or one with a commit under it) carries no
                # `dirty_on_resume`, so the prompt is unchanged and the agent
                # may not start over or discard the files.
                # `max_continues_per_attempt = 0` turns the whole mechanism off — this
                # half included: no tree read, no paragraph, today's prompt.
                if int(config.max_continues_per_attempt) > 0 and _commits_above(ws) == 0:
                    try:
                        dirty = _dirty_tree(ws)
                    except TreeReadError as exc:
                        # FL-2: the tree is not clean, it is unreadable — no
                        # paragraph, and a warning so nobody reads this as a
                        # clean worktree.
                        _log.warning("tree of %s unreadable — %s", ws.path,
                                     _brief(str(exc)))
                        dirty = ""
                    if dirty:
                        run.dirty_on_resume = dirty
        runs.append(run)
    return runs


def run_round(config: ContestConfig, round_no: int, ticket_path: Path, workspaces: list, *,
              make_backend: Callable[[Workspace], ContestBackend], out_dir: Path,
              resume: RoundState | None = None,
              run_tests: bool = False,
              server_pid: int | None = None) -> RoundState:
    """One round: a `run_agent` per workspace in a pool of `config.max_parallel`.

    Each agent gets its own `ContestBackend` for its directory: `make_backend`
    is called once per non-terminal agent and decides which backend that is
    (KC-34) — a `KiloBackend` over `kilo serve`, or an `OpenRouterBackend`
    subprocess agent. `run_round` never touches a client, a tap or a server:
    `backend.wait_ready()` before the first prompt, `backend.close()` after the
    last, are the only two calls it makes besides `run_agent`.

    `state.json` is rewritten atomically after every transition of any agent.
    With *resume*, terminal agents are skipped and mid-flight agents restart in
    their worktree (a tree that already scores READY needs no session). Ctrl-C
    tells the pool to stop, aborts every running session, writes `state.json`
    and re-raises.

    *run_tests* is the one KC-16 keyword: it is passed unchanged to the harvest
    after every turn and to the resume's mid-flight harvest, so the four pytest
    roots become the round's judge instead of the ticket's self-check. Off by
    default, and keyword-only — every earlier call of this function is untouched.

    *server_pid* (KC-62) is the round server's pid, so the heartbeat can count the
    other Kilo processes that share its store and say so on the line. `None` is
    every earlier caller: no check, no suffix.
    """
    out_dir, ticket_path = Path(out_dir), Path(ticket_path)
    workspaces = list(workspaces)
    runs = _plan(config, workspaces, ticket_path, resume, run_tests=run_tests)
    state = RoundState(round_no=round_no, ticket=ticket_path.name,
                       base_sha=workspaces[0].base_sha if workspaces else "",
                       started_at=resume.started_at if resume is not None else time.time(),
                       agents=runs)
    lock = threading.Lock()
    stop = threading.Event()
    since: dict = {run.agent.name: time.monotonic() for run in runs}   # state entered at

    # KC-65: the last worker count `save` wrote, so a save that sees the same
    # count does no I/O. `save` holds `lock`, so the memo needs none of its own.
    _workers_memo: dict = {}
    # KC-65: the round's share of `[contest] agent_tmpdir` — where the CLI
    # pointed the agents' $TMPDIR — is scratch the policy lets through.
    agent_tmp = agent_tmp_path(config, round_no)

    def save() -> None:
        with lock:
            _write_json(out_dir / "state.json", state.to_dict())
            # KC-65: the worker file follows the round too — a slot that just
            # freed is workers the next pytest can have.
            refresh_pytest_workers(out_dir, config, state, _workers_memo)

    def on_transition(run: AgentRun) -> None:
        if stop.is_set():
            raise _Stopped()
        since[run.agent.name] = time.monotonic()
        save()

    heartbeat = _Heartbeat(state, since, float(config.progress_every_sec or 0),
                           server_pid=server_pid,
                           neighbour_warn=int(getattr(config, "neighbour_kilo_warn", 4) or 0))

    policy = Policy(config)
    live: list = []                     # (run, backend) of every agent in the pool
    for run in runs:
        if run.terminal:
            continue
        live.append((run, make_backend(run.workspace)))
    save()

    def work(run: AgentRun, backend: ContestBackend) -> None:
        backend.wait_ready()
        try:
            run_agent(run, backend=backend, policy=policy, config=config,
                      ticket_path=ticket_path, out_dir=out_dir, on_transition=on_transition,
                      run_tests=run_tests, agent_tmp=agent_tmp)
        except _Stopped:
            pass
        finally:
            backend.close()

    pool = ThreadPoolExecutor(max_workers=max(1, int(config.max_parallel)),
                              thread_name_prefix="contest")
    heartbeat.start()
    interrupted = False
    try:
        futures = {pool.submit(work, *item): item[0] for item in live}
        for future in as_completed(futures):
            run = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 — one agent's crash is that agent's ERROR
                _log.exception("agent %s crashed", run.agent.name)
                run.state, run.last_error = AgentState.ERROR, f"runner: {type(exc).__name__}: {exc}"
                save()
    except KeyboardInterrupt:
        interrupted = True
        stop.set()  # from here on a worker's transition raises instead of saving
        # KC-48: the round's own sweep over every worktree, before the interrupt
        # below wakes the workers — so the save right after carries `reaped`
        # while the mid-flight agents are still saved mid-flight for --resume.
        _reap_round_worktrees(state.agents)
        save()      # the round as it stood, carrying the reap records
        for run, backend in live:
            session = None
            if not run.terminal and run.session_id:
                session = SessionRef(run.session_id, run.agent.provider_id,
                                     run.agent.model_id, str(run.workspace.path),
                                     run.agent.kilo_agent)
                _abort_quietly(backend, session)
            backend.interrupt(session)  # wakes the worker's wait
        raise
    finally:
        heartbeat.stop()
        pool.shutdown(wait=False, cancel_futures=True)
        # KC-48: the same sweep on a normal return, when every agent has ended and
        # the round's exit is the last chance to close the gap after one of them.
        # On Ctrl-C the handler already ran it and saved the round as it stood — a
        # second `save()` here would write the states the just-woken workers are
        # still dragging on, and turn a mid-flight agent into an ERROR for --resume.
        if not interrupted:
            _reap_round_worktrees(state.agents)
            save()
    return state


def _lock_status(run: AgentRun) -> tuple | None:
    """The round's pytest lock, as this run sees it — or None when there is none.

    `("queued", waited, ahead)` while it waits for the one pytest slot,
    `("running", elapsed, 0)` while it holds it. Fail-open: an absent lock is no
    queue to report, and a resumed `state.json` cannot have had one.
    """
    try:
        return _TEST_RUNS_LOCK.status(run.agent.name)
    except (AttributeError, TypeError, ValueError):
        return None


class _Heartbeat:
    """The once-a-minute line: the round's age and every agent's state, its
    files-based progress bar, and how long its last tests took —
    `round 66 14m: mimo WAITING 14m [######....] 60% 5f · ... — 3 live`.

    A daemon thread waiting on an `Event`, so `stop()` returns at once and the
    thread never outlives `run_round`. `every <= 0` starts nothing.
    """

    def __init__(self, state: RoundState, since: dict, every: float,
                 server_pid: int | None = None, neighbour_warn: int | None = None):
        self.state, self.since, self.every = state, since, every
        self.server_pid, self.neighbour_warn = server_pid, neighbour_warn
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="contest-progress", daemon=True)

    def start(self) -> None:
        if self.every > 0:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(2.0)

    def _harvest_note(self, run: AgentRun) -> str | None:
        """What the line says about this run's roots, or None for no suffix.

        KC-57: a run in `HARVESTING` is read live off the round's lock — `queued`
        while it stands behind the one pytest slot, `tests` once it holds it, so
        the queue that used to be invisible to the round is now on the line. Any
        other state shows the last harvest that finished.
        """
        if run.state is AgentState.HARVESTING:
            status = _lock_status(run)
            if status and status[0] == "queued":
                ahead = f", {status[2]} ahead" if status[2] else ""
                return f"(queued {_age(status[1])}{ahead})"
            if status and status[0] == "running":
                return f"(tests {_age(status[1])})"
        elapsed = self._last_harvest_elapsed(run)
        if elapsed is not None:
            return f"(tests {_age(elapsed)})"
        return None

    def line(self) -> str:
        now = time.monotonic()
        parts = []

        # First pass: collect files for all non-terminal agents; the median
        # is over the agents currently working (WAITING/REWORK), floor 1.
        files_by_name = {}
        working_files = []
        for run in self.state.agents:
            if not run.terminal:
                f = _worktree_files(run.workspace)
                files_by_name[run.agent.name] = f
                if run.state in (AgentState.WAITING, AgentState.REWORK):
                    working_files.append(f)
        median = max(1.0, statistics.median(working_files)) if working_files else 1.0

        for run in self.state.agents:
            part = f"{run.agent.name} {run.state.value}"
            if not run.terminal:
                part += f" {_age(now - self.since.get(run.agent.name, now))}"
                files = files_by_name.get(run.agent.name, 0)
                if run.state in (AgentState.WAITING, AgentState.REWORK):
                    committed = _commits_above(run.workspace) > 0 and run.attempt == 0
                else:
                    committed = False
                pct = _progress(run.state, files, median, committed)
                if pct is not None:
                    part += f" {_bar(pct)} {pct}% {files}f"
                # KC-36: a turn running past its nominal clock says so, so an
                # extended agent is not mistaken for a hung round. The suffix
                # rides the attempt marker when there is one.
                live_clock = getattr(run, "_turn_clock", None)
                granted = float(live_clock.granted) if live_clock is not None else 0.0
                if run.attempt:
                    part += f" ↺{run.attempt}"
                if granted > 0:
                    part += f"+{_age(granted)}" if run.attempt else f" +{_age(granted)}"
            note = self._harvest_note(run)
            if note:
                part += f" {note}"
            parts.append(part)

        live = sum(1 for run in self.state.agents if not run.terminal)
        age = _age(time.time() - self.state.started_at)
        text = f"round {self.state.round_no} {age}: " + " · ".join(parts) + f" — {live} live"
        count = self._kilo_neighbours()
        if count is not None:
            # KC-62: a store this busy is what ends agents on `Failed to execute
            # statement` — the operator needs to see the crowd, not just the states.
            text += f" · kilo neighbours {count}"
        return text

    def _kilo_neighbours(self) -> int | None:
        """KC-62: how many other Kilo processes share the round server's store,
        `None` when the line must not name a number.

        `None` when there is no server pid, no threshold to compare against, or a
        threshold that is not a number — every earlier caller, and every failure
        of `kilo_neighbours`, ends here rather than in a broken heartbeat line.
        """
        if self.server_pid is None or self.neighbour_warn is None:
            return None
        try:
            warn = int(self.neighbour_warn)
        except (TypeError, ValueError):
            return None
        count, _dir = kilo_neighbours(self.server_pid)
        return count if count > warn else None

    def _last_harvest_elapsed(self, run: AgentRun) -> float | None:
        """The `elapsed` from the last turn's harvest, or None."""
        for turn in reversed(run.turns):
            h = turn.get("harvest")
            if h and "elapsed" in h:
                return h["elapsed"]
        return None

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            _log.info("%s", self.line())


def _bar(pct: int) -> str:
    """A ten-cell progress bar: `[######....]` for 60 %."""
    filled = max(0, min(10, round(pct / 10)))
    return "[" + "#" * filled + "." * (10 - filled) + "]"


def _progress(state: AgentState, files: int, median: float, committed: bool) -> int | None:
    """A files-count progress estimate per cent, or None when no bar.

    A files-count against the pack's median, not a measure of the work —
    good enough to see the field converging and one agent stuck at 1 file
    for 20 minutes. An agent at or above the pack's median is at 60 %;
    one with a commit on its first attempt is at 70 %. HARVESTING is 80 %,
    READY is 100 %. Terminal states other than READY are None.
    """
    if state in (AgentState.CREATED, AgentState.PROMPTED):
        return 0
    if state in (AgentState.WAITING, AgentState.REWORK):
        if committed:
            return 70
        return min(60, round(60 * files / max(median, 1)))
    if state is AgentState.HARVESTING:
        return 80
    if state is AgentState.READY:
        return 100
    return None


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds}s" if seconds < 90 else f"{seconds // 60}m"
