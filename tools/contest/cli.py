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
before anything is created: the base resolves and `epic-tasks/` is clean at it
— KC-4's own check through its `WorkspaceError`, not a copy of it; the ticket
exists and is `open`; no lower-numbered ticket is still on offer, because the
runner's prompt does not name a ticket and `scripts/next_task.py` would hand the
session that one; and the server answers. The ticket statuses are read from the
base tree the sessions read, not from this checkout, so the two agree on a word
only when the base is HEAD (KC-24); the refusal names the ticket the sessions
would get and prints the two ways past it. Nothing is edited here: a ticket in
the way is reported, never flipped — `epic-tasks/` is the orchestrator's.

When the server answers, `intake` asks it what it offers
(`KiloClient.providers`, KC-25) and runs the roster's `provider/model` pairs
through `roster_on_offer`: the display name spelled as the id, a provider with
no credentials, a model that is not there — each a refusal naming the id or the
model to use, instead of half the round's slots dying on the first turn with
`Model not found`. With `server = spawn` that costs a throwaway server of its
own; a server that cannot be started is not a failure here, the round's own
`server:` line says the same thing. `--provider` is the default provider behind
a bare `--models` id.

`main(argv)` takes subcommands so KC-7 (round 46) adds `status` and `--dry-run`
without moving anything. Exit codes: 0 when at least one agent is READY, 2
when none is, 1 on an intake or a server failure; a bare `python3 -m
tools.contest` is argparse's usage, exit 2.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from tools.contest import gates
from tools.contest.kilo_client import (
    KiloClient,
    KiloHttpError,
    KiloServerError,
    KiloServer,
    find_kilo_binary,
)
from tools.contest.roster import AgentSpec, ContestConfig, RosterError, load_roster
from tools.contest.runner import AgentState, RoundState, run_round
from tools.contest.workspace import WorkspaceError, prepare_round

__all__ = [
    "DEFAULT_PROVIDER",
    "DEFAULT_ROSTER",
    "TASKS_DIR",
    "Intake",
    "agents_from_models",
    "cmd_run",
    "export_patches",
    "intake",
    "main",
    "roster_on_offer",
]

#: The ticket folder the round reads; `scripts/next_task.py` hands it out.
TASKS_DIR = "epic-tasks"

#: The committed roster at the repo root; a `contest.local.ini` next to it
#: overrides it, `load_roster`'s own rule (KC-2).
DEFAULT_ROSTER = "contest.ini"

#: The provider id behind a `--models` id that does not name its own. The id
#: `POST /session` wants — not the display name the id may have been read from.
DEFAULT_PROVIDER = "kenary"

#: The status this command runs, exactly: the first word of `**Status:**`.
OPEN = "open"

#: The words that take a lower ticket off the offer — exactly
#: `scripts/next_task.py`'s `SKIP_STATUS`, duplicated the way `_STATUS_RE`
#: already duplicates that script's status regex: the runner does not read
#: `scripts/`. Anything else is still on offer — `open`, but also `running` or
#: `wip` when a round runs on another machine, and a ticket with no status line
#: at all — and a session prompted without a ticket would get it.
PARKED = ("landed", "queued")

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

def _status_of(body: str) -> str:
    """The body's `**Status:**` first word, lower-cased; `""` when absent."""
    match = _STATUS_RE.search(body)
    return match.group(1).strip("`*").lower() if match else ""


def _ticket_body(tasks_dir, name, at=None) -> str:
    """One ticket's body: from the commit *at* when it is set, else from disk.

    `git show <at>:epic-tasks/<name>` is the copy the sessions read — their
    worktrees are built at *at*, not at this checkout. A name that is not in
    that tree reads as `""`, the same as an unreadable file below.
    """
    if at:
        # `tasks_dir` is one level under the repo root, so its own name is the
        # repo-root-relative path `git ls-tree` / `git show` want
        return gates.git(str(Path(tasks_dir).parent), "show",
                         f"{at}:{Path(tasks_dir).name}/{name}")
    try:
        return Path(tasks_dir).joinpath(name).read_text(encoding="utf-8")
    except OSError:
        return ""


def _tickets(tasks_dir, at=None) -> list:
    """`(number, path, status, body)` per `NN-*.md`, in numeric order.

    Without *at* the names and the statuses come from *tasks_dir* as checked
    out. With *at* they come from that commit's tree instead —
    `git ls-tree -r --name-only <at> epic-tasks/` for the names,
    `git show <at>:epic-tasks/<name>` for each body — because the sessions read
    the folder from the worktree built at the base, not from this checkout. The
    `path` entries stay checkout paths: only `.name` is used, and their files
    may be absent on disk then. `-r`: without it `ls-tree` lists the tree
    itself and nothing under it.
    """
    tasks_dir = Path(tasks_dir)
    if at:
        rel_dir = Path(tasks_dir).name
        listed = gates.git(str(tasks_dir.parent), "ls-tree", "-r", "--name-only",
                           str(at), rel_dir)
        names = [line.rsplit("/", 1)[-1] for line in listed.splitlines() if line.strip()]
    else:
        names = [path.name for path in tasks_dir.glob("*.md")]
    found = []
    for name in names:
        match = _TICKET_RE.match(name)
        if match:
            body = _ticket_body(tasks_dir, name, at)
            found.append((int(match.group(1)), tasks_dir / name, _status_of(body), body))
    return sorted(found, key=lambda item: item[0])


def _title_id(body: str, name: str) -> str:
    """`KC-7` — the id the ticket announces in its H1, or its file name."""
    match = _TITLE_RE.search(body)
    return match.group(1) if match else name


def _label(body: str, name: str, number: int) -> str:
    """`KC-7 (46)` — the id the ticket announces in its H1, plus its number."""
    return _title_id(body, name) + f" ({number})"


# ─────────────────────────────────────────────────────────────────────────────
# the roster's flags
# ─────────────────────────────────────────────────────────────────────────────

def agents_from_models(models: str, provider: str = DEFAULT_PROVIDER) -> tuple:
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


def roster_on_offer(providers: dict, agents: tuple) -> list:
    """One failure line per roster agent that is not on offer, in roster order.

    *providers* is `GET /provider` as `KiloClient.providers` hands it over,
    decoded as is. A provider's `id` is what `POST /session` wants and its
    `name` is the display string from `kilo.jsonc`, so a roster that spells the
    display name is refused with the id to use instead — the one-word error that
    used to surface as the first turn's `Model not found` on one agent of the
    round, minutes in. Pure: no server, no roster, one line per bad agent.

    A model whose `status` is not `active` is not a failure — that field's other
    values are unverified — and nothing is inferred from `capabilities`.
    """
    by_id = {}
    by_name = {}
    for provider in providers.get("all") or []:
        if not isinstance(provider, dict):
            continue
        provider_id = provider.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            continue
        by_id[provider_id] = provider
        name = provider.get("name")
        if isinstance(name, str) and name.lower() not in by_name:
            by_name[name.lower()] = provider_id
    connected = [item for item in (providers.get("connected") or []) if isinstance(item, str)]

    failures = []
    for agent in agents:
        pair = agent.model
        if agent.provider_id not in by_id:
            display = by_name.get(agent.provider_id.lower())
            if display:
                use = display + "/" + agent.model_id
                failures.append(
                    f"[{agent.name}] {pair}: no provider '{agent.provider_id}' — "
                    f"that is the display name of provider '{display}'; use {use}")
            else:
                failures.append(
                    f"[{agent.name}] {pair}: no provider '{agent.provider_id}' — "
                    f"connected: {', '.join(sorted(connected))}")
            continue
        if agent.provider_id not in connected:
            failures.append(
                f"[{agent.name}] {pair}: provider '{agent.provider_id}' "
                "has no credentials (not connected)")
            continue
        models = by_id[agent.provider_id].get("models") or {}
        if agent.model_id in models:
            continue
        ids = sorted(str(model) for model in models)
        line = (f"[{agent.name}] {pair}: no model '{agent.model_id}' "
                f"under '{agent.provider_id}' — on offer: {', '.join(ids)}")
        close = difflib.get_close_matches(agent.model_id, ids, n=1)
        if close:
            line += " (did you mean " + close[0] + "?)"
        failures.append(line)
    return failures


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

def _run_line(argv, number: int) -> str:
    """`python3 -m tools.contest` + this invocation's argv with the `--ticket`
    value replaced by *number* — nothing else is re-derived."""
    words: list = []
    i = 0
    while i < len(argv):
        word = argv[i]
        if word == "--ticket" and i + 1 < len(argv):
            words += ["--ticket", str(number)]
            i += 2
        elif word.startswith("--ticket="):
            words.append("--ticket=" + str(number))
            i += 1
        else:
            words.append(word)
            i += 1
    return "python3 -m tools.contest " + " ".join(words)


def _park_line(body, name, number: int, rel_dir) -> str:
    r"""The `sed` and the `git commit` that park one ticket in this checkout.

    An uncommitted `epic-tasks/` is refused by `prepare_round` and an
    uncommitted edit is invisible to the sessions, so the park is a `sed` and a
    commit of that one file. The `**Status:**` line is addressed by its number
    in *body*, not by a hard-coded 3. Built by concatenation: a 3.10 f-string
    cannot hold the `\*` the sed expression needs.
    """
    file_ref = rel_dir + "/" + name
    word = _status_of(body)
    match = _STATUS_RE.search(body)
    line_no = body[:match.start()].count("\n") + 1 if match else 0
    if line_no:
        sed = "'" + str(line_no) + "s/^\\*\\*Status:\\*\\* " + word + "/**Status:** queued/'"
    else:
        sed = "'1s/^/**Status:** queued\\n/'"
    message = rel_dir + ": " + _title_id(body, name) + " queued — round " \
        + str(number) + " runs elsewhere"
    return "sed -i " + sed + " " + file_ref + " && git commit -m '" + message + "' -- " + file_ref


def _on_offer(body, rel_dir, name, number, wanted, argv, base_ref, base_is_head) -> list:
    """The four `intake:` lines for one lower ticket that is still on offer.

    The first names the ticket the sessions would get and why; the next two are
    the two ways out, each indented two spaces so every line still lands under
    the one `intake:` prefix `intake` prints.
    """
    file_ref = rel_dir + "/" + name
    label = _label(body, name, number)
    status = _status_of(body)
    lines = [
        label + " is on offer ahead of " + wanted + " — the session prompt names no ticket; "
        + "scripts/next_task.py would hand the sessions " + label,
        "  " + "planned for the sessions:".ljust(26) + file_ref
        + " (**Status:** " + (status or "missing") + ")",
        "  " + "run that one instead:".ljust(26) + _run_line(argv, number),
    ]
    if base_is_head:
        lines.append("  " + "or park it, then re-run:".ljust(26) +
                     _park_line(body, name, number, rel_dir))
    else:
        lines.append("  or park it in a commit reachable from --base " + str(base_ref)
                     + " (the sessions read " + rel_dir + "/ from there, not from this checkout)")
    return lines


@dataclass(frozen=True)
class Intake:
    """The round, once `intake` has checked it: what `cmd_run` runs it from."""

    ticket_path: Path
    title: str
    base_sha: str
    out_dir: Path


def _offer_failures(repo, config: ContestConfig, attached) -> list:
    """The roster's `provider/model` pairs, checked against `GET /provider`.

    With `server = spawn` no server is attached yet, so a throwaway one is
    started for the call alone and removed with its log — `cmd_run` starts its
    own afterwards, and leaving that order alone is the point, so the second
    spawn is the price. Whatever stops that throwaway from starting is caught
    wide on purpose and not reported here: the check asks one question, and it
    must not fail louder than the round it guards — the round's own `server:`
    line reports the same word. With a URL the attached server answers and
    nothing is spawned. A failure of the call itself is one line, so the check
    never crashes intake.
    """
    log_path = None
    own_server = None
    server = attached
    try:
        if server is None and config.server == "spawn":
            fd, log_path = tempfile.mkstemp(prefix="kilo-offer-", text=True)
            os.close(fd)
            try:
                server = KiloServer.spawn(find_kilo_binary(config.kilo_bin),
                                          log_path=log_path)
                own_server = server
            except Exception:
                return []
        if server is None:
            return []
        providers = KiloClient(server, str(repo)).providers()
        return roster_on_offer(providers, config.agents)
    except (KiloHttpError, KiloServerError, ValueError) as exc:
        return [f"GET /provider failed: {exc}"]
    finally:
        if own_server is not None:
            own_server.close()
        if log_path:
            try:
                os.unlink(log_path)
            except OSError:
                pass


def intake(repo, tasks_dir, round_no, base_ref, config, argv=None):
    """Run every pre-round check; return the `Intake`, or `None` with the failures printed.

    All checks run and every failure goes to stderr on its own line before
    anything is created, in this order: the base resolves and `epic-tasks/` is
    clean at it — `workspace.prepare_round` with an empty roster runs exactly
    KC-4's check and builds nothing; the ticket exists and its `**Status:**`
    first word is `open`; no lower-numbered ticket is still on offer, and each
    of those is named with the two ways past it, because the runner's prompt
    does not name a ticket and `scripts/next_task.py` would hand that one to
    the session; the server answers — the `kilo` binary resolves when the
    roster says `spawn`, else `KiloServer.attach` reaches the URL; and the
    roster's `provider/model` pairs are on offer — `KiloClient.providers`
    against `roster_on_offer`, so a display name spelled as an id, a provider
    with no credentials, and a model that is not there are all refused here
    instead of on the first turn (KC-25).
    `Intake.out_dir` is the default `<out_dir>/<NN>`; `--out` replaces it in
    `cmd_run`.

    Every status read is from the base tree, not from this checkout: the
    sessions read `epic-tasks/` from the worktree built at the base, so the two
    only agree on a word when the base is HEAD. A base that does not resolve
    yields no tree to read, so the checkout stands in for the ticket checks and
    the `WorkspaceError` line is the base's own failure. *argv* is this
    invocation's, for the printed `run` line; `None` means `sys.argv[1:]`.
    """
    repo, tasks_dir = Path(repo), Path(tasks_dir)
    failures: list = []
    argv = list(argv) if argv else list(sys.argv[1:])
    rel_dir = tasks_dir.name

    base_sha = ""
    try:
        prepare_round(repo, replace(config, agents=()), round_no, base_ref)
        base_sha = gates.git(str(repo), "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    except WorkspaceError as exc:
        failures.append(str(exc))
    # the sessions read `epic-tasks/` from the base's tree; without a base to
    # read from the checkout stands in for those reads, and the line above says
    # why the base is unusable
    at = base_sha or None

    found = gates.ticket_for_round(tasks_dir, round_no)
    name = found[0]
    ticket_path = tasks_dir / name if name else None
    title = ""
    if ticket_path is None or not ticket_path.is_file():
        failures.append(f"no ticket numbered {round_no} in {tasks_dir}")
    else:
        title = found[1]
        body = _ticket_body(tasks_dir, name, at)
        if at and not body:
            # not in the base tree: the checkout copy stands in for its status
            body = _ticket_body(tasks_dir, name, None)
        status = _status_of(body)
        if status != OPEN:
            failures.append(
                f"{name} is not open (**Status:** {status or 'missing'}) — "
                "only an open ticket is on offer"
            )
        wanted = _label(body, name, round_no)
        head_sha = gates.git(str(repo), "rev-parse", "HEAD") if base_sha else ""
        base_is_head = bool(base_sha) and base_sha == head_sha
        for number, path, state, lower_body in _tickets(tasks_dir, at):
            if state.startswith(PARKED) or number >= round_no:
                continue
            failures.extend(
                _on_offer(lower_body, rel_dir, path.name, number, wanted, argv,
                          base_ref, base_is_head)
            )

    attached = None
    if config.server == "spawn":
        try:
            find_kilo_binary(config.kilo_bin)
        except (FileNotFoundError, OSError) as exc:
            failures.append(str(exc))
    else:
        try:
            attached = KiloServer.attach(config.server)
        except KiloServerError as exc:
            failures.append(str(exc))

    # the roster the round would run, against what the server offers: the
    # refusal that used to arrive as one agent's first-turn `session.error`
    failures.extend(_offer_failures(repo, config, attached))

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
    """`--models` (with `--provider` behind its bare ids), `--max-parallel` and
    `--no-gate` on top of the roster."""
    if args.models:
        config = replace(config, agents=agents_from_models(args.models,
                                                           provider=args.provider))
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

    result = intake(repo, tasks_dir, args.ticket, args.base, config,
                    argv=getattr(args, "argv", None))
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
                          "(a:free,b:free → --provider unless the id names its own)")
    run.add_argument("--provider", default=DEFAULT_PROVIDER, metavar="ID",
                     help="the provider id behind a --models id that names no provider of "
                          "its own (default kenary; the roster spells a model provider/model, "
                          "and --roster's agents are unaffected)")
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
    # this invocation's argv, so intake can print the `run` line with the
    # blocked ticket's number in it instead of re-deriving the command
    args.argv = list(argv) if argv else list(sys.argv[1:])
    return args.func(args)
