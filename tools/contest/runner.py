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
harvest, no pytest, `commit: null` — unless KC-41 is on.

KC-41 (round 80) removes that last precondition. A turn that ends with the work
on disk and nothing committed was scored as though it had produced nothing, and
the round dropped it: round 64 held two entries that pass all four pytest roots
and harvested none, and round 92's `laguna-s-2-1` worked its whole turn, was
given three deadline extensions, and left 510 uncommitted lines behind. So when
`config.deadline_commit` is on (the default; `contest.ini` says so), the
terminal branch commits what the tree holds — `WIP (deadline commit, <reason>):
<ticket>`, `runs/` excluded from the add — writes the one
`runs/<agent>/PROGRESS.csv` row `harvest` asks for, and harvests it exactly as
it harvests a commit the model made. `run.deadline_commit` and the turn's
`deadline_commit: true` say which kind of entry it was, so a `READY` from a
deadline commit is never read as a claim the model finished. Both writes are
fail-open: a clean tree, a tree that cannot be read and a commit that refuses
all answer "nothing to commit", and that is the pre-KC-41 path byte for byte.
`deadline_commit = false` restores it deliberately.

KC-29 (round 68) draws the line KC-21 left open: a stall the runner asked for
is not a session that finished. `stall()` is the runner's own edge — the
questions cap, the agent's hard limit — and its `STALLED` turns keep the state
and `last_error` whatever the harvest's verdict: the harvest still runs,
`turn["harvest"]` and `run.commit` are set, and `export_patches` still names
the patch by the terminal state. What changes is the promotion — a READY
verdict finishes the run as READY only when the session ended on its own: a
silence, the turn clock, a `session.error`. An aborted turn stays STALLED,
the KC-18 line names the verdict in place —
`agent-a: STALLED — 3 questions in one turn (harvest: READY)` — and a round
of nothing but aborted agents exits the way a round of stalls always has,
because a READY is what `cmd_run` counts.

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

KC-53 (round 97) tells the agent which scratch space needs no reviewer, because
the prompt before only said "copy `agents_128k.ini` to a scratch path" and "any
command that reaches outside your worktree is decided by a reviewer" and never
which `/tmp` paths were free — round 86 run 4 wrote to `/tmp/scratch_suite`,
`/tmp/debug_argv.py` and friends, and every one of them went to the gate.
`round_prompt(tmp_roots=)` lists the round's globs in their own paragraph right
before the reviewer sentence, and names one folder for the agent's own files:
KC-59's scratch dir when the round derived one — the dir that exists and that
the note after it names, so the prompt never offers two different "own" dirs —
and otherwise `/tmp/contest/<agent>/`, only when the round names that root. No
`tmp_roots` leaves the prompt byte for byte, and a malformed key degrades to the
same as none. The same change names one more mechanical rule:
git commands run one after another, never as parallel tool calls, because two
collide on the worktree's `index.lock`.

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

The work of a spent budget is kept rather than dropped, and KC-41 keeps it
twice over: an `ERROR` on a store error whose tree is still dirty is written to
`state.json` with `resumable: true`, and the same turn's deadline commit puts
that work on the branch where the harvest scores it. `_plan` restarts such an
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
remembered for that provider and model is the number, and then the round's
`context_limit_fallback` (KC-10) for the free tiers the provider declares no
limit for at all; a session that holds `compact_at_percent` of it is compacted
first — one `POST /session/{id}/summarize`, then the prompt. The fill and
whether a compact happened go onto every `turns.jsonl` line, the run's compact
count rides on the run and into SUMMARY. Every failure degrades to "no size",
which is today's prompt, and every overflow is recorded whether or not the
round ends STALLED.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
import os
import random
import re
import signal
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable

from tools.backoff import save_state
from tools.contest import context_memory
from tools.contest.backend import (ContestBackend, ContestBackendError, KiloLimitKept,
                                  KiloLimitRefused, KiloTapReconnectError,
                                  drop_stale_kilo_file)
from tools.contest.gates import DEADLINE_COMMIT_EMAIL, declared_files, git
from tools.contest.harvest import harvest, rework_message
from tools.contest.kilo_client import (
    AGENT_TEST_TIMEOUT_MS,
    SessionRef,
    kilo_neighbours,
)
from tools.contest import testcache as _testcache
from tools.contest.testcache_glue import TestCacheGate
from tools.contest.policy import (HARD_DENYLIST, Decision, Policy, PolicyContext,
                                  is_full_suite_command)
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
    "IdleKind",
    "RELAY_STATES",
    "RoundState",
    "classify_idle",
    "CONTINUE_PROMPT",
    "leg_message",
    "leg_record",
    "LEG_RECORD_MAX_LINES",
    "round_prompt",
    "run_agent",
    "run_leg",
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
        # Per agent: when it asked for the lock and when it got it; absent after it exits.
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


#: One worktree's pytest roots run at a time, since parallel suites are very slow (KC-50).
_TEST_RUNS_LOCK = _TestRunsLock()

#: Git timeout per worktree for the heartbeat, short, since a late tick is not a failed one (KC-27).
_TICK_GIT_TIMEOUT_S = 5.0
_TICK_GIT_RETRIES = 1

#: Seconds between TERM and KILL for a leftover process in a finished worktree (KC-48).
REAP_GRACE_SEC = 5.0

#: The proc root the reap scans; a constant, so a test can point it at a missing directory (KC-48).
_PROC_ROOT = "/proc"

#: How long the reap waits for a SIGKILLed process to go; only an uninterruptible one takes long (KC-48).
_REAP_KILL_WAIT_SEC = 2.0

#: KC-48: how much of a reaped process's command line `state.json` keeps.
_REAP_CMD_CHARS = 120

# Guards the reap record, which two threads may update at once (KC-48).
_REAPED_LOCK = threading.Lock()


# KC-65: the agents' pytest worker count

#: File holding the pytest-xdist worker count; the runner writes it and the plugin reads it (KC-65).
WORKERS_FILE = "pytest-workers"

#: Last lines of every prompt, with the current worker count; kept last so older prompt tests still pass (KC-65, KC-22).
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

    KC-68: with KC-58's suite slots armed, no more than `agent_suite_slots`
    whole roots run at once, whatever the crowd — so the count is the cores
    split between the suites that can run at once, never below the crowd's own
    count. Round 69: eight live agents, two slots, 8 cores gave every root two
    workers, so two roots used four cores and the judge's serialized harvests
    ran one by one at `-n 2`. A targeted run holds no slot and reads the same
    file; it is short, so the brief over-subscription is the cheaper side.
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
    slots = _suite_slots_armed(config)
    if slots > 0:
        count = max(count, cores // max(1, min(live, slots)))
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


#: The prompt sent after a retryable session error; {reason} is the brief error text (KC-19).
RETRY_PROMPT = (
    "The provider dropped the connection mid-turn ({reason}). "
    "Your worktree and this conversation are intact — continue from where you "
    "were; do not start over."
)

#: The prompt sent when a turn ended unfinished: cut off, or silent past the idle window (KC-9).
CONTINUE_PROMPT = (
    "Your previous reply stopped before the task was finished. Continue exactly "
    "where you left off in this same worktree: finish the change, make sure it "
    "is one commit, then record it with `append_task.py`. Do not start over."
)

_RETRYABLE_CODES = frozenset({
    "ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "EPIPE", "UND_ERR_SOCKET",
})
_RETRYABLE_MSG_RE = re.compile(
    # a status code standing alone: `req_429ab503`, a request id, is no 429
    r"(?<!\w)(?:429|502|503|504)(?!\w)|overloaded|rate limit|timeout"
    r"|interrupted the response|upstream unavailable", re.IGNORECASE
)

#: Kilo's own store refusing a write (shared SQLite), retried on its own budget, not the provider's (KC-62).
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

#: Provider refusal of the request itself; retried on resend, and a gateway 403 is the same class (KC-45, round 104).
_PROVIDER_REJECTED_RE = re.compile(
    r"provider rejected the request|blocked by a gateway or proxy", re.IGNORECASE)

#: Context overflow payload, by its name or either spelling of its message (KC-54).
_OVERFLOW_RE = re.compile(
    r"ContextOverflowError|maximum context length|context_length_exceeded", re.IGNORECASE
)

#: Kilo's own compaction-exhausted message, under the same overflow name, since Kilo's wall is the window (round 146).
_KILO_WALL_RE = re.compile(r"compaction\s+exhausted", re.IGNORECASE)

#: Matches a request refused for its size, by its shape (size noun plus overflow word) (round 152).
_SIZE_REFUSAL_RE = re.compile(
    r"\b(?:prompt|context|input)s?\b[^.\n]{0,60}?"
    r"(?:\btoo\s+(?:long|large|big)\b|\bexceed\w*|\bover\s+the\s+(?:max\w*|limit)\b"
    r"|\blonger\s+than\s+(?:the\s+)?(?:max\w*|limit|context|allowed\s+length)\b)"
    r"|\bexceed\w*\b[^.\n]{0,40}?\b(?:context|length|size|tokens?)\b"
    r"|\btoo\s+(?:long|large|big)\b[^.\n]{0,40}?\b(?:context|length|size|tokens?|model)\b"
    r"|\btoo\s+many\s+(?:input\s+|prompt\s+)?tokens\b"
    r"|\bmaximum\s+(?:prompt|context|input)\s+(?:length|size)\b[^.\n]{0,40}?"
    r"(?:\b\d[\d,]*(?!\s*(?:kb|mb|gb|tb|bytes?)\b)\b|\b(?:tokens?|length|size|limit|allowed)\b)"
    r"|\btokens?\b[^.\n]{0,50}?\bmust\s+be\s*(?:<=|<|=|at\s+most|no\s+more\s+than)\s*"
    r"\d[\d,]*",
    re.IGNORECASE,
)

#: Refusals with size words that are not size refusals: quota, rate, upload, output and attachment caps.
_NOT_SIZE_RE = re.compile(
    r"rate.?limit|too\s+many\s+requests|quota|credit|balance|payment"
    r"|free.?tier|unauthori[sz]ed|forbidden|api.?key"
    r"|per\s+(?:min|minute|hour|day)|\bTPM\b|\bRPM\b"
    r"|timed?\s*out|deadline\s+exceeded|time\s+limit|overloaded|queue\s+exceeded"
    r"|entity\s+too\s+large|body\s+(?:exceeds|too)"
    r"|max_tokens|output\s+tokens"
    r"|\bimages?\b|attachment"
    r"|token\s+budget|budget\s+exceeded|allowance",
    re.IGNORECASE,
)

#: Vetoes that a named context wall overrides; attachment never is.
_WALL_VETO_RE = re.compile(
    r"\bimages?\b|token\s+budget|budget\s+exceeded|allowance", re.IGNORECASE)

#: Size verbs that _SIZE_REFUSAL_RE looks for; a display error is not a model size (A1).
_SIZE_VERB_RE = re.compile(
    r"\btoo\s+(?:long|large|big)\b|\bexceed\w*\b|\bover\s+the\s+(?:max\w*|limit)\b"
    r"|\btoo\s+many\s+(?:input\s+|prompt\s+)?tokens\b",
    re.IGNORECASE,
)

#: Share of the known window at which a size-less refusal still counts as an overflow (round 149).
FULL_REFUSAL_SHARE = context_memory.DEFAULT_FULL_REFUSAL_PERCENT / 100.0

#: A repeat overflow much smaller than the first is not the window again; another compact would only loop.
REPEAT_OVERFLOW_SHARE = 0.5

#: HTTP statuses that can be a size refusal (400, 413); auth, rate, server and schema errors never are (round 152).
_SIZE_REFUSAL_STATUSES = (400, 413)

#: A 400 that names another cause (bad body, schema, safety filter) is not a size refusal (wordless reading only).
_REQUEST_FAULT_RE = re.compile(
    r"\bjson\b|tool[\s_-]*call|function[\s_-]*call|\bschema\b|invalid\s+(?:argument|parameter|request\s+format)"
    r"|content[\s_-]*(?:policy|filter|management)|moderation|\bflagged\b|\bsafety\b"
    r"|validation\s+error|is\s+required|unexpected\s+(?:end|token|property)",
    re.IGNORECASE,
)
_CONTEXT_WALL_RE = re.compile(r"\bcontext\s+(?:limit|length|window|size)\b", re.IGNORECASE)
_OUTPUT_CAP_RE = re.compile(r"`?max_tokens`?|output\s+tokens", re.IGNORECASE)
_TOKENS_RE = re.compile(r"\btokens?\b", re.IGNORECASE)

#: Reply cut at the length limit at this share of the context is a full context, not an output cap (KC-56).
CONTEXT_FULL_SHARE = 0.9

#: Prompt kinds that go into a session already holding a conversation; only these may be compacted first (KC-67).
_CONTEXT_GATE_KINDS = ("rework", "continue", "retry")

#: Characters per token when a summary is sized from its text (KC-69).
SUMMARY_CHARS_PER_TOKEN = 4

#: Refusal text for a permission asked when the session is full, so the turn ends and the runner compacts (KC-69).
CONTEXT_FULL_REJECT = (
    "Refused: this session's context is nearly full. Do not retry this call. "
    "Stop here and end your turn; the session will be summarized and you will "
    "continue the same task right after the summary.")

#: Seconds after a full-context refusal before the turn is aborted, so the reply can reach the server (KC-69).
CONTEXT_ABORT_DELAY_SEC = 0.5

#: What a new session gets after a failed or still-full compact: the old summary and an instruction to carry on (KC-69).
CONTEXT_CONTINUE_NOTE = (
    "## You are continuing unfinished work\n\n"
    "Your previous session ran out of context and was closed. Below is the summary "
    "it wrote of everything done so far. The task is NOT finished: continue it from "
    "where it stopped. Do not start over, do not redo finished steps, and re-read "
    "only the files you need for the next step.\n\n"
    "### Summary of the previous session\n\n{summary}")

#: The continue sent after an overflow was compacted; the summary is already the history (KC-69).
OVERFLOW_CONTINUE = (
    "The context overflowed and the session was summarized: the summary above is "
    "what you did so far. The task is not finished. Continue it from where you "
    "stopped; do not start over, and read only the parts of files you need now.")

#: Prompt sent before a compact when a session with an uncommitted diff is near full; the reply is the summary (KC-40).
SUMMARY_PROMPT = (
    "Your context is nearly full and nothing is committed yet. Before anything "
    "else, write a short summary here in the chat of exactly what you changed, "
    "why, and what is left to do. Do not modify any files in this reply.")

#: Note added to the prompt after a summary turn, so the model does not restate the ticket (KC-40).
SUMMARY_CONTINUES_NOTE = (
    "Your summary above still applies: it is what this conversation is about. "
    "Carry it on, do not restate it, and do not re-read what it already covers.")

#: Note for a fresh session that replaced one whose summary failed; carries the partial answer, if any (KC-40).
SUMMARY_FELL_BACK_NOTE = (
    "## A previous attempt at this ticket ran out of context\n\n"
    "A previous session working this ticket ran out of context and was closed. It "
    "left this note before it did; it may be incomplete, so treat it as a lead "
    "and re-read the files it names.\n\n{note}")

#: Seconds a session with an overflowed turn may stay silent before the runner takes over (KC-73).
OVERFLOW_SETTLE_SEC = 3.0

#: Continue message for a reply cut at the output limit with no text or tool call (KC-56).
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


def _is_external_abort(error) -> bool:
    """Round 87: True when *error* is Kilo's `MessageAbortedError`.

    The runner never ends a turn this way itself. Its own stops come back as
    `stalled`, or as its own `ProviderQuota` / `ProviderUnavailable` — and
    KC-69's stop for a full context, which `run_agent` reads as a plain idle
    before this is asked (round 49). So any other abort that reaches a turn
    was Kilo's: its server shut down (the Ctrl-C that
    stops the round reaches `kilo serve` in the same process group) and it
    aborted every session it held on the way out.
    """
    return isinstance(error, dict) and error.get("name") == "MessageAbortedError"


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
        # A count that cannot be trusted is no count, so an unreadable transcript proves nothing (§2).
        return isinstance(finished, int) and not isinstance(finished, bool) and finished > 0
    return False


def _error_texts(error) -> str:
    """``name``, ``data.message`` and a top-level ``message`` of *error*, joined
    by spaces; a plain string is itself; anything else is ``""``."""
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
    return " ".join(parts)


def _not_a_size(text: str, quota_re=None) -> bool:
    """Money, a plan, a key or a rate (`_NOT_SIZE_RE`, *quota_re*) — never an overflow.

    A cap named beside the context wall (`_CONTEXT_WALL_RE`) is a term of the
    sum, not the refusal: ``input length and `max_tokens` exceed context
    limit: 188240 + 21333 > 200000`` is an overflow. So is a cap the wall only
    mentions in passing — `_WALL_VETO_RE`: ``Your prompt with 3 images exceeds
    the context window`` is the window, not the attachments. A message with no
    wall keeps every veto: `input exceeds 20 images` and `image attachment too
    large` stay the refusals they are."""
    if _CONTEXT_WALL_RE.search(text) is not None:
        text = _OUTPUT_CAP_RE.sub(" ", text)
        text = _WALL_VETO_RE.sub(" ", text)
    if _NOT_SIZE_RE.search(text):
        return True
    return quota_re is not None and quota_re.search(text) is not None


def _is_overflow(error, quota_re=None) -> bool:
    """True when *error* is a context overflow (KC-54).

    An overflow has filled the session's context, so a prompt into the *same*
    session overflows again: the runner opens a fresh session for the work
    instead of retrying, and a clean tree is a stall rather than a crash.
    True when ``name`` is ``"ContextOverflowError"``, or when ``data.message``
    (or a top-level ``message``) carries ``"maximum context length"`` or
    ``"context_length_exceeded"``. A plain string matches on any of those
    three. A payload that is not a dict, or carries none of them, is not an
    overflow — ``None`` and ``{}`` are False.

    Round 145: also when the text is a size refusal in the provider's own
    words (`_SIZE_REFUSAL_RE`) that is not money, a plan, a key or a rate
    (`_NOT_SIZE_RE`, and *quota_re* — the round's ``quota_patterns`` — when
    the caller has it). zai's ``Prompt exceeds max length`` ended glm-4.5-flash
    ERROR at 98 777 tokens of a window Kilo called 131 072: no memory, no
    compact, the run lost.
    """
    text = _error_texts(error)
    if _OVERFLOW_RE.search(text) is not None:
        return True
    data = error.get("data") if isinstance(error, dict) else None
    status = data.get("statusCode") if isinstance(data, dict) else None
    if status is not None and status not in _SIZE_REFUSAL_STATUSES:
        # a size's words on a rate's or the provider's status: `_overflow_of` agrees
        return False
    return _SIZE_REFUSAL_RE.search(text) is not None and not _not_a_size(text, quota_re)


def _is_full_refusal(error, last_ok: int, size, quota_re=None,
                     share: float = FULL_REFUSAL_SHARE) -> bool:
    """Round 145: a provider refusal of a session that is already full enough to
    be refused for its size, though the message says nothing about one.

    True when *error* is the provider refusing the request itself — a dict whose
    ``data.statusCode`` is present and one of `_SIZE_REFUSAL_STATUSES` (400 and
    413; a status that is not there at all, or a 422, is the request or its
    shape, not its size) — whose ``name`` is an API error, not flagged retryable
    in any of the runner's senses (`_retryable`, which reads
    ``data.isRetryable`` and the socket codes too), not Kilo's own abort, not an
    auth error, and not money/plan/key/rate. A message that carries a size word
    (`_SIZE_VERB_RE`) the wording path declined for lack of the model's own noun
    is one too — A1 left `Your error message is too large to display` out on
    purpose, and this reading takes the message that says nothing about a size.
    ``last_ok`` (the last reply that
    went through) must then be at least *share* of *size*, the window the runner
    sizes the model by; *share* 0 is the reading off.

    A refusal with no status, no size to measure against, no reply that went
    through, or the session's first request is never one: a model that cannot
    take the round prompt at all (round 144's deepseek-free) is not an overflow
    to compact. An inferred reading never names a size, so it never writes one
    to the memory.
    """
    if not isinstance(error, dict) or not size or not last_ok or last_ok <= 0:
        return False
    if not share or share <= 0:
        return False
    name = error.get("name")
    if _is_provider_unavailable(error) or _is_external_abort(error) \
            or name == "ProviderQuota":
        return False
    if isinstance(name, str) and "Auth" in name:
        return False
    if not (name == "APIError" or (isinstance(name, str) and name.endswith("APIError"))):
        return False
    if _retryable(error):
        return False
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    if data.get("statusCode") not in _SIZE_REFUSAL_STATUSES:
        return False
    text = _error_texts(error)
    if _not_a_size(text, quota_re):
        return False
    if _REQUEST_FAULT_RE.search(text) is not None and _TOKENS_RE.search(text) is None:
        return False
    # A size word that the wording path declined is not the session's size; this reading covers the rest.
    if _SIZE_VERB_RE.search(text) is not None and _SIZE_REFUSAL_RE.search(text) is None:
        return False
    return last_ok >= share * float(size)


def _is_provider_unavailable(error) -> bool:
    """KC-64: wait_idle ended the turn after too many provider retries.

    Never retried by the runner: Kilo already tried ``provider_retry_max_attempts``
    times, and the runner's ``max_error_retries`` would spend two more full
    rounds of the same against a provider that is not answering. True only for
    the payload ``wait_idle`` builds itself — ``name == "ProviderUnavailable"``;
    anything else, including a string or a payload that is not a dict, is False.
    """
    return isinstance(error, dict) and error.get("name") == "ProviderUnavailable"


#: Hint appended when a provider's 503 may be an exhausted plan rather than an outage (KC-61).
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


def classify_idle(backend: ContestBackend, session: SessionRef,
                  turn_started_at: float, workspace: Workspace | None = None) -> IdleKind:
    """KC-9: how the turn that just went idle actually ended.

    Reads the session's messages once and decides, in order:

    * ``FINISHED`` — the last assistant message has non-empty text, or its last
      part is a ``tool`` part with ``state.status == "completed"`` and the
      agent's ``PROGRESS.csv`` was modified after *turn_started_at*.
    * ``CUT`` — the last assistant message has empty text and its last part is
      a ``tool`` part with ``status`` ``"running"`` or ``"pending"``, or the
      message ``info.error`` is set, or ``info.finish`` / ``finish_reason`` is
      ``"length"``.
    * ``SILENT`` — empty text, no ``tool`` parts in this turn at all.
    * otherwise ``FINISHED`` — let the harvest decide.

    Every failure degrades to ``FINISHED``: a transcript that cannot be read,
    a malformed message, or a ``PROGRESS.csv`` that cannot be statted is not
    an exception into the round.
    """
    try:
        messages = backend.messages(session)
    except Exception:  # noqa: BLE001 — a broken transcript is not a round
        return IdleKind.FINISHED
    if not isinstance(messages, list):
        return IdleKind.FINISHED

    last_assistant = None
    for message in reversed(messages):
        info = message.get("info") if isinstance(message, dict) else None
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        last_assistant = message
        break

    if last_assistant is None:
        # No assistant message means the backend keeps no transcript, not that the model was silent.
        return IdleKind.FINISHED

    info = last_assistant.get("info") or {}
    parts = last_assistant.get("parts") or []

    text = ""
    tool_parts = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = part.get("text", "")
        elif part.get("type") == "tool":
            tool_parts.append(part)

    if text:
        return IdleKind.FINISHED

    if info.get("error"):
        return IdleKind.CUT

    finish = info.get("finish") or info.get("finish_reason")
    if finish == "length":
        return IdleKind.CUT

    if tool_parts:
        last_tool = tool_parts[-1]
        state = last_tool.get("state") or {}
        status = state.get("status", "")
        if status in ("running", "pending"):
            return IdleKind.CUT
        if status == "completed":
            try:
                if workspace is not None:
                    mtime = workspace.progress_csv.stat().st_mtime
                    if mtime > turn_started_at:
                        return IdleKind.FINISHED
            except OSError:
                pass
            return IdleKind.FINISHED
        return IdleKind.FINISHED

    return IdleKind.SILENT


def _context_limit_fallback(value) -> int:
    """KC-10: ``context_limit_fallback`` as a positive int, else ``0`` — off.

    The round sets it from the ini, where ``roster`` already clamps a typo to 0.
    A caller that builds a config of its own may not have, so a bool, a missing
    key, letters or a non-positive number all stand the rule down rather than
    sizing every prompt against a nonsense window.
    """
    if isinstance(value, bool):
        return 0
    try:
        number = int(str(value).strip().replace(",", "") or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _declared_window(spec) -> int | None:
    """The window Kilo declares for *spec*, as a positive int, else ``None``.

    ``None`` is the size the round has to supply itself — the memory, then
    ``context_limit_fallback`` — which is what the floor is lowered against
    (`context_memory.size_of`, `_overflow_of`). A missing key, a bool, a
    non-int or a non-positive number all mean the same: nothing declared.
    """
    declared = getattr(spec, "context_limit", None)
    if isinstance(declared, bool) or not isinstance(declared, int) or declared <= 0:
        return None
    return declared


def _context_budget(spec, records, config=None) -> tuple:
    """KC-67 + KC-10: ``(size, source)`` for *spec*'s model.

    Kilo's own ``limit.context`` first — intake's read, put on the spec: then
    Kilo compacts the session on its own and the runner does nothing more with
    the number, which is why the sources are told apart. Otherwise the smallest
    size remembered for that provider and model, ``"remembered"`` — KC-67's,
    the provider's own words out of its last overflow, the models the provider
    declares no limit for. Otherwise the round's ``context_limit_fallback``
    (KC-10), ``"fallback"``: the window the round assumes for the free tiers,
    which send only a name and their reasoning and overflow the same way every
    round. ``("none", "none")`` when there is nothing at all — not even a
    fallback — which is today's prompt and today's overflow.

    Round 145: a remembered size *smaller* than Kilo's wins over it — the
    provider refused below what it declares. Round 149: a *loose* remembered
    size (``grew`` far past ``last_ok``) only wins when it is close enough to
    the declared window to be evidence of it — `context_memory.smallest_size`
    measures that against *spec*'s own ``context_limit``, the fallback and
    ``context_full_refusal_percent``.
    """
    limit = getattr(spec, "context_limit", None)
    share_pct = context_memory.full_refusal_percent(config)
    fallback = _context_limit_fallback(getattr(config, "context_limit_fallback", None))
    size = context_memory.smallest_size(
        records, spec.provider_id, spec.model_id,
        context_memory.min_window(config),
        declared=limit, fallback=fallback, share_pct=share_pct)
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        # The smaller of Kilo's declared limit and a remembered overflow is the window (round 145).
        if size is not None and int(size) < limit:
            return int(size), "remembered"
        return int(limit), "kilo"
    if size is not None:
        return int(size), "remembered"
    if fallback:
        return fallback, "fallback"
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

    KC-69: a compact's summary is where the history now starts, so a summary
    met before any reply is the fill — `_summary_size` of it, the text's size
    when Kilo gave it no tokens. Skipping it for the reply before it (live,
    laguna: 64 894 read again after a compact to 15 000) refused every
    permission after the compact that was meant to free them.
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
        if info.get("summary"):
            return _summary_size(message) or 0
        tokens = info.get("tokens")
        used = _tokens_used(tokens) if isinstance(tokens, dict) else 0
        if used > 0:
            return used
    return 0


def _last_reply(backend: ContestBackend, session: SessionRef) -> tuple[int, int | None]:
    """``(last_ok, grew)`` for an overflow's record (KC-67, KC-73).

    ``last_ok`` is the last *reply* that went through, in tokens:
    `_context_tokens` without KC-69's summary rule — a summary is the fill of a
    compacted session, but it is not a reply the provider accepted. Round 49's
    agnes-2-0-flash: on the overflow Kilo started its own chunked compact at
    once, and the session's last assistant message was that summary, every
    count zero — remembered as ``last_ok: 0`` with a 247 386-token reply just
    before it. Summaries and messages that report no tokens are skipped.

    ``grew`` is what that reply's tool
    results added before the next request — the one that overflowed — in
    tokens at `SUMMARY_CHARS_PER_TOKEN` of their text: a reply that asked for
    twenty `read`s went through at 17 382, and its results were ~269 000 more.
    ``None`` when there is no such reply or the transcript cannot be read.
    """
    try:
        messages = backend.messages(session)
    except Exception:  # noqa: BLE001 — a transcript that cannot be read is no size
        return 0, None
    if not isinstance(messages, list):
        return 0, None
    for message in reversed(messages):
        info = message.get("info") if isinstance(message, dict) else None
        if not isinstance(info, dict) or info.get("role") != "assistant" or info.get("summary"):
            continue
        tokens = info.get("tokens")
        used = _tokens_used(tokens) if isinstance(tokens, dict) else 0
        if used <= 0:
            continue
        chars = 0
        for part in message.get("parts") or ():
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            state = part.get("state")
            output = state.get("output") if isinstance(state, dict) else None
            if isinstance(output, str):
                chars += len(output)
        return used, chars // SUMMARY_CHARS_PER_TOKEN
    return 0, None


def _summary_size(message: dict) -> int | None:
    """KC-69: one summary message's size in tokens — its ``output``, else its
    text at ``SUMMARY_CHARS_PER_TOKEN``, else ``None``.

    Kilo 7.6.2 leaves a chunked summary at 0 tokens (live, laguna: 0/0 over
    4 198 characters), so the text is the only size it has.
    """
    info = message.get("info") if isinstance(message, dict) else None
    tokens = info.get("tokens") if isinstance(info, dict) else None
    output = tokens.get("output") if isinstance(tokens, dict) else None
    if isinstance(output, int) and not isinstance(output, bool) and output > 0:
        return output
    parts = message.get("parts") if isinstance(message.get("parts"), list) else []
    chars = sum(len(part["text"]) for part in parts
                if isinstance(part, dict) and part.get("type") == "text"
                and isinstance(part.get("text"), str))
    return -(-chars // SUMMARY_CHARS_PER_TOKEN) if chars else None


def _summary_tokens(backend: ContestBackend, session: SessionRef) -> int | None:
    """KC-67: the size of the summary a compact left, in tokens, else ``None``.

    Kilo writes the compact as an assistant message flagged ``summary``; its
    ``output`` is the summary the next prompt carries instead of the history,
    so it is what the context shrank to — before the next prompt's own text.
    A summary with no token count — Kilo's chunked compact writes one — is sized
    by its text instead (KC-69). ``None`` when there is no such message or it
    carries neither: the console then says the size is not known yet, and the
    next turn's fill shows it.
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
        return _summary_size(message)
    return None


def _summary_text(backend: ContestBackend, session: SessionRef) -> str:
    """KC-69: the text of the summary a compact left, ``""`` when there is none.

    The last assistant message flagged ``summary``, its text parts joined — what
    a new session is handed when the compact left the old one too full to go on
    in. Fail-open like `_summary_tokens`: a transcript that cannot be read is no
    summary, and the new session then starts from the round prompt alone.
    """
    try:
        messages = backend.messages(session)
    except Exception:  # noqa: BLE001 — a transcript that cannot be read is no summary
        return ""
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        info = message.get("info") if isinstance(message, dict) else None
        if not isinstance(info, dict) or info.get("role") != "assistant" \
                or not info.get("summary"):
            continue
        parts = message.get("parts") if isinstance(message.get("parts"), list) else []
        return "\n".join(part["text"] for part in parts
                         if isinstance(part, dict) and part.get("type") == "text"
                         and isinstance(part.get("text"), str)).strip()
    return ""


def _last_assistant_text(backend: ContestBackend, session: SessionRef) -> str:
    """KC-40: the text of *session*'s last assistant reply, ``""`` when there is none.

    The summary ask reads its answer this way, because a `ContestBackend` has
    `messages` and no `last_assistant_text` of its own — the client does, and the
    backends are the client wrapped in one shape. The last assistant message,
    its text parts joined — `_summary_text` without the `summary` flag, which
    marks only Kilo's own compaction and never a reply to a prompt of its own.
    Fail-open like `_summary_text`: a transcript that cannot be read, or one
    that is not a list, is no answer.
    """
    try:
        messages = backend.messages(session)
    except Exception:  # noqa: BLE001 — a transcript that cannot be read is no reply
        return ""
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        info = message.get("info") if isinstance(message, dict) else None
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        parts = message.get("parts") if isinstance(message.get("parts"), list) else []
        return "\n".join(part["text"] for part in parts
                         if isinstance(part, dict) and part.get("type") == "text"
                         and isinstance(part.get("text"), str)).strip()
    return ""


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


class _RootsHold:
    """The round's serialization for one harvest's roots, as the context
    manager `harvest` holds around `run_tests_detail` (KC-50).

    On the way in: `_TEST_RUNS_LOCK` under the agent's name — `ahead` and
    `waited` are read there, for KC-57's turn record — and then, with KC-58's
    slots armed, one suite slot under `<agent>:harvest`, scoped, with the
    harvest's own budget as its ceiling. On the way out both go back, slot
    first. A harvest whose roots are skipped never enters this, so it stands in
    neither queue. Fail-open throughout: an absent or broken lock or slot is
    "no queue to stand in", never an exception into a run.
    """

    def __init__(self, agent: str, ws_path, budget: float, slots: int) -> None:
        self._agent = agent
        self._path = ws_path
        self._budget = budget
        self._slots = slots
        self._entered = False
        self._slot_held = False
        self.waited: float = 0.0
        self.ahead: int = 0

    def __enter__(self):
        lock = _TEST_RUNS_LOCK
        try:
            self.ahead = max(0, int(lock.ahead(self._agent)))
        except (AttributeError, TypeError, ValueError):
            self.ahead = 0
        try:
            self.waited = max(0.0, float(lock.enter(self._agent)))
            self._entered = True
        except (AttributeError, TypeError, ValueError):
            self.waited = 0.0
        if self._slots > 0:
            key = f"{self._agent}:harvest"
            try:
                held = _SUITE_SLOTS.acquire(key, self._path, ceiling=self._budget, scoped=True)
                self._slot_held = True
                # Only a wait of a second or more is shown, since "held 0s" was misleading (round 69).
                if held >= 1:
                    _log.info("%s: HARVESTING — waited %s for a suite slot",
                              self._agent, _age(held))
            except Exception:  # noqa: BLE001 — the slots never hold up the judge
                self._slot_held = False
        return self

    def __exit__(self, *exc):
        if self._slot_held:
            self._slot_held = False
            try:
                _SUITE_SLOTS.release(f"{self._agent}:harvest")
            except Exception:  # noqa: BLE001 — the roots are over either way
                pass
        if self._entered:
            self._entered = False
            try:
                _TEST_RUNS_LOCK.exit(self._agent)
            except Exception:  # noqa: BLE001 — the roots are over either way
                pass
        return False


def _harvest(ws, ticket_path, run_tests, config=None):
    """`harvest` for one worktree, with the pytest roots serialized round-wide.

    KC-57: `config.harvest_budget_sec` bounds the roots' wall time, and the lock
    is entered under the agent's name so the turn can carry how long it waited
    and how many were ahead.

    KC-58 adds the round's suite slots: the judge's roots take one of them, so
    an agent's own full-suite run never runs beside them. `agent_suite_slots = 0`
    skips that queue for good — `_TEST_RUNS_LOCK` stays the only serialization.
    The ceiling is this harvest's own budget, so the judge is never cut short
    by an agent's clock. The slot is held under `<agent>:harvest`, scoped to
    the roots and never read off `/proc`.

    KC-50: both are taken by `_RootsHold`, which `harvest` holds around
    `run_tests_detail` alone. The git calls and the scorecard run outside
    them, and a harvest already `REWORK` on its mechanical facts runs no roots,
    so it never waits behind another agent's.
    """
    budget = _budget_left(config)
    if not run_tests:
        return harvest(ws, ticket_path, budget_sec=budget)
    hold = _RootsHold(getattr(ws, "agent", ""), ws.path, budget, _suite_slots_armed(config))
    h = harvest(ws, ticket_path, run_tests=True, budget_sec=budget, test_lock=hold)
    return replace(h, waited=hold.waited, ahead=hold.ahead)


def _commits_above(ws: Workspace) -> int:
    """The commits on the branch above its base — `judge_worktree`'s `commits`.

    KC-21's gate: a turn that died with one under it has work to be scored. Zero
    both when the branch is at the base and when the count cannot be read — a
    worktree with nothing on it keeps the old path, with no harvest and no pytest.
    """
    count = git(ws.path, "rev-list", "--count", f"{ws.base_sha}..HEAD")
    return int(count) if count.isdigit() else 0


def _split_numstat(line: str) -> tuple[int, int, str]:
    """`(added, deleted, path)` of one `git diff --numstat` line.

    `(0, 0, "")` for anything that is not a numstat line: a binary file is a
    `-` where a number belongs, a rename arrives as `path => path`, and an
    empty output has no lines at all.
    """
    parts = line.split("\t")
    if len(parts) != 3:
        return 0, 0, ""
    # A binary file shows "-" for its counts, but the path still counts as touched.
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
            # core.quotepath=false: an untracked non-ASCII file was a quoted name, and its lines counted 0
            r = run_git(["git", "--no-optional-locks", "-c", "core.quotepath=false", *args], cwd=ws.path,
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
    """One agent's position in the loop. The last five are terminal."""

    CREATED = "CREATED"
    PROMPTED = "PROMPTED"
    WAITING = "WAITING"
    HARVESTING = "HARVESTING"
    REWORK = "REWORK"
    READY = "READY"
    GAVE_UP = "GAVE_UP"
    STALLED = "STALLED"
    ERROR = "ERROR"
    #: Agent never touched a file in its first-touch budget; terminal, not harvested, shown apart from stalls (KC-42).
    DEAD = "DEAD"

    @property
    def terminal(self) -> bool:
        return self in (AgentState.READY, AgentState.GAVE_UP, AgentState.STALLED,
                        AgentState.ERROR, AgentState.DEAD)


class IdleKind(Enum):
    """How a turn that ended idle actually ended."""

    FINISHED = "FINISHED"
    CUT = "CUT"
    SILENT = "SILENT"


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
    `deadline_commit: true` (KC-41) on a turn means the runner — not the model —
    committed the work that turn left behind, so the harvest that scored it is
    not a verdict on a claim the model made.
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
    #: Run ended ERROR on Kilo's store with uncommitted work, so --resume may restart it (KC-62).
    resumable: bool = False
    commit: str | None = None
    #: Set when the runner committed this run's entry at the end of a terminal turn, not the model (KC-41).
    deadline_commit: bool = False
    cost: float | None = None
    tokens: dict | None = None
    #: Processes the reap found in the worktree at the terminal state (KC-48).
    reaped: list | None = None
    #: Per-attempt count of continues sent into the same session; reset on rework and resume (KC-9).
    continues: int = 0
    #: Sessions this run has opened in all, shown in SUMMARY's sessions column (KC-39).
    sessions: int = 0
    #: Sessions the running attempt has opened; reset on rework, capped by max_sessions_per_attempt (KC-39).
    sessions_this_attempt: int = 0
    #: Diff signature at the previous continue of this attempt, to tell a continue from the one before (KC-39).
    last_diff_signature: str = ""
    #: First-touch nudge and reset already spent for this attempt, so the budget is per attempt (KC-42).
    first_touch_nudges_used: int = 0
    first_touch_resets: int = 0
    #: Times this run's session was summarised by a compact; turns.jsonl has the same per turn (KC-10).
    compactions: int = 0
    #: Latest summary asked for before a compact; the full chain is in the summary file (KC-40).
    summary: str = ""
    #: Summaries captured for this run, one per non-empty reply (KC-40).
    summaries: int = 0
    #: Attempt that already asked for a summary; -1 means never. Kept on a rework (KC-40).
    summary_attempted_at_attempt: int = -1
    #: Session that last answered the summary ask; a fresh session may be asked again (KC-40).
    summary_session_id: str = ""
    #: Paragraph for a later leg's first prompt: leg number, continue instruction and earlier records (KC-43).
    leg_note: str = ""

    @property
    def terminal(self) -> bool:
        return self.state.terminal

    def to_dict(self) -> dict:
        data = asdict(self)
        data["workspace"]["path"] = str(self.workspace.path)
        data["state"] = self.state.value
        if not data.get("reaped"):
            data.pop("reaped", None)
        # KC-43: a round of one leg writes the state.json it always wrote
        if not data.get("leg_note"):
            data.pop("leg_note", None)
        # deadline_commit is always written, so state.json says whether the entry was the model's or the runner's (KC-41).
        data["deadline_commit"] = bool(self.deadline_commit)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AgentRun":
        ws = dict(data["workspace"])
        ws["path"] = Path(ws["path"])
        run = cls(agent=AgentSpec(**data["agent"]), workspace=Workspace(**ws))
        for name in ("session_id", "attempt", "turns", "permissions", "questions",
                     "last_error", "resumable", "commit", "cost", "tokens", "reaped",
                      "deadline_commit", "continues", "compactions", "sessions",
                      "sessions_this_attempt",
                      "first_touch_nudges_used", "first_touch_resets",
                      "last_diff_signature", "summary", "summaries",
                      "summary_attempted_at_attempt", "summary_session_id",
                      "leg_note"):
            if name in data:
                setattr(run, name, data[name])
        run.deadline_commit = bool(getattr(run, "deadline_commit", False))
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

    def last_fill(self) -> float | None:
        """KC-10: the fill of the last recorded turn, as a percent, else ``None``.

        The fill a turn wrote into `turns.jsonl` — ``fill``, a percent of that
        turn's ``context_size``. Read from the end, because the turns before the
        last one are the ones a rework or a continue followed; the last one is
        the context the run ended in. A turn with no size is ``None``, which is
        the ``-`` SUMMARY prints, never a guess.
        """
        for turn in reversed(self.turns):
            fill = turn.get("fill") if isinstance(turn, dict) else None
            if isinstance(fill, (int, float)) and not isinstance(fill, bool):
                return float(fill)
        return None

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
    #: Leg number of this round (1, 2, ...), or None for a round that is not a relay (KC-43).
    leg: int | None = None
    #: Legs chosen for a relay and where the number came from (flag, size or config); absent for one leg (KC-44).
    legs: int | None = None
    legs_from: str | None = None
    #: Last silent-server warning, written by the heartbeat so contest status can show it (KC-81).
    server_silent: dict | None = None

    @property
    def label(self) -> str:
        """`65` for a round of one leg, `65.2` for its second leg — the number every
        log line, `out_dir` and `state.json` of a relay carries."""
        return str(self.round_no) if self.leg is None else f"{self.round_no}.{self.leg}"

    def to_dict(self) -> dict:
        data = {"round_no": self.round_no, "ticket": self.ticket, "base_sha": self.base_sha,
                "started_at": self.started_at, "agents": [run.to_dict() for run in self.agents]}
        if self.leg is not None:
            data["leg"] = self.leg
        # The leg count and its source are kept only on a relay's state; one leg keeps the old bytes (KC-44).
        if self.legs is not None:
            data["legs"] = self.legs
        if self.legs_from:
            data["legs_from"] = self.legs_from
        # Only a round that warned carries server_silent; every older state.json is unchanged (KC-81).
        if self.server_silent is not None:
            data["server_silent"] = self.server_silent
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RoundState":
        leg = data.get("leg")
        legs = data.get("legs")
        silent = data.get("server_silent")
        return cls(round_no=int(data["round_no"]), ticket=str(data["ticket"]),
                   base_sha=str(data["base_sha"]), started_at=float(data["started_at"]),
                   agents=[AgentRun.from_dict(a) for a in data.get("agents", [])],
                   leg=int(leg) if leg is not None else None,
                   legs=int(legs) if legs is not None else None,
                   legs_from=data.get("legs_from"),
                   server_silent=silent if isinstance(silent, dict) else None)

    def table_rows(self) -> list:
        """One dict per agent — the SUMMARY's inputs; KC-7 renders them."""
        return [{
            "name": run.agent.name,
            "model": run.agent.model,
            "state": run.state.value,
            "attempts": run.attempt,
            "sessions": run.sessions,
            "turns": len(run.turns),
            "permissions": dict(run.permissions),
            "questions": run.questions,
            "cost": run.cost,
            "tokens": run.tokens,
            # KC-10: the context the run ended in, and how many compacts it took
            "fill": run.last_fill(),
            "compactions": run.compactions,
            # The summary is shown so the operator sees which agent wrote one (KC-40).
            "summary": run.summary,
            "summaries": run.summaries,
            "commit": run.commit,
            "last_reason": run.last_reason(),
            "test_wait": run.test_wait(),
        } for run in self.agents]


# The prompt

# AGENT_TEST_TIMEOUT_MS is shared with the Kilo client, so the prompt and the silence clock agree (KC-47).

# Prompt text the agents are scored on; {scratch_note} is the optional scratch-dir paragraph (KC-53).
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

Run git commands one after another, never as parallel tool calls: two of them
at once collide on your worktree's index.lock.

Running the test suite on this machine can take up to 20 minutes under load:
give that `bash` call a `timeout` of at least {test_timeout_ms} ms.

When you are done, report: the commit sha, each Acceptance checkbox and
whether you met it, and anything in the ticket you found to be wrong about the
live code — each ticket names the commit it was written against in its
`**Status:**` line (the original 24 used `68b78a0`); the code is the
authority, not the ticket.

Your starting tree is commit {base_sha}; your one commit goes on top of it.
{scratch_note}Any command that reaches outside your worktree is decided by a reviewer, and a
rejection is final for that command — do not retry it.
"""


#: Sentence added when the round gives an agent a scratch dir; empty means no sentence (KC-59).
_SCRATCH_DIR_NOTE = (
    "\n\nYour scratch dir is {tmp_dir} — put temporary files there. The policy "
    "allows that dir and not the other agents'."
)

#: Root whose per-agent folder the prompt may name when no scratch dir was derived (KC-53).
_CONTEST_ROOT = "/tmp/contest"


def _scratch_roots(tmp_roots) -> tuple[str, ...]:
    """The scratch globs the prompt may name, in the order the config has them.

    Fail-open, the way the policy reads them (`tmp_root_dirs`): a non-string, an
    empty one and one that is not an absolute or ``~``-absolute path are all
    dropped, and a non-iterable *tmp_roots* (``42``, a bare ``None``) is ``()``
    — a round with a malformed key names no scratch space at all instead of
    raising into it. A bare string is one root rather than its characters.
    """
    if isinstance(tmp_roots, str):
        tmp_roots = (tmp_roots,)
    try:
        items = tuple(tmp_roots or ())
    except (TypeError, ValueError):
        return ()
    roots: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        root = item.strip()
        if not root or not root.startswith(("/", "~")):
            continue
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _scratch_note(agent_name: str, tmp_roots, tmp_dir: str = "") -> str:
    """KC-53: the paragraph that names the scratch space needing no reviewer.

    ``""`` when the round named no scratch root, so a prompt without one is
    today's text byte for byte. Otherwise the globs, and one folder for the
    agent's own files: *tmp_dir* — KC-59's scratch dir, the one that exists and
    that `_SCRATCH_DIR_NOTE` names — when the round derived it, else
    `/tmp/contest/<agent>/` when `_CONTEST_ROOT` is one of the roots, else no
    folder at all. The leading newline splits the start-tree line from the
    paragraph and the trailing blank line keeps the reviewer sentence its own
    paragraph.
    """
    roots = _scratch_roots(tmp_roots)
    if not roots:
        return ""
    note = ("Scratch space outside your worktree that needs no reviewer: "
            + ", ".join(roots) + ".")
    folder = ""
    if isinstance(tmp_dir, str) and tmp_dir.strip():
        folder = tmp_dir.strip().rstrip("/") + "/"
    elif (isinstance(agent_name, str) and agent_name.strip()
          and any(r.removesuffix("/*").rstrip("/") == _CONTEST_ROOT for r in roots)):
        folder = f"{_CONTEST_ROOT}/{agent_name.strip()}/"
    if folder:
        note += (f"\nPut your own scratch files under {folder} — anywhere else in "
                 "/tmp goes to the reviewer.")
    return "\n" + note + "\n\n"


def round_prompt(agent_name: str, ticket_path: Path, base_sha: str, *, dirty: str = "",
                 tmp_dir: str = "", tmp_roots=(), leg_note: str = "") -> str:
    """The runbook's prompt for *agent_name*, plus the base sha and the permission rule.

    The ticket is not repeated: the session reads it from its own worktree via
    `next_task.py`, so *ticket_path* is accepted for the caller's clarity only.
    When *tmp_dir* is non-empty (KC-59), the sentence that names the agent's own
    scratch dir is appended first; when *dirty* is non-empty (a `--resume` into a
    worktree that still holds uncommitted work, KC-22), the `continue_message`
    paragraph is appended after it so the fresh session learns of the work on its
    first prompt. When *tmp_roots* names at least one root (KC-53), the paragraph
    that lists them and names the agent's own folder (`_scratch_note`) is
    inserted right before the reviewer sentence, so the agent stops guessing at a
    scratch path the reviewer would refuse. When *leg_note* is non-empty (KC-43,
    the first prompt of a leg after the first) it goes last, after the `dirty`
    paragraph: `leg_message`'s account of the relay so far and the instruction to
    continue. Every existing caller passes none of them and gets the unchanged text.
    """
    del ticket_path
    text = _PROMPT.format(name=agent_name, base_sha=base_sha,
                          test_timeout_ms=AGENT_TEST_TIMEOUT_MS,
                          scratch_note=_scratch_note(agent_name, tmp_roots, tmp_dir))
    if tmp_dir:
        text = text + _SCRATCH_DIR_NOTE.format(tmp_dir=tmp_dir)
    if dirty:
        text = text + "\n\n" + continue_message(dirty)
    if leg_note:
        text = text + "\n\n" + leg_note
    return text


#: How many git status lines continue_message lists before it says "and N more".
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
        # --no-optional-locks: status only reads, so it never takes the index lock an agent may need.
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


def _diff_signature(ws: Workspace) -> str:
    """KC-39: the uncommitted work of *ws* as one hash of its *content*.

    ``git add -A -N`` marks every untracked file intent-to-add first — with no
    pathspec: ``-- ':!runs'`` there makes git refuse the whole add (exit 1,
    "paths are ignored") whenever the gitignored ``runs/`` exists, which is every
    live worktree once the agent wrote its progress row — so a file
    that git does not track yet is in the diff text too, and ``git diff HEAD``
    then covers the worktree against the branch — staged edits and unstaged
    ones alike, which bare ``git diff`` (worktree against index) misses once the
    agent has staged anything. ``runs/`` is excluded, as in `_dirty_tree`: the
    runner's own progress rows are not the agent's work.

    A hash of the diff text, not of the file list: two continues that touch the
    same file with different half-finished content must come back different, and
    two that write the same bytes back must come back equal.

    Returns ``""`` for a clean tree and for every failure — git that is not in a
    repository yet, a held index, an exit of either command. The caller must read
    ``""`` as "cannot compare", not "the diff is unchanged": a tree that could
    not be read must never look like the repeat that opens a new session.
    """
    try:
        staged = run_git(["git", "add", "-A", "-N"], cwd=ws.path)
        if staged.returncode != 0:
            # Untracked files would be missing from the diff, so such a diff is never a repeat.
            return ""
        diff = run_git(["git", "diff", "HEAD", "--no-color", "--", ":!runs"], cwd=ws.path)
    except (OSError, subprocess.SubprocessError):
        return ""
    if diff.returncode != 0:
        return ""
    text = diff.stdout
    if not text.strip():
        return ""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


#: Progress columns of scripts/append_task.py, in order; the deadline row is written by the runner (KC-41).
_PROGRESS_COLUMNS = "ticket,finding,outcome,commit,note"

#: What a deadline commit is called in the log; WIP is the model's word, not the runner's claim (KC-41).
_DEADLINE_PREFIX = "WIP (deadline commit,"

#: Only a sha may go in the progress row; the same shape harvest checks, kept in step by hand (KC-41).
_SHA_RE = re.compile(r"[0-9a-f]{7,40}", re.IGNORECASE)

#: Max length of the reason in a deadline commit subject, so git stores it whole (KC-41).
REASON_LIMIT = 120


def _write_progress_row(ws: Workspace, ticket_path, sha: str, *, note: str) -> bool:
    """Append the one `runs/<agent>/PROGRESS.csv` row a deadline commit needs,
    header included when the file is new. `True` when the row is on disk.

    KC-41: `harvest` wants a row for the ticket whose outcome is DONE/FIXED and
    whose commit resolves on the branch, and an agent that ran out of clock wrote
    neither — so the runner writes the row itself. It is a statement of fact, not
    a claim: the sha is the deadline commit this runner just made, and the note
    names the reason the clock ran out. The outcome is `FIXED` because the
    commit exists and the harvest has not contradicted it yet; the *verdict* is
    still `harvest`'s to give, and it reads the branch, the tests and the
    ticket's declared files exactly as it would for a row the model wrote.

    Fail-open throughout: a `runs/` path that cannot be created, a row that
    cannot be appended, a progress file with a header of its own — all leave the
    tree as it was and answer `False`. The harvest then says `no_progress_row`,
    which is the pre-KC-41 outcome, so a broken write costs the round the
    entry it would have scored and never raises into the run.

    `runs/` is in the worktree's `.gitignore` (and the deadline commit's
    `git reset -- runs` after the add takes it out again), so this row can never end up inside
    the commit it names.
    """
    path = ws.progress_csv
    ticket = Path(ticket_path).name
    # Quoted like csv does for a comma or a quote; a ticket name has neither, a note may.
    def cell(text: str) -> str:
        if any(c in text for c in (",", '"', "\n", "\r")):
            return '"' + text.replace('"', '""') + '"'
        return text
    row = ",".join([cell(ticket), "", "FIXED", cell(sha), cell(note)])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8", newline="") as fh:
            if new:
                fh.write(_PROGRESS_COLUMNS + "\n")
            fh.write(row + "\n")
            fh.flush()
    except OSError as exc:
        _log.warning("%s: could not write the deadline progress row %s: %s",
                     getattr(ws, "agent", "?"), path, _brief(str(exc)))
        return False
    return True


def _deadline_commit_enabled(config) -> bool:
    """`config.deadline_commit`, read fail-open: a missing config, a config object
    without the key and a value that is not a bool at all all answer the default,
    `True`.

    KC-41: the key decides whether an agent's uncommitted work is committed for
    it. A caller holding a hand-built config (a test, a script) must get the
    documented behaviour rather than an `AttributeError` into the terminal
    branch, and the runner — not the round's intake — is where that is settled:
    the roster already refuses a malformed value at load time with a warning.
    """
    value = getattr(config, "deadline_commit", True)
    return True if value is None else bool(value)


def _sessions_ceiling(config) -> int:
    """KC-39: `config.max_sessions_per_attempt`, read fail-open as a count.

    A missing config, a config object without the key and a value that is not an
    int at all all answer ``0``, which turns the ticket off — today's behaviour,
    the continue is still granted and the exhausted budget is still harvested in
    the same session. A ``bool`` is not a count (`True` is `1`, which would arm
    a ceiling the round never asked for), and a negative one is refused the same
    way: neither may open a session the round did not budget for. `roster`
    already clamps a malformed ini value to 0, so this only guards a hand-built
    config, which is what the tests hold.
    """
    value = getattr(config, "max_sessions_per_attempt", 0)
    if isinstance(value, bool):
        return 0
    try:
        count = int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0
    return count if count > 0 else 0


def _deadline_reason(state, error, stalled) -> str:
    """The one short phrase a deadline commit's subject and its progress note
    carry: why the turn ended, in the runner's words rather than the model's.

    KC-41: the commit subject is `WIP (deadline commit, <reason>): <ticket>`, and
    the reason is what tells the operator later which clock ran out. It is
    derived from the *state* the turn earned and the error text, both of which
    the runner already holds, so it costs no extra read and never raises. A
    state with no error text still names the state.

    The reason is cut to `REASON_LIMIT` and stripped of the separators a commit
    subject may not carry: `_brief` bounds it at 300 characters, which is a
    `last_error`'s budget and not a subject line's, and a multi-line error text
    would fold into a header git cannot read back as one line.
    """
    text = _brief(str(error or "")).strip()
    if not text:
        return state.value.lower() if state is not None else "terminal"
    text = text.replace("\n", " ").replace("\r", " ")
    for sep in (";", ",", " — ", " - "):
        text = text.split(sep, 1)[0]
    text = " ".join(text.split())
    return text[:REASON_LIMIT].rstrip(" .") or state.value.lower()


def _deadline_commit(ws: Workspace, *, reason: str, ticket=None) -> str | None:
    """Commit the work a terminal turn left behind, on the agent's own branch.
    `None` when there is nothing to commit, and on any failure.

    KC-41: `harvest` scores a *commit*, and an agent whose turn the clock ended
    left edits in the tree and no commit — so the turn scored as though it had
    produced nothing, however finished the work was (round 64 held two entries
    that pass all four roots and harvested none). This commits what is there:
    `git add -A` with `runs/` unstaged again, then a commit on `ws.branch` subject
    `WIP (deadline commit, <reason>): <ticket>`, and the new sha back. *ticket*
    is the ticket file the turn worked, named in the subject when the caller
    has it; it is optional so the two-argument form the ticket spells is the one
    the tests can call.

    Only when there is genuinely nothing to commit: the count above the base is
    re-read here, and a tree that is clean (or a status that could not be read —
    FL-2's `TreeReadError`, never "clean") returns `None` without touching git.
    `runs/` is excluded from the add, and it is `.gitignore`d as well, so the
    row `_write_progress_row` writes after this is not inside the commit it
    names.

    Fail-open on every step: a worktree that is not a repository, an index that
    stays locked, a commit that refuses (nothing staged after all, no identity,
    a hook that fails) all return `None` with a warning. `None` is the
    pre-KC-41 outcome — no commit, no row, no harvest — so a broken deadline
    commit degrades to today's behaviour rather than raising into the run.
    """
    if _commits_above(ws) != 0:
        return None  # the model committed: that commit is the entry, not a new one
    try:
        if not _dirty_tree(ws):
            return None  # a clean tree is "no uncommitted work", not a commit
    except TreeReadError as exc:
        _log.warning("%s: tree unreadable — no deadline commit: %s",
                     getattr(ws, "agent", "?"), _brief(str(exc)))
        return None
    who = getattr(ws, "agent", "contest")
    name = Path(str(ticket)).name if ticket else ""
    subject = f"{_DEADLINE_PREFIX} {reason})"
    if name:
        subject += f": {name}"
    body = _deadline_body(reason, who, name)
    try:
        # runs/ is added with the rest and removed from the index after, since git add fails on ignored paths (round 48).
        add = run_git(["git", "add", "-A"], cwd=ws.path)
        if add.returncode != 0:
            _log.warning("%s: git add for the deadline commit exited %d: %s",
                         who, add.returncode, _brief(add.stderr or add.stdout))
            return None
        unstage = run_git(["git", "reset", "-q", "--", "runs"], cwd=ws.path)
        if unstage.returncode != 0:
            _log.warning("%s: could not keep runs/ out of the deadline commit (exit %d): %s",
                         who, unstage.returncode, _brief(unstage.stderr or unstage.stdout))
            return None
        # Commit with the runner's identity and --no-verify, so a hook cannot block the round's commit.
        commit = run_git(["git", "-c", "user.name=contest runner",
                          "-c", f"user.email={DEADLINE_COMMIT_EMAIL}",
                          "-c", "commit.gpgsign=false",
                          "commit", "-q", "--no-verify",
                          "-m", subject, "-m", body],
                         cwd=ws.path)
        if commit.returncode != 0:
            _log.warning("%s: the deadline commit exited %d: %s",
                         who, commit.returncode, _brief(commit.stderr or commit.stdout))
            return None
        sha = git(str(ws.path), "rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("%s: the deadline commit did not run: %s", who, _brief(str(exc)))
        return None
    sha = (sha or "").strip()
    if not _SHA_RE.fullmatch(sha):
        _log.warning("%s: the deadline commit left no sha to name (%r)", who, sha[:40])
        return None
    _log.info("%s: deadline commit %s — %s", who, sha[:12], reason)
    return sha


def _deadline_body(reason: str, agent: str, ticket: str) -> str:
    """The body of a deadline commit, and the note the progress row carries.

    The same sentence in both places, so the operator reading `git log` and the
    judge reading `runs/<agent>/PROGRESS.csv` read one fact and not two. It says
    what the commit is — the tree as it stood when the turn ended — and never
    says the work is finished: the harvest's verdict is what decides that, and
    it reads the tests, not this note.
    """
    return (f"Committed by the round, not by {agent}: the turn ended {reason} with "
            f"this work uncommitted"
            + (f" for {ticket}" if ticket else "")
            + ". The harvest scores it like any other entry and "
            "`run.deadline_commit` is true for it.")


def _deadline_note(reason: str, agent: str, ticket: str) -> str:
    """The `note` of the deadline commit's progress row — the same fact as
    `_deadline_body`, in one line and without the second sentence, so a CSV cell
    holds a sentence and not a paragraph."""
    return (f"deadline commit: the turn ended {reason}; {agent}'s tree committed "
            + (f"for {ticket} " if ticket else "")
            + "by the runner (KC-41), not claimed by the model")


# Artifacts: every write is fail-open

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


def _context_watch_sec(config) -> float:
    """Round 145: ``[contest] context_watch_sec`` — seconds between two reads of
    a turn's fill; 0, a negative, a non-number or a non-finite one turns the
    watch off. ``inf`` is refused like the others (`math.isfinite`, as
    `full_refusal_percent` does): a config built in code can hold it, and a
    watch that never wakes is a thread that reads nothing but keeps one alive."""
    try:
        value = float(getattr(config, "context_watch_sec", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if 0.0 < value and math.isfinite(value) else 0.0


class _ContextWatch:
    """Round 145: one turn's in-turn fill watch — its stop signal, the thread
    behind it, and the lock between them.

    `stop()` sets the signal *under the same lock* the watch takes before it
    acts, and waits `every` seconds for the thread to leave. Without both, a
    read that returns after the turn ended can still set `context_full` and
    `context_aborted`, write `context_watch_stop` into a turn already appended
    to `turns.jsonl`, and abort a session this turn no longer holds — and the
    runner reads that as its own stop on the next turn, and compacts for no
    reason. Fail-open: a join that does not return in time gives up and the
    turn goes on.
    """

    def __init__(self, stop: threading.Event, thread: threading.Thread,
                 every: float, lock: threading.Lock) -> None:
        self.stop_event = stop
        self.thread = thread
        self.every = every
        self.lock = lock

    def stop(self) -> None:
        """End the turn: the watch may not act on the next one."""
        with self.lock:
            self.stop_event.set()
        self.thread.join(timeout=self.every)


def _abort_quietly(backend: ContestBackend, session: SessionRef) -> None:
    try:
        backend.abort(session)
    except Exception as exc:  # noqa: BLE001 — the session may already be gone
        _log.warning("abort(%s) failed: %s: %s", session.id, type(exc).__name__, exc)


# The wait, with the round's stall edge

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

    KC-58: ``suite_waited`` is the same kind of time for the round's suite
    queue — the seconds a whole-root ``pytest`` spent waiting for a slot while
    its permission was held. That wait happens inside ``wait_idle``'s loop too,
    where neither the deadline nor the silence clock can run, and it is the
    round's queue, not the agent's work: given back in full, outside
    ``granted``, never paid for out of the turn.
    """

    on_deadline: Callable[[float], float | None] | None
    extensions: list = field(default_factory=list)
    prev_sample: tuple[int, int] = (0, 0)
    last_sample: tuple[int, int] = (0, 0)
    last_change_at: float = 0.0
    granted: float = 0.0
    #: Seconds this turn got back for the gate's own waits, and the tries those waits cost (KC-66).
    gate_added: float = 0.0
    gate_attempts: int = 0
    #: KC-58: the seconds this turn got back for its suite-slot waits.
    suite_waited: float = 0.0

    def grant_suite(self, seconds: float) -> float:
        """KC-58: the seconds a suite-slot wait is granted back to this turn.

        Returns what the deadline moves by. Fail open: a malformed or
        non-positive amount is 0.0.
        """
        try:
            waited = max(0.0, float(seconds))
        except (TypeError, ValueError):
            return 0.0
        self.suite_waited += waited
        return waited

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


def _turn_deadline(run: AgentRun, config: ContestConfig, working=None) -> _TurnClock:
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
    KC-58 adds a second kind: *working*, a `bash` call still running, a suite
    slot held or waited for, a `pytest` in the worktree, or an `edit`/`write`
    part completed inside the last `turn_extend_sec` — a suite that takes an
    hour changes nothing on disk, and churn that goes *down* is work too,
    round 106's 1073 → 1072 with an edit 4 s before the abort. On progress the
    deadline is pushed by `turn_extend_sec`, clipped to `turn_max_sec`, and
    the sample becomes the new previous; on no progress — and at the cap,
    whatever the churn says — it aborts as today.

    ``last_change_at`` moves on every sample that differs from the one before
    it, either number, up or down: that is what ``unchanged for`` measures,
    and the old one moved only when an extension was *granted*, so a sample
    the deadline refused still read as unchanged for the whole turn.
    """
    extend = float(getattr(config, "turn_extend_sec", 0) or 0)
    clock = _TurnClock(on_deadline=None)
    # With turn_extend_sec = 0 nothing is armed and no git is read, as before.
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
        # The churn is compared with the previous sample, so a churn that stopped is seen.
        if current != clock.last_sample:
            clock.last_change_at = float(elapsed)
        clock.last_sample = current
        previous = clock.prev_sample
        grew = current[0] > previous[0] or current[1] > previous[1]
        if not grew and not _is_working(working):
            return None
        grant = min(extend, ceiling - nominal - clock.granted)
        if grant <= 0:
            return None
        clock.granted += grant
        clock.prev_sample = current
        extension = {"at": round(float(elapsed), 1), "files": current[0],
                     "lines": current[1], "granted": int(grant)}
        if not grew:
            # KC-58: earned by work the churn cannot see, not by the churn
            extension["working"] = True
        clock.extensions.append(extension)
        try:
            _log.info("%s: WAITING — +%s at %s (%d files, %d lines)",
                      spec.name, _age(grant), _age(float(elapsed)),
                      current[0], current[1])
        except Exception:  # noqa: BLE001 — the grant stands, the line is not a round
            pass
        return grant

    clock.on_deadline = on_deadline
    return clock


#: How often the first-touch watch checks the worktree (KC-42).
FIRST_TOUCH_POLL_SEC = 5.0
#: Silence after an idle that means no queued nudge is behind it (KC-75).
QUEUED_NUDGE_QUIET_SEC = 5.0


class _FirstTouch:
    """KC-42: the clock on a turn that has not written anything yet.

    Round 64: three of the eight agents finished the round with a worktree
    byte-identical to the base. One of them spent 22 minutes inside a single
    `task` call — never silent, so `idle_event_timeout_sec` never fired; busy,
    so KC-9's continue never ran; and with nothing on disk, so there was no
    diff to compare. None of the runner's three clocks asks the only question
    that separates a working agent from a useless one: *has the worktree
    changed at all?*

    So this one does, and it is measured on the **worktree**, never on the
    event stream: a provider in an offline/retry loop emits a heartbeat every
    five minutes and looks, to any clock on events, exactly like an agent at
    work. The predicate is deliberately the dumbest one available — "touched
    anything at all" — because anything smarter would start rejecting
    exploratory work, which is the opposite of the point.

    Armed from a turn's prompt for `first_touch_sec`; at the deadline it sends
    one nudge into the session, names the ticket's own files and says plainly
    that nothing has been modified, re-arms, and repeats at most
    `first_touch_nudges` times. With the budget spent and the tree still
    untouched it escalates the way KC-39 does for a repeated diff — `abort` and
    a fresh session with the same model, `run.attempt` unchanged — subject to
    `max_sessions_per_attempt`. With no session left to open, or with that
    spent too, the turn is `DEAD`.

    One instance per turn, started by `run_agent` and stopped by it in a
    `finally`. Every read is fail-open: a worktree that cannot be read (FL-2's
    `TreeReadError`) is "no news", never a raise and never a reason to declare
    anything — a tree the runner cannot see must not be able to end a round.
    """

    def __init__(self, run: "AgentRun", ws: Workspace, spec, config, *, nudge, reset, kill,
                 budget: float, nudges: int, ceiling: int = 0):
        self.run, self.ws, self.spec, self.config = run, ws, spec, config
        self._nudge, self._reset, self._kill = nudge, reset, kill
        self.budget = float(budget)
        self.nudges = max(0, int(nudges))
        #: Ceiling shared with max_sessions_per_attempt; a first-touch reset is a session too (KC-39, KC-42).
        self.ceiling = max(0, int(ceiling))
        #: What this turn's watch did (nudged, dead); the attempt's totals live on the run (KC-42).
        self.nudged = 0
        self.dead = False
        self._touched = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._deadline = time.monotonic() + self.budget

    def touched(self) -> bool:
        """Has the watch already disarmed — the worktree showed a sign of life?"""
        return self._touched

    def elapsed(self) -> float:
        """Seconds this watch has been armed — what the nudge reports as elapsed.

        Measured from the arming, not from the start of the turn, so a watch
        armed for the second turn of a rework does not claim its predecessor's
        time. It only ever reaches one `first_touch_sec`, since that is the
        deadline it fires at.
        """
        return max(0.0, time.monotonic() - (self._deadline - self.budget))

    # ── the predicate ─────────────────────────────────────────────────────
    def _has_touched(self) -> bool:
        """Has this worktree any sign of the agent's work? Never raises.

        `_commits_above` is asked first because it is a `rev-list --count` off
        the branch — cheap, and it is the half KC-21's gate is built on. A
        commit with a clean tree is a touched worktree, so a deadline commit
        (KC-41) disarms this too, not a false positive. `_dirty_tree` excludes
        `runs/`, so the runner's own progress row never counts as the agent
        working.
        """
        try:
            if _commits_above(self.ws) > 0:
                return True
            return bool(_dirty_tree(self.ws))
        except TreeReadError as exc:
            # FL-2: unreadable is not "clean", and it is certainly not "touched"
            _log.debug("%s: first-touch: tree unreadable — %s", self.spec.name, _brief(str(exc)))
            return False
        except Exception as exc:  # noqa: BLE001 — a read that raises is no news
            _log.debug("%s: first-touch: %s: %s", self.spec.name, type(exc).__name__, exc)
            return False

    # ── the loop ──────────────────────────────────────────────────────────
    def start(self) -> "_FirstTouch":
        """Watch on a thread of its own.

        The watch has to keep asking the worktree while the turn is in flight,
        and the turn is one blocking `wait_idle` — the runner's main thread for
        this agent is inside it and cannot poll anything. The nudge and the
        escalation go out from here, on this thread, for the same reason: they
        are the runner's own writes into a session that is busy answering, and
        the wait has to be woken by something.
        """
        if self.budget <= 0:
            return self
        self._thread = threading.Thread(
            target=self._watch, name=f"first-touch-{self.spec.name}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=FIRST_TOUCH_POLL_SEC * 2)

    def _watch(self) -> None:
        while not self._stop.is_set():
            # The stop wins over the deadline, so a turn that ended on its own is never nudged afterwards.
            if self._stop.wait(timeout=max(0.2, FIRST_TOUCH_POLL_SEC)):
                return
            if self._has_touched():
                # Disarmed for good: a git checkout by the model does not restore the deadline.
                self._touched = True
                return
            if time.monotonic() < self._deadline:
                continue
            if not self._fire():
                return

    def _fire(self) -> bool:
        """One deadline passed with an untouched tree. False when the turn ends.

        The nudge first, and the prompt is the runner's own — sent into a
        session that is still working, which is the point: round 64's
        `nex-n2-5-pro` was never idle, so nothing else could have said
        anything to it. After the attempt's budget of nudges, the session is
        reset (KC-39's escape hatch for a model wedged in a tool loop, which
        cannot be talked out of it inside the session that is wedged), and after
        that the turn is `DEAD`.

        The budget is counted on `run`, not here, so the fresh session the reset
        opens does not start over with a full one: a round that asked for one
        nudge gets one nudge, one reset and then `DEAD`, whether that is one
        turn's worth of waiting or two.
        """
        spent = int(getattr(self.run, "first_touch_nudges_used", 0) or 0)
        if spent < self.nudges:
            self.run.first_touch_nudges_used = spent + 1
            self.nudged += 1
            # Read before the re-arm moves the origin, so each nudge reports its real time into the turn.
            elapsed = self.elapsed()
            self._deadline = time.monotonic() + self.budget
            _log.info("%s: nothing modified after %s (nudge %d of %d)",
                      self.spec.name, _age(self.budget), spent + 1, self.nudges)
            try:
                self._nudge(elapsed)
            except Exception as exc:  # noqa: BLE001 — a refused nudge is not a reason
                _log.warning("%s: first-touch nudge: %s: %s", self.spec.name,
                             type(exc).__name__, _brief(str(exc)))
            return not self._stop.is_set()
        self._deadline = time.monotonic() + self.budget
        if not int(getattr(self.run, "first_touch_resets", 0) or 0):
            self.run.first_touch_resets = 1
            if not self.ceiling:
                _log.info("%s: still nothing modified after %s and %d nudge(s), and "
                          "the round allows no second session — DEAD", self.spec.name,
                          _age(self.budget), spent)
                self._stop.set()
                self.dead = True
                self._kill()
                return False
            _log.info("%s: still nothing modified after %s and %d nudge(s) — new session",
                      self.spec.name, _age(self.budget), spent)
            try:
                self._reset()
            except Exception as exc:  # noqa: BLE001 — the kill below is the fallback
                _log.warning("%s: first-touch reset: %s: %s", self.spec.name,
                             type(exc).__name__, _brief(str(exc)))
            return not self._stop.is_set()
        _log.info("%s: nothing modified in %d rounds of the clock — DEAD",
                  self.spec.name, spent)
        self._stop.set()
        self.dead = True
        try:
            self._kill()
        except Exception as exc:  # noqa: BLE001 — the turn is ended either way
            _log.warning("%s: first-touch kill: %s: %s", self.spec.name,
                         type(exc).__name__, _brief(str(exc)))
        return False


#: Nudge sent into a turn that wrote nothing: plain text, no critique, the ticket's declared items (KC-42).
FIRST_TOUCH_NUDGE = (
    "Nothing has been modified in this worktree yet — {elapsed} into this turn and "
    "not one file changed and no commit made. This ticket asks for changes to:\n\n"
    "{files}\n\n"
    "Open the first of those files and start the change now. Do not re-read the "
    "ticket, do not re-plan, and do not delegate this to a sub-agent: the turn's "
    "clock is running, and the next check is against the worktree, not against "
    "whether you answered."
)


def first_touch_nudge(files, elapsed: float) -> str:
    """KC-42: the nudge, with the ticket's declared paths and the elapsed time.

    *files* is `declared_files(ticket_path)` — the same tuple `harvest` and the
    policy's context are built from, so the model is told exactly the paths the
    gates will read. An empty tuple, or one no longer on disk, degrades to the
    ticket's own path rather than to a bare paragraph with nothing in it: the
    nudge is never sent with no list of what to change.
    """
    names = [f"- `{name}`" for name in files if name]
    if not names:
        names = ["- (the ticket file named by the round prompt)"]
    return FIRST_TOUCH_NUDGE.format(elapsed=_age(elapsed), files="\n".join(names))


def _first_touch_budget(config) -> float:
    """KC-42: `[contest] first_touch_sec`, read fail-open as seconds.

    A missing config, a config object without the key, and a value that is not
    a number at all all answer ``0``, which is *off* — the tree before this
    ticket, byte for byte. So a hand-built config with the key left out cannot
    acquire a clock the round never asked for. ``roster`` rejects a malformed
    ini value at load time and clamps a negative one to 0 here.
    """
    value = getattr(config, "first_touch_sec", 0)
    if isinstance(value, bool):
        return 0.0
    try:
        return max(0.0, float(str(value).strip() or 0))
    except (TypeError, ValueError):
        return 0.0


def _first_touch_nudges(config) -> int:
    """KC-42: `[contest] first_touch_nudges`, read fail-open as a count.

    0 is a real setting — a turn that has written nothing by its first deadline
    goes straight to the escalation — so it is kept, and only a value that is
    not a number, or a negative one, is refused the way `_sessions_ceiling`
    refuses it.
    """
    value = getattr(config, "first_touch_nudges", 0)
    if isinstance(value, bool):
        return 0
    try:
        count = int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0
    return count if count > 0 else 0


def _wait_turn(backend: ContestBackend, session: SessionRef, config: ContestConfig,
               *, on_permission, on_question,
               on_deadline: Callable[[float], float | None] | None = None,
               since: int | None = None, quiet_after: float | None = None):
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

    *quiet_after* (KC-73) is armed only by `run_agent`'s overflow edge, to
    wait out what Kilo still does in a session after its turn ended; `None`
    is not passed at all.
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
    if quiet_after is not None:
        wait_kwargs["quiet_after"] = quiet_after
    # A retry scheduled past the bound is a quota reset; the agent ends at once (KC-61).
    max_retry_wait = float(getattr(config, "provider_retry_max_wait_sec", 0) or 0)
    if max_retry_wait > 0:
        wait_kwargs["max_retry_wait"] = max_retry_wait
    quota_re = _quota_re(config)
    if quota_re is not None:
        wait_kwargs["quota_re"] = quota_re
    # N retries in a row with no output end the turn; 0 or a missing key turns it off (KC-64).
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


#: The title line of <agent>.summary.md, written only for a new file, so later sessions append.
SUMMARY_FILE_TITLE = (
    "# The agent's own account of its uncommitted work\n\n"
    "Written in chat by {agent} before its context was compacted, one section per "
    "session, in order. The text is the agent's, not the runner's.")


def append_summary_text(path: Path, agent: str, text: str, *,
                        session_id: str = "", attempt: int = 0) -> None:
    """KC-40: append *text* to *path* — the file's whole reason for being.

    Every session of an attempt appends, never overwrites: the operator opening
    the file later reads the chain, from the first summary to the last, not just
    the newest. A section header names the session and the attempt it came from,
    so the chain can be followed against `state.json`. Fail-open like every
    artifact write here: a directory that cannot be created or a file that cannot
    be opened is logged and dropped, because a summary that could not be written
    is still what the session said, and the run has to go on.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        blank = ""
        if not path.exists():
            blank = SUMMARY_FILE_TITLE.format(agent=agent) + "\n\n"
        heading = f"\n## session {session_id or '?'} — attempt {attempt}\n\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(blank + heading + (text or "").rstrip() + "\n")
    except Exception as exc:  # noqa: BLE001 — a summary file is not a round
        _log.warning("could not append the summary to %s: %s: %s", path, type(exc).__name__, exc)


def _summary_read_tree(ws: Workspace) -> str:
    """KC-40: the uncommitted diff the summary edge needs, KC-39's way.

    ``""`` only when there is nothing uncommitted against the branch base: with a
    commit already on the branch the working tree's extra edits are not the diff
    the ticket asks for, and a status that cannot be read is not a clean tree —
    the edge stands down, the compact goes on as today, and the tree is never
    read as clean.
    """
    dirty = ""
    if _commits_above(ws) == 0:
        try:
            dirty = _dirty_tree(ws)
        except TreeReadError as exc:
            _log.warning("tree of %s unreadable — %s", ws.path, _brief(str(exc)))
    return dirty


def maybe_summarize_before_compact(backend: ContestBackend, session: SessionRef, run: AgentRun,
                                   ws: Workspace, config: ContestConfig, out_dir: Path, turn: dict,
                                   *, session_id: str, fill: float, on_permission, on_question) -> tuple:
    """KC-40: the summary a nearly-full session writes before it is compacted.

    Called by `_context_gate` after the fill is read and before the compact.
    `fill` and `session_id` are the gate's own read of them — the fill over the
    size it already decided — so the summary edge and the compact that follows
    reason about the same number. At or past `summary_at_percent`, once per
    session in the attempt, on a tree that already holds an uncommitted diff,
    the session gets `SUMMARY_PROMPT` as a prompt of its own and waits for
    `session.idle` like any other prompt. Its reply is read with
    `last_assistant_text`, copied out to `<out_dir>/<agent>.summary.md`, and put
    on `run.summary` — text that leaves the session before whatever the server
    does to its history next.

    ``(text, outcome, note)``: `text` is the reply, ``""`` when there was none;
    `outcome` is ``"captured"`` for an idle turn that answered, ``"empty"`` for
    an idle turn that said nothing, ``"error"`` or ``"timeout"`` for a turn that
    ended in a `session.error` or the silence window, and ``""`` when the ask did
    not happen at all — the threshold was not reached, this session already
    asked, the tree was clean, or nothing sized the model. `note` is
    `SUMMARY_FELL_BACK_NOTE` filled with `text` whenever there was any text at
    all, ``""`` otherwise — the partial answer the attempt managed, ready for the
    fresh session the caller opens, which is why it is never thrown away.

    `turn` carries `summary_attempted`, `summary_captured`, `summary_outcome`,
    `summary_fill` and `summary_at` on every attempt, whatever the answer.

    Every read is fail-open: no size, a fill under the threshold, a fill at or
    past the session's own size, a clean tree, a prompt that refused, a wait that
    raised, a tree that cannot be read and a file that cannot be written all
    stand this edge down and never end the run.
    `run.summary_attempted_at_attempt` is set only once all the guards passed and
    the ask was actually sent, and cleared again when the ask itself could not be
    sent, so a session whose ask failed outright is asked of again next time.
    """
    percent = context_memory.summary_at_percent(config)
    if not (isinstance(percent, (int, float)) and percent > 0):
        return "", "", ""
    if (getattr(run, "summary_attempted_at_attempt", -1) == run.attempt
            and getattr(run, "summary_session_id", "") == session_id):
        # One summary per session: a second crossing in the same history is not asked again (KC-40).
        return "", "", ""
    if not (isinstance(fill, (int, float)) and not isinstance(fill, bool)):
        return "", "", ""
    if fill < percent:
        return "", "", ""
    if fill >= 100:
        # No summary ask at or past 100% fill; the compact runs first and the ask waits (KC-69).
        return "", "", ""
    dirty = _summary_read_tree(ws)
    if not dirty:
        # Nothing uncommitted means the plain compact is right, with nothing to explain.
        return "", "", ""

    run.summary_attempted_at_attempt = run.attempt
    run.summary_session_id = session_id
    turn["summary_attempted"] = True
    turn["summary_captured"] = False
    turn["summary_fill"] = round(fill, 1)
    turn["summary_at"] = round(percent, 1)
    outcome = "error"
    text = ""
    try:
        mark_fn = getattr(backend, "mark", None)
        since = mark_fn() if callable(mark_fn) else None
        started = time.monotonic()
        backend.prompt(session, SUMMARY_PROMPT)
        idle = _wait_turn(backend, session, config, on_permission=on_permission,
                          on_question=on_question, since=since)
        turn["summary_sec"] = round(time.monotonic() - started, 1)
        status = getattr(idle, "status", "") or ""
        text = _last_assistant_text(backend, session)
        # The reply is judged by its ending: idle with text is a summary, silence keeps the partial (KC-40).
        outcome = "captured" if (status == "idle" and text) else (
            "empty" if status == "idle" else ("timeout" if status == "timeout" else "error"))
    except Exception as exc:  # noqa: BLE001 — a summary that cannot be asked is no ask
        _log.warning("%s: summary ask: %s", run.agent.name, _brief(str(exc)))
        run.summary_attempted_at_attempt = -1
        run.summary_session_id = ""
        turn.pop("summary_attempted", None)
        return "", "", ""
    turn["summary_outcome"] = outcome
    if text:
        # Captured and partial summaries are both written to the file (KC-40).
        run.summary = text
        if outcome == "captured":
            run.summaries += 1
            turn["summary_captured"] = True
        append_summary_text(Path(out_dir) / context_memory.summary_file_name(run.agent.name),
                            run.agent.name, text, session_id=session_id,
                            attempt=run.attempt)
    return text, outcome, (SUMMARY_FELL_BACK_NOTE.format(note=text) if text else "")


# KC-48: the reap leaves no process in an ended agent's worktree

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
            # Already dead: only its parent has to wait on it, and it is not recorded as reaped.
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
    # Wait for the KILLed process to exit, so the harvest does not see it still running (KC-48).
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


# KC-58: a whole pytest root takes one suite slot

#: The poll that finds a holder's pytest gone and moves a slow holder out of the queue.
_SUITE_POLL_SEC = 3.0

#: Default ceiling for a suite holding a slot, in seconds (round 92).
_SUITE_CEILING_SEC = 900.0

#: Seconds a fresh suite holder keeps its slot before the scan has seen its pytest (KC-58).
_SUITE_START_GRACE_SEC = 30.0


def _is_pytest_cmdline(cmd: str) -> bool:
    """Whether a `/proc/<pid>/cmdline` is a pytest: a word that *is* pytest.

    `pytest`, `/venv/bin/pytest`, `py.test`, or the `pytest` of
    `python3 -m pytest`. A word that merely contains it — `grep -rn pytest_x`,
    an editor on `test_pytest_ini.py` — is not a suite and holds no slot.
    """
    for word in str(cmd or "").split():
        if word.rsplit("/", 1)[-1] in ("pytest", "py.test"):
            return True
    return False


def _path_of(worktree) -> str:
    """*worktree* as a resolved path string for the suite scan.

    `""` when it cannot be read, which the scan reads as "no process", so a
    broken worktree path never holds a slot against the rest of the round.
    """
    try:
        return str(Path(worktree).resolve())
    except Exception:  # noqa: BLE001 — see the docstring
        return ""


def _suite_pids(worktree, proc_root: str | None = None) -> set:
    """The live `pytest` pids whose cwd is *worktree* or under it.

    KC-58's hold: the tool part is the wrong signal twice over. A suite started
    in the background (`background_process`, `… &`, `nohup`) completes its part
    at once while pytest runs on — two agents did that in round 103 — and a
    part cut by Kilo's own `bash` timeout ends two minutes into a four-minute
    suite, two agents a dozen times apiece.

    So the signal is the process, read the way KC-48's reap reads it:
    `/proc/<pid>/cwd` resolved and compared with the worktree, `cmdline`
    naming `pytest` as a word (`_is_pytest_cmdline`), the pid not a zombie, and
    never the runner itself or one of its ancestors. Empty, and never raised, for a
    worktree that cannot be read, a `/proc` that is not there at all, or a pid
    that exits between the listing and the read.

    *proc_root* defaults to `_PROC_ROOT` read at the call, not bound as a
    default, so patching the module constant is enough — the same seam the reap
    tests use for `REAP_GRACE_SEC`.
    """
    if proc_root is None:
        proc_root = _PROC_ROOT
    if not worktree:
        return set()
    try:
        root = Path(worktree).resolve()
    except Exception:  # noqa: BLE001 — a broken path is not a worktree
        return set()
    pids: set = set()
    try:
        excluded = _ancestor_pids(proc_root)
        for entry in os.listdir(proc_root):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid in excluded or not _alive(proc_root, pid):
                continue
            if not _is_pytest_cmdline(_proc_cmd(proc_root, pid)):
                continue
            cwd = _proc_cwd(proc_root, pid)
            if cwd is None:
                continue
            try:
                cwd.relative_to(root)
            except ValueError:
                continue
            pids.add(pid)
    except OSError:
        return set()
    return pids


def _suite_slots_armed(config) -> int:
    """`agent_suite_slots`: the round's slots for a whole pytest root.

    0 is "no queue" — every reply immediate, `_TEST_RUNS_LOCK` the only
    serialization. A config without the key, or one whose value is not a
    number, arms the ticket's default of 1 rather than raising into a run.
    """
    try:
        return max(0, int(getattr(config, "agent_suite_slots", 1) or 0))
    except (TypeError, ValueError):
        return 1


def _suite_ceiling(config) -> float:
    """`agent_suite_max_sec`: how long one holder may block the next waiter."""
    try:
        return max(0.0, float(getattr(config, "agent_suite_max_sec", _SUITE_CEILING_SEC) or 0))
    except (TypeError, ValueError):
        return float(_SUITE_CEILING_SEC)


def _is_full_suite_ask(props: dict) -> bool:
    """Whether this permission ask runs a whole pytest root, and so holds a slot.

    The `bash` permission whose command `is_full_suite_command` reads as a whole
    root. `external_directory` and `doom_loop` never hold one: they are about
    where a command reaches, not how long it runs.
    """
    try:
        if props.get("permission") != "bash":
            return False
        metadata = props.get("metadata")
        command = metadata.get("command") if isinstance(metadata, dict) else None
    except AttributeError:
        return False
    return is_full_suite_command(command)


class _SuiteSlots:
    """The round's slots for a whole pytest root: the agents' own runs and the
    runner's harvest, one queue for both.

    KC-58. Round 103's four lost entries had finished their code half an hour
    early and spent the rest of their hour on their own `pytest tests -n 4`,
    which took 20–25 minutes because every other finished agent ran one too and
    the judge's harvest ran alongside them. `_TEST_RUNS_LOCK` serialized only
    the harvest; this is the queue the agents' own runs stand in, and the
    harvest takes one of these slots as well, so nothing runs beside it.

    A slot is held by a *process*, not by a tool part — see `_suite_pids`. So a
    holder holds while its worktree still has a `pytest` running, and
    `_grant` drops the hold when the last one is gone, polled every
    `_SUITE_POLL_SEC` and on the exit of anyone who asked.

    The ceiling is a queue rule, not a kill: past `agent_suite_max_sec` the
    holder stops blocking, the free count grows by one and the next waiter goes
    in beside it, while its pytest keeps running — the agent is still alive and
    KC-48 leaves live agents' processes alone. Round 92's 33-minute suite then
    costs the round one slot for 15 minutes instead of every agent's hour.

    Everything here is fail-open. `limit = 0` is "no queue": every `acquire`
    returns 0.0 at once, records nothing and `status` is `None` for everyone,
    which is today's behaviour for real, not just in the shape of the call.
    A holder with no worktree to read is not a hold, an unreadable `/proc` is
    not a process, and no exception reaches the caller.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._entries: dict = {}
        self._limit = 1
        self._watcher: "threading.Thread | None" = None
        self._stop = threading.Event()

    # ── the round's shape ────────────────────────────────────────────────

    @property
    def limit(self) -> int:
        """The slots armed: 0 is "no queue", today's behaviour."""
        with self._cond:
            return int(self._limit)

    def configure(self, slots) -> int:
        """Arm the round's `agent_suite_slots`. A malformed value arms 1."""
        try:
            limit = int(slots)
        except (TypeError, ValueError):
            limit = 1
        with self._cond:
            self._limit = max(0, limit)
            self._cond.notify_all()
        return self._limit

    def reset(self) -> None:
        """Test seam: no holders, no waiters, one slot — the module's own start."""
        self._stop.set()
        with self._cond:
            self._entries.clear()
            self._limit = 1
            self._cond.notify_all()
        thread = self._watcher
        self._watcher = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)
        self._stop.clear()

    # ── the queue ────────────────────────────────────────────────────────

    def acquire(self, name, worktree, ceiling=None, stop=None, scoped=False) -> float:
        """Wait for a slot for *name*, holding *worktree*; the return is the wait.

        `limit = 0` returns 0.0 at once and records nothing. Otherwise the asker
        is queued FIFO and enters when a holder becomes releasable — its pytest
        gone or its ceiling past. A waiter whose *stop* fires (the agent stalled
        or ended mid-wait) drops out of the queue for good, so a dead waiter
        never blocks the one behind it.

        *scoped* is the harvest's hold: the caller runs the roots itself and
        calls `release` when they are done, so the scan never reads it — the
        judge's pytest has not started when the slot is granted, and a poll in
        that gap must not hand the slot on.
        """
        if self.limit <= 0:
            return 0.0
        asked = time.monotonic()
        with self._cond:
            entry = {"asked": asked, "held": None, "over": False, "done": False,
                     "seen": False, "scoped": bool(scoped),
                     "path": _path_of(worktree), "ceiling": ceiling}
            self._entries[name] = entry
            self._wake()
            while entry["held"] is None:
                if stop is not None and stop():
                    self._entries.pop(name, None)
                    return 0.0
                self._grant()
                if self._free() > 0:
                    entry["held"] = time.monotonic()
                else:
                    self._cond.wait(_SUITE_POLL_SEC)
            waited = max(0.0, time.monotonic() - asked)
        return waited

    def release(self, name) -> None:
        """Give the slot back for good: the agent ended, or the harvest finished.

        Called from `run_agent`'s `finally` and the harvest's own, so a slot
        never outlives its owner and the next waiter is answered at once rather
        than on the next poll.
        """
        with self._cond:
            if self._entries.pop(name, None) is not None:
                self._cond.notify_all()

    def ahead(self, name) -> int:
        """Who is in front of *name*: the holders that still block, the waiters
        who asked first."""
        with self._cond:
            return self._ahead(name)

    def status(self, name) -> tuple | None:
        """`("queued", waited, ahead)`, `("running", held, 0)`, `("over", held, 0)` or
        `("done", held, 0)` — `None` when nobody asked for a slot.

        `done` is a holder whose pytest is gone: the slot is free, the entry is
        what the heartbeat still reads until the agent releases it. The third
        number is 0 outside `queued`, as in `_TestRunsLock.status`, so a
        heartbeat can read both shapes.
        """
        with self._cond:
            entry = self._entries.get(name)
            if entry is None:
                return None
            now = time.monotonic()
            if entry["held"] is None:
                return ("queued", max(0.0, now - entry["asked"]), self._ahead(name))
            if entry["done"]:
                return ("done", max(0.0, now - entry["held"]), 0)
            return ("over" if entry["over"] else "running",
                    max(0.0, now - entry["held"]), 0)

    # ── under the guard ──────────────────────────────────────────────────

    def _free(self) -> int:
        """The slots open: `limit` minus the holders that still block."""
        blocking = sum(1 for entry in self._entries.values()
                       if entry["held"] is not None and not entry["over"] and not entry["done"])
        return max(0, int(self._limit) - blocking)

    def _ahead(self, name) -> int:
        mine = self._entries.get(name)
        if mine is None:
            return len(self._entries)
        asked = mine["asked"]
        count = 0
        for other_name, other in self._entries.items():
            if other_name == name:
                continue
            if other["held"] is not None:
                if not other["over"] and not other["done"]:
                    count += 1
            elif other["asked"] <= asked:
                count += 1
        return count

    def _grant(self) -> None:
        """Re-read the holders: a pytest that is gone, or a ceiling that is past,
        frees the slot for the next waiter. Neither is a kill.

        Called under the guard by the watcher, by every waiter on its own poll,
        and by `status` — so a hold is dropped within one poll of the process
        exiting, whatever else in the round is awake.
        """
        moved = False
        for entry in self._entries.values():
            if entry["held"] is None or entry["over"] or entry["done"]:
                continue
            if not entry.get("scoped"):
                try:
                    if _suite_pids(entry["path"]):
                        entry["seen"] = True
                    elif entry["seen"] or \
                            time.monotonic() - entry["held"] >= float(_SUITE_START_GRACE_SEC):
                        # gone — or never came up within the grace
                        entry["done"] = True
                        moved = True
                        continue
                except Exception:  # noqa: BLE001 — a scan that fails is not a release
                    pass
            ceiling = entry["ceiling"] if entry["ceiling"] is not None else _SUITE_CEILING_SEC
            if ceiling > 0 and time.monotonic() - entry["held"] >= ceiling:
                entry["over"] = True
                moved = True
        if moved:
            self._cond.notify_all()

    def _wake(self) -> None:
        """Make sure something is polling for the holders' pytest."""
        if self._watcher is not None and self._watcher.is_alive():
            return
        try:
            self._stop.clear()
            self._watcher = threading.Thread(target=self._poll, name="contest-suite-slots",
                                             daemon=True)
            self._watcher.start()
        except Exception:  # noqa: BLE001 — the waiters poll on their own too
            pass

    def _poll(self) -> None:
        """The round's suite poll, and its own way out once nobody is queued."""
        quiet = 0
        while not self._stop.wait(_SUITE_POLL_SEC):
            try:
                with self._cond:
                    self._grant()
                    quiet = quiet + 1 if not self._entries else 0
                    if quiet >= 4:      # a minute of nobody: no point in polling on
                        self._watcher = None
                        return
            except Exception:  # noqa: BLE001 — a poll is a hint, not a round
                pass


#: The round's suite slots; the harvest takes one too, so roots never run side by side.
_SUITE_SLOTS = _SuiteSlots()


def _suite_status(name) -> tuple | None:
    """The round's suite slots, as one agent sees them — `None` on any failure."""
    try:
        return _SUITE_SLOTS.status(name)
    except (AttributeError, TypeError, ValueError):
        return None


def _session_tool_parts(backend: ContestBackend, session) -> list:
    """The session's `tool` parts, `()` when the server cannot say.

    KC-58 §2 reads these at the turn deadline: a `bash` still `running`, and an
    `edit`/`write` completed inside the last `turn_extend_sec`, are both work
    the churn sample cannot see. A session that has gone, a backend without
    `tool_parts`, or a server that has is an empty history, never an exception
    into a deadline.
    """
    try:
        if session is None or not hasattr(backend, "tool_parts"):
            return ()
        parts = backend.tool_parts(session)
        return parts if isinstance(parts, list) else ()
    except Exception:  # noqa: BLE001 — see the docstring
        return ()


def _part_end(state: dict) -> float | None:
    """A `tool` part's `state.time.end` as epoch seconds, `None` when absent."""
    value = state.get("time")
    try:
        if isinstance(value, dict):
            value = value.get("end")
    except AttributeError:
        value = None
    try:
        end = float(value)
    except (TypeError, ValueError):
        return None
    return end if end > 0 else None


def _parts_say_working(parts, window: float, now: float) -> bool:
    """Whether *parts* show work the churn sample cannot see.

    KC-47's open `bash` is a call that has not come back: `state.status` still
    `running` or `pending`. Round 106's `edit` at 4 s before the abort is a
    `tool` part `edit`/`write` that completed inside the last *window* seconds —
    the churn went *down* there, 1073 → 1072, so a line count that grows is not
    the only work a turn can have. `state.time.end` is epoch seconds, so the
    window is measured against `time.time()` and never the runner's monotonic.
    """
    for part in parts:
        if not isinstance(part, dict):
            continue
        state = part.get("state")
        if not isinstance(state, dict):
            continue
        tool = str(part.get("tool") or "")
        if tool == "bash" and str(state.get("status") or "") in ("running", "pending"):
            return True
        if window <= 0 or tool not in ("edit", "write"):
            continue
        end = _part_end(state)
        if end is None:
            continue
        if 0 <= now - end <= window:
            return True
    return False


def _retry_waited_of(idle) -> float:
    """Round 146: the seconds a wait's deadline was moved for the provider's
    retries (`IdleResult.retry_waited`), ``0.0`` when the result has none — an
    OpenRouter turn, a stub, a result from before the field. Never raises."""
    value = getattr(idle, "retry_waited", 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, float(value))


def _is_working(working) -> bool:
    """KC-58 §2: is there work the churn sample cannot see? A `working` that
    raises is `False` — a read failure is never a reason to extend a turn."""
    try:
        return bool(working()) if callable(working) else False
    except Exception:  # noqa: BLE001 — see the docstring
        return False


# One agent

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
    # Each agent gets its own scratch dir, allowed to it and forbidden to the others (KC-59).
    tmp_roots = getattr(config, "tmp_roots", ()) or ()
    scratch_dir = agent_tmp_dir(tmp_roots, spec.name)
    scratch_arg = str(scratch_dir) if scratch_dir is not None else ""
    scratch_others = tuple(
        d for d in agent_tmp_dirs(tmp_roots, config.agents) if d.name != spec.name
    )
    # The round's TMPDIR is scratch for the agent too, so tmp_path fixtures are not gate calls.
    agent_tmp_glob = f"{agent_tmp}/*" if agent_tmp is not None else ""
    session: SessionRef | None = None
    # Sessions already counted with _record_session, so a replaced one is counted once (KC-39).
    recorded_sessions: set = set()
    stalled: list = []          # the reason, once the runner's stall edge fired
    time_up: "threading.Timer | None" = None   # the agent's hard limit, `agent_max_sec`
    # [seconds left, as of monotonic] once armed; a pause for the suite queue stops it and resume restarts it (KC-58).
    time_left: list = []
    questions_this_turn = [0]
    # A permission refused for a full context means the next prompt compacts first (KC-69).
    context_full = [False]
    # Note for the next prompt of this session after a captured summary (KC-40).
    carry_note = [""]
    # A pending abort for a full-context refusal is cancelled when the turn ends first (KC-69).
    context_stopper: list = []
    # Set when the runner itself aborted the turn, so the stop is read as a plain idle (round 49).
    context_aborted = [False]
    # Set while the summary is asked, so the tool stop is not armed for that reply (KC-40).
    summary_asking = [False]
    # The first-touch watch's decision for this turn; the turn's own thread acts on it (KC-42).
    escalate = [""]
    # Suite slots: one semaphore round-wide, one slot by default (KC-58).
    try:
        _SUITE_SLOTS.configure(_suite_slots_armed(config))
    except Exception:  # noqa: BLE001 — an unarmed queue answers every ask
        pass
    try:
        ticket_files = declared_files(ticket_path)
    except OSError:
        ticket_files = ()

    def transition(state: AgentState, error: str | None = None, *, note: str | None = None) -> None:
        run.state = state
        if error is not None:
            run.last_error = error
        detail = error if error is not None else note
        if error is not None and note:
            # The error keeps the stall reason alone; the verdict goes in the note (KC-29, KC-18).
            detail = f"{error} ({note})"
        _log.info("%s: %s%s", spec.name, state.value, f" — {detail}" if detail else "")
        on_transition(run)

    def stall(reason: str) -> None:
        """Abort the session and end the wait; it wakes on the closed stream."""
        if stalled:
            return
        stalled.append(reason)
        _abort_quietly(backend, session)
        backend.interrupt(session)

    def arm_time_up(seconds: float) -> None:
        """Start the agent's hard limit with *seconds* left on it."""
        nonlocal time_up
        time_left[:] = [float(seconds), time.monotonic()]
        time_up = threading.Timer(
            float(seconds), stall,
            args=(f"time up: {_age(float(config.agent_max_sec))} for the agent",))
        time_up.daemon = True
        time_up.start()

    def pause_time_up() -> bool:
        """KC-58: stop the hard limit while a whole-root suite queues for a slot.

        The queue is the round's, not the agent's: an agent stuck behind four
        other suites must not reach `agent_max_sec` for it. True when it was
        paused — nothing to pause (no limit, the agent already stopped, or the
        limit is due this instant) is False, and the limit runs on as armed.
        """
        if time_up is None or not time_left or stalled:
            return False
        left = time_left[0] - (time.monotonic() - time_left[1])
        if left <= 0:
            return False
        time_up.cancel()
        time_left[:] = [left, time.monotonic()]
        return True

    def resume_time_up() -> None:
        """Start the paused hard limit again with what it had left."""
        if stalled or not time_left:
            return
        arm_time_up(time_left[0])

    def working() -> bool:
        """KC-58 §2: is this agent waiting on something the churn cannot see?

        A `bash` call that has not come back, a suite slot it holds or waits
        for, a `pytest` still running in its own worktree, or an `edit`/`write`
        part completed inside the last `turn_extend_sec`. Round 103's four
        lost entries were here for an hour, an hour after their code was done,
        and round 106's was making an edit 4 s before the abort. Every read is
        fail-open: nothing here may raise into a deadline.
        """
        try:
            status = _suite_status(spec.name)
            if status is not None and status[0] in ("queued", "running", "over"):
                return True
            if _suite_pids(ws.path):
                return True
        except Exception:  # noqa: BLE001 — the queue and the scan are hints
            pass
        try:
            return _parts_say_working(
                _session_tool_parts(backend, session),
                float(getattr(config, "turn_extend_sec", 0) or 0),
                time.time())
        except Exception:  # noqa: BLE001 — a history that cannot be read is empty
            return False

    # Test-run cache, one gate per agent over one file; off unless enabled (ticket 212).
    test_cache_gate = None
    if getattr(config, "test_cache", False):
        try:
            _gate_settings = getattr(policy, "_settings", None)
            test_cache_gate = TestCacheGate(
                out_dir / "test-cache.jsonl", agent=spec.name,
                share=getattr(config, "test_cache_share", "round"),
                classify=((lambda cmd: _testcache.classify_with_llm(cmd, _gate_settings))
                          if getattr(config, "test_cache_llm", True) and _gate_settings is not None
                          else None))
        except Exception as exc:  # noqa: BLE001
            _log.warning("%s: test cache unavailable: %s", spec.name, exc)

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
            # The agent's tmp roots: the round's globs, its own scratch dir (KC-59), and the TMPDIR (KC-65).
            tmp_roots=tuple(tmp_roots) + agent_tmp_globs(tmp_roots, spec.name)
                      + ((agent_tmp_glob,) if agent_tmp_glob else ()),
            # Forbidden paths: the rounds folder, other agents' scratch dirs; this agent's own worktree is allowed (KC-46).
            forbidden=tuple(HARD_DENYLIST) + (ws.path.parent,) + scratch_others,
            ticket_title=Path(ticket_path).name,
            ticket_files=tuple(ticket_files),
            recent_tools=recent,
            gate_budget_left=max(0, int(config.gate_max_calls_per_session) - spent),
        )
        # Fill is read before each decision, so every ask line shows it; at the threshold the ask is refused (KC-69).
        fill_now = _context_now()
        if _context_is_full(fill_now):
            decision = Decision(reply="reject", layer="context", reason=CONTEXT_FULL_REJECT)
            if not context_full[0] and not summary_asking[0]:
                # Once the model keeps using tools at a full context, the turn is stopped after its reply (live 7.6.2).
                _log.info("%s: context full at a permission — stopping the turn to "
                          "compact before the next prompt", spec.name)
                def _stop_for_context(target=session):
                    context_aborted[0] = True
                    _abort_quietly(backend, target)

                stopper = threading.Timer(CONTEXT_ABORT_DELAY_SEC, _stop_for_context)
                stopper.daemon = True
                context_stopper.append(stopper)
                stopper.start()
            context_full[0] = True
        else:
            decision = policy.decide(event, ctx)
            if (test_cache_gate is not None and decision.reply == "once"
                    and props.get("permission") == "bash"):
                test_cache_gate.harvest(recent)
                cached = test_cache_gate.ask(props, ws.path)
                if cached:
                    decision = Decision(reply="reject", layer="test-cache", reason=cached)
                    _log.info("%s: test cache hit — %s", spec.name, cached[:160])
        policy.record(decision, event, agent_dir / "decisions.jsonl", context=fill_now)
        run.permissions["allowed" if decision.reply == "once" else "rejected"] += 1
        if decision.layer == "gate":
            run.permissions["gated"] += 1
        elif decision.layer in ("gate-failed", "budget"):
            run.permissions["gate_failed"] += 1
        _log.info("%s: permission %s -> %s (%s), %s", spec.name, props.get("permission"),
                  decision.reply, decision.layer, _context_words(fill_now))
        # A pytest root waits for a round suite slot before it is answered; the wait is given back to the turn (KC-58).
        queued = 0.0
        if decision.reply == "once":
            try:
                if _suite_slots_armed(config) > 0 and _is_full_suite_ask(props):
                    paused = pause_time_up()
                    try:
                        queued = float(_SUITE_SLOTS.acquire(
                            spec.name, ws.path, ceiling=_suite_ceiling(config),
                            stop=lambda: bool(stalled) or bool(backend.interrupted())))
                    finally:
                        if paused:
                            resume_time_up()
                    if queued > 0:
                        _log.info("%s: suite slot after %s in the queue",
                                  spec.name, _age(queued))
            except Exception:  # noqa: BLE001 — the reply is never held by the queue
                queued = 0.0
        # Gate seconds are given back to this turn's deadline, so the agent never pays for the gate's wait (KC-66).
        back = 0.0
        tries = int(getattr(decision, "gate_attempts", 1) or 1)
        turn_clock = getattr(run, "_turn_clock", None)
        if turn_clock is not None:
            back = turn_clock.grant_gate(
                float(getattr(decision, "gate_added_sec", 0.0) or 0.0), tries)
        if back > 0:
            _log.info("%s: gate %d tries, +%gs to the turn", spec.name, tries, back)
        if turn_clock is not None and queued > 0:
            back += turn_clock.grant_suite(queued)
        return decision.reply, decision.reason, back

    def on_question(event: dict) -> None:
        del event  # rejected by wait_idle regardless; only the count matters here
        run.questions += 1
        questions_this_turn[0] += 1
        if questions_this_turn[0] >= int(config.max_questions_per_turn):
            stall(f"{questions_this_turn[0]} questions in one turn")

    def finish(state: AgentState, error: str | None = None, *, note: str | None = None) -> AgentRun:
        # Only the file this run pushed goes; a file the agent wrote on purpose survives (round 151).
        drop_stale_kilo_file(ws.path, expected_limit=pushed_spec[0])
        transition(state, error, note=note)
        return run

    def _record_once(session_ref: SessionRef | None) -> None:
        """KC-39: record a session's cost, tokens and transcript — once.

        `finally` records the last session the run held; a session replaced
        mid-run must be recorded at the moment it is replaced, or its cost and
        its messages are gone with it. A swap whose ``POST /session`` is refused
        has already recorded the outgoing session and ``finally`` sees the same
        one again, so the id is remembered and the second read is skipped —
        otherwise one session's cost would be counted twice.
        """
        if session_ref is None or session_ref.id in recorded_sessions:
            return
        recorded_sessions.add(session_ref.id)
        _record_session(run, backend, session_ref, out_dir)

    def _replace_session(turn: dict, *, reason: str = "") -> str | None:
        """KC-39: replace the session the work is going on in.

        One helper for every swap: KC-54's overflow (`fresh_session` below),
        KC-69's swap after a compact that freed nothing (`swap_session`) and
        KC-39's reset for a continue that repeats its diff. The new session is
        created with the same provider, model, rules, title and agent as the one
        it replaces; `run.session_id` and `session` become it, `run.sessions`
        and `run.sessions_this_attempt` both grow by one, and *turn* carries
        `session_id` for the one that goes away and `new_session` for the one
        that replaces it, with `reason` naming in `turns.jsonl` why the swap
        happened.

        The outgoing session is recorded first — its cost, tokens and messages
        are added to the run's (§5), since `finally` only sees the last one —
        then stopped (KC-73's `retire_session`). Returns ``None`` when the swap
        happened, else the error line for `finish`.
        """
        nonlocal session
        turn["session_id"] = run.session_id
        if reason:
            turn["session_reason"] = reason
        _record_once(session)
        retire_session(turn)
        try:
            session = backend.create_session(
                spec.provider_id, spec.model_id, rules=config.session_rules(),
                title=ws.branch, agent=spec.kilo_agent, variant=spec.variant)
        except (ContestBackendError, ValueError) as exc:
            return f"POST /session failed: {_brief(str(exc))}"
        turn["new_session"] = session.id
        run.session_id = session.id
        run.sessions += 1
        # The ceiling is per attempt, so a replacement spends one of its allowance (KC-39).
        run.sessions_this_attempt += 1
        return None

    def _carry_note() -> str:
        """KC-40: the summary this run already has, ready for a fresh session.

        A session that opened mid-attempt — a reset, a swap, an overflow — has
        never seen the diff the previous one was working on, so it gets the
        summary the previous one wrote, alongside the `git status` lines its
        round prompt carries. `""` when there was never one: the prompt is then
        exactly what it was before KC-40. Every caller appends it to a prompt, so
        it leads with its own blank line.
        """
        summary = (getattr(run, "summary", "") or "").strip()
        return "\n\n" + SUMMARY_FELL_BACK_NOTE.format(note=summary) if summary else ""

    def fresh_session(turn: dict, dirty: str, *, note: str = "") -> str | None:
        """Go on in a new session: the error line if ``POST /session`` failed.

        KC-54's swap, shared by KC-56's context cut-off. The full session is
        replaced, never prompted again; the new one has never seen the ticket
        or the round prompt, so its first prompt is `round_prompt(dirty=)`
        (KC-22's `--resume` shape), without the paragraph when *dirty* is
        empty. *turn* — the one that filled the old session — is recorded with
        that session's id, because `finally` records only the last session,
        and with ``new_session`` once there is one. `run.attempt` is left
        alone; the swap spends one of `max_continues_per_attempt`.

        KC-40: *note* is carried in front of the round prompt — the partial
        answer the summary ask left before it failed, or the summary a reset
        takes along with it. `""` is today's prompt, unchanged.
        """
        nonlocal continue_text
        failed = _replace_session(turn)
        if failed is not None:
            run.turns.append(turn)
            _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
            return failed
        run.turns.append(turn)
        _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
        prompt = round_prompt(spec.name, ticket_path, ws.base_sha, dirty=dirty,
                              tmp_dir=scratch_arg, tmp_roots=tuple(tmp_roots))
        continue_text = (note + "\n\n" + prompt) if note else prompt
        run.continues += 1
        return None

    def retire_session(turn: dict) -> None:
        """KC-73: stop the session a swap leaves behind.

        The old session is never prompted again, but Kilo may still be working
        in it: round 78's agnes-2-0-flash, whose session went on after Kilo's
        own overflow compact and sat on an unanswered `external_directory` ask
        for as long as the round ran — in the worktree the new session edits.
        `abort` is quiet on a session that is already done; a failure is
        logged and the swap goes on.
        """
        try:
            backend.abort(session)
            turn["old_session_aborted"] = True
        except Exception as exc:  # noqa: BLE001 — a failed abort never ends a run
            _log.warning("%s: abort of the old session %s: %s", spec.name,
                         getattr(session, "id", session), _brief(str(exc)))
            turn["old_session_aborted"] = False

    def settle_overflow(turn: dict) -> bool:
        """KC-73: wait out what Kilo still does in a session that overflowed.

        On a context overflow Kilo 7.6.2 compacts the session by itself and goes
        on with the agent loop, so the `session.error` that ended the turn is
        not the session's last word. Round 78, agnes-2-0-flash: the runner POSTed
        its own `summarize` into that busy session, Kilo's loop asked
        `external_directory`, nothing read the ask while the POST was open, and
        the POST ran its 900 s out — 15 of the agent's 90 minutes, then a swap
        away from a session Kilo had already compacted.

        So the session is waited on first, asks answered, the turn's budgets
        and silence clock armed — `turn_timeout_sec` without KC-36's churn
        extension: this is Kilo finishing what it started, not a turn the
        runner prompted, and `agent_max_sec` still bounds the agent. A session silent for `OVERFLOW_SETTLE_SEC` has
        nothing running (``quiet``); one that ends in anything but an idle is
        left to the recovery as before. True only when Kilo compacted the
        session itself, it went idle, and what it holds now is under
        `compact_at_percent` (or its size is not known): the work goes on in it
        with `OVERFLOW_CONTINUE`, and the caller spends the continue. A backend
        with no shared event stream (`mark()` is `None`) has nothing to wait on.
        """
        mark_fn = getattr(backend, "mark", None)
        if not callable(mark_fn) or mark_fn() is None:
            return False
        started = time.monotonic()
        settled = _wait_turn(backend, session, config, on_permission=on_permission,
                             on_question=on_question, quiet_after=OVERFLOW_SETTLE_SEC)
        took = time.monotonic() - started
        status = getattr(settled, "status", "")
        if status == "quiet":
            return False
        turn["overflow_settle_sec"] = round(took, 1)
        turn["overflow_settle_status"] = status
        if status == "error":
            # A second overflow may leave the session busy, so it is stopped before the summarize call (round 78).
            _abort_quietly(backend, session)
        if status != "idle" or not getattr(settled, "compacted", False):
            _log.info("%s: after the overflow Kilo worked on for %.0f s and ended %s%s — "
                      "the runner recovers it", spec.name, took, status,
                      "" if getattr(settled, "compacted", False) else ", no compact of its own")
            return False
        size, _source = _context_budget(spec, _memory(), config)
        after = _context_tokens(backend, session)
        percent = context_memory.compact_at_percent(config)
        left = after * 100.0 / float(size) if size and after else None
        if left is not None and percent > 0 and left >= percent:
            _log.info("%s: Kilo compacted the overflow itself, but it holds %.1f%% of %s — "
                      "the runner compacts it again", spec.name, left, f"{size:,}")
            return False
        turn["overflow_compacted"] = True
        turn["compacted"] = True
        turn["compacted_by"] = "kilo"
        turn["context_after"] = after or None
        run.compactions += 1
        _log.info("%s: Kilo compacted the overflow itself and went idle in %.0f s%s — "
                  "the work goes on in the same session", spec.name, took,
                  f" at {left:.1f}% of {size:,}" if left is not None else "")
        return True

    def recover_overflow(turn: dict) -> str | None:
        """KC-69: an overflow with nothing uncommitted goes on instead of stalling.

        The session is compacted — Kilo summarizes it in chunks, so a history
        past the model's size still compacts (live, laguna: 249 022 tokens) —
        and the work continues in the same session with `OVERFLOW_CONTINUE`
        when the summary left it under the threshold. A compact that failed, or
        that left too much, goes on in a new session: the round prompt and the
        old session's summary (`CONTEXT_CONTINUE_NOTE`). ``"stall"`` when the
        backend cannot compact at all, which is KC-54's clean-overflow stall;
        the error line when ``POST /session`` failed; else ``None``, with the
        turn recorded and one of `max_continues_per_attempt` spent.
        """
        nonlocal continue_text
        if not callable(getattr(backend, "compact", None)):
            return "stall"
        before = _context_tokens(backend, session)
        started = time.monotonic()
        done = _compact_session(backend, session, config, on_permission, on_question)
        took = time.monotonic() - started
        size, _source = _context_budget(spec, _memory(), config)
        after = _context_tokens(backend, session) if done else None
        percent = context_memory.compact_at_percent(config)
        turn["overflow_compacted"] = bool(done)
        turn["compacted"] = bool(done)
        if done:
            run.compactions += 1
        # KC-73: 0 is "the last message reports no tokens", not a size
        turn["context_before"] = before or None
        turn["context_after"] = after
        turn["compact_sec"] = round(took, 1)
        left = after * 100.0 / float(size) if done and size and after else None
        if done and (left is None or percent <= 0 or left < percent):
            _log.info("%s: context overflow compacted in %.0f s: %s -> %s tokens%s — the "
                      "work goes on in the same session", spec.name, took, f"{before:,}",
                      f"{after:,}" if after else "?",
                      f" = {left:.1f}% of {size:,}" if left is not None else "")
            continue_text = OVERFLOW_CONTINUE
        else:
            _log.info("%s: context overflow: the compact %s — going on in a new session",
                      spec.name, "did not finish" if not done
                      else f"left {left:.1f}% of {size:,}")
            failed = swap_session(turn)
            if failed:
                return failed
            summary = turn.pop("summary_text", "") or "(the previous session wrote no summary)"
            continue_text = (round_prompt(spec.name, ticket_path, ws.base_sha,
                                          tmp_dir=scratch_arg)
                             + "\n\n" + CONTEXT_CONTINUE_NOTE.format(summary=summary)
                             + _carry_note())
        run.continues += 1
        run.turns.append(turn)
        _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
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

    def _context_now() -> dict | None:
        """KC-69: the session's fill right now, for a permission's log line.

        ``{"tokens", "size", "source", "fill", "compact_at"}`` — ``size`` and
        ``fill`` ``None`` when nothing sizes the model. ``None`` when it cannot be
        read at all: an ask is never held up by the read.
        """
        try:
            size, source = _context_budget(spec, _memory(), config)
            tokens = _context_tokens(backend, session)
        except Exception:  # noqa: BLE001 — no read, no line, never a held ask
            return None
        fill = round(tokens * 100.0 / float(size), 1) if size else None
        return {"tokens": tokens, "size": size, "source": source, "fill": fill,
                "compact_at": context_memory.compact_at_percent(config)}

    def _context_is_full(now: dict | None) -> bool:
        """KC-69: at or past ``compact_at_percent`` of a known size, with the
        compact on — whichever source the size came from, Kilo's or the memory's."""
        if not now or now.get("fill") is None:
            return False
        at = float(now.get("compact_at") or 0)
        return at > 0 and float(now["fill"]) >= at

    def _context_words(now: dict | None) -> str:
        """KC-69: the fill as one phrase of a console line."""
        if not now:
            return "context unknown"
        if now.get("size") is None:
            return f"context {now['tokens']:,} tokens, size unknown"
        return (f"context {now['tokens']:,} tokens = {now['fill']:.1f}% of "
                f"{now['size']:,} ({now['source']}), compact at {now['compact_at']:g}%")

    pushed_limit: list = [None]
    pushed_spec: list = [None]
    push_refused: list = [None]

    def _push_remembered_limit() -> None:
        """Round 145: a remembered window below Kilo's handed to the running
        server (`KiloBackend.set_model_limit`, ``PATCH /config`` for this
        workspace), so Kilo compacts inside the turn by it — the same
        `context_memory.kilo_limit` the next round's spawn overlay carries. Only
        when the memory is smaller than what Kilo was told (or Kilo was told
        nothing), once per size, and only on a backend that can: anything that
        goes wrong is a warning and the watch below still stands.

        Round 148: nothing is handed over when the workspace tracks
        ``.kilo/kilo.jsonc`` — the patch would rewrite a tracked file and land
        in the agent's diff, and `info/exclude` does nothing there. That check
        is the backend's (`tracked_kilo_files`), which raises: a silent return
        would look like a window Kilo now holds. Here the refusal is remembered
        so the check is not re-run for the same size and the warning is not
        repeated — and `pushed_limit[0]` stays unset, so `context_watch` arms,
        because a push that never happened is exactly why the watch is needed.

        Called only between turns, right before a prompt's mark: live, 7.6.2
        answers the patch by reloading the workspace's instance
        (``server.instance.disposed``) — a turn or a compact running in it
        would be cut, and the event stream ends (the backend reconnects it).

        Round 151: ``pushed_limit[0]`` is set the moment the patch answers 200,
        not after the backend has reopened the event stream for it. A reload
        that the reconnect could not follow is this round's stream, not its
        window: Kilo answered the patch and now sizes the session by the
        pushed limit, so `pushed_limit[0]` is set and `context_watch` stands
        down — aborting a turn Kilo was about to compact throws away the step
        in progress — and the failure gets its own line, not the "not handed
        to Kilo" line a caller would read as a window still unset. A refused
        or a failed patch is the other way: nothing went over, the watch arms.

        Round 153: 200 is not the end of it either. A model whose ``limit`` the
        server's own config content carries — this round's spawn overlay, or
        the operator's — keeps it field by field, whatever the patch said, and
        the backend's read-back off ``GET /provider`` is what shows it
        (`KiloLimitKept`). That is the refused case, not the pushed one: Kilo
        sizes the session by its own window, so `pushed_limit[0]` stays unset
        and `context_watch` arms, the turn is stopped by the watch at the size
        the memory just recorded, and the size is remembered so it is not
        re-patched — and Kilo reloaded — every prompt. The file the patch wrote
        is still ours, so `pushed_spec[0]` is set and it is dropped on the way
        out like a push that took.
        """
        setter = getattr(backend, "set_model_limit", None)
        if not callable(setter):
            return
        declared = getattr(spec, "context_limit", None)
        try:
            size, output = context_memory.remembered(
                _memory(), spec.provider_id, spec.model_id,
                context_memory.min_window(config),
                declared=declared,
                fallback=_context_limit_fallback(
                    getattr(config, "context_limit_fallback", None)),
                share_pct=context_memory.full_refusal_percent(config))
        except Exception:  # noqa: BLE001 — no memory, nothing to hand over
            return
        if not size or pushed_limit[0] == size or push_refused[0] == size:
            return
        if isinstance(declared, int) and not isinstance(declared, bool) and 0 < declared <= size:
            return
        limit = context_memory.kilo_limit(size, output, context_memory.compact_at_percent(config))
        if limit is None:
            return
        percent = context_memory.compact_at_percent(config)
        try:
            setter(spec.provider_id, spec.model_id, limit)
        except KiloLimitRefused as exc:
            # The checkout refuses the same way again, so the refusal is remembered and not re-run (KC-69).
            push_refused[0] = size
            _log.warning("%s: the remembered window was not handed to Kilo: %s",
                         spec.name, _brief(str(exc)))
            return
        except KiloLimitKept as exc:
            # The read-back still shows the server's own limit, so the size is remembered and not re-patched (round 153).
            push_refused[0] = size
            pushed_spec[0] = limit
            detail = _brief(str(exc))
            if exc.kept:
                _log.warning("%s: Kilo kept %s from KILO_CONFIG_CONTENT — the watch stays "
                             "armed (%s)", spec.name, f"{exc.kept:,}", detail)
            else:
                _log.warning("%s: Kilo did not take the patched window — the watch stays "
                             "armed (%s)", spec.name, detail)
            return
        except KiloTapReconnectError as exc:
            # The patch was accepted and the window went over; only the stream is broken, so it is reported (KC-69).
            pushed_limit[0] = size
            pushed_spec[0] = limit
            _log.warning("%s: the window went to Kilo but its event stream was not "
                         "reopened after the reload: %s", spec.name, _brief(str(exc)))
            return
        except Exception as exc:  # noqa: BLE001 — the watch still stands
            _log.warning("%s: the remembered window was not handed to Kilo: %s",
                         spec.name, _brief(str(exc)))
            return
        pushed_limit[0] = size
        pushed_spec[0] = limit
        # Kilo compacts at input minus a reserve, so that is the figure printed (round 146).
        compact_point = max(0, int(limit.get("input") or 0)
                            - min(context_memory.KILO_COMPACT_RESERVE,
                                  int(limit.get("output") or 0)))
        _log.info("%s: Kilo now sizes %s/%s = %s (compact at %g %% of it, after %s "
                  "tokens) — was %s", spec.name, spec.provider_id, spec.model_id, f"{size:,}",
                  percent, f"{compact_point:,}",
                  f"{declared:,}" if isinstance(declared, int) else "unknown")

    def context_watch(turn: dict) -> "_ContextWatch | None":
        """Round 145: the fill, watched *inside* a turn, for a model Kilo cannot
        size — the fallback for a window that could not be handed to Kilo.

        Kilo compacts a session inside a turn by the ``limit`` it was given, and
        since round 147 that can be handed to it between turns
        (`_push_remembered_limit`, a ``PATCH /config`` for the workspace, the
        event stream reconnected after the reload, the read-back checked against
        the limit Kilo still reports). So the watch is armed only when the
        window was **not** handed over — the push failed, was refused,
        was kept by Kilo's own config content, was skipped, or the backend has
        no `set_model_limit`: `pushed_limit[0]` is not the
        size the session is sized by now. When it did go, Kilo compacts by the
        pushed window at the same fill the watch would fire at
        (`kilo_limit` sets `input = percent * size + min(20 000, output)` and
        compacts at `input - reserve`), and the watch's abort would only throw
        away the step in progress — whichever of the two fired first would win,
        and the loser is a turn's work.

        This thread reads the fill every ``[contest] context_watch_sec`` and, at
        ``compact_at_percent``, stops the turn the way KC-69's refused ask does:
        `context_full` set, the session aborted once, and the next prompt
        compacts first. It lags one step behind what the model is doing and
        cannot catch a single large jump — round 145's glm read about 42 000
        tokens in one step and met the wall with no read in between.

        The read is an HTTP call and can return after the turn ended, so it is
        re-checked against the stop signal before it acts, and the actions are
        taken under the lock the turn's `finally` takes too (`_ContextWatch`).
        ``None`` — nothing armed — when the watch is off (0), the compact is
        off, there is no size, the size is Kilo's own, or the window went to
        Kilo.
        """
        every = _context_watch_sec(config)
        percent = context_memory.compact_at_percent(config)
        if every <= 0 or percent <= 0:
            return None
        try:
            size, source = _context_budget(spec, _memory(), config)
        except Exception:  # noqa: BLE001 — no size, no watch
            return None
        if not size or source == "kilo":
            return None
        if pushed_limit[0] == size:
            # the window went to Kilo before this prompt: it compacts by it
            _log.info("%s: Kilo compacts inside the turn by the window handed to it "
                      "(%s tokens) — the in-turn watch stays off", spec.name, f"{size:,}")
            return None
        stop = threading.Event()
        target = session
        lock = threading.Lock()

        def watch() -> None:
            while not stop.wait(every):
                try:
                    tokens = _context_tokens(backend, target)
                except Exception:  # noqa: BLE001 — an unreadable fill is no fill
                    continue
                if stop.is_set():
                    # The read took so long the turn ended, so its actions would land on the next wait.
                    return
                fill = tokens * 100.0 / float(size)
                if fill < percent or context_full[0]:
                    continue
                with lock:
                    if stop.is_set():
                        return
                    context_full[0] = True
                    context_aborted[0] = True
                    turn["context_watch_stop"] = round(fill, 1)
                    _log.info("%s: context %s tokens = %.1f%% of %s (%s) inside the turn — "
                              "stopping it to compact before the next prompt", spec.name,
                              f"{tokens:,}", fill, f"{size:,}", source)
                    # The abort is sent under the lock, so it cannot land on the next turn.
                    _abort_quietly(backend, target)
                return

        thread = threading.Thread(target=watch, name=f"context-watch-{spec.name}", daemon=True)
        thread.start()
        return _ContextWatch(stop, thread, every, lock)

    def first_touch_watch(turn: dict) -> "_FirstTouch | None":
        """KC-42: the watch on this turn, or None when the ticket is off.

        One per turn, armed from the turn's own `sent_at`, and stopped by the
        `finally` of the wait below. The three things it can do are all the
        runner's own writes, and each is done where it belongs:

        * the **nudge** is a `prompt` into a session that is still working, so
          it goes from the watch's own thread — the main thread is inside
          `wait_idle` and cannot send anything, which is exactly why round
          64's worst slot could not be reached at all;
        * the **reset** and the **DEAD** both need this thread — a replacement
          session is a new `POST /session` and `run.session_id` with it, and
          the terminal state is `on_transition`. So the watch asks for them
          through `escalate` and aborts, and the WAITING loop below carries
          them out.
        """
        budget = _first_touch_budget(config)
        if budget <= 0:
            return None
        if escalate[0]:
            escalate[0] = ""
        ceiling = _sessions_ceiling(config)
        # A session is available only below the per-attempt ceiling; 0 or a spent attempt means DEAD (KC-39).
        room = 1 if 0 < ceiling > run.sessions_this_attempt else 0
        return _FirstTouch(
            run, ws, spec, config,
            nudge=lambda since: backend.prompt(
                session,
                first_touch_nudge(ticket_files, since)
                + prompt_workers_note(out_dir, config)),
            reset=lambda: _wake_for_first_touch(turn, "reset"),
            kill=lambda: _wake_for_first_touch(turn, "dead"),
            budget=budget, nudges=_first_touch_nudges(config), ceiling=room).start()

    def _wake_for_first_touch(turn: dict, what: str) -> None:
        """KC-42: end the wait so the WAITING loop can do *what*.

        The watch has already spent its nudges and cannot ask for a session or
        a terminal state itself, so it says which one it wants and stops the
        wait the way `stall` does — an abort. `wait_idle` then comes back on
        the `session.idle` (or `MessageAbortedError`) the abort brings, and the
        branch below reads `escalate` before it reads any of that as a model
        error. `backend.interrupt` is deliberately *not* called: it stops the
        event tap for good, so a reset could never wait in the new session.
        """
        turn["first_touch_wake"] = what
        escalate[0] = what
        _abort_quietly(backend, session)

    def swap_session(turn: dict) -> str | None:
        """KC-69: go on in a new session after a compact that did not free the
        context; the error line if ``POST /session`` failed.

        The old session is left as it is. The summary it wrote, when it wrote
        one, is read first, then the new session is made the way KC-54's swap
        makes one; the caller builds its first prompt with `round_prompt` and
        `CONTEXT_CONTINUE_NOTE`, because it has never seen the ticket.
        """
        nonlocal session
        turn["summary_text"] = _summary_text(backend, session)
        turn["swapped_from"] = run.session_id
        return _replace_session(turn)

    def _context_gate(turn: dict, kind: str) -> str:
        """KC-67 + KC-69: what this session holds before the prompt, and what it
        earns: ``""`` for nothing, ``"compacted"`` when a compact freed the
        context, ``"swap"`` when the caller must go on in a new session. Either
        of the last two means the caller needs a fresh mark for its wait.

        The size is Kilo's own ``limit.context`` when the spec has one — intake's,
        or the remembered one KC-69 hands Kilo — else the smallest one remembered
        for this provider and model, else the round's ``context_limit_fallback``
        (KC-10) for the free tiers the provider declares no limit for, else
        nothing at all. The fill is the last
        reply that still went through over that size. A compact happens at
        ``compact_at_percent`` of any known size, and always after a permission
        was refused for a full context (KC-69), but only for a prompt that goes
        into a session which already holds a conversation: a fresh session holds
        nothing to compact away.

        A compact that did not finish, or that left the context still at or past
        the threshold, is ``"swap"`` (KC-69): the prompt going out as it stands
        is the overflow the compact was for.

        The four fields are written whether or not there is a size, so a turn
        always says what it knew: ``context_size``, ``context_source``,
        ``fill`` and ``compacted``.

        KC-40: at ``summary_at_percent``, with an uncommitted diff in the
        worktree, the summary edge fires first — forced or not.
        ``"summary_fallback"`` when the ask got no answer and the attempt has a
        session to spare; otherwise the compact goes on as above, and a prompt
        that needs no compact after the ask is ``"summarized"`` — the ask's idle
        is on the stream, so the caller needs a fresh mark then too.
        """
        forced, context_full[0] = context_full[0], False
        size, source, fill, tokens = None, "none", None, 0
        try:
            size, source = _context_budget(spec, _memory(), config)
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
            return ""
        percent = context_memory.compact_at_percent(config)
        if forced and fill is not None and fill < percent:
            # A compact since the refusal already freed the session, so the refusal is stale.
            _log.info("%s: a permission was refused for a full context, but it is "
                      "%.1f%% of %s now — no compact before %s", spec.name, fill,
                      f"{size:,}", kind)
            forced = False
        if not forced and (not size or percent <= 0 or fill is None):
            return ""
        # The summary is asked below the compact threshold, so the model's own knowledge is kept (KC-40).
        asked = ""
        summary_percent = context_memory.summary_at_percent(config)
        if (size and fill is not None
                and isinstance(summary_percent, (int, float)) and summary_percent > 0
                and fill >= summary_percent):
            summary_asking[0] = True
            try:
                text, outcome, note = maybe_summarize_before_compact(
                    backend, session, run, ws, config, out_dir, turn,
                    session_id=getattr(session, "id", "") or "", fill=fill,
                    on_permission=on_permission, on_question=on_question)
            finally:
                summary_asking[0] = False
            # The summary ask is its own turn, so any stop it armed is undone before the compact (KC-69).
            while context_stopper:
                context_stopper.pop().cancel()
            context_full[0] = False
            if outcome:
                # The summary ask's own idle is on the stream; the caller takes a fresh mark (KC-63).
                asked = "summarized"
            if note:
                turn["summary_note"] = note
            if outcome == "captured":
                # The next prompt carries the one-line note; a later reset carries the full text.
                carry_note[0] = SUMMARY_CONTINUES_NOTE
                _log.info("%s: the summary is out (%s chars) — the compact "
                          "waits for %g%% before %s", spec.name, len(text),
                          percent, kind)
            elif outcome == "":
                # Already asked here, or nothing is uncommitted, so the prompt gets the compact's decision.
                _log.info("%s: no summary to ask for at %.1f%% of %s", spec.name,
                          fill, f"{size:,}")
            else:
                # No summary means the fresh-session edge takes over, only while a session is spare (KC-39, KC-40).
                ceiling = _sessions_ceiling(config)
                if ceiling > 0 and run.sessions_this_attempt < ceiling:
                    _log.info("%s: summary ask at %.1f%% of %s ended %s%s — no "
                              "answer, the work goes on in a new session", spec.name,
                              fill, f"{size:,}", outcome,
                              f" ({len(text)} chars written)" if text else "")
                    return "summary_fallback"
                _log.info("%s: summary ask at %.1f%% of %s ended %s — no answer, and "
                          "no session left in the attempt: compacting", spec.name,
                          fill, f"{size:,}", outcome)
            # Re-read after the summary turn, so the compact is decided on the current size.
            try:
                re_tokens = _context_tokens(backend, session)
                tokens = re_tokens
                fill = re_tokens * 100.0 / float(size)
                turn["fill"] = round(fill, 1)
            except Exception:  # noqa: BLE001 — the read before stands
                pass
        if not forced:
            if fill < percent:
                # One console line per prompt of a sized model, so the fill is seen growing toward the compact.
                _log.info("%s: context %s tokens = %.1f%% of %s (%s), below %g%% — "
                          "no compact before %s", spec.name, f"{tokens:,}", fill,
                          f"{size:,}", source, percent, kind)
                return asked
            _log.info("%s: context %s tokens = %.1f%% of %s (%s), at %g%% — "
                      "compacting before %s", spec.name, f"{tokens:,}", fill,
                      f"{size:,}", source, percent, kind)
        else:
            turn["context_refused"] = True
            _log.info("%s: a permission was refused for a full context (%s tokens) — "
                      "compacting before %s", spec.name, f"{tokens:,}", kind)
        started = time.monotonic()
        done = _compact_session(backend, session, config, on_permission, on_question)
        took = time.monotonic() - started
        if not done:
            if not callable(getattr(backend, "compact", None)):
                # a backend with no server-side history has nothing to swap from
                return ""
            _log.info("%s: compact did not finish after %.0f s — going on in a new "
                      "session with the %s prompt", spec.name, took, kind)
            return "swap"
        after = _summary_tokens(backend, session)
        turn["compacted"] = True
        run.compactions += 1
        turn["context_before"] = tokens
        turn["context_after"] = after
        turn["compact_sec"] = round(took, 1)
        left = after * 100.0 / float(size) if size and after is not None else None
        if after is not None and after < tokens:
            _log.info("%s: compact finished in %.0f s: context %s -> %s tokens "
                      "(-%s, -%.0f%%)%s", spec.name, took, f"{tokens:,}", f"{after:,}",
                      f"{tokens - after:,}", (tokens - after) * 100.0 / max(tokens, 1),
                      f" = {left:.1f}% of {size:,}" if left is not None else "")
        else:
            _log.info("%s: compact finished in %.0f s: context %s tokens before, the "
                      "size after is %s", spec.name, took, f"{tokens:,}",
                      "not reported yet" if after is None else f"{after:,}")
        if left is not None and percent > 0 and left >= percent:
            _log.info("%s: the compact left %.1f%% — still at or past %g%%, going on "
                      "in a new session with the %s prompt", spec.name, left, percent, kind)
            return "swap"
        _log.info("%s: the round continues in the same session with the %s prompt",
                  spec.name, kind)
        return "compacted"

    overflows_seen: list = []

    def _overflow_of(error):
        """KC-54's overflow check, round 145's two readings on top of it.

        The three fixed spellings (`_OVERFLOW_RE`) are an overflow as they
        always were. Round 145 adds two more, and both demand a session that
        actually grew — the refused *request* at or above ``[contest]
        context_min_window`` — so a model a plan cuts off at a small prompt
        (round 144's free tier) ends the way it always ended and never writes a
        size into the memory. The floor is lowered to
        ``context_full_refusal_percent`` of the declared window when Kilo
        declares one, and of ``context_limit_fallback`` when it does not —
        `context_memory.size_of`, which decides what the next round remembers,
        measures the same two walls and must not disagree with this. With
        neither, the floor stands: a window under it stays unremembered.

          * a size refusal in the provider's own words (`_SIZE_REFUSAL_RE` with
            the round's ``quota_patterns``, so money, a plan, a key or a rate
            never is one);
          * a refusal with no words about a size, of a session at or past
            ``context_full_refusal_percent`` of its window (`_is_full_refusal`).

        And neither may repeat into a loop: a second one in this run whose
        *request* was under `REPEAT_OVERFLOW_SHARE` of the first one's *request*
        came after a compact (or a fresh session) left far less than the wall,
        so the refusal is about something else — the error it is, not another
        compact. The guard compares one unit to one unit — the refused request,
        the reply plus what its tools added; the memory record keeps `last_ok`,
        the reply alone, but the guard never does.
        Each reading is logged, so the operator sees why an error became one.

        Reads the error *before* either new reading — and only *after* the fixed
        spellings, which keep their verdict — whether it is a status the provider
        does not use for a size, or an error any other path owns: a retry, the
        provider being unavailable, or Kilo's own abort. A rate cap that names
        tokens, a timeout that names a deadline and a body-size 413 would each
        read as an overflow otherwise, and an overflow ends the agent's run
        instead of retrying it.

        Returns how it read the error, or ``False``: ``"words"`` when a size
        refusal named a size, ``"inferred"`` when the session's fill did. An
        inferred reading names no size, so it never writes one to the memory —
        ``_remember_overflow`` takes the answer and skips — and it still counts
        for the repeat guard. Round 146: ``"wall"`` is Kilo's own ``Compaction
        exhausted`` (`_KILO_WALL_RE`) — an overflow for the turn, in whatever
        words it came, and the same for the memory: no size, so nothing written.
        """
        text = _error_texts(error)
        kilo_wall = _KILO_WALL_RE.search(text) is not None
        if _OVERFLOW_RE.search(text) is not None:
            # The old overflow path also records the size of the refused request (round 145).
            try:
                last_ok, grew = _last_reply(backend, session)
            except Exception:  # noqa: BLE001 — no read, no record
                last_ok, grew = 0, 0
            overflows_seen.append(last_ok + (grew or 0))
            _log.info("%s: %r at %s tokens — read as a context overflow%s", spec.name,
                      _brief(_error_message(error)), f"{last_ok:,}",
                      " (Kilo's own wall: no size, not remembered)" if kilo_wall else "")
            return "wall" if kilo_wall else "words"
        status = None
        data = error.get("data") if isinstance(error, dict) else None
        if isinstance(data, dict):
            status = data.get("statusCode")
        if status is not None and status not in _SIZE_REFUSAL_STATUSES:
            return False
        if _retryable(error) or _is_provider_unavailable(error) or _is_external_abort(error):
            return False
        quota_re = _quota_re(config)
        if _not_a_size(text, quota_re):
            return False
        try:
            last_ok, grew = _last_reply(backend, session)
            size, source = _context_budget(spec, _memory(), config)
        except Exception:  # noqa: BLE001 — no read, no guess: the error as before
            return False
        # The refused request is the last reply plus what its tools added, so that size is compared (round 145).
        requested = last_ok + (grew or 0)
        floor = context_memory.min_window(config)
        share = context_memory.full_refusal_percent(config) / 100.0
        if share > 0:
            # A declared window the session is refused below counts as full, else the fallback or the floor (round 149, 152).
            wall = _declared_window(spec)
            if wall is None:
                wall = _context_limit_fallback(
                    getattr(config, "context_limit_fallback", 0))
            if wall > 0:
                floor = min(floor, int(share * wall))
        if not last_ok or (floor and requested < floor):
            # A refusal of the first request cannot mean a full context, whatever the floor.
            _log.info("%s: %r at %s tokens — under context_min_window %s, not read "
                      "as a context overflow", spec.name,
                      _brief(_error_message(error)), f"{last_ok:,}",
                      f"{floor:,}")
            return False
        worded = _SIZE_REFUSAL_RE.search(text) is not None
        if worded:
            how = "read as a context overflow"
        elif _is_full_refusal(error, last_ok, size, quota_re, share):
            how = f"at {last_ok * 100.0 / float(size):.1f}% of {size:,} ({source}) " \
                  "read as a context overflow"
        else:
            return False
        if overflows_seen and requested < REPEAT_OVERFLOW_SHARE * max(overflows_seen):
            _log.info("%s: %r at %s tokens, after an overflow at %s — not the window "
                      "again, not read as a context overflow", spec.name,
                      _brief(_error_message(error)), f"{requested:,}",
                      f"{max(overflows_seen):,}")
            return False
        # The unit is the refused request, not last_ok, since the reply alone was half of it.
        overflows_seen.append(requested)
        _log.info("%s: %r at %s tokens %s", spec.name, _brief(_error_message(error)),
                  f"{last_ok:,}", how)
        if not worded:
            return "inferred"
        return "wall" if kilo_wall else "words"

    def _remember_overflow(error, how="words") -> None:
        """KC-67: one line in the shared memory, for the next round.

        The provider's own numbers — the limit it named, the prompt it named,
        the last reply that still went through — plus the round and the agent
        that produced them, so a remembered size always says where it came from.
        The record is added before the decision KC-54 makes of this overflow, so
        it is remembered whether the round ends STALLED or READY. A write that
        fails is a warning and nothing else: the overflow still ends the turn
        the way it does today, and the line is for the next session, not this
        one.

        An inferred reading (``"inferred"`` from `_overflow_of`) names no size —
        the session's fill did, not the provider — so it writes nothing and
        hands nothing to Kilo: the record would size the model for every agent
        of the next rounds from a number the provider never said. The compact
        and the continue still happen, and the guard still counts it.

        Round 146: the same for ``"wall"`` — Kilo's own ``Compaction exhausted``
        (`_KILO_WALL_RE`). It is the wall Kilo holds, declared or pushed from this
        memory, speaking; its ``last_ok`` is where the session stood when Kilo
        gave up, not a window, and a record of it lowers the wall the next agent
        gets and so breeds the next one.
        """
        if how in ("inferred", "wall"):
            return
        try:
            limit, prompt = context_memory.parse_overflow(_error_message(error))
            last_ok, grew = _last_reply(backend, session)
            record = context_memory.OverflowRecord(
                at=time.time(),
                round=str(out_dir.name or ""),
                agent=spec.name,
                provider=spec.provider_id,
                model=spec.model_id,
                limit=limit,
                last_ok=last_ok,
                prompt=prompt,
                output=context_memory.parse_output(_error_message(error)),
                grew=grew,
            )
            if not context_memory.add(context_memory.memory_path(config, out_dir),
                                      record, days=context_memory.days_of(config)):
                _log.warning("%s: context memory: not written", spec.name)
            # The window goes to Kilo before the next prompt, since Kilo may still be compacting (round 148).
        except Exception as exc:  # noqa: BLE001 — the overflow still ends the turn
            _log.warning("%s: context memory: %s", spec.name, _brief(str(exc)))

    try:
        # Remove the untracked project file a PATCH /config left, so a resumed agent does not read it (round 148, 151).
        drop_stale_kilo_file(ws.path, assume_stale=True)
        # ── CREATED: one session, kept for every turn ─────────────────────
        if not run.sessions:
            # A run with no sessions must not append to an earlier run's transcript file (KC-39).
            try:
                (out_dir / f"{spec.name}.session.json").unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log.warning("%s: stale transcript not removed: %s", spec.name, exc)
        try:
            session = backend.create_session(
                spec.provider_id, spec.model_id, rules=config.session_rules(),
                title=ws.branch, agent=spec.kilo_agent, variant=spec.variant)
        except (ContestBackendError, ValueError) as exc:
            return finish(AgentState.ERROR, f"POST /session failed: {_brief(str(exc))}")
        run.session_id = session.id
        # One more session for this run; the attempt's own count starts at one (KC-39).
        run.sessions += 1
        run.sessions_this_attempt = 1
        # The agent's hard limit: agent_max_sec from here, 0 means no limit (KC-39).
        agent_max = float(getattr(config, "agent_max_sec", 0) or 0)
        if agent_max > 0:
            arm_time_up(agent_max)

        rework_text = None
        retry_text = None
        continue_text = None
        retries_used = 0
        local_retries = 0
        # KC-75: empty idle turns in a row (a rate-limited free model).
        silent_retries = 0
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
                # A later leg opens with the relay so far, as a continue and never a rework (KC-43).
                leg_note = getattr(run, "leg_note", "") or ""
                if dirty:
                    # The worktree may hold work from before a --resume; the fresh session is told (KC-22).
                    text = round_prompt(spec.name, ticket_path, ws.base_sha, dirty=dirty,
                                        tmp_dir=scratch_arg, tmp_roots=tuple(tmp_roots),
                                        leg_note=leg_note)
                    run.dirty_on_resume = ""  # only the first prompt carries it
                else:
                    text = round_prompt(spec.name, ticket_path, ws.base_sha, tmp_dir=scratch_arg,
                                        tmp_roots=tuple(tmp_roots), leg_note=leg_note)
                run.leg_note = ""  # only the first prompt carries it
            turn = {"kind": kind, "attempt": run.attempt, "sent_at": time.time()}
            if kind == "continue":
                note = (f"attempt {run.attempt} (continue {run.continues} of "
                        f"{int(config.max_continues_per_attempt)})")
            else:
                note = f"attempt {run.attempt} ({kind})"
            transition(AgentState.PROMPTED, note=note)
            # A remembered window below Kilo's is sent to Kilo now, before the mark is taken (round 145).
            _push_remembered_limit()
            # The mark is taken just before the POST, so earlier events are seen and this turn's are not (KC-63).
            mark_fn = getattr(backend, "mark", None)
            since = mark_fn() if callable(mark_fn) else None
            # The session may already hold more than the model takes, so the first prompt is gated (KC-67).
            gated = _context_gate(turn, kind)
            if gated == "summary_fallback":
                # The summary ask got no answer: a fresh session takes the round prompt and the partial text (KC-40).
                carry_note[0] = ""
                # The partial answer of a failed ask goes at the front of the new prompt.
                failed = fresh_session(turn, _summary_read_tree(ws),
                                       note=turn.pop("summary_note", ""))
                if failed:
                    return finish(AgentState.ERROR, failed)
                continue
            if gated == "swap":
                # The compact did not free the session, so the work goes on in a new one (KC-69).
                failed = swap_session(turn)
                if failed:
                    run.turns.append(turn)
                    _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                    return finish(AgentState.ERROR, failed)
                try:
                    dirty = _dirty_tree(ws) if _commits_above(ws) == 0 else ""
                except TreeReadError:
                    dirty = ""
                summary = turn.pop("summary_text", "") or "(the previous session wrote no summary)"
                carried = _carry_note()
                text = (round_prompt(spec.name, ticket_path, ws.base_sha, dirty=dirty,
                                      tmp_dir=scratch_arg)
                        + "\n\n" + CONTEXT_CONTINUE_NOTE.format(summary=summary)
                        + carried
                        + "\n\n" + text)
            if gated:
                since = mark_fn() if callable(mark_fn) else None
            # A captured summary is carried once, into the prompt right after the ask (KC-40).
            carry = carry_note[0]
            carry_note[0] = ""
            if carry:
                text = carry + "\n\n" + text
            try:
                # The worker count of this moment is the last line of the message (KC-65).
                backend.prompt(session, text + prompt_workers_note(out_dir, config))
            except ContestBackendError as exc:
                return finish(AgentState.ERROR, f"prompt failed: {_brief(str(exc))}")

            # ── WAITING ────────────────────────────────────────────────────
            transition(AgentState.WAITING)
            questions_this_turn[0] = 0
            # This turn's deadline comes from its own worktree; the heartbeat reads it mid-wait (KC-36, KC-58).
            clock = _turn_deadline(run, config, working)
            run._turn_clock = clock
            context_aborted[0] = False
            # The first-touch watch is armed from the turn's prompt and stopped with the wait (KC-42).
            touch = first_touch_watch(turn)
            watch = context_watch(turn)
            try:
                idle = _wait_turn(backend, session, config,
                                  on_permission=on_permission, on_question=on_question,
                                  on_deadline=clock.on_deadline, since=since)
            finally:
                run._turn_clock = None
                if touch is not None:
                    touch.stop()
                if watch is not None:
                    watch.stop()
                while context_stopper:
                    context_stopper.pop().cancel()
            # Each nudge queues one more idle to wait for, since the first idle is the old turn's (KC-75).
            queued = int(getattr(touch, "nudged", 0) or 0) if touch is not None else 0
            while queued > 0 and not escalate[0] and getattr(idle, "status", "") == "idle":
                queued -= 1
                # No mark is taken here, since the queued turn may already be past it (KC-73).
                again = _wait_turn(backend, session, config,
                                   on_permission=on_permission, on_question=on_question,
                                   on_deadline=clock.on_deadline,
                                   quiet_after=QUEUED_NUDGE_QUIET_SEC)
                if getattr(again, "status", "") not in ("idle", "error"):
                    # A quiet or closed wait keeps the idle already read, as before KC-75.
                    break
                turn["queued_waits"] = int(turn.get("queued_waits", 0)) + 1
                _log.info("%s: that idle closed the turn before the nudge — the "
                          "nudge's own turn ran on, and its end is the turn's",
                          spec.name)
                idle = again
            if getattr(idle, "compacted", False) and getattr(idle, "status", "") == "idle":
                # The compacts Kilo runs mid-turn are counted once per turn (round 83).
                run.compactions += 1
                turn["compacted"] = True
                turn["compacted_by"] = "kilo"
                _log.info("%s: Kilo compacted the session itself during the turn",
                          spec.name)
            if escalate[0]:
                # The watch ended this turn; read this first, before the abort is scored as an ending (KC-42).
                wanted = escalate[0]
                escalate[0] = ""
                turn["first_touch_nudges"] = int(run.first_touch_nudges_used or 0)
                if touch is not None and touch.touched():
                    # The model wrote in the window before the abort, so the turn is ordinary again.
                    turn["first_touch_touched"] = True
                elif wanted == "dead":
                    turn["first_touch_reset"] = int(run.first_touch_resets or 0)
                    turn["first_touch_dead"] = True
                    run.turns.append(turn)
                    _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                    # DEAD releases the slot at once and is never harvested or patched (KC-42).
                    _log.info("%s: DEAD — nothing modified after %d nudge(s) and %d "
                              "fresh session(s); the slot is released", spec.name,
                              turn["first_touch_nudges"], turn["first_touch_reset"])
                    return finish(AgentState.DEAD,
                                  f"nothing modified in {turn['first_touch_nudges'] + 1} "
                                  f"rounds of the {_first_touch_budget(config):g}s "
                                  "first-touch clock")
                else:
                    turn["first_touch_reset"] = 1
                    _log.info("%s: nothing modified — a fresh session, %d of %d",
                              spec.name, run.sessions_this_attempt + 1,
                              _sessions_ceiling(config))
                    failed = _replace_session(turn, reason="first_touch")
                    if failed is not None:
                        run.turns.append(turn)
                        _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                        run.resumable = True
                        return finish(AgentState.ERROR, failed)
                    run.continues = 0
                    run.last_diff_signature = ""
                    run.turns.append(turn)
                    _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                    continue_text = round_prompt(
                        spec.name, ticket_path, ws.base_sha, dirty="",
                        tmp_dir=scratch_arg, tmp_roots=tuple(tmp_roots))
                    continue
            if (context_aborted[0] and idle.status == "error"
                    and _is_external_abort(idle.error)):
                # The runner's own stop can come back as MessageAbortedError; the turn is read as idle (round 49).
                _log.info("%s: the turn stopped for a full context ended in "
                          "MessageAbortedError — the runner's own abort, a plain idle",
                          spec.name)
                turn["context_aborted"] = True
                idle = dataclasses.replace(idle, status="idle", error=None)
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
            if clock.suite_waited > 0:
                # KC-58: how long this turn's whole-root suites queued for a slot
                turn["suite_wait_sec"] = int(clock.suite_waited)
            retry_waited = _retry_waited_of(idle)
            if retry_waited > 0:
                # How long the provider kept Kilo retrying is time the deadline gave back (round 146).
                turn["provider_wait_sec"] = int(retry_waited)
            if stalled:
                turn["idle_status"] = "stalled"
                error, state = stalled[0], AgentState.STALLED
            elif idle.status == "timeout":
                # A timeout is a silence stall or a never-idle turn, told apart by the deadline (KC-12, KC-36, KC-47, KC-66).
                silence = float(config.idle_event_timeout_sec or 0)
                limit = (float(config.turn_timeout_sec) + clock.granted + clock.gate_added
                         + clock.suite_waited + retry_waited)
                quiet = 0 < silence and idle.elapsed < limit
                if quiet:
                    turn["idle_status"] = "stalled"
                    turn["idle_kind"] = "SILENT"
                    open_tool = getattr(idle, "open_tool", None) or {}
                    if open_tool:
                        error = (f"no event for {silence:g}s during "
                                 f"{open_tool.get('tool', 'bash')}: "
                                 f"{open_tool.get('command') or '(no command)'}")
                    else:
                        error = f"no event for {silence:g}s"
                    budget = int(config.max_continues_per_attempt)
                    if 0 < budget and run.continues < budget:
                        continue_text = CONTINUE_PROMPT
                        run.continues += 1
                        turn["continues"] = run.continues
                        backoff = float(getattr(config, "error_retry_backoff_sec", 0) or 0)
                        if not _wait_backoff(backoff):
                            if stalled:
                                turn_r = {"kind": "continue", "attempt": run.attempt,
                                          "sent_at": time.time(), "idle_at": time.time(),
                                          "idle_status": "stalled", "idle_kind": "SILENT",
                                          "continues": run.continues,
                                          "continues_exhausted": True}
                                run.turns.append(turn_r)
                                _append_jsonl(agent_dir / "turns.jsonl",
                                              {"agent": spec.name, **turn_r})
                                return finish(AgentState.STALLED, stalled[0])
                        run.turns.append(turn)
                        _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                        continue
                    turn["continues_exhausted"] = True
                    state = AgentState.STALLED
                else:
                    error = _no_idle_error(config, idle.elapsed, clock)
                    state = AgentState.STALLED
            elif idle.status == "error":
                overflow = _overflow_of(idle.error)
                if overflow:
                    _remember_overflow(idle.error, overflow)
                    # An overflowed session is never retried; work goes on in a fresh session (KC-54).
                    budget = int(config.max_continues_per_attempt)
                    room = 0 < budget and run.continues < budget
                    # Kilo is waited out before the tree is read or compacted, since it compacts on its own (KC-73).
                    if room and settle_overflow(turn):
                        continue_text = OVERFLOW_CONTINUE
                        run.continues += 1
                        run.turns.append(turn)
                        _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                        continue
                    if stalled:
                        # A stall while Kilo still works: no compact and no new session (KC-73).
                        room = False
                    dirty = ""
                    tree_read = False
                    # The tree is read in every case, including a branch that already has commits (round 80).
                    above = _commits_above(ws)
                    try:
                        dirty = _dirty_tree(ws)
                        tree_read = True
                    except TreeReadError as exc:
                        # An unreadable tree is not "no work"; the stall says so (FL-2).
                        _log.warning("%s: tree unreadable — %s", spec.name,
                                     _brief(str(exc)))
                    if not above and dirty and room:
                        failed = fresh_session(turn, dirty)
                        if failed:
                            return finish(AgentState.ERROR, failed)
                        continue
                    if tree_read and room and bool(above) == bool(dirty):
                        # Nothing written yet means a fresh prompt; commits on a clean tree go to the harvest (KC-69).
                        failed = recover_overflow(turn)
                        if failed is None:
                            continue
                        if failed != "stall":
                            return finish(AgentState.ERROR, failed)
                    # A clean overflow is a stall, not a crash; fall through to the harvest, which can still score it READY (KC-21).
                    if dirty:
                        error = "context overflow"
                    elif above:
                        error = f"context overflow ({above} commit{'s' if above != 1 else ''}, clean tree)"
                    else:
                        error = "context overflow with no uncommitted work"
                    state = AgentState.STALLED
                    if stalled:
                        error = stalled[0]
                        turn["idle_status"] = "stalled"
                # The quota check runs first, so a daily limit is not retried as a 429 (KC-61).
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
                    # A provider outage is never retried here, since Kilo already retried it (KC-64).
                    down = _is_provider_unavailable(idle.error)
                    # A local store error keeps its own retry budget, not the provider's (KC-62).
                    local_budget = int(getattr(config, "max_local_store_retries", 5) or 0)
                    if (not overflow and not down and _is_local_store(idle.error)
                            and local_retries < local_budget):
                        run.turns.append(turn)
                        _append_jsonl(agent_dir / "turns.jsonl",
                                      {"agent": spec.name, **turn, "cause": "local_store"})
                        local_retries += 1
                        # Backoff is doubled per retry and jittered, so agents do not retry in step (KC-62).
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
                    # The count of finished replies is read only for a provider rejection (KC-45).
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
                        # The hint sits after the cut, since an exhausted plan reads like an outage (KC-64).
                        data = (idle.error or {}).get("data") or {}
                        error = (f"provider_unavailable after {data.get('attempts')} retries: "
                                 f"{_brief(data.get('message') or '')}"
                                 f"{_PLAN_HINT}")
                        state = AgentState.ERROR
                    elif not overflow:
                        # An overflow sets its own error above; this is the fallback for other session errors.
                        error = f"session.error: {_brief(idle.error)}"
                        if retries_used:
                            error = f"after {retries_used} retries: {error}"
                        # The store budget is spent, so the log says how hard the runner tried (KC-62).
                        if local_retries and _is_local_store(idle.error):
                            error = f"after {local_retries} kilo store retries: {error}"
                        state = AgentState.ERROR
            elif idle.status == "closed":
                error, state = f"event stream closed: {_brief(idle.error)}", AgentState.ERROR
            elif idle.status == "idle":
                # An idle turn is a good answer, so the error budgets start over (KC-62).
                retries_used = 0
                local_retries = 0
                idle_kind = classify_idle(backend, session, turn["sent_at"], ws)
                turn["idle_kind"] = idle_kind.value
                # A silent turn during a provider error is retried with a longer wait, on its own budget (KC-75).
                if idle_kind is not IdleKind.SILENT:
                    silent_retries = 0
                elif (int(getattr(idle, "provider_errors", 0) or 0) > 0
                      and silent_retries < int(getattr(config, "max_silent_retries", 0) or 0)):
                    silent_retries += 1
                    turn["silent_retries"] = silent_retries
                    turn["provider_errors"] = int(idle.provider_errors)
                    backoff = _retry_backoff(silent_retries, config.error_retry_backoff_sec,
                                             getattr(config, "silent_retry_max_backoff_sec", 0))
                    _log.info("%s: empty reply after %d provider error(s) (4xx/5xx) — "
                              "retry %d/%d in %ds, no continue spent", spec.name,
                              int(idle.provider_errors), silent_retries,
                              int(config.max_silent_retries), backoff)
                    continue_text = CONTINUE_PROMPT
                    run.turns.append(turn)
                    _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                    if not _wait_backoff(backoff) and stalled:
                        return finish(AgentState.STALLED, stalled[0])
                    continue
                if idle_kind in (IdleKind.CUT, IdleKind.SILENT):
                    budget = int(config.max_continues_per_attempt)
                    # A reply cut at the length limit: a full window gets a fresh session, an output cut gets a continue (KC-56).
                    cut = (_cut_off(backend, session, spec.context_limit)
                           if idle_kind is IdleKind.CUT else None)
                    if cut is not None:
                        turn["cut_off"] = cut
                    if 0 < budget and run.continues < budget and cut == "context":
                        dirty = ""
                        if _commits_above(ws) == 0:
                            try:
                                dirty = _dirty_tree(ws)
                            except TreeReadError as exc:
                                _log.warning("%s: tree unreadable — %s", spec.name,
                                             _brief(str(exc)))
                        failed = fresh_session(turn, dirty)
                        if failed:
                            return finish(AgentState.ERROR, failed)
                        continue
                    if 0 < budget and run.continues < budget:
                        continue_text = CUT_OFF_MESSAGE if cut == "output" else CONTINUE_PROMPT
                        run.continues += 1
                        turn["continues"] = run.continues
                        if idle_kind == IdleKind.SILENT:
                            backoff = float(getattr(config, "error_retry_backoff_sec", 0) or 0)
                            if not _wait_backoff(backoff):
                                if stalled:
                                    turn_r = {"kind": "continue", "attempt": run.attempt,
                                              "sent_at": time.time(), "idle_at": time.time(),
                                              "idle_status": "stalled", "idle_kind": "SILENT",
                                              "continues": run.continues,
                                              "continues_exhausted": True}
                                    run.turns.append(turn_r)
                                    _append_jsonl(agent_dir / "turns.jsonl",
                                                  {"agent": spec.name, **turn_r})
                                    return finish(AgentState.STALLED, stalled[0])
                        run.turns.append(turn)
                        _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                        continue
                    turn["continues_exhausted"] = True
                    error = state = None
                else:
                    # An idle turn with edits but no commit is nudged on in the same session, not harvested (KC-22).
                    budget = int(config.max_continues_per_attempt)
                    if 0 < budget and run.continues < budget:
                        dirty = ""
                        if _commits_above(ws) == 0:
                            try:
                                dirty = _dirty_tree(ws)
                            except TreeReadError as exc:
                                # An unreadable tree falls through to the harvest, which reads git itself (FL-2).
                                _log.warning("%s: tree unreadable — %s", spec.name,
                                             _brief(str(exc)))
                        # A reply cut at the token limit goes on, clean tree or not (KC-56).
                        cut = _cut_off(backend, session, spec.context_limit)
                        if cut is not None:
                            turn["cut_off"] = cut
                        if cut == "context":
                            # A continue into a full window would overflow again, so a fresh session is used (round 74).
                            failed = fresh_session(turn, dirty)
                            if failed:
                                return finish(AgentState.ERROR, failed)
                            continue
                        if cut == "output" or dirty:
                            # A continue is granted and the diff signature is compared with the last one (KC-39).
                            signature = ""
                            repeat = False
                            ceiling = _sessions_ceiling(config)
                            if dirty and ceiling > 0:
                                # Diff signature is written only when the repeated-diff check is on (KC-39).
                                signature = _diff_signature(ws)
                                turn["diff_signature"] = signature
                                previous = getattr(run, "last_diff_signature", "") or ""
                                repeat = (bool(signature) and signature == previous
                                          and run.continues + 1 >= budget
                                          and run.sessions_this_attempt < ceiling)
                            if repeat:
                                # A continue that adds nothing ends in a fresh session with the status lines (KC-39).
                                _log.info("%s: continue %d of %d would repeat the "
                                          "previous diff (%s) — new session %d of %d",
                                          spec.name, run.continues + 1, budget,
                                          signature[:12], run.sessions_this_attempt + 1,
                                          ceiling)
                                failed = _replace_session(turn, reason="repeat")
                                if failed is not None:
                                    # A refused replacement ends the run here; the worktree stays for --resume.
                                    run.turns.append(turn)
                                    _append_jsonl(agent_dir / "turns.jsonl",
                                                  {"agent": spec.name, **turn})
                                    run.resumable = True
                                    return finish(AgentState.ERROR, failed)
                                run.continues = 0
                                run.last_diff_signature = ""
                                run.turns.append(turn)
                                _append_jsonl(agent_dir / "turns.jsonl",
                                              {"agent": spec.name, **turn})
                                continue_text = (round_prompt(
                                    spec.name, ticket_path, ws.base_sha, dirty=dirty,
                                    tmp_dir=scratch_arg, tmp_roots=tuple(tmp_roots))
                                    + _carry_note())
                                continue
                            continue_text = CUT_OFF_MESSAGE if cut else continue_message(dirty)
                            run.continues += 1
                            if signature:
                                run.last_diff_signature = signature
                            run.turns.append(turn)
                            _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                            continue
                    error = state = None
            else:
                error = state = None
            if state is not None:
                # A turn that died with a commit is scored once before the terminal state lands (KC-21).
                note = None
                # An abort the runner did not ask for is not the agent's end; it is resumable (round 87).
                external_abort = (state is AgentState.ERROR and idle.status == "error"
                                  and _is_external_abort(idle.error))
                if external_abort:
                    run.resumable = True
                    _log.warning("%s: session aborted by Kilo (its server stopped) — "
                                 "--resume restarts it", spec.name)
                if state in (AgentState.STALLED, AgentState.ERROR) and not external_abort:
                    above = _commits_above(ws)
                    # A turn that ends with uncommitted work is committed for the model, with a progress row (KC-41).
                    if above == 0 and _deadline_commit_enabled(config):
                        reason = _deadline_reason(state, error, stalled)
                        sha = _deadline_commit(ws, reason=reason, ticket=ticket_path)
                        if sha:
                            run.deadline_commit = True
                            turn["deadline_commit"] = True
                            _write_progress_row(
                                ws, ticket_path, sha,
                                note=_deadline_note(reason, spec.name,
                                                    Path(ticket_path).name),
                            )
                            above = _commits_above(ws)
                    if above:
                        # Stray processes are reaped before the harvest reads the tree (KC-48).
                        _record_reaped(run, spec.name, ws.path)
                        verdict = _harvest(ws, ticket_path, run_tests, config)
                        turn["harvest"] = {
                            "verdict": verdict.verdict,
                            "reasons": [r.code for r in verdict.reasons],
                            "elapsed": round(verdict.elapsed, 1),
                            "waited": round(verdict.waited, 1),
                            "tests_run": _harvest_tests_run(verdict),
                        }
                        # The last words are saved from the transcript, since the session is already closed (KC-74).
                        turn["last_message"] = _last_assistant_text(backend, session)
                        # The harvest names the commit itself, so the verdict's sha is taken as it is (KC-30).
                        run.commit = verdict.commit
                        if verdict.verdict == "READY" and not stalled:
                            # A session that ended on its own with claimed work counts as READY (KC-21).
                            note = f"{(run.commit or '')[:12]} after {error}"
                            state = AgentState.READY
                            error = None
                        elif stalled:
                            # A stall the runner asked for is never promoted, whatever the verdict (KC-29, KC-18).
                            note = f"harvest: {verdict.verdict}"
                if state is AgentState.ERROR and _is_local_store(idle.error):
                    # A store-error run with no commit is restartable in its own tree (KC-62).
                    try:
                        # A deadline commit makes the tree clean, so the flag asks whether there was work (KC-41, KC-62).
                        if run.deadline_commit or _dirty_tree(ws):
                            run.resumable = True
                    except TreeReadError as exc:
                        # An unreadable status is not a clean tree; it is flagged with a warning (FL-2).
                        _log.warning("%s: tree unreadable — %s", spec.name,
                                     _brief(str(exc)))
                run.turns.append(turn)
                _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                return finish(state, error, note=note)

            # ── HARVESTING ─────────────────────────────────────────────────
            transition(AgentState.HARVESTING, note="tests on" if run_tests else "tests off")
            # The harvest is the judge's time, so the hard limit is paused for it (round 83, KC-58).
            paused = pause_time_up()
            try:
                verdict = _harvest(ws, ticket_path, run_tests, config)
            finally:
                if paused:
                    resume_time_up()
            turn["harvest"] = {"verdict": verdict.verdict,
                               "reasons": [r.code for r in verdict.reasons],
                               "elapsed": round(verdict.elapsed, 1),
                               "waited": round(verdict.waited, 1),
                               # The judge's ran tests are kept on the turn for the leg record (KC-74).
                               "tests_run": _harvest_tests_run(verdict)}
            # The last words are saved here, since the session is closed by the time the record is written (KC-74).
            turn["last_message"] = _last_assistant_text(backend, session)
            run.commit = verdict.commit
            label = "tests" if run_tests else "harvest"
            elapsed_str = f"({label} {_age(verdict.elapsed)})"
            run.turns.append(turn)
            _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
            if verdict.verdict == "READY":
                return finish(AgentState.READY, note=f"{(run.commit or '')[:12]} {elapsed_str}")
            if run.attempt >= int(config.max_rework):
                return finish(AgentState.GAVE_UP, "REWORK after the last attempt: "
                              + ", ".join(r.code for r in verdict.reasons if r.blocking))
            run.attempt += 1
            # A rework resets the per-attempt continue counter and the diff signature (KC-22, KC-39, KC-62).
            run.continues = 0
            run.last_diff_signature = ""
            # A new attempt gets its first-touch budget back, as continues do (KC-42).
            run.first_touch_nudges_used = 0
            run.first_touch_resets = 0
            # The attempt has just started, so this session is its first.
            run.sessions_this_attempt = 1
            local_retries = 0
            silent_retries = 0
            transition(AgentState.REWORK, note=f"attempt {run.attempt} {elapsed_str} — "
                       + ", ".join(r.code for r in verdict.reasons))
            rework_text = rework_message(verdict, run.attempt, int(config.max_rework))
    finally:
        # A slot does not outlive its agent; the next waiter is answered at once (KC-58).
        try:
            _SUITE_SLOTS.release(spec.name)
        except Exception:  # noqa: BLE001 — the round goes on without the slot
            pass
        if time_up is not None:
            time_up.cancel()
        if run.terminal:
            # The turn is over, so anything its calls left running is reaped before the harvest (KC-48).
            _record_reaped(run, spec.name, ws.path)
        if session is not None and run.terminal:
            _record_once(session)


def _add_tokens(total: dict | None, part: dict) -> dict:
    """KC-39: two sessions' token dicts, added key by key.

    The dict is the session's own shape — `input`, `output`, `reasoning`,
    `total`, and the nested `cache` — so a value that is itself a dict is added
    recursively, a number is summed, and anything else is replaced: a `None`
    from the newer session wins, which is what "the session reported nothing
    here" means, rather than a sum with it.
    """
    out = dict(total) if isinstance(total, dict) else {}
    for key, value in part.items():
        if isinstance(value, dict):
            out[key] = _add_tokens(out.get(key), value)
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            previous = out.get(key, 0)
            if isinstance(previous, (int, float)) and not isinstance(previous, bool):
                out[key] = previous + value
            else:
                out[key] = value
        else:
            out[key] = value
    return out


def _record_session(run: AgentRun, backend: ContestBackend, session: SessionRef, out_dir: Path) -> None:
    """KC-39: the cost, tokens and transcript of one session of an attempt.

    An attempt may hold more than one session — KC-39 opens a new one when a
    continue repeats the diff, or when a rework would not fit in the window the
    session already fills — so nothing here overwrites what an earlier session
    of the same attempt already put down: `run.cost` and `run.tokens` are sums
    across the attempt's sessions, and this session's messages are appended to
    the transcript the earlier ones wrote, so a replacement never erases the
    session it replaced. One session is today's behaviour: the sum is its own
    number and the file is its own message list.

    Fail-open throughout, the way the call at the end of `run_agent` is: a
    session recorded at the moment it is replaced may already be gone, and a
    round must not die for that.
    """
    try:
        info = backend.session_info(session)
        cost = info.get("cost") if isinstance(info, dict) else None
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            run.cost = round((run.cost or 0) + float(cost), 6)
        tokens = info.get("tokens") if isinstance(info, dict) else None
        if isinstance(tokens, dict):
            run.tokens = _add_tokens(run.tokens, tokens)
    except Exception as exc:  # noqa: BLE001 — the server may be gone
        _log.warning("session_info(%s) failed: %s: %s", session.id, type(exc).__name__, exc)
    try:
        messages = backend.messages(session)
        path = out_dir / f"{run.agent.name}.session.json"
        existing: list = []
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = loaded
        except (OSError, ValueError):
            existing = []   # no transcript yet, or one a reader cannot parse
        if isinstance(messages, list):
            existing.extend(messages)
        _write_json(path, existing)
    except Exception as exc:  # noqa: BLE001
        _log.warning("messages(%s) failed: %s: %s", session.id, type(exc).__name__, exc)


# The round

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
            # Mid-flight at the round's death, or ERROR on Kilo's store with a dirty tree (KC-62).
            run.resumable = False      # spent on this restart, so a later plain ERROR is not
            run.workspace = ws
            verdict = _harvest(ws, ticket_path, run_tests, config)
            if verdict.verdict == "READY":
                run.state, run.commit = AgentState.READY, verdict.commit
            else:
                run.state, run.session_id, run.attempt = AgentState.CREATED, None, 0
                run.continues = 0
                # The summary is asked again in the restarted session; answers append to the file (KC-40).
                run.summary_attempted_at_attempt = -1
                run.summary_session_id = ""
                # A restart opens a new attempt, so its session allowance comes back; the total is kept (KC-39).
                run.sessions_this_attempt = 0
                run.last_diff_signature = ""
                # The first-touch budget is per attempt, so a nudge from the dead round does not count (KC-42).
                run.first_touch_nudges_used = 0
                run.first_touch_resets = 0
                # A resumed worktree with uncommitted work is named in the first prompt; a clean one is not (KC-22).
                if int(config.max_continues_per_attempt) > 0 and _commits_above(ws) == 0:
                    try:
                        dirty = _dirty_tree(ws)
                    except TreeReadError as exc:
                        # An unreadable tree is not clean; the resume names it as unreadable (FL-2).
                        _log.warning("tree of %s unreadable — %s", ws.path,
                                     _brief(str(exc)))
                        dirty = ""
                    if dirty:
                        run.dirty_on_resume = dirty
        runs.append(run)
    return runs


# Legs: a relay of whole turns in one worktree (KC-43)

#: Ends that hand an agent to the next leg: ran out of clock, context or rework; others stop the relay (KC-43).
RELAY_STATES = (AgentState.GAVE_UP, AgentState.STALLED)

#: Touched files a leg record lists before it says "and N more"; records stay short for the next prompt.
_LEG_FILES_SHOWN = 30

#: Paths a leg record never lists: the agent queue and the test-tier links.
_LEG_SCRATCH = ("runs/", ".smoke_tests/")


def _leg_files(ws: Workspace) -> list:
    """`(path, added, deleted, untracked)` of everything the leg left in *ws*.

    Committed and uncommitted alike: `git diff --numstat <base_sha>` is the
    working tree against the base, and `git ls-files --others` adds the files
    `diff` cannot see. Sorted by path. Never raises — a tree that cannot be read is
    `[]`, which the record says as `none`, never as a guess.
    """
    found: dict = {}
    try:
        for raw in git(ws.path, "diff", "--numstat", ws.base_sha).splitlines():
            added, deleted, path = _split_numstat(raw)
            if path and not path.startswith(_LEG_SCRATCH):
                found[path] = (added, deleted, False)
        for path in git(ws.path, "ls-files", "--others", "--exclude-standard").splitlines():
            path = path.strip()
            if path and path not in found and not path.startswith(_LEG_SCRATCH):
                found[path] = (_line_count(Path(ws.path) / path), 0, True)
    except Exception as exc:  # noqa: BLE001 — a record must never raise into a round
        _log.warning("could not read what the leg left in %s: %s", ws.path, _brief(str(exc)))
        return []
    return [(path, *found[path]) for path in sorted(found)]


#: Line cap of a leg record pasted into the next prompt; over it the model fields are cut (KC-74).
LEG_RECORD_MAX_LINES = 80

#: KC-74: the last message's own cap, applied before the record's cap.
_LEG_MESSAGE_LINES = 20

#: KC-74: where a model field was cut to fit the record's cap.
_LEG_CUT = "… cut"

#: pytest's short-summary row for a leg record: the node id, without the group and the message (KC-74).
_LEG_FAIL_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?:@[^\s\[]+)?(?: - .*)?$")

#: Pytest stats line, found by its duration (KC-74).
_LEG_STATS_LINE = re.compile(r"\bin [\d.]+[smh]?\b")

#: KC-74: one `N passed` / `N failed` / `N errors` of that line.
_LEG_COUNT = re.compile(r"\b(\d+) (passed|failed|errors?)\b")

#: KC-74: a shell token, for the roots a `bash` call named.
_LEG_CMD_TOKEN = re.compile(r"[^\s;|&<>]+")

#: Command wrappers and prefixes that a pytest command is wrapped in (timeout 1500 python3 -m pytest).
_LEG_PIECE_SPLIT = re.compile(r"\s*(?:&&|\|\||[;|&])\s*")
_LEG_WRAPPER = frozenset({
    "timeout", "nohup", "env", "nice", "time", "exec", "command", "stdbuf", "ionice",
    "sudo", "bash", "sh", "dash", "/bin/bash", "/bin/sh",
})
_LEG_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_LEG_PYTHON = re.compile(r"^python\d*(?:\.\d+)?$")

#: pytest options that take a value, so the next token is not a test root (KC-74).
_LEG_VALUE_FLAGS = frozenset({
    "-n", "--workers", "--dist", "--dist-load", "--timeout", "--durations",
    "--maxfail", "--max-fail", "--maxprocesses", "-k", "--keywords", "-m",
    "--markers", "--deselect", "--stepwise", "--max-worker-restart", "--new-first",
})

#: pytest options that narrow the selection, so the run names no whole root.
_LEG_SELECT_FLAGS = frozenset({
    "-k", "--keywords", "-m", "--markers", "--deselect", "--co", "--collect-only",
    "--lf", "--last-failed", "--nf", "--new-first", "--ff", "--failed-first",
    "--stepwise",
})


def _harvest_tests_run(verdict) -> str:
    """KC-74: the harvest's `tests_run` fact for the turn record, `""` when there
    is none.

    `facts["tests_run"]` is the judge's one line per root — `tests:3✗` or
    `skipped: no_progress_row` — and it used to die with the verdict. The record
    is written after the session is closed, so it is kept on the turn for the
    record to show. A verdict without facts is `""`, never a KeyError.
    """
    facts = getattr(verdict, "facts", None)
    if isinstance(facts, dict):
        raw = facts.get("tests_run")
        if isinstance(raw, str) and raw.strip():
            return raw
    return ""


def _leg_pytest_roots(command) -> list:
    """KC-74: the pytest roots one shell *command* names.

    `python3 -m pytest tests -n 4` is one, `timeout 1500 python3 -m pytest
    tests tests_bugfix` is two, and a bare `pytest -n 4` is the worktree's own
    `.`. Nothing is named when the piece runs no pytest — `ls pytest tests` says
    the word, it does not run it — when it names a test file or a `::node` or a
    marker, which is a slice of a root rather than the root, and never raises: a
    command the record cannot parse is no root, never an exception into a round.
    """
    if not isinstance(command, str) or not ("pytest" in command or "py.test" in command):
        return []
    try:
        for piece in _LEG_PIECE_SPLIT.split(command):
            tokens = _LEG_CMD_TOKEN.findall(piece)
            start = None
            prev = ""
            for i, token in enumerate(tokens):
                head = token.rsplit("/", 1)[-1]
                if head in ("pytest", "py.test"):
                    start = i
                    break
                if token == "-m" and i + 1 < len(tokens) and tokens[i + 1] == "pytest":
                    start = i + 1                            # `python3 -m pytest`
                    break
                if head.isdigit() and prev in _LEG_WRAPPER:
                    prev = head
                    continue                           # `timeout`'s own duration
                if (head in _LEG_WRAPPER or _LEG_ENV_ASSIGN.match(token)
                        or _LEG_PYTHON.match(head)):
                    prev = head
                    continue
                break                                  # something else: no pytest here
            if start is None:
                continue
            roots = []
            rejected = False
            value = False
            for token in tokens[start + 1:]:
                head = token.rsplit("/", 1)[-1]
                if value:
                    value = False
                    continue
                if head.startswith("-"):
                    value = head in _LEG_VALUE_FLAGS
                    if head in _LEG_SELECT_FLAGS:
                        rejected = True
                    continue
                if "::" in token or head.endswith(".py"):
                    rejected = True
                    continue
                if head.isdigit():                     # an option's value without a flag
                    continue
                root = token[2:] if token.startswith("./") else token
                if root and root not in roots:
                    roots.append(root)
            if rejected:                               # a slice of a root, not a root
                return []
            return roots or ["."]
        return []
    except Exception:  # noqa: BLE001 — a record must not die on a command it cannot read
        return []


def _leg_pytest_result(output) -> tuple:
    """KC-74: `(passed, failed, failing node ids)` off one pytest run's output.

    The counts are pytest's own stats line — the last line that names a number of
    seconds, `1 failed, 41 passed in 2.34s`, its `===` frame or not; errors add to
    the failures, as the judge counts them. Under `-qq` pytest prints no stats line
    and the counts stay `0`, the ids alone. The ids are the short summary's `FAILED`
    and `ERROR` rows, first occurrence first. Nothing pytest did not print is
    invented: no stats line and no summary is `(0, 0, [])`.
    """
    lines = str(output or "").splitlines()
    ids = []
    for line in lines:
        match = _LEG_FAIL_LINE.match(line.strip())
        if match and match.group(1) not in ids:
            ids.append(match.group(1))
    passed = failed = 0
    for line in reversed(lines):
        if not _LEG_STATS_LINE.search(line):
            continue
        for count, kind in _LEG_COUNT.findall(line):
            if kind == "passed":
                passed = int(count)
            else:
                failed += int(count)
        break
    return passed, failed, ids


def _leg_pytest_runs(events_log) -> list:
    """KC-74: `[(root, counts, failing ids)]` of the roots the agent ran itself.

    Read off the leg's event log: every `bash` part that finished, whose command
    names roots, with the counts and the failing node ids of its output. The last
    update of a part id wins — the streaming ones carry no output. `[]` when the
    log is absent, unreadable or holds no pytest run: a broken artifact is
    `tests: none`, never an exception into a round.
    """
    try:
        raw = Path(events_log).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    finals: dict = {}
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        event = entry.get("event") if isinstance(entry, dict) else None
        props = event.get("properties") if isinstance(event, dict) else None
        part = props.get("part") if isinstance(props, dict) else None
        if not isinstance(part, dict) or part.get("type") != "tool" \
                or part.get("tool") != "bash":
            continue
        state = part.get("state")
        if not isinstance(state, dict) or state.get("status") in ("running", "pending"):
            continue
        finals[part.get("id") or len(finals)] = state
    found: list = []
    for state in finals.values():
        input_ = state.get("input")
        command = input_.get("command") if isinstance(input_, dict) else None
        roots = _leg_pytest_roots(command)
        if not roots:
            continue
        passed, failed, failing = _leg_pytest_result(state.get("output"))
        counts = f"{passed}✓ {failed}✗"
        for root in roots:
            if any(item[0] == root for item in found):
                continue
            found.append((root, counts, failing))
    return found


def _leg_declared(ticket_path) -> list:
    """KC-74: the ticket's declared files, `[]` when the path is not readable."""
    if not ticket_path:
        return []
    try:
        return list(declared_files(ticket_path))
    except OSError:
        return []


def _leg_tests(run: AgentRun, events_log) -> list:
    """KC-74: `[(root, counts, failing ids)]` of the roots the leg ran.

    The agent's own runs from the event log first — they carry the counts and the
    failing node ids — and the judge's roots off the turns' `tests_run` for every
    root the agent did not name itself: the judge runs all of them, the agent
    usually only a few. Newest turn first, so a rework's run is what it reports,
    and first name wins: a root is reported once. `skipped: …`, the tier check and
    the checkout failure are not roots and are dropped.
    """
    runs: dict = {}
    for root, counts, failing in _leg_pytest_runs(events_log):
        runs.setdefault(root, (root, counts, failing))
    turns = getattr(run, "turns", None) or []
    for turn in reversed(turns):
        harvest = turn.get("harvest") if isinstance(turn, dict) else None
        raw = harvest.get("tests_run") if isinstance(harvest, dict) else None
        if not isinstance(raw, str):
            continue
        for token in raw.split():
            if ":" not in token:
                continue
            root, detail = token.split(":", 1)
            if not root or not detail or root in ("skipped", "tiers") or root in runs:
                continue
            runs[root] = (root, detail, [])
    return list(runs.values())


def _leg_last_message(run: AgentRun) -> str:
    """KC-74: the newest turn's saved last assistant text, `""` when none was saved."""
    for turn in reversed(getattr(run, "turns", None) or []):
        text = turn.get("last_message") if isinstance(turn, dict) else None
        if isinstance(text, str) and text.strip():
            return text
    return ""


def _leg_lines(text) -> list:
    """The non-blank lines of *text*, trimmed — the shape both model fields are cut to."""
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _leg_block(name: str, lines: list, cap: int) -> list:
    """KC-74: the record's field for *name*.

    `name: none` when there is no text; otherwise the header, the first *cap*
    lines quoted, and the `… cut` marker when the cap dropped any — including a
    cap of zero, where the marker is all that is left of the field.
    """
    if not lines:
        return [f"{name}: none"]
    block = [f"{name}:"]
    block.extend("  > " + line for line in lines[:max(0, cap)])
    if cap < len(lines):
        block.append("  " + _LEG_CUT)
    return block


def _leg_cap(count: int, budget: int, ceiling: int) -> int:
    """KC-74: how many of *count* lines fit in *budget* record lines at most *ceiling*.

    A field is its header, its kept lines and — only when something was dropped —
    the `… cut` marker, so *budget* must hold `1 + kept`, or `2 + kept` when the
    marker goes in. `0` when the budget cannot hold a single line.
    """
    if count <= 0 or budget <= 0:
        return 0
    if count <= ceiling:
        return count if 1 + count <= budget else max(0, budget - 1)
    return ceiling if 2 + ceiling <= budget else max(0, budget - 2)


def _leg_model_fields(message: str, summary: str, budget: int) -> list:
    """KC-74: the two model fields, cut to fit *budget* record lines.

    The last message keeps its own 20 lines first and the summary takes whatever
    is left: the summary has no cap of its own, so it is the one that gives ground
    when the record runs out, and the mechanical fields never yield a line. Both
    empty is the two `none` lines, so the field is never blank.
    """
    message_lines = _leg_lines(message)
    summary_lines = _leg_lines(summary)
    if not message_lines and not summary_lines:
        return ["last message: none", "summary: none"]
    message_block = _leg_block("last message", message_lines,
                               _leg_cap(len(message_lines), max(0, budget), _LEG_MESSAGE_LINES))
    summary_block = _leg_block("summary", summary_lines,
                               _leg_cap(len(summary_lines),
                                        max(0, budget) - len(message_block),
                                        LEG_RECORD_MAX_LINES))
    return message_block + summary_block


def leg_record(run: AgentRun, ws: Workspace, out_dir, ticket_path=None) -> Path:
    """KC-43 + KC-74: write `<out_dir>/<agent>.leg.md` — what the leg left.

    The mechanical fields are KC-43's: the files touched with a diffstat (committed
    and not), the commit the leg ended on — KC-41's deadline commit or the model's —
    and the newest harvest verdict with its reason codes. KC-74 adds, after them:
    the pytest roots the leg ran with their counts, the agent's last message quoted
    to 20 lines, KC-40's summary when the leg has one, and what is left — the
    ticket's declared files not touched yet plus the failing test names.

    Only the last message and the summary come from a model; every other field comes
    from git and the logs, and an empty mechanical field says `none`, never prose.
    The whole record stays inside `LEG_RECORD_MAX_LINES`: over it the two model
    fields are cut, in order, and the mechanical fields are never cut.
    *ticket_path* is the ticket the leg worked; `None` leaves "what is left" to the
    failing tests. Returns the path.
    """
    out_dir = Path(out_dir)
    files = _leg_files(ws)
    lines: list = []
    if files:
        added = sum(f[1] for f in files)
        deleted = sum(f[2] for f in files)
        lines.append(f"files: {len(files)} (+{added} -{deleted})")
        for path, plus, minus, untracked in files[:_LEG_FILES_SHOWN]:
            lines.append(f"  {path}  +{plus} -{minus}" + (" (new)" if untracked else ""))
        if len(files) > _LEG_FILES_SHOWN:
            lines.append(f"  ... and {len(files) - _LEG_FILES_SHOWN} more")
    else:
        lines.append("files: none")
    # The branch end is recorded for the record only; no verdict is built from this sha (KC-30).
    commit = git(ws.path, "log", "-1", "--format=%H") if _commits_above(ws) > 0 else ""
    verdict = ""
    for turn in reversed(getattr(run, "turns", None) or []):
        h = turn.get("harvest") if isinstance(turn, dict) else None
        if h:
            verdict = " ".join([str(h.get("verdict", ""))]
                               + [str(c) for c in h.get("reasons", [])]).strip()
            break
    lines += [f"commit: {commit or 'none'}", f"harvest: {verdict or 'none'}"]
    tests = _leg_tests(run, out_dir / run.agent.name / "events.jsonl")
    if tests:
        lines.append("tests:")
        lines += [f"  {root}: {counts}" for root, counts, _ in tests]
    else:
        lines.append("tests: none")
    # The mechanical fields set the cap for the model fields, and they are never cut (KC-74).
    failing = [node for _, _, nodes in tests for node in nodes]
    touched = {path for path, *_ in files}
    left = [path for path in _leg_declared(ticket_path) if path not in touched] + failing
    # minus the title line and the "what is left" line written below it
    lines += _leg_model_fields(_leg_last_message(run), getattr(run, "summary", "") or "",
                               LEG_RECORD_MAX_LINES - len(lines) - 2)
    lines.append("what is left: " + (", ".join(left) if left else "none"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run.agent.name}.leg.md"
    path.write_text(f"leg {out_dir.name} — {run.agent.name}\n" + "\n".join(lines) + "\n",
                    encoding="utf-8")
    return path


def leg_message(leg: int, ticket_path, records=()) -> str:
    """KC-43: the paragraph the first prompt of leg *leg* carries.

    What a fresh session needs to pick the work up: which leg this is, the
    instruction to *continue* from the files the earlier legs changed rather than
    start over — a relay is not a rework, so no critique — where the ticket is even
    when `next_task.py` has nothing left to hand out, the files the ticket declares
    (so a leg that produced nothing still says where the work belongs), and the
    leg records so far, *records* being newest first.
    """
    ticket = Path(ticket_path).name
    try:
        declared = ", ".join(declared_files(ticket_path)) or "none"
    except OSError:
        declared = "none"
    body = "\n\n".join(str(r).strip() for r in records if str(r).strip()) or "nothing recorded"
    return (
        f"This is leg {leg} of a relay on ticket {ticket}. The legs before yours worked "
        f"in this same worktree: their session is gone, their work is not. Your job is "
        f"to continue from the files they changed — the worktree already holds that "
        f"work, it is yours; do not start over and do not discard it.\n\n"
        f"The ticket is epic-tasks/{ticket}; if `next_task.py` hands you nothing because "
        f"an earlier leg already recorded it, read it there. The files it declares: "
        f"{declared}.\n\n"
        f"What the earlier legs left, newest first:\n\n{body}"
    )


def _plan_leg(config: ContestConfig, workspaces: list, ticket_path: Path,
              prior: RoundState, records: dict, leg: int) -> list:
    """The `AgentRun` per workspace for leg *leg*, off the leg that just ended.

    An agent whose *prior* run ended outside `RELAY_STATES` keeps that run as it
    ended, terminal, so the runner skips it and the leg's `state.json` is still the
    whole round — the last leg's is what gets exported. Every other agent gets a
    fresh run on the same workspace: state `CREATED`, `attempt` 0, no session — the
    new session is the point — and its first prompt carries `leg_message` with the
    agent's *records*, newest first.
    """
    specs = {spec.name: spec for spec in config.agents}
    ended = {run.agent.name: run for run in prior.agents}
    runs = []
    for ws in workspaces:
        before = ended.get(ws.agent)
        if before is not None and before.state not in RELAY_STATES:
            runs.append(before)
            continue
        spec = before.agent if before is not None else specs.get(ws.agent)
        run = AgentRun(agent=spec or AgentSpec(ws.agent, "", ""), workspace=ws)
        run.leg_note = leg_message(leg, ticket_path, records.get(ws.agent, ()))
        runs.append(run)
    return runs


def run_leg(config: ContestConfig, round_no: int, ticket_path: Path, workspaces: list, *,
              make_backend: Callable[[Workspace], ContestBackend], out_dir: Path,
              leg: int | None = None, carry: RoundState | None = None,
              records: dict | None = None,
              resume: RoundState | None = None,
              run_tests: bool = False,
              server_pid: int | None = None,
              legs: int | None = None,
              legs_from: str | None = None,
              log_path: str | None = None) -> RoundState:
    """One round — or, with *leg*, one leg of it: a `run_agent` per workspace in a
    pool of `config.max_parallel`.

    KC-43: a leg is today's round body, unmodified. *leg* numbers it (`65.2` in the
    state, the folder and the log lines); `None` is a round that is not a relay,
    and every earlier caller. *carry* is the state of the leg just before this one,
    *records* its per-agent leg records (newest first): together they plan the leg
    with `_plan_leg` — every agent that ran out gets a fresh session in its own
    worktree, every agent that finished stays as it ended. *carry* and *resume*
    are two different ways of starting from an earlier state, and not both.

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

    *legs* and *legs_from* (KC-44) are the round's chosen leg count and where it
    came from — `flag`, `size` or `config` — which a relay's `state.json`
    records, alongside the leg number every leg's `state.json` already carries.
    `None` both is `run_round`, the tree KC-43 kept byte for byte.

    *log_path* (KC-81) is the `kilo-serve.log` the round started its server
    with, so the heartbeat can name a hung server. `None` — a server the round
    did not start, or every earlier caller — is "no log": the silent-server
    check uses the events side alone and the line says `log ?`.
    """
    out_dir, ticket_path = Path(out_dir), Path(ticket_path)
    workspaces = list(workspaces)
    if carry is not None and resume is not None:
        raise ValueError("a leg starts from the leg before it or from a resumed state, not both")
    if carry is not None:
        runs = _plan_leg(config, workspaces, ticket_path, carry, records or {}, int(leg or 2))
    else:
        runs = _plan(config, workspaces, ticket_path, resume, run_tests=run_tests)
    state = RoundState(round_no=round_no, ticket=ticket_path.name,
                       base_sha=workspaces[0].base_sha if workspaces else "",
                       started_at=resume.started_at if resume is not None else time.time(),
                       agents=runs, leg=leg, legs=legs, legs_from=legs_from)
    lock = threading.Lock()
    stop = threading.Event()
    since: dict = {run.agent.name: time.monotonic() for run in runs}   # state entered at

    # The last worker count written is remembered, so an unchanged save does no I/O (KC-65).
    _workers_memo: dict = {}
    # The round's share of agent_tmpdir is scratch the policy lets through (KC-65).
    agent_tmp = agent_tmp_path(config, round_no)

    def save() -> None:
        with lock:
            _write_json(out_dir / "state.json", state.to_dict())
            # The worker file follows the round too, since a freed slot is a worker the next pytest can have (KC-65).
            refresh_pytest_workers(out_dir, config, state, _workers_memo)

    def on_transition(run: AgentRun) -> None:
        if stop.is_set():
            raise _Stopped()
        since[run.agent.name] = time.monotonic()
        save()

    heartbeat = _Heartbeat(state, since, float(config.progress_every_sec or 0),
                           server_pid=server_pid,
                           neighbour_warn=int(getattr(config, "neighbour_kilo_warn", 4) or 0),
                           # Health checks read their config with getattr, so an old config means "off" (KC-81).
                           kilo_silent_sec=(float(getattr(config, "kilo_silent_sec", 0) or 0)
                                            if getattr(config, "backend", "kilo") == "kilo"
                                            else 0.0),
                           log_path=log_path,
                           events_root=str(out_dir),
                           save=save)

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
        # The round sweeps every worktree before the interrupt, so the save records what was reaped (KC-48).
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
        # The sweep also runs on a normal return; a second save on Ctrl-C would overwrite the agents' states (KC-48).
        if not interrupted:
            _reap_round_worktrees(state.agents)
            save()
    return state


def run_round(config: ContestConfig, round_no: int, ticket_path: Path, workspaces: list, *,
              make_backend: Callable[[Workspace], ContestBackend], out_dir: Path,
              resume: RoundState | None = None,
              run_tests: bool = False,
              server_pid: int | None = None,
              log_path: str | None = None) -> RoundState:
    """One round of one leg — `run_leg` with no leg number, as before KC-43.

    Nothing about the state, the folder or the log lines says "leg": `legs = 1`
    is this function, byte for byte, and KC-44's `legs` / `legs_from` ride on
    a relay's `state.json` through `run_leg`'s own keywords, not on this one.
    See `run_leg` for the pool, `state.json`, *resume*, *run_tests*,
    *server_pid* and *log_path*.
    """
    return run_leg(config, round_no, ticket_path, workspaces, make_backend=make_backend,
                   out_dir=out_dir, resume=resume, run_tests=run_tests,
                   server_pid=server_pid, log_path=log_path)


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


#: Server CPU percent above which a silent spell also advises deleting Kilo's store (round 153).
HUNG_STORE_CPU_PERCENT = 50.0


def _proc_cpu_ticks(pid: int, proc_root: str = "/proc") -> int | None:
    """KC-81: `utime + stime` of *pid* from ``/proc/<pid>/stat``, or None.

    Fail-open like every other `/proc` read here: no pid, not Linux, a
    vanished process or a field that is not a number all answer None, which
    the caller prints as `cpu ?` rather than raising into a round.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        with open(f"{proc_root}/{pid}/stat", "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        # The command name may hold spaces, so the stable fields are read after its last ")".
        fields = text.rsplit(")", 1)[-1].split()
        return int(fields[11]) + int(fields[12])
    except (OSError, ValueError, IndexError):
        return None


#: Agent states that wait on kilo serve for an event; HARVESTING is left out (round 128).
_EVENT_WANTED = (AgentState.PROMPTED, AgentState.WAITING, AgentState.REWORK)


class _Heartbeat:
    """The once-a-minute line: the round's age and every agent's state, its
    files-based progress bar, and how long its last tests took —
    `round 66 14m: mimo WAITING 14m [######....] 60% 5f · ... — 3 live`.

    KC-81 adds one more check on the same tick: a `kilo serve` that has gone
    silent — no live agent event and a `kilo-serve.log` that has not grown,
    both for `kilo_silent_sec` — gets one WARNING line naming the pid, the CPU
    and the log's last-change time, and `server_silent` in `state.json`. It
    never kills and never aborts: the decision is the operator's.

    A daemon thread waiting on an `Event`, so `stop()` returns at once and the
    thread never outlives `run_round`. `every <= 0` starts nothing.
    """

    def __init__(self, state: RoundState, since: dict, every: float,
                 server_pid: int | None = None, neighbour_warn: int | None = None,
                 kilo_silent_sec: float = 0, log_path: str | None = None,
                 events_root: str | None = None, save: Callable[[], None] | None = None):
        self.state, self.since, self.every = state, since, every
        self.server_pid, self.neighbour_warn = server_pid, neighbour_warn
        # KC-81: 0 (the default, and every earlier caller) is the check off.
        self.kilo_silent_sec = kilo_silent_sec
        self.log_path = log_path
        self.events_root = events_root
        self.save = save
        self._silent_warned = False
        self._log_size: int | None = None
        self._cpu_prev: tuple[float, float] | None = None
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

    def _suite_note(self, run: AgentRun) -> str | None:
        """What the line says about this run's own suite, or None for no suffix.

        KC-58: a `WAITING` agent is read live off the round's suite slots —
        `queued` with its wait and how many are ahead of it, `suite` while it
        holds one, and `over the ceiling` once it has passed
        `agent_suite_max_sec` and stopped blocking the next waiter. `HARVESTING`
        reads the same queue, but `_harvest_note` already names the roots there,
        so this one only speaks for a run the harvest note cannot.
        """
        if run.state not in (AgentState.WAITING, AgentState.HARVESTING):
            return None
        try:
            status = _suite_status(run.agent.name)
        except (AttributeError, TypeError, ValueError):
            return None
        if not status:
            return None
        state = status[0]
        if state == "queued":
            ahead = status[2] if len(status) > 2 else 0
            tail = f", {ahead} ahead" if ahead else ""
            return f"(suite queued {_age(status[1])}{tail})"
        if state == "over":
            return f"(suite {_age(status[1])}, over the ceiling)"
        if state != "running":
            return None                 # `done`: the suite is over, nothing to say
        return f"(suite {_age(status[1])})"

    def line(self) -> str:
        now = time.monotonic()
        parts = []

        # The median is taken over the agents currently working (WAITING or REWORK), with a floor of 1.
        files_by_name = {}
        working_files = []
        for run in self.state.agents:
            if not run.terminal:
                f = _worktree_files(run.workspace)
                files_by_name[run.agent.name] = f
                if run.state in (AgentState.WAITING, AgentState.REWORK):
                    working_files.append(f)
        median = max(1.0, statistics.median(working_files)) if working_files else 1.0

        # One agent per line, padded to the widest name and state so the ages line up.
        name_w = max((len(run.agent.name) for run in self.state.agents), default=0)
        state_w = max((len(run.state.value) for run in self.state.agents), default=0)
        for run in self.state.agents:
            part = f"{run.agent.name:<{name_w}} {run.state.value:<{state_w}}"
            if not run.terminal:
                part += f" {_age(now - self.since.get(run.agent.name, now)):>4}"
                files = files_by_name.get(run.agent.name, 0)
                if run.state in (AgentState.WAITING, AgentState.REWORK):
                    committed = _commits_above(run.workspace) > 0 and run.attempt == 0
                else:
                    committed = False
                pct = _progress(run.state, files, median, committed)
                if pct is not None:
                    part += f" {_bar(pct)} {pct:>3}% {files}f"
                # A turn past its nominal clock is shown as extended, not as a hung round (KC-36).
                live_clock = getattr(run, "_turn_clock", None)
                granted = float(live_clock.granted) if live_clock is not None else 0.0
                if run.attempt:
                    part += f" ↺{run.attempt}"
                if granted > 0:
                    part += f"+{_age(granted)}" if run.attempt else f" +{_age(granted)}"
            note = self._harvest_note(run) or self._suite_note(run)
            if note:
                part += f" {note}"
            parts.append(part.rstrip())

        live = sum(1 for run in self.state.agents if not run.terminal)
        age = _age(time.time() - self.state.started_at)
        text = f"round {self.state.label} {age}, {live} live:" + "".join(f"\n  {p}" for p in parts)
        count = self._kilo_neighbours()
        if count is not None:
            # A busy store is what ends agents on "Failed to execute statement", so the crowd is shown (KC-62).
            text += f"\n  kilo neighbours {count}"
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

    def _sample_cpu(self) -> str:
        """KC-81: `"100%"` from `/proc/<pid>/stat` across the last tick, else `"?"`.

        Sampled once per heartbeat interval, so the number the WARNING prints is
        the server's CPU over roughly that window — what `top` showed in round
        127. Unreadable `/proc`, no pid, or the first sample of the thread all
        answer `?` and never raise.
        """
        pid = self.server_pid
        ticks = _proc_cpu_ticks(pid) if pid else None
        if ticks is None:
            self._cpu_prev = None
            return "?"
        now = time.monotonic()
        if self._cpu_prev is None:
            self._cpu_prev = (ticks, now)
            return "?"
        prev_ticks, prev_at = self._cpu_prev
        self._cpu_prev = (ticks, now)
        elapsed = now - prev_at
        if elapsed <= 0:
            return "?"
        try:
            hz = float(os.sysconf("SC_CLK_TCK"))
        except (ValueError, OSError, AttributeError):
            hz = 100.0
        if hz <= 0:
            return "?"
        pct = max(0.0, (ticks - prev_ticks) / hz / elapsed * 100.0)
        return f"{round(pct)}%"

    def _event_idle(self, run: AgentRun, wall: float) -> float | None:
        """KC-81: seconds since *run* last received an event, or None when the
        side cannot be read (no `events_root`) — None means "do not warn".

        An absent `events.jsonl` counts as idle since the round started: a
        session that was prompted and never spoke has been quiet the whole
        time, which is exactly round 127's shape.
        """
        if not self.events_root:
            return None
        path = Path(self.events_root) / run.agent.name / "events.jsonl"
        try:
            return wall - path.stat().st_mtime
        except OSError:
            return wall - float(self.state.started_at)

    def _server_silent_note(self, cpu: str) -> str | None:
        """KC-81: the one WARNING line of a silent spell, or None.

        Silent means *both* hold for `kilo_silent_sec`: no agent waiting on the
        server has had an event, and `kilo-serve.log` (when the round started a
        server) has not grown. Either side moving ends the spell and re-arms the
        warning; the next spell warns again and replaces `server_silent`. `kilo_silent_sec <= 0` or no event data is the check off — never
        a raise into a round, never a kill, never an abort of a session: the
        agents' own stall edge is untouched.

        Only an agent with a turn in flight is waiting on the server
        (`_EVENT_WANTED`). A round whose live agents are all HARVESTING runs
        pytest in the worktrees with no session speaking, so a quiet server
        there is idle by design, not hung, and the check says nothing.

        The line and `server_silent` carry how long the server has actually
        been silent — the newer of the last event and the log's last change —
        not the window, so a spell that has run for an hour reads `1h`-ish,
        not `10m` forever.
        """
        try:
            window = float(self.kilo_silent_sec or 0)
        except (TypeError, ValueError):
            return None
        if window <= 0 or not self.events_root:
            return None
        waiting = [run for run in self.state.agents
                   if not run.terminal and run.state in _EVENT_WANTED]
        if not waiting:
            return self._end_spell()
        wall = time.time()
        # The log is checked only once events have gone quiet; one quiet agent is the stall logic's job.
        silent_for = None
        for run in waiting:
            idle = self._event_idle(run, wall)
            if idle is None or idle < window:
                return self._end_spell()
            silent_for = idle if silent_for is None else min(silent_for, idle)
        log_note = "log ?"
        if self.log_path:
            try:
                st = os.stat(self.log_path)
            except OSError:
                st = None
            if st is not None:
                if self._log_size is None:
                    self._log_size = st.st_size
                elif st.st_size != self._log_size:
                    self._log_size = st.st_size
                    return self._end_spell()
                when = time.strftime("%H:%M:%S", time.localtime(st.st_mtime))
                log_note = f"log idle since {when}"
                if wall - st.st_mtime < window:
                    return self._end_spell()
                silent_for = min(silent_for, wall - st.st_mtime)
        if self._silent_warned:
            return None
        self._silent_warned = True
        pid = self.server_pid
        pid_note = f"pid {pid}" if pid else "pid ?"
        kill_pid = str(pid) if pid else "<pid>"
        count = len(waiting)
        plural = "agent" if count == 1 else "agents"
        # A silent, CPU-pinned server is usually Kilo's store grown huge; the advice names the path only (round 153).
        advice = ""
        try:
            busy = float(str(cpu).rstrip("%")) >= HUNG_STORE_CPU_PERCENT
        except ValueError:  # "?" — no sample, no advice
            busy = False
        if busy:
            store = ""
            if pid:
                try:
                    _count, store = kilo_neighbours(pid)
                except Exception:  # noqa: BLE001 — the hint names the store, never raises
                    store = ""
            db_path = f"{store}/kilo.db" if store else "kilo's data dir (kilo.db)"
            advice = (f". With the cpu this high the store is the usual cause: just delete "
                      f"{db_path} (and the snapshot/ dir next to it) and restart")
        note = (
            f"kilo serve silent {_age(silent_for)}: {pid_note}, cpu {cpu}, {log_note}, "
            f"{count} live {plural} without an event — the server looks hung; "
            f"stop it (kill {kill_pid}, kill -9 if it stays) and restart the round "
            f"with --fresh{advice}"
        )
        self.state.server_silent = {"seconds": int(silent_for), "pid": pid}
        self._save_state()
        return note

    def _end_spell(self) -> None:
        """KC-81: something moved (or nobody is waiting): re-arm the warning.

        `server_silent` stays on the state as the record of the last spell, so
        `contest status` still shows it after the round; the next spell's
        warning replaces it."""
        self._silent_warned = False
        return None

    def _save_state(self) -> None:
        if self.save is not None:
            try:
                self.save()
            except Exception:  # noqa: BLE001 — a failed save is not a round failure
                _log.debug("server_silent: state save failed", exc_info=True)

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            cpu = self._sample_cpu()
            note = self._server_silent_note(cpu)
            if note:
                _log.warning("%s", note)
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
