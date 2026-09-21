"""tools/contest/cli.py — KC-16: `python3 -m tools.contest run --ticket NN`.

`contest-bench/kc6/live_smoke.py` is this command written by hand for a
sandbox: `load_roster` → `replace(config, …)` → `prepare_round` →
`KiloServer.spawn` → `run_round` → `server.close()` → the table →
`git log <base>..HEAD` per worktree. This module is that sequence on the real
repo and the real ticket, plus `git format-patch` of every result — the patches
then go through the same hands as every round before them
(`docs/collect-epics/RUN-THE-EPIC-COMPETITION.md` stages 3–5, the ideal
commit). No summary, no `entrants.json`, no scoring: the harvest's tests are
the only judge here — `run_round(..., run_tests=True)` runs the pytest roots in
every harvest, one worktree at a time.

`intake` runs every pre-round check and reports every failure, one line each,
before anything is created: the ticket exists and is `open`; it is the
lowest-numbered `open` ticket, because the runner's prompt does not name a
ticket and `scripts/next_task.py` would hand the session that one; the base
resolves and `epic-tasks/` is clean at it — KC-4's own check through its
`WorkspaceError`, not a copy of it; and the server answers. Nothing is edited
here: an `open` ticket in the way is reported, never flipped — `epic-tasks/`
is the orchestrator's.

`main(argv)` takes subcommands so KC-7 (round 46) adds `status` and `--dry-run`
without moving anything. Exit codes: 0 when at least one agent is READY, 2
when none is, 1 on an intake or a server failure; a bare `python3 -m
tools.contest` is argparse's usage, exit 2.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from tools.contest import gates
from tools.contest.kilo_client import KiloServer, KiloServerError, find_kilo_binary
from tools.contest.roster import AgentSpec, ContestConfig, RosterError, load_roster
from tools.contest.runner import AgentState, RoundState, run_round
from tools.contest.workspace import WorkspaceError, prepare_round

__all__ = [
    "DEFAULT_ROSTER",
    "TASKS_DIR",
    "Intake",
    "agents_from_models",
    "cmd_run",
    "export_patches",
    "intake",
    "main",
]

#: The ticket folder the round reads; `scripts/next_task.py` hands it out.
TASKS_DIR = "epic-tasks"

#: The committed roster at the repo root; a `contest.local.ini` next to it
#: overrides it, `load_roster`'s own rule (KC-2).
DEFAULT_ROSTER = "contest.ini"

#: The status this command runs, exactly: the first word of `**Status:**`.
OPEN = "open"

#: Exit codes.
EXIT_OK, EXIT_NO_READY, EXIT_FAILED = 0, 2, 1

#: `**Status:** open — round 55 …` → `open`. `next_task.py`'s `_status`,
#: duplicated the way `gates.declared_files` already duplicates that ticket's
#: `**File:**` parse instead of importing a script.
_STATUS_RE = re.compile(r"^\*\*Status:\*\*\s*(\S+)", re.MULTILINE)

#: `# KC-16 — \`python3 -m tools.contest run …\`` → `KC-16`.
_TITLE_RE = re.compile(r"^#\s*([A-Za-z0-9_.-]+)", re.MULTILINE)

#: `NN-…md`, `next_task.py`'s `TICKET_RE` with its optional leading zeros.
_TICKET_RE = re.compile(r"^0*(\d+)-.*\.md$")


# ─────────────────────────────────────────────────────────────────────────────
# tickets
# ─────────────────────────────────────────────────────────────────────────────

def _status(ticket_path) -> str:
    """The ticket's `**Status:**` first word, lower-cased; `''` when absent."""
    try:
        body = Path(ticket_path).read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _STATUS_RE.search(body)
    return match.group(1).strip("`*").lower() if match else ""


def _tickets(tasks_dir) -> list:
    """`(number, path, status)` per `NN-*.md` in *tasks_dir*, numeric order."""
    found = []
    for path in Path(tasks_dir).glob("*.md"):
        match = _TICKET_RE.match(path.name)
        if match:
            found.append((int(match.group(1)), path, _status(path)))
    return sorted(found, key=lambda item: item[0])


def _label(ticket_path, number: int) -> str:
    """`KC-7 (46)` — the id the ticket announces in its H1, plus its number."""
    try:
        body = Path(ticket_path).read_text(encoding="utf-8")
    except OSError:
        return Path(ticket_path).name
    match = _TITLE_RE.search(body)
    title = match.group(1) if match else Path(ticket_path).name
    return f"{title} ({number})"


# ─────────────────────────────────────────────────────────────────────────────
# the roster's flags
# ─────────────────────────────────────────────────────────────────────────────

def agents_from_models(models: str, provider: str = "kenary") -> tuple:
    """`--models a:free,b:free` → a roster of `AgentSpec`s.

    Copied from `contest-bench/kc6/live_smoke.py`: the name is the model id
    without its `:tag`, squeezed to the `[a-z0-9][a-z0-9_-]*` a branch name
    needs, and the provider is *provider* unless the id says its own.
    """
    specs = []
    for item in filter(None, (m.strip() for m in models.split(","))):
        prov, _, model_id = item.rpartition("/")
        name = "".join(c if c.isalnum() or c in "_-" else "-" for c in model_id.split(":")[0].lower())
        specs.append(AgentSpec(name=name.lstrip("_-"), provider_id=prov or provider, model_id=model_id))
    return tuple(specs)


def _roster_path(repo, roster: str) -> Path:
    """`--roster` as a path: absolute as given, else relative to the repo."""
    path = Path(roster)
    return path if path.is_absolute() else repo / path


def _round_out_dir(repo, config: ContestConfig, round_no: int) -> Path:
    """`<config.out_dir>/<NN>` relative to the repo, e.g. `contest-out/55` —
    zero-padded like the tickets and the worktrees (`rounds/NN-<agent>`)."""
    return repo / config.out_dir / f"{round_no:02d}"


# ─────────────────────────────────────────────────────────────────────────────
# intake
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Intake:
    """The round, once `intake` has checked it: what `cmd_run` runs it from."""

    ticket_path: Path
    title: str
    base_sha: str
    out_dir: Path


def intake(repo, tasks_dir, round_no, base_ref, config):
    """Run every pre-round check; return the `Intake`, or `None` with the failures printed.

    All checks run and every failure goes to stderr on its own line before
    anything is created, in this order: the ticket exists and its `**Status:**`
    first word is `open`; every lower-numbered `open` ticket is named, because
    the runner's prompt does not name a ticket and `scripts/next_task.py` would
    hand that one to the session; the base resolves and `epic-tasks/` is clean
    at it — `workspace.prepare_round` with an empty roster runs exactly KC-4's
    check and builds nothing; and the server answers — the `kilo` binary
    resolves when the roster says `spawn`, else `KiloServer.attach` reaches
    the URL. `Intake.out_dir` is the default `<out_dir>/<NN>`; `--out` replaces
    it in `cmd_run`.
    """
    repo, tasks_dir = Path(repo), Path(tasks_dir)
    failures: list = []

    found = gates.ticket_for_round(tasks_dir, round_no)
    ticket_path = tasks_dir / found[0] if found[0] else None
    title = ""
    if ticket_path is None or not ticket_path.is_file():
        failures.append(f"no ticket numbered {round_no} in {tasks_dir}")
    else:
        title = found[1]
        status = _status(ticket_path)
        if status != OPEN:
            failures.append(
                f"{ticket_path.name} is not open (**Status:** {status or 'missing'}) — "
                "only an open ticket is on offer"
            )
        for number, path, state in _tickets(tasks_dir):
            if state == OPEN and number < round_no:
                failures.append(
                    f"{_label(path, number)} is open too — set it to queued or run it first"
                )

    base_sha = ""
    try:
        prepare_round(repo, replace(config, agents=()), round_no, base_ref)
        base_sha = gates.git(str(repo), "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    except WorkspaceError as exc:
        failures.append(str(exc))

    if config.server == "spawn":
        try:
            find_kilo_binary(config.kilo_bin)
        except (FileNotFoundError, OSError) as exc:
            failures.append(str(exc))
    else:
        try:
            KiloServer.attach(config.server)
        except KiloServerError as exc:
            failures.append(str(exc))

    if failures:
        for line in failures:
            print(f"intake: {line}", file=sys.stderr)
        return None

    return Intake(
        ticket_path=ticket_path,
        title=title,
        base_sha=base_sha,
        out_dir=_round_out_dir(repo, config, round_no),
    )


# ─────────────────────────────────────────────────────────────────────────────
# the patches
# ─────────────────────────────────────────────────────────────────────────────

def export_patches(state: RoundState, workspaces: list, out_dir) -> list:
    """One `.patch` per agent whose `run.commit` is set, into *out_dir*.

    `git format-patch --stdout <base_sha>..HEAD` per worktree — the patch the
    operator then applies by hand (`docs/collect-epics/
    RUN-THE-EPIC-COMPETITION.md` stages 3–5). A `GAVE_UP` keeps its patch too,
    named `<agent>.GAVE_UP.patch`, because the operator still wants to read
    those — as does a `STALLED` or `ERROR` whose branch has a commit on it (KC-21
    harvests it, so `run.commit` is set), named `<agent>.STALLED.patch` and
    `<agent>.ERROR.patch`: the terminal state is the file name. An empty diff
    writes no file and says so: a READY without a commit cannot happen, the
    harvest sets it, so there is no fake sha to fall back on.
    """
    out = Path(out_dir)
    by_agent = {ws.agent: ws for ws in workspaces}
    written: list = []
    for run in state.agents:
        name = run.agent.name
        if not run.commit:
            continue
        ws = by_agent.get(name)
        if ws is None:
            print(f"warning: {name} claimed a commit but has no workspace — no patch",
                  file=sys.stderr)
            continue
        suffix = ".patch" if run.state is AgentState.READY else f".{run.state.value}.patch"
        target = out / f"{name}{suffix}"
        proc = subprocess.run(
            ["git", "-C", str(ws.path), "format-patch", "--stdout", f"{ws.base_sha}..HEAD"],
            capture_output=True,
            text=True,
        )
        patch = proc.stdout.strip()
        if not patch:
            detail = proc.stderr.strip() or "nothing to format"
            print(f"warning: {name}: no patch for {ws.base_sha}..HEAD — {detail}", file=sys.stderr)
            continue
        out.mkdir(parents=True, exist_ok=True)
        target.write_text(patch + "\n", encoding="utf-8")
        written.append(target)
    return written


# ─────────────────────────────────────────────────────────────────────────────
# the command
# ─────────────────────────────────────────────────────────────────────────────

def _start_server(config: ContestConfig, out_dir: Path):
    """`KiloServer.spawn` when `server = spawn`, else `.attach` to the URL."""
    if config.server == "spawn":
        return KiloServer.spawn(find_kilo_binary(config.kilo_bin),
                                log_path=str(out_dir / "kilo-serve.log"))
    return KiloServer.attach(config.server)


def _apply_flags(config: ContestConfig, args: argparse.Namespace) -> ContestConfig:
    """`--models`, `--max-parallel` and `--no-gate` on top of the roster."""
    if args.models:
        config = replace(config, agents=agents_from_models(args.models))
    if args.max_parallel is not None:
        config = replace(config, max_parallel=int(args.max_parallel))
    if args.no_gate:
        # the mechanical layer still decides; every ask it cannot decide is the
        # existing `gate-failed` reject, recorded like any decision
        config = replace(config, gate_settings=None)
    return config


def _print_plan(result: Intake, config: ContestConfig, out_dir: Path, *, run_tests: bool) -> None:
    """The plan, one line per fact: what the round will do, before it does it."""
    models = ", ".join(agent.model for agent in config.agents)
    facts = (
        ("ticket", f"{result.ticket_path.name} — {result.title}"),
        ("base", result.base_sha[:12]),
        ("agents", f"{len(config.agents)}: {models}"),
        ("parallel", str(config.max_parallel)),
        ("tests", "on" if run_tests else "off"),
        ("gate", "on" if config.gate_settings is not None else "off"),
        ("out", str(out_dir)),
    )
    width = max(len(key) for key, _ in facts)
    for key, value in facts:
        print(f"{key:<{width}} {value}")


def cmd_run(args: argparse.Namespace) -> int:
    """`run --ticket NN …` — the round on the real repo, the patches in `<out>/`.

    `intake` first (exit 1 with one line per failure); then `prepare_round` at
    the base, or `<out>/state.json` under `--resume` with the workspaces taken
    from it; then `KiloServer.spawn` or `.attach`; then `run_round(...,
    run_tests=…)`; then `export_patches` and one JSON line per agent from
    `state.table_rows()`. `server.close()` in `finally`: on Ctrl-C `run_round`
    has already saved `state.json` and re-raised, so the KeyboardInterrupt
    propagates after the close. 0 with a READY, 2 with none.

    A worktree left by a crashed attempt is not reset silently (KC-23): the
    refusal is the same `intake:` line as every other `WorkspaceError`, exit 1,
    no server started — `--fresh` discards the work, `--resume` continues it.
    """
    # `contest-bench/kc6/live_smoke.py` sets the same default: the committed
    # roster's ${CONTEST_GATE_API_KEY} reference must resolve for `load_roster`
    # to read the file at all, and a key that is not real is safe — the gate
    # then answers `gate unavailable: …` per ask, a `gate-failed` reject in
    # decisions.jsonl. This command adds no key check; KC-7's intake does.
    os.environ.setdefault("CONTEST_GATE_API_KEY", "unset-for-the-round")

    repo = Path.cwd().resolve()
    tasks_dir = repo / TASKS_DIR

    try:
        config = load_roster(_roster_path(repo, args.roster))
    except RosterError as exc:
        print(f"intake: {exc}", file=sys.stderr)
        return EXIT_FAILED

    config = _apply_flags(config, args)
    run_tests = not args.no_tests

    result = intake(repo, tasks_dir, args.ticket, args.base, config)
    if result is None:
        return EXIT_FAILED
    out_dir = Path(args.out).resolve() if args.out else result.out_dir
    _print_plan(result, config, out_dir, run_tests=run_tests)

    resume = None
    if args.resume:
        state_path = out_dir / "state.json"
        if not state_path.is_file():
            print(f"intake: --resume: no {state_path} — nothing to resume", file=sys.stderr)
            return EXIT_FAILED
        try:
            resume = RoundState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
        except (ValueError, KeyError, OSError) as exc:
            print(f"intake: --resume: {state_path} is unreadable: {exc}", file=sys.stderr)
            return EXIT_FAILED
        workspaces = [run.workspace for run in resume.agents]
    else:
        try:
            workspaces = prepare_round(repo, config, args.ticket, args.base,
                                       force=args.fresh)
        except WorkspaceError as exc:
            print(f"intake: {exc}", file=sys.stderr)
            return EXIT_FAILED

    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        server = _start_server(config, out_dir)
    except (KiloServerError, FileNotFoundError, OSError) as exc:
        print(f"server: {exc}", file=sys.stderr)
        return EXIT_FAILED

    try:
        state = run_round(config, args.ticket, result.ticket_path, workspaces,
                          server=server, out_dir=out_dir, resume=resume,
                          run_tests=run_tests)
    finally:
        server.close()

    patches = export_patches(state, workspaces, out_dir)
    for row in state.table_rows():
        print(json.dumps(row, ensure_ascii=False))
    for patch in patches:
        print(f"patch: {patch}")

    ready = sum(1 for run in state.agents if run.state is AgentState.READY)
    return EXIT_OK if ready else EXIT_NO_READY


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools.contest",
        description="The Kilo model contest: one round, one ticket, N agents.",
    )
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser(
        "run",
        help="run one round and export the patches",
        description="One round on the real repo: prompt, wait, harvest with the "
                    "tests, rework — and one .patch per agent.",
    )
    run.add_argument("--ticket", type=int, required=True, metavar="NN",
                     help="the ticket number, NN from epic-tasks/NN-*.md")
    run.add_argument("--roster", default=DEFAULT_ROSTER,
                     help="the roster ini (default contest.ini at the repo root; "
                          "contest.local.ini next to it overrides it)")
    run.add_argument("--base", default="HEAD",
                     help="the base ref the worktrees start from (default HEAD)")
    run.add_argument("--models", default="",
                     help="comma-separated model ids run INSTEAD of the roster's agents "
                          "(a:free,b:free → provider kenary unless the id says its own)")
    run.add_argument("--max-parallel", type=int, default=None, metavar="N",
                     help="override the roster's max_parallel")
    run.add_argument("--no-tests", action="store_true",
                     help="do not run the pytest roots in the harvest (the default is on)")
    run.add_argument("--no-gate", action="store_true",
                     help="no gate model: the mechanical layer decides, the rest is gate-failed")
    run.add_argument("--resume", action="store_true",
                     help="resume from <out>/state.json — only the mid-flight agents restart")
    run.add_argument("--fresh", action="store_true",
                     help="reset the round's worktrees even when they hold uncommitted "
                          "work or commits")
    run.add_argument("--out", default=None, metavar="DIR",
                     help="the round's output directory (default <out_dir>/<NN>)")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv=None) -> int:
    """`python3 -m tools.contest …` — subcommands, so KC-7 adds to this list only."""
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_usage(sys.stderr)
        return 2
    # the runner's progress lines (one per transition) are the operator's only
    # view of a live round: `live_smoke.py` configures the same
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(message)s")
    return args.func(args)
