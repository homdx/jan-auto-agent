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

After every transition `on_transition(run)` fires — `run_round` writes
`state.json` there — and one line per turn goes to `out_dir/<agent>/turns.jsonl`.
Every artifact write is fail-open: a log line or a state file that cannot be
written is a warning, never an exception into a round.

Stall detection: KC-12 (round 51) gives `wait_idle` an `idle_event_timeout=`,
so this module only passes the round's `idle_event_timeout_sec` (KC-2's
`[contest]` key) and reads the result. The primitive owns the clock — the last
event *of this session* on its stream, in `time.monotonic()` — and sends the
`abort` itself. A silence stall and the overall `turn_timeout_sec` both come
back as `IdleResult.status == "timeout"`; `elapsed` tells them apart. The
third-question edge is the runner's own, still: `stall()` aborts and interrupts
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
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from tools.backoff import save_state
from tools.contest.backend import ContestBackend, ContestBackendError
from tools.contest.gates import declared_files, git
from tools.contest.harvest import harvest, rework_message
from tools.contest.kilo_client import SessionRef
from tools.contest.policy import HARD_DENYLIST, Policy, PolicyContext
from tools.contest.roster import AgentSpec, ContestConfig
from tools.contest.workspace import Workspace
from tools.git_run import run_git

__all__ = ["AgentRun", "AgentState", "RoundState", "round_prompt", "run_agent", "run_round"]

_log = logging.getLogger(__name__)

#: How many of the session's latest tool parts the gate sees.
RECENT_TOOLS = 8

#: The four pytest roots run one worktree at a time. The judge machine takes
#: about 20 minutes for eight parallel suites and about 105 s for one, so two
#: agents harvesting at once must not fan the roots out; only the test run is
#: slow, so the lock is held for the whole `harvest` call — its mechanical part
#: is a handful of `git` calls and costs nothing next to the roots.
_TEST_RUNS_LOCK = threading.Lock()


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
    r"429|502|503|504|overloaded|rate limit|timeout", re.IGNORECASE
)


def _retryable(error) -> bool:
    """True when *error* is a retryable provider error (KC-19).

    Retryable when ``data.isRetryable`` is truthy, or ``data.metadata.code``
    is one of the known transient socket codes, or the message matches a
    status-code or overload pattern.  A payload that is not a dict, or has
    no recognizable retryable signal, is not retryable.
    """
    if not isinstance(error, dict):
        return False
    data = error.get("data") or {}
    if isinstance(data, dict) and data.get("isRetryable"):
        return True
    metadata = data.get("metadata") if isinstance(data, dict) else None
    if isinstance(metadata, dict) and metadata.get("code") in _RETRYABLE_CODES:
        return True
    msg = ""
    if isinstance(data, dict) and isinstance(data.get("message"), str):
        msg = data["message"]
    elif isinstance(error.get("message"), str):
        msg = error["message"]
    if _RETRYABLE_MSG_RE.search(msg):
        return True
    return False


def _retry_reason(error) -> str:
    """The brief reason string for ``RETRY_PROMPT`` from a retryable payload."""
    if isinstance(error, dict):
        data = error.get("data") or {}
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            return _brief(data["message"])
    return _brief(error)


def _harvest(ws, ticket_path, run_tests):
    """`harvest` for one worktree, with the pytest roots serialized round-wide."""
    if not run_tests:
        return harvest(ws, ticket_path)
    with _TEST_RUNS_LOCK:
        return harvest(ws, ticket_path, run_tests=True)


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
    `sent_at`, `idle_at`, `idle_status`, and `harvest` = `{"verdict", "reasons": [codes]}`
    once the turn was scored. `permissions` counts what the policy was asked
    and how it answered; `questions` counts the questions over the whole run.
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
    commit: str | None = None
    cost: float | None = None
    tokens: dict | None = None

    @property
    def terminal(self) -> bool:
        return self.state.terminal

    def to_dict(self) -> dict:
        data = asdict(self)
        data["workspace"]["path"] = str(self.workspace.path)
        data["state"] = self.state.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AgentRun":
        ws = dict(data["workspace"])
        ws["path"] = Path(ws["path"])
        run = cls(agent=AgentSpec(**data["agent"]), workspace=Workspace(**ws))
        for name in ("session_id", "attempt", "turns", "permissions", "questions",
                     "last_error", "commit", "cost", "tokens"):
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
        } for run in self.agents]


# ─────────────────────────────────────────────────────────────────────────────
# the prompt
# ─────────────────────────────────────────────────────────────────────────────

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

When you are done, report: the commit sha, each Acceptance checkbox and
whether you met it, and anything in the ticket you found to be wrong about the
live code — each ticket names the commit it was written against in its
`**Status:**` line (the original 24 used `68b78a0`); the code is the
authority, not the ticket.

Your starting tree is commit {base_sha}; your one commit goes on top of it.
Any command that reaches outside your worktree is decided by a reviewer, and a
rejection is final for that command — do not retry it.
"""


def round_prompt(agent_name: str, ticket_path: Path, base_sha: str, *, dirty: str = "") -> str:
    """The runbook's prompt for *agent_name*, plus the base sha and the permission rule.

    The ticket is not repeated: the session reads it from its own worktree via
    `next_task.py`, so *ticket_path* is accepted for the caller's clarity only.
    When *dirty* is non-empty (a `--resume` into a worktree that still holds
    uncommitted work, KC-22), the `continue_message` paragraph is appended so the
    fresh session learns of the work on its first prompt — every existing caller
    passes no *dirty* and gets the unchanged text.
    """
    del ticket_path
    text = _PROMPT.format(name=agent_name, base_sha=base_sha)
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
        r = run_git(["git", "status", "--porcelain", "--untracked-files=all"],
                    cwd=ws.path)
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

def _wait_turn(backend: ContestBackend, session: SessionRef, config: ContestConfig,
               *, on_permission, on_question):
    """`backend.wait_idle` for one turn, with the round's stall edge wired in.

    Returns the `IdleResult`. `idle_event_timeout` is the round's
    `idle_event_timeout_sec`: a session silent for that long is aborted by the
    backend and comes back as `status="timeout"`, at an `elapsed` well under
    `turn_timeout_sec` — which is how the runner names it a silence stall
    rather than a turn timeout. Zero or unset disables the clock, which is
    `wait_idle`'s behaviour without the argument.
    """
    silence = float(config.idle_event_timeout_sec or 0)
    return backend.wait_idle(session, float(config.turn_timeout_sec),
                             idle_event_timeout=silence or None,
                             on_permission=on_permission, on_question=on_question)


# ─────────────────────────────────────────────────────────────────────────────
# one agent
# ─────────────────────────────────────────────────────────────────────────────

def run_agent(run: AgentRun, *, backend: ContestBackend, policy: Policy,
              config: ContestConfig, ticket_path: Path, out_dir: Path,
              on_transition: Callable[[AgentRun], None],
              run_tests: bool = False) -> AgentRun:
    """Drive *run* to a terminal state — single-threaded, one session for every turn.

    *backend* is a `ContestBackend` (KC-34): everything this function needs of the
    session — create, prompt, wait, abort, tool history, close — goes through
    it, so a Kilo server and an OpenRouter subprocess are the same round. This
    function never touches a client or a tap.

    `on_transition(run)` is called after every state change. In `finally`, on a
    terminal state, the session's `cost` and `tokens` are read and its messages
    are written to `out_dir/<agent>.session.json`.

    *run_tests* passes the harvest's `run_tests` through to it after every turn:
    the four pytest roots are then the round's judge (KC-16) and run one
    worktree at a time, instead of the ticket's own self-check being the only
    evidence. Off by default, so every earlier call of this function is unchanged.
    """
    out_dir = Path(out_dir)
    ws, spec = run.workspace, run.agent
    agent_dir = out_dir / spec.name
    session: SessionRef | None = None
    stalled: list = []          # the reason, once the runner's stall edge fired
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
            tmp_roots=tuple(config.tmp_roots),
            # the other agents' worktrees are this one's siblings: never theirs to read
            forbidden=tuple(HARD_DENYLIST) + (ws.path.parent,),
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
        return decision.reply, decision.reason

    def on_question(event: dict) -> None:
        del event  # rejected by wait_idle regardless; only the count matters here
        run.questions += 1
        questions_this_turn[0] += 1
        if questions_this_turn[0] >= int(config.max_questions_per_turn):
            stall(f"{questions_this_turn[0]} questions in one turn")

    def finish(state: AgentState, error: str | None = None, *, note: str | None = None) -> AgentRun:
        transition(state, error, note=note)
        return run

    try:
        # ── CREATED: one session, kept for every turn ─────────────────────
        try:
            session = backend.create_session(
                spec.provider_id, spec.model_id, rules=config.session_rules(),
                title=ws.branch, agent=spec.kilo_agent)
        except (ContestBackendError, ValueError) as exc:
            return finish(AgentState.ERROR, f"POST /session failed: {_brief(str(exc))}")
        run.session_id = session.id

        rework_text = None
        retry_text = None
        continue_text = None
        continue_used = 0
        retries_used = 0
        while True:
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
                    text = round_prompt(spec.name, ticket_path, ws.base_sha, dirty=dirty)
                    run.dirty_on_resume = ""  # only the first prompt carries it
                else:
                    text = round_prompt(spec.name, ticket_path, ws.base_sha)
            turn = {"kind": kind, "attempt": run.attempt, "sent_at": time.time()}
            if kind == "continue":
                note = (f"attempt {run.attempt} (continue {continue_used} of "
                        f"{int(config.max_continues_per_attempt)})")
            else:
                note = f"attempt {run.attempt} ({kind})"
            transition(AgentState.PROMPTED, note=note)
            try:
                backend.prompt(session, text)
            except ContestBackendError as exc:
                return finish(AgentState.ERROR, f"prompt failed: {_brief(str(exc))}")

            # ── WAITING ────────────────────────────────────────────────────
            transition(AgentState.WAITING)
            questions_this_turn[0] = 0
            idle = _wait_turn(backend, session, config,
                              on_permission=on_permission, on_question=on_question)
            turn["idle_at"] = time.time()
            turn["idle_status"] = idle.status
            if stalled:
                turn["idle_status"] = "stalled"
                error, state = stalled[0], AgentState.STALLED
            elif idle.status == "timeout":
                # KC-12 sends the silence stall back as "timeout" too, so the
                # label comes from elapsed: under the overall deadline means
                # the silence window fired, at it means the turn never idled.
                silence = float(config.idle_event_timeout_sec or 0)
                quiet = 0 < silence and idle.elapsed < float(config.turn_timeout_sec)
                if quiet:
                    turn["idle_status"] = "stalled"
                    error = f"no event for {silence:g}s"
                else:
                    error = f"no idle after {config.turn_timeout_sec}s"
                state = AgentState.STALLED
            elif idle.status == "error":
                if _retryable(idle.error) and retries_used < int(config.max_error_retries):
                    run.turns.append(turn)
                    _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                    retries_used += 1
                    backoff = int(config.error_retry_backoff_sec) * (2 ** (retries_used - 1))
                    _log.info("%s: retry %d/%d in %ds — %s", spec.name,
                              retries_used, int(config.max_error_retries),
                              backoff, _retry_reason(idle.error))
                    if backoff > 0:
                        _backoff_event = threading.Event()
                        _timer = threading.Timer(backoff, _backoff_event.set)
                        _timer.daemon = True
                        _timer.start()
                        try:
                            while not _backoff_event.is_set():
                                _backoff_event.wait(timeout=0.2)
                                if stalled or backend.interrupted():
                                    break
                        finally:
                            _timer.cancel()
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
                error = f"session.error: {_brief(idle.error)}"
                if retries_used:
                    error = f"after {retries_used} retries: {error}"
                state = AgentState.ERROR
            elif idle.status == "closed":
                error, state = f"event stream closed: {_brief(idle.error)}", AgentState.ERROR
            elif idle.status == "idle":
                # KC-22: a turn that ended idle with edits in the tree but no
                # commit is a model that has not handed in yet, not one that
                # handed in a wrong entry. Nudge it on in this same session —
                # do not harvest, which would fail every hard gate by
                # construction and burn the pytest roots on an unfinished tree.
                # A clean tree (the model did nothing) is *not* a continue: it
                # falls through to today's path (HARVESTING → REWORK/GAVE_UP),
                # which is the right answer for "you did nothing". A rework
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
                    if dirty:
                        # the current turn keeps its own kind (initial/rework);
                        # the continue becomes the *next* PROMPTED turn, whose
                        # PROMPTED transition (with "(continue N of M)") runs at
                        # the top of the loop. Record this idle turn as it stands.
                        continue_text = continue_message(dirty)
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
                        verdict = _harvest(ws, ticket_path, run_tests)
                        turn["harvest"] = {
                            "verdict": verdict.verdict,
                            "reasons": [r.code for r in verdict.reasons],
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
                run.turns.append(turn)
                _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                return finish(state, error, note=note)

            # ── HARVESTING ─────────────────────────────────────────────────
            transition(AgentState.HARVESTING, note="tests on" if run_tests else "tests off")
            verdict = _harvest(ws, ticket_path, run_tests)
            turn["harvest"] = {"verdict": verdict.verdict, "reasons": [r.code for r in verdict.reasons]}
            run.turns.append(turn)
            _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
            run.commit = verdict.commit
            if verdict.verdict == "READY":
                return finish(AgentState.READY, note=(run.commit or "")[:12])
            if run.attempt >= int(config.max_rework):
                return finish(AgentState.GAVE_UP, "REWORK after the last attempt: "
                              + ", ".join(r.code for r in verdict.reasons if r.blocking))
            run.attempt += 1
            continue_used = 0  # KC-22: a rework resets the per-attempt continue counter
            transition(AgentState.REWORK, note=f"attempt {run.attempt} — "
                       + ", ".join(r.code for r in verdict.reasons))
            rework_text = rework_message(verdict, run.attempt, int(config.max_rework))
    finally:
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
        elif not run.terminal:
            # mid-flight when the round died: the session is gone, the worktree is not
            run.workspace = ws
            verdict = _harvest(ws, ticket_path, run_tests)
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
              run_tests: bool = False) -> RoundState:
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

    def save() -> None:
        with lock:
            _write_json(out_dir / "state.json", state.to_dict())

    def on_transition(run: AgentRun) -> None:
        if stop.is_set():
            raise _Stopped()
        since[run.agent.name] = time.monotonic()
        save()

    heartbeat = _Heartbeat(state, since, float(config.progress_every_sec or 0))

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
                      run_tests=run_tests)
        except _Stopped:
            pass
        finally:
            backend.close()

    pool = ThreadPoolExecutor(max_workers=max(1, int(config.max_parallel)),
                              thread_name_prefix="contest")
    heartbeat.start()
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
        stop.set()  # from here on a worker's transition raises instead of saving
        save()      # the round as it stood: mid-flight agents stay mid-flight for --resume
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
    save()
    return state


class _Heartbeat:
    """The once-a-minute line: the round's age and every agent's state and time
    in it — `round 52 12m: mistral WAITING 3m (attempt 1) · laguna ERROR · hy3 ERROR — 1 live`.

    A daemon thread waiting on an `Event`, so `stop()` returns at once and the
    thread never outlives `run_round`. `every <= 0` starts nothing.
    """

    def __init__(self, state: RoundState, since: dict, every: float):
        self.state, self.since, self.every = state, since, every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="contest-progress", daemon=True)

    def start(self) -> None:
        if self.every > 0:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(2.0)

    def line(self) -> str:
        now = time.monotonic()
        parts = []
        for run in self.state.agents:
            part = f"{run.agent.name} {run.state.value}"
            if not run.terminal:
                part += f" {_age(now - self.since.get(run.agent.name, now))}"
                if run.attempt:
                    part += f" (attempt {run.attempt})"
            parts.append(part)
        live = sum(1 for run in self.state.agents if not run.terminal)
        age = _age(time.time() - self.state.started_at)
        return f"round {self.state.round_no} {age}: " + " · ".join(parts) + f" — {live} live"

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            _log.info("%s", self.line())


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds}s" if seconds < 90 else f"{seconds // 60}m"
