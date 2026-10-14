"""tools/contest/cli.py — KC-16: `python3 -m tools.contest run --ticket NN`.

`contest-bench/kc6/live_smoke.py` is this command written by hand for a
sandbox: `load_roster` → `replace(config, …)` → `prepare_round` →
`KiloServer.spawn` → `run_round` → `server.close()` → the table →
`git log <base>..HEAD` per worktree. This module is that sequence on the real
repo and the real ticket, plus `git format-patch` of every result, then
`export.write_entrants` and `export.write_summary` — the round's folder is the
one `contest-bench/harness/setup_worktrees.py` reads and the scorer scores, so
the operator does not hand-write `entrants.json` or a summary after it. No
scoring: the harvest's tests are the only judge here — `run_round(...,
run_tests=True)` runs the pytest roots in every harvest, one worktree at a
time.

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

`backend = openrouter` runs the same command on a non-Kilo backend:
`--backend openrouter` skips the `kilo serve` start, the `KiloServer.attach`
health check and the `GET /provider` offer check alike, gives each agent an
`OpenRouterBackend` subprocess instead of a `KiloClient` session, and makes
`openrouter` the provider behind a bare `--models` id — an operator with a
gateway key and no `kilo` binary can run the round. That credential is
`[contest] openrouter_llm_profile`, resolved at load time for that backend
only, and it is not the gate profile.

`main(argv)` takes subcommands, so KC-7 (round 46) adds `status` and `--dry-run`
without moving anything. `run --dry-run` runs `intake` and `prepare_round`,
prints the plan and the exact prompt the first agent would get, and stops there:
no `kilo serve` — not even intake's throwaway offer server — no session, no gate
call, one `dry-run: skipped …` line per check that needed a server. `status
--ticket NN` prints the SUMMARY table off `<out>/state.json` and touches
nothing, mid-round as well as after it, and exits 1 with one line when there is
no `state.json` to read. Exit codes: 0 when at least one agent is READY, 2
when none is, 1 on an intake or a server failure; a bare `python3 -m
tools.contest` is argparse's usage, exit 2.
"""

from __future__ import annotations

import argparse
import copy
import difflib
from collections import Counter
import json
import logging
import os
import re
import shlex
import shutil
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from tools.contest import context_memory, draft, export, gates, probe_memory
from tools.contest.backend import KiloBackend, OpenRouterBackend, drop_stale_kilo_file
from tools.contest.kilo_client import (
    KiloClient,
    KiloHttpError,
    KiloServerError,
    KiloServer,
    find_kilo_binary,
    kilo_neighbours,
)
from tools.contest.policy import (
    Policy,
    gate_error_name,
    gate_verdict,
    gate_worst_case_sec,
)
from tools.contest.roster import (
    AgentSpec,
    ContestConfig,
    LOCAL_FILENAME,
    RosterError,
    load_roster,
)
from tools.contest.runner import (
    WORKERS_FILE,
    _age,
    _context_limit_fallback,
    RELAY_STATES,
    AgentState,
    RoundState,
    _is_quota,
    _quota_re,
    agent_pytest_workers,
    agent_tmp_path,
    core_count,
    round_live_agents,
    leg_record,
    round_prompt,
    run_leg,
    run_round,
    write_pytest_workers,
)
from tools.contest.variant import (
    DEFAULT,
    HIGHEST,
    hello_probe,
    ladder,
    listed_variants,
    needs_login,
    pick_variant,
    with_retries,
)
from tools.contest.think_probe import (
    PROBE_CACHE_FILE,
    load_probe_cache,
    probe_model,
    save_probe_cache,
)
from tools.contest.workspace import LegCarry, WorkspaceError, agent_tmp_dir, prepare_round
from tools.git_run import run_git

__all__ = [
    "DEFAULT_PROVIDER",
    "DEFAULT_ROSTER",
    "GATE_PLACEHOLDER_MODEL",
    "TASKS_DIR",
    "DraftSetupError",
    "Intake",
    "agents_from_models",
    "cmd_run",
    "cmd_draft",
    "draft_callables",
    "cmd_status",
    "export_patches",
    "gate_model_refusals",
    "gate_share_line",
    "intake",
    "main",
    "resolve_variants",
    "roster_on_offer",
    "roster_missing",
    "ticket_size",
    "registration_overlay",
    "merge_config_content",
]

#: The ticket folder the round reads; `scripts/next_task.py` hands it out.
TASKS_DIR = "epic-tasks"

#: The committed roster at the repo root; contest.local.ini next to it overrides it (KC-2).
DEFAULT_ROSTER = "contest.ini"

#: Provider id for a --models id that does not name its own; the id POST /session wants.
DEFAULT_PROVIDER = "kenary"

#: The committed gate model placeholder; a round still on it has no gate to probe (KC-55).
GATE_PLACEHOLDER_MODEL = "some/model"

#: Pytest plugin module name, loaded from its own directory so the agent imports its own tools (KC-65).
PYTEST_WORKERS_PLUGIN = "contest_pytest_workers"
PYTEST_WORKERS_PLUGIN_DIR = Path(__file__).resolve().parent / "pytest_plugin"

#: The /proc root scanned for neighbours sharing the store (KC-62); tests point it at a fake.
_PROC_ROOT = "/proc"

#: The status this command runs, exactly: the first word of `**Status:**`.
OPEN = "open"

#: Ticket statuses that take a ticket off the offer, duplicated from scripts/next_task.py (SKIP_STATUS).
PARKED = ("landed", "queued", "closed")

#: Exit codes.
EXIT_OK, EXIT_NO_READY, EXIT_FAILED = 0, 2, 1

#: Status word of a ticket (open, running, ...), parsed the way next_task.py parses it.
_STATUS_RE = re.compile(r"^\*\*Status:\*\*[ \t]*(\S+)", re.MULTILINE)

#: `# KC-16 — \`python3 -m tools.contest run …\`` → `KC-16`.
_TITLE_RE = re.compile(r"^#\s*([A-Za-z0-9_.-]+)", re.MULTILINE)

#: Field labels (File, Symbol, Status) of a ticket block.
_FIELD_LABEL_RE = re.compile(r"^\*\*([A-Za-z][A-Za-z0-9 _-]*):\*\*", re.MULTILINE)

#: Labels that next_task.py needs to name the code for a session; without them there is no finding.
_REQUIRED_LABELS = ("File", "Symbol")

#: `NN-…md`, `next_task.py`'s `TICKET_RE` with its optional leading zeros.
_TICKET_RE = re.compile(r"^0*(\d+)-.*\.md$")

#: Size word of a Size line: its first word, with any parenthetical after it ignored (KC-44).
_SIZE_RE = re.compile(r"^\*\*Size:\*\*\s*(\S+)", re.MULTILINE)

#: The four ticket sizes, upper-cased; any other Size value counts as no size (KC-44).
KNOWN_SIZES = ("XS", "S", "M", "L")


# Tickets

def _status_of(body: str) -> str:
    """The body's `**Status:**` first word, lower-cased; `""` when absent.

    Punctuation that trails the word is not part of it, the way
    `next_task._status` reads it: `queued, judged on arena` is `queued`.
    """
    match = _STATUS_RE.search(body)
    return match.group(1).strip("`*,;:.-()[]{}<>—–'\"").lower() if match else ""


def _missing_labels(body: str) -> list:
    """The labels of `_REQUIRED_LABELS` *body* declares no line for, in order.

    `scripts/next_task.py` reads `**File:**` and `**Symbol:**` to build the
    finding it prints under `code to fix`, so a ticket without one of them hands
    the sessions a ticket the runner cannot point them at. One label per
    `intake:` line, the way every other intake refusal prints. `[]` when both
    are there, and `[]` for an empty body — an unreadable ticket has already
    printed the `no ticket numbered` line above.
    """
    labels = set()
    for match in _FIELD_LABEL_RE.finditer(body):
        labels.add(match.group(1).strip().lower())
    return [label for label in _REQUIRED_LABELS if label.lower() not in labels]


def _ticket_body(tasks_dir, name, at=None) -> str:
    """One ticket's body: from the commit *at* when it is set, else from disk.

    `git show <at>:epic-tasks/<name>` is the copy the sessions read — their
    worktrees are built at *at*, not at this checkout. A name that is not in
    that tree reads as `""`, the same as an unreadable file below.
    """
    if at:
        # tasks_dir sits one level under the repo root, so its name is the path git wants.
        return gates.git(str(Path(tasks_dir).parent), "show",
                         f"{at}:{Path(tasks_dir).name}/{name}")
    try:
        return Path(tasks_dir).joinpath(name).read_text(encoding="utf-8")
    except OSError:
        return ""


def _tree_names(repo, at, folder, recursive=False) -> list:
    r"""The names under *folder* in *at*'s tree: `ls-tree -z`, NUL-split, unquoted.

    Bug 206: without `-z` git C-quotes a name holding a non-ASCII letter, a `"` or
    a `\` (`"epic-tasks/\320\260.md"`), and no ticket pattern knows such a string —
    a hand-named ticket was invisible on a base. `tools/arena/gitref.py` reads the
    same thing through `ls_tree_names`; this side keeps `run_git` and its `[]`
    contract, so a base that cannot be read is "no ticket there", not an exception.
    """
    args = ["ls-tree"]
    if recursive:
        args.append("-r")
    args += ["-z", "--name-only", str(at), str(folder)]
    proc = run_git(["git", *args], cwd=str(repo), encoding="utf-8",
                   errors="surrogateescape")
    if proc.returncode:
        return []
    return [name for name in proc.stdout.split("\0") if name]


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
        names = [name.rsplit("/", 1)[-1]
                 for name in _tree_names(tasks_dir.parent, at, Path(tasks_dir).name,
                                         recursive=True)]
    else:
        names = [path.name for path in tasks_dir.glob("*.md")]
    found = []
    for name in names:
        match = _TICKET_RE.match(name)
        if match:
            body = _ticket_body(tasks_dir, name, at)
            found.append((int(match.group(1)), tasks_dir / name, _status_of(body), body))
    return sorted(found, key=lambda item: item[0])


#: Temp folders for base-only tickets (AR-3); cmd_run removes them on return, even on failure.
_TEMP_TICKET_DIRS: list = []


def ticket_file(repo, tasks_dir, round_no, at) -> tuple:
    """AR-3: `(name, path)` of the ticket numbered *round_no* — `("", None)` for none.

    The checkout's own file first: `(name, tasks_dir / name)`, today's path.
    Else, when the base *at* holds the ticket (`git ls-tree --name-only <at>
    <rel>/`, the same `^0*(\\d+)-.*\\.md$` match as `gates.ticket_for_round`),
    its text is written to a file of the same name in a fresh temp folder, so
    `declared_files` and `ticket_size` read a real file. `arena run start`
    builds such a base (`arena-round/NN`) without touching the checkout.
    """
    repo, tasks_dir = Path(repo), Path(tasks_dir)
    try:
        name = gates.ticket_for_round(tasks_dir, round_no)[0]
    except OSError:
        name = None
    if name:
        return name, tasks_dir / name
    if not at:
        return "", None
    try:
        rel = tasks_dir.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return "", None
    for full in _tree_names(repo, at, rel + "/"):
        base_name = full.rsplit("/", 1)[-1]
        match = re.match(r"^0*(\d+)-.*\.md$", base_name)
        if not match or int(match.group(1)) != round_no:
            continue
        r = run_git(["git", "show", f"{at}:{rel}/{base_name}"], cwd=str(repo))
        if r.returncode:
            return "", None
        folder = Path(tempfile.mkdtemp(prefix="contest-ticket-"))
        _TEMP_TICKET_DIRS.append(folder)
        path = folder / base_name
        path.write_text(r.stdout, encoding="utf-8")
        return base_name, path
    return "", None


def _title_id(body: str, name: str) -> str:
    """`KC-7` — the id the ticket announces in its H1, or its file name."""
    match = _TITLE_RE.search(body)
    return match.group(1) if match else name


def _label(body: str, name: str, number: int) -> str:
    """`KC-7 (46)` — the id the ticket announces in its H1, plus its number."""
    return _title_id(body, name) + f" ({number})"


def ticket_size(ticket_path) -> str | None:
    """KC-44: the ticket's own `**Size:**` — `XS`, `S`, `M` or `L`, normalised to
    upper case. `None` when the line is absent, when the value is not one of the
    four, or when the file cannot be read.

    The line's first word is the size and whatever follows it is a
    parenthetical note: `S (measurement only)` is `S`, and the `L` that carries
    its own `**Size note:**` line is still `L`. `None` behaves as today: the
    ticket's size decides nothing, and the `[contest] legs` is what decides.
    """
    try:
        body = Path(ticket_path).read_text(encoding="utf-8")
    except OSError:
        return None
    match = _SIZE_RE.search(body)
    if not match:
        return None
    size = match.group(1).strip("`*").upper()
    return size if size in KNOWN_SIZES else None


# Roster flags

def agents_from_models(models: str, provider: str = DEFAULT_PROVIDER) -> tuple:
    """`--models a:free,b:free` → a roster of `AgentSpec`s.

    The name is the model id without its `:tag`, squeezed to the
    `[a-z0-9][a-z0-9_-]*` a branch name needs, and the provider is *provider*
    unless the id says its own — copied from `contest-bench/kc6/live_smoke.py`.

    Beyond the copy, a name that appears more than once in *models* is
    suffixed: `<name>-var1`, `<name>-var2`, … in list order. The name is the
    branch, the worktree folder, `runs/<name>/` and `<name>.patch`, so two
    agents with one name share one checkout and one branch and the round still
    exits 0; the roster path already refuses a duplicate agent name, the
    `--models` path did not. "Same name" means the squeezed name, so
    `hy3:free,hy3:pro` are variants too and so are `kenary/hy3:free` and
    `openrouter/hy3:free`, each keeping its own provider. A name that appears
    once is untouched, and a variant skips a name the list already holds
    (`hy3-var1:free,hy3:free,hy3:free` → `hy3-var1`, `hy3-var2`, `hy3-var3`).
    The names are a pure function of the *models* string, so `--resume` with
    the same string finds the same agents in `state.json`.

    The provider is split off at the first `/`, as the roster does, so a model
    id with a `/` of its own keeps it: `kilo/nex-agi/nex-n2.5-pro:free` is
    provider `kilo`, model `nex-agi/nex-n2.5-pro:free`, name `nex-n2-5-pro`
    (the part after the model's own last `/`).

    KC-49: an item may end in `@<variant>` — `sensenova123/sensenova-6.8-flash-lite@high`,
    `glm-4-7-flash:free@highest` — the reasoning variant that agent runs at.
    The variant is not part of the name: `m@high,m@low` are two variants of one
    name, suffixed like any repeat.
    """
    parsed = []
    for item in filter(None, (m.strip() for m in models.split(","))):
        item, _, variant = item.partition("@")
        # The split is at the first slash, as the roster does: kilo is the provider.
        prov, slash, model_id = item.partition("/")
        if not slash:
            prov, model_id = "", item
        name = "".join(c if c.isalnum() or c in "_-" else "-"
                       for c in model_id.rpartition("/")[2].split(":")[0].lower())
        parsed.append((name.lstrip("_-"), prov or provider, model_id, variant.strip() or None))

    counts = Counter(name for name, _, _, _ in parsed)
    taken = {name for name, count in counts.items() if count == 1}
    last: dict = {}
    specs = []
    for name, prov, model_id, variant in parsed:
        if counts[name] > 1:
            number = last.get(name, 0)
            while True:
                number += 1
                candidate = f"{name}-var{number}"
                if candidate not in taken:
                    break
            last[name] = number
            taken.add(candidate)
            name = candidate
        specs.append(AgentSpec(name=name, provider_id=prov, model_id=model_id,
                               variant=variant))
    return tuple(specs)


def login_hint(kilo_bin: str | None, provider_id: str) -> str:
    """` — fix: <kilo> auth login -p <provider>`, the binary this round resolved
    (never a hard-coded path); `""` without one."""
    if not kilo_bin:
        return ""
    return f" — fix: {shlex.quote(kilo_bin)} auth login -p {shlex.quote(provider_id)}"


def resolve_variants(providers: dict, agents: tuple, probe_for=None, *,
                     kilo_bin: str | None = None, quota_re=None,
                     parallel: int = 1, per_provider: int = 0) -> tuple:
    """KC-49: `(agents, failures, notes)` — every agent's variant made real.

    *providers* is `GET /provider`. An agent with no variant is untouched. A
    named variant must be one the model lists, or it is one failure line with
    the list, and it is probed once too when *probe_for* is attached (KC-61) —
    a named variant used to be sent unasked, so an exhausted key reached the
    round when the variant was named. The answer is `None` (hello came back)
    and the agent is appended as today; a quota is a note naming the agent
    and the reset text, and the agent is left out — the round runs one short;
    anything else is a note, not a failure — today's
    tolerance for a rung that refused for its own reason. Without *probe_for*
    a named variant is still sent as it was. `highest` walks `variant.ladder`
    of the listed variants with `probe_for(agent)` — a `try_one` for
    `variant.pick_variant` — and the agent gets the first rung that answers
    (`None` when only the plain request did); nothing answering is a failure
    line naming every rung and why. A model that lists no variants is not
    probed: `highest` of nothing is no variant. The same `provider/model` is
    probed once however many agents ask for it, and so is the same
    `provider/model@variant`.
    *notes* is one line per probed model for the operator. Without
    *probe_for*, `highest` on a model that lists variants is a failure: there
    is nothing to ask. A model refused for its credentials on every rung
    (`needs_login`) gets `login_hint(kilo_bin, …)` on its failure line.

    KC-70: *parallel* above 1 asks every probe the loop below would ask up
    front, that many at once and at most *per_provider* at once against one
    provider (0 is no provider cap) — `_prefetch`. The loop then reads the
    answers in roster order, so the agents, the failures and the notes are what
    the one-at-a-time walk gives, line for line; only the wall clock differs.
    """
    resolved = []
    failures: list = []
    notes: list = []
    picks: dict = {}
    probed: dict = {}
    noted: set = set()
    if probe_for is not None and int(parallel or 1) > 1:
        probed, picks = _prefetch(providers, agents, probe_for,
                                  parallel=int(parallel), per_provider=int(per_provider or 0))
    for agent in agents:
        wanted = agent.variant
        if not wanted:
            resolved.append(agent)
            continue
        listed = listed_variants(providers, agent.provider_id, agent.model_id)
        if wanted != HIGHEST:
            if wanted not in listed:
                # A refused key is listed as a failure; no probe is sent for it.
                failures.append(
                    f"[{agent.name}] {agent.model}: no variant '{wanted}' — listed: "
                    f"{', '.join(listed) if listed else '(none)'}")
                resolved.append(agent)
                continue
            # Probe a named variant once per provider/model, since a failed request counts against the quota (KC-61).
            if probe_for is not None:
                key = (agent.provider_id, agent.model_id, wanted)
                if key not in probed:
                    probed[key] = _probe_answer(probe_for, agent, wanted)
                answer = probed[key]
                if answer is None:
                    resolved.append(agent)      # hello came back, as today
                elif _is_quota(answer, quota_re):
                    # A dry key leaves that agent out, and the round runs one agent short.
                    notes.append(_quota_skip_line(agent, answer))
                else:
                    # today's tolerance: a refusal for its own reason is a note
                    notes.append(f"{agent.model}@{wanted}: the probe refused it — {answer}")
                    resolved.append(agent)
            else:
                resolved.append(agent)
            continue
        if not listed:
            resolved.append(replace(agent, variant=None))
            continue
        if probe_for is None:
            failures.append(f"[{agent.name}] {agent.model}: variant '{HIGHEST}' needs "
                            "GET /provider and a server to ask — neither is attached")
            resolved.append(agent)
            continue
        if agent.model not in picks:
            picks[agent.model] = pick_variant(ladder(listed), probe_for(agent))
        pick = picks[agent.model]
        if agent.model not in noted:
            noted.add(agent.model)
            notes.append(f"{agent.model}@{HIGHEST} → {pick.describe()}")
        if not pick.usable and pick.tried and all(
                _is_quota(reason, quota_re) for _rung, reason in pick.tried):
            # A dry key is left out like a named variant, since the model itself can talk (KC-61).
            notes.append(_quota_skip_line(agent, pick.tried[-1][1]))
            continue
        if not pick.usable:
            hint = login_hint(kilo_bin, agent.provider_id) if needs_login(pick) else ""
            failures.append(f"[{agent.name}] {agent.model}: no variant answered "
                            f"'say: hello' — {pick.describe()}{hint}")
            resolved.append(agent)
            continue
        resolved.append(replace(agent, variant=pick.variant))
    return tuple(resolved), failures, notes


def _prefetch(providers: dict, agents: tuple, probe_for, *, parallel: int,
              per_provider: int) -> tuple[dict, dict]:
    """KC-70: `(probed, picks)` — every probe `resolve_variants` would ask, asked
    in a pool of *parallel*.

    One job per `provider/model@variant` a named variant asks and per
    `provider/model` a `highest` walks — the same dedup as the loop, so no
    model is asked twice because two agents run it. A job skipped here (a
    variant that is not listed, a `highest` with no list) is the loop's to
    refuse. A `highest` ladder stays one job: its rungs go top-down, each only
    when the one above refused, so they cannot run side by side.

    *per_provider* caps the jobs one provider runs at once — a free tier counts
    requests per key, and the round's own agents are about to use the same
    key. The jobs are dealt round-robin over the providers, so a pool slot is
    rarely held by a job waiting for its provider's turn.
    """
    jobs: dict = {}
    for agent in agents:
        wanted = agent.variant
        if not wanted:
            continue
        listed = listed_variants(providers, agent.provider_id, agent.model_id)
        if wanted != HIGHEST:
            if wanted in listed:
                jobs.setdefault(("named", (agent.provider_id, agent.model_id, wanted)),
                                (agent, wanted, listed))
        elif listed:
            jobs.setdefault(("highest", agent.model), (agent, wanted, listed))
    probed: dict = {}
    picks: dict = {}
    if not jobs:
        return probed, picks

    by_provider: dict = {}
    for job, (agent, _wanted, _listed) in jobs.items():
        by_provider.setdefault(agent.provider_id, []).append(job)
    ordered = []
    queues = list(by_provider.values())
    while any(queues):
        for queue in queues:
            if queue:
                ordered.append(queue.pop(0))

    caps = {provider: threading.BoundedSemaphore(per_provider) if per_provider > 0 else None
            for provider in by_provider}

    def run(job):
        agent, wanted, listed = jobs[job]
        cap = caps[agent.provider_id]
        if cap is not None:
            cap.acquire()
        try:
            if job[0] == "named":
                return _probe_answer(probe_for, agent, wanted)
            return pick_variant(ladder(listed), probe_for(agent))
        finally:
            if cap is not None:
                cap.release()

    with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(ordered))),
                            thread_name_prefix="variant-probe") as pool:
        answers = list(pool.map(run, ordered))
    for (kind, key), answer in zip(ordered, answers):
        (probed if kind == "named" else picks)[key] = answer
    return probed, picks


def _quota_skip_line(agent, answer) -> str:
    """KC-61: the console line for an agent intake leaves out for its quota."""
    return f"[{agent.name}] {agent.model}: provider_quota — {answer} — not started"


def _probe_answer(probe_for, agent, wanted):
    """One `say: hello` at *wanted* for one agent; its text, or `None`.

    KC-61: the named-variant probe. A probe that raised answers `""` — which is
    no quota, so the agent is started with a note, exactly as a rung that
    refused for its own reason. Nothing here raises into intake.
    """
    try:
        answer = probe_for(agent)(wanted)
    except Exception:  # noqa: BLE001 — a broken probe is a refusal, not a round-killing one
        return ""
    return None if answer is None else str(answer)


def roster_on_offer(providers: dict, agents: tuple, *, kilo_bin: str | None = None) -> list:
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
                "has no credentials (not connected)" + login_hint(kilo_bin, agent.provider_id))
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


def roster_missing(providers: dict, agents) -> list:
    """KC-35: the agents Kilo will refuse from its own model list, in roster order.

    Exactly the third case of :func:`roster_on_offer`, split out so the remedy can
    name it: the provider is *known* (it is in the offer's `all`) and *connected*
    (it has credentials), but the model id is not in its `models`. An unknown
    provider and a provider without credentials are **not** missing — registering
    a model cannot create a provider or supply a key, so both stay
    `roster_on_offer` lines. Nothing is inferred from `status` or `capabilities`,
    and a provider's `name` is never treated as its id. Pure: no server, no roster.
    """
    by_id = {}
    for provider in providers.get("all") or []:
        if not isinstance(provider, dict):
            continue
        provider_id = provider.get("id")
        if isinstance(provider_id, str) and provider_id:
            by_id[provider_id] = provider
    connected = [item for item in (providers.get("connected") or []) if isinstance(item, str)]

    missing = []
    for agent in agents:
        provider = by_id.get(agent.provider_id)
        if provider is None or agent.provider_id not in connected:
            continue
        if agent.model_id in (provider.get("models") or {}):
            continue
        missing.append(agent)
    return missing


def registration_overlay(agents) -> dict:
    """KC-35: the `KILO_CONFIG_CONTENT` body that adds *agents*' models to Kilo's list.

    ``{"provider": {<pid>: {"models": {<mid>: {"name": <mid>, "reasoning": True}}}}}``,
    grouped by provider id, ids in roster order, ``{}`` for an empty list.
    `reasoning: True` mirrors the existing `kenary` / `sensenova123` entries and is
    what `kilo models` reported `capabilities.reasoning: true` for. The caller
    merges it with the operator's own `KILO_CONFIG_CONTENT` — this function never
    reads or writes `kilo.jsonc`.
    """
    models_by_provider: dict = {}
    for agent in agents:
        models_by_provider.setdefault(agent.provider_id, []).append(agent.model_id)
    providers = {}
    for provider_id, model_ids in models_by_provider.items():
        models = {model_id: {"name": model_id, "reasoning": True} for model_id in model_ids}
        providers[provider_id] = {"models": models}
    return {} if not providers else {"provider": providers}


def _additive_merge(base: dict, overlay: dict) -> dict:
    """*base* with *overlay*'s keys added: dicts merge, a leaf is only ever added.

    Additive rather than replacing, so an operator's own `name`, `options.baseURL`,
    `apiKey` and already-listed models survive the merge untouched — the overlay
    registers what is absent, and says nothing about what is there. Neither input
    is mutated.
    """
    merged = {key: copy.deepcopy(value) for key, value in (base or {}).items()}
    for key, value in (overlay or {}).items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _additive_merge(current, value)
        elif key not in merged:
            merged[key] = copy.deepcopy(value)
    return merged


def merge_config_content(existing: str | None, overlay: dict) -> str:
    """KC-35: the JSON string for `KILO_CONFIG_CONTENT`, *overlay* merged over *existing*.

    An unset or empty *existing* gives the overlay alone; a JSON object is deep-merged
    additively, so it keeps its own providers, `options.baseURL`, `apiKey` and models
    and gains the overlay's models; anything else — a JSON array, a bare string, or
    text that is not JSON at all — is a `ValueError` whose message intake prints as
    `intake: KILO_CONFIG_CONTENT is not a JSON object: …`. A malformed value is a
    refusal, never a crash: without it the round would run with no overlay and find
    out ten seconds in, the way it did before this check.
    """
    if not existing:
        return json.dumps(overlay)
    try:
        parsed = json.loads(existing)
    except ValueError as exc:
        raise ValueError(f"KILO_CONFIG_CONTENT is not a JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"KILO_CONFIG_CONTENT is not a JSON object: "
                         f"expected a JSON object, got {type(parsed).__name__}")
    return json.dumps(_additive_merge(parsed, overlay))


def _missing_hint_lines(missing) -> list:
    """KC-35: one `hint:` line per provider whose models the roster wants but Kilo lacks.

    Printed after `roster_on_offer`'s own lines, once per provider — `roster_missing`
    groups by provider, and one line naming one `<pid>` is what an operator can act on.
    It says the id is refused by Kilo, not by the provider, and that "did you mean"
    compares spelling only, so the two remedies — the entry to add to `kilo.jsonc`, or
    `--register-missing` — are both on the line.
    """
    counts: dict = {}
    order: list = []
    for agent in missing:
        if agent.provider_id not in counts:
            order.append(agent.provider_id)
            counts[agent.provider_id] = 0
        counts[agent.provider_id] += 1
    lines = []
    for provider_id in order:
        lines.append(
            f"hint: {counts[provider_id]} id(s) are not in Kilo's model list for provider "
            f"'{provider_id}'. "
            '"did you mean" compares spelling only — if the provider serves the id, add it '
            f"under provider.{provider_id}.models in kilo.jsonc, or re-run with --register-missing."
        )
    return lines


def _roster_path(repo, roster: str) -> Path:
    """`--roster` as a path: absolute as given, else relative to the CWD.

    KC-76: the CWD is where the operator stands and the repo is the `--target`,
    so a relative roster is "my contest.ini", not "the target's". Without
    `--target` the repo is the CWD and nothing changes. *repo* stays in the
    signature for the callers; it no longer takes part in the resolution.
    """
    path = Path(roster)
    return path if path.is_absolute() else Path.cwd() / path


#: Scripts the agents' prompt runs in their clone of the target; a base without them fails at once (KC-76).
_TARGET_FILES = ("scripts/next_task.py", "scripts/append_task.py")


def _target_repo(target) -> Path:
    """The repo the round runs on: `--target` resolved, else the CWD (KC-76)."""
    return Path(target).expanduser().resolve() if target else Path.cwd().resolve()


def _target_failures(repo, base_ref: str) -> list:
    """Intake lines for a `--target` whose base tree cannot host a round.

    The repo must be a git repo whose *base_ref* holds the two scripts the
    prompt runs and an `epic-tasks/` (KC-76 §8). An unresolvable base is
    skipped: `intake` reports it in its own words.
    """
    repo = Path(repo)
    if not repo.is_dir():
        return [f"--target {repo} is not a directory"]
    if not gates.git(str(repo), "rev-parse", "--is-inside-work-tree"):
        return [f"--target {repo} is not a git repository"]
    if not gates.git(str(repo), "rev-parse", "--verify", f"{base_ref}^{{commit}}"):
        return []
    missing = []
    for rel in (*_TARGET_FILES, "epic-tasks"):
        if not _tree_names(repo, base_ref, rel):
            missing.append(rel)
    if not missing:
        return []
    return [f"--target {repo}: {base_ref} lacks {', '.join(missing)} — the agents run "
            "scripts/next_task.py and scripts/append_task.py in their clone and read "
            "epic-tasks/ there; copy the scripts from jan-auto-agent and commit them"]


def _round_out_dir(repo, config: ContestConfig, round_no: int) -> Path:
    """`<config.out_dir>/<NN>` relative to the repo, e.g. `contest-out/55` —
    zero-padded like the tickets and the worktrees (`rounds/NN-<agent>`)."""
    return repo / config.out_dir / f"{round_no:02d}"


# Intake

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
    # shell-quoted, so a brief with a space or a parenthesis pastes back as one argument
    return "python3 -m tools.contest " + shlex.join(words)


def _park_line(body, name, number: int, rel_dir) -> str:
    r"""The `sed` and the `git commit` that park one ticket in this checkout.

    An uncommitted `epic-tasks/` is refused by `prepare_round` and an
    uncommitted edit is invisible to the sessions, so the park is a `sed` and a
    commit of that one file. The `**Status:**` line is addressed by its number
    in *body*, not by a hard-coded 3. Built by concatenation: a 3.10 f-string
    cannot hold the `\*` the sed expression needs.
    """
    file_ref = rel_dir + "/" + name
    match = _STATUS_RE.search(body)
    line_no = body[:match.start()].count("\n") + 1 if match else 0
    if line_no:
        sed = "'" + str(line_no) + "s/^\\*\\*Status:\\*\\*.*$/**Status:** queued/'"
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
    #: the roster with every `highest` resolved (KC-49); empty = the config's as is
    agents: tuple = ()

    #: The KILO_CONFIG_CONTENT for a --register-missing round: the operator's value plus the missing models (KC-35).
    config_content: str | None = None

    #: the `provider/model` ids that registration put on offer this round
    registered: tuple = ()


def _without_highest(agents: tuple) -> tuple:
    """*agents* with every unresolved `highest` sent as no variant."""
    return tuple(replace(agent, variant=None) if agent.variant == HIGHEST else agent
                 for agent in agents)


def _host_of(url) -> str:
    """The host of a base URL — the only part of it a round may print.

    KC-55 compares the gate's `base_url` with a provider's `options.baseURL` by
    host, not by path, so `https://kenari.id/v1` and `https://kenari.id/v1/chat`
    are the same endpoint; a malformed or empty URL is no host, not an exception.
    """
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        return urlsplit(url.strip()).hostname or ""
    except ValueError:
        return ""


def _seconds(value) -> str:
    """A number of seconds the way an operator reads it: `660`, not `660.0`."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _gate_base_url(config) -> str:
    """The gate's `base_url`, or `""` when the round has no gate."""
    settings = config.gate_settings
    if settings is None:
        return ""
    return str(getattr(settings, "base_url", "") or "")


def gate_share_line(providers, agents, gate_base_url) -> str:
    """KC-55 §5: the `gate: shares <host> with N agents (…) — …` warning.

    The round 66 root cause, and the reason this compares hosts rather than
    model ids: the gate was a different *model* from the agents but sat on the
    same *key and endpoint* as eight of them, so one quota of 15 requests a
    window was spent by eight agents plus the gate and the gate's 429 became a
    `reject`. A provider's `options.baseURL` is read by host, so a gate on
    `kenary/mistral-medium-3-5:free` next to eight other `kenary` agents is
    caught where a `model_id` comparison would miss it.

    ``""`` when nothing matches: a provider with no `options.baseURL` does not
    match, nor does a gate with no host. Names at most four agents, then `…`,
    and never the key. A warning, never a refusal — the operator may hold a
    paid key on that host.
    """
    host = _host_of(gate_base_url)
    if not host:
        return ""
    by_id = {}
    for provider in (providers or {}).get("all") or []:
        if isinstance(provider, dict) and isinstance(provider.get("id"), str):
            by_id[provider["id"]] = provider
    names = []
    for agent in agents or ():
        provider = by_id.get(getattr(agent, "provider_id", ""))
        options = provider.get("options") if isinstance(provider, dict) else None
        if not isinstance(options, dict):
            continue
        if _host_of(options.get("baseURL")) != host:
            continue
        name = str(getattr(agent, "name", "") or "").strip()
        if name:
            names.append(name)
    if not names:
        return ""
    shown = ", ".join(names[:4]) + (" …" if len(names) > 4 else "")
    word = "agent" if len(names) == 1 else "agents"
    return ("gate: shares " + host + " with " + str(len(names)) + " " + word + " ("
            + shown + ") — a rate limit there hits the gate too; point "
            + "[contest_gate_llm] in " + LOCAL_FILENAME + " at another endpoint")


def gate_model_refusals(config, agents) -> list:
    """KC-37 §2: the intake lines refusing a gate model the roster runs, or `[]`.

    Rounds 64, 74 and 86: the gate was `hy3:free` and `hy3` was an agent, so
    every ask landed on an endpoint the round itself was saturating and came
    back `gate-failed`. KC-55's shared-host line only warns; a gate that *is* a
    competitor is refused.

    Matched on the model, never the agent name, and every matching agent is
    named (KC-34 §7 puts `hy3-var1` and `hy3-var2` on one model). The gate's
    `model` is what its endpoint calls the model, so it matches an agent's
    `model_id` — `nex-agi/nex-n2.5-pro:free` is `kilo/nex-agi/nex-n2.5-pro:free`
    — or its whole `provider/model`; `openrouter/hy3:free` is neither for
    `kenary/hy3:free`, so those two do not collide.

    `[]` with no gate (`--no-gate`) and for the committed placeholder, which
    has its own complaint.
    """
    settings = config.gate_settings
    model = getattr(settings, "model", "") if settings is not None else ""
    model = model.strip() if isinstance(model, str) else ""
    if not model or model == GATE_PLACEHOLDER_MODEL:
        return []
    names = [agent.name for agent in agents if model in (agent.model_id, agent.model)]
    if not names:
        return []
    shown = names[0] + (f" (and {', '.join(names[1:])})" if len(names) > 1 else "")
    return [
        f"gate model {model} is also agent {shown} in this round",
        "  the gate must be a second, independent model: set [contest_gate_llm] model",
        f"  in {LOCAL_FILENAME} to something the roster does not run, or pass --no-gate",
    ]


def _gate_probe(config) -> tuple:
    """KC-55 §5: `(refusals, warnings)` from one call to the gate before the round.

    Nine recorded rounds learned the gate was unusable from `gate-failed`
    rejects — 48 of them in five rounds, because the endpoint refused the key or
    held no such model and the round found out one permission at a time. One
    attempt, no retries, `GATE_TIMEOUT`, through `Policy`'s own
    `completion_fn`, so a test stubs it the way it stubs the gate. It is not one
    of any agent's `gate_max_calls_per_session` calls: no session exists yet.

    A 401 or a 403 refuses the round and names `contest.local.ini`; a 404
    refuses it naming the model and the host. Anything else only warns and the
    round starts — a 429 or a 5xx is transient and is exactly what a real ask
    retries, and so are a timeout, a dropped connection and an unparsable
    reply. A reply that carries a verdict is a healthy gate and says nothing.
    """
    settings = config.gate_settings
    model = getattr(settings, "model", "") if settings is not None else ""
    model = model.strip() if isinstance(model, str) else ""
    if not model or model == GATE_PLACEHOLDER_MODEL:
        return (), ()
    try:
        reply = Policy(config).probe_gate()
    except Exception as exc:
        status = gate_error_name(exc)
        if status in ("HTTP 401", "HTTP 403"):
            return ([f"gate [contest_gate_llm] refused its key ({status}) — set api_key "
                     f"in {LOCAL_FILENAME}"], ())
        if status == "HTTP 404":
            host = _host_of(_gate_base_url(config)) or "its endpoint"
            return ([f"gate model '{model}' not found at {host} ({status}) — set model "
                     f"in [contest_gate_llm]"], ())
        return ((), (f"gate: {status} on the intake probe — the round starts anyway; "
                     f"the gate retries that itself",))
    if gate_verdict(reply):
        return (), ()
    return ((), ("gate: the intake probe came back unparsable — the round starts "
                 "anyway; the gate asks it twice itself",))


def _offer_failures(repo, config: ContestConfig, attached) -> list:
    """`_check_offer`'s failures alone — its first KC-25 shape, kept for callers."""
    return _check_offer(repo, config, attached).failures


def _context_limits(providers) -> dict:
    """KC-56: ``{"provider/model": limit.context}`` for every model *providers* offers.

    *providers* is ``GET /provider`` as ``KiloClient.providers`` hands it over
    — the read intake already makes; `_with_context_limits` then puts each
    agent's entry on its ``AgentSpec.context_limit``. A model with no positive
    integer ``limit.context`` is left out, so its limit is unknown, never 0: an
    unknown limit makes every cut-off an output one (`runner._cut_off`). Pure and
    total — a malformed offer is an empty dict, not an error.
    """
    limits: dict = {}
    if not isinstance(providers, dict):
        return limits
    for provider in providers.get("all") or []:
        if not isinstance(provider, dict) or not isinstance(provider.get("id"), str):
            continue
        models = provider.get("models")
        if not isinstance(models, dict):
            continue
        for model_id, model in models.items():
            # Keyed by the models key, as roster_missing reads the offer and AgentSpec.model_id names it.
            limit = model.get("limit") if isinstance(model, dict) else None
            context = limit.get("context") if isinstance(limit, dict) else None
            if isinstance(context, int) and not isinstance(context, bool) and context > 0:
                limits[f"{provider['id']}/{model_id}"] = context
    return limits


def _with_context_limits(agents: tuple, limits: dict) -> tuple:
    """KC-56: *agents* with each one's ``context_limit`` from *limits*.

    *limits* is `_context_limits`. An agent whose ``provider/model`` has no
    entry keeps the limit it has (``None`` from the roster), so a model the
    offer gives no ``limit.context`` stays unknown.
    """
    return tuple(replace(agent, context_limit=limits[agent.model])
                 if agent.model in limits else agent for agent in agents)


@dataclass(frozen=True)
class _Offer:
    """What one `GET /provider` read is worth to intake.

    *failures* are the printed `intake:` lines; *agents* the roster with its
    `highest` resolved (KC-49); *notes* one line per probed variant; *warnings*
    the `gate:` lines. KC-35 adds the two registration fields: *config_content*
    is the `KILO_CONFIG_CONTENT` the round's own server must be spawned with, and
    *registered* the `provider/model` ids that put it there — both empty when no
    model needed registering.
    """

    failures: list
    agents: tuple
    notes: list
    warnings: list
    config_content: str | None = None
    registered: tuple = ()


def _offer_server(kilo_bin: str | None, env: dict | None = None) -> tuple:
    """KC-35: one throwaway `kilo serve`, or `(None, log_path)` — never an error.

    The check asks one question and must not fail louder than the round it
    guards, so whatever stops the child from starting is swallowed here, the way
    `_check_offer`'s old inline `except Exception` did. *env* goes to
    `KiloServer.spawn`'s own `env=` and is added on top of `os.environ` for the
    child; `None` spawns exactly as today — no `env=` at all, so a caller's own
    keyword-only signature stays unchanged.
    """
    fd, log_path = tempfile.mkstemp(prefix="kilo-offer-", text=True)
    os.close(fd)
    kwargs = {}
    if env:
        kwargs["env"] = env
    try:
        server = KiloServer.spawn(find_kilo_binary(kilo_bin), log_path=log_path, **kwargs)
    except Exception:
        return None, log_path
    return server, log_path


def _check_offer(repo, config: ContestConfig, attached, *, resolve: bool = True,
                 register_missing: bool = False,
                 probe_cache: dict | None = None,
                 probe_cache_path: str | None = None,
                 reprobe: bool = False,
                 allow_unprobed: bool = False) -> _Offer:
    """The roster's `provider/model` pairs checked against `GET /provider`, then
    its variants resolved there (KC-49), then the gate's two intake checks
    (KC-55), returning an `_Offer`.

    *agents* is the roster with every `highest` replaced by the variant that
    answered, or `config.agents` as is when nothing was resolved; *notes* is
    one line per probed model; *warnings* are the `gate:` lines the round
    may warn on and still start — the gate sharing an endpoint with the roster,
    and a probe that hit a transient error. A probe that refuses the round goes
    into *failures* instead, so it is printed as every other refusal.

    With no offer to read, `highest` becomes no variant: it is the round's
    default, and a server that did not start is reported by the round itself.
    `resolve=False` stops after the pair check: intake passes it when the round
    is already refused, so a refused round spends no model call — the variant
    probes and the gate probe alike.

    With `server = spawn` no server is attached yet, so a throwaway one is
    started for the call alone and removed with its log — `cmd_run` starts its
    own afterwards, and leaving that order alone is the point, so the second
    spawn is the price. Whatever stops that throwaway from starting is caught
    wide on purpose and not reported here: the check asks one question, and it
    must not fail louder than the round it guards — the round's own `server:`
    line reports the same word. With a URL the attached server answers and
    nothing is spawned. A failure of the call itself is one line, so the check
    never crashes intake.

    KC-35: when a model is missing from the provider's own list, the hint line
    is printed after the KC-25 lines unless *register_missing* registered it
    first. That flag needs a spawn: it builds `merge_config_content` over the
    operator's own `KILO_CONFIG_CONTENT`, spawns a **second** throwaway with
    `env={"KILO_CONFIG_CONTENT": …}`, and requires the models to be on offer
    there — `registration did not take: <ids>` when they are not, with no
    worktree created. The string is handed back on the `_Offer` so `cmd_run`
    gives the round's real server the same one.
    """
    agents = config.agents
    # Without an offer, highest quietly becomes no variant, and the server line says why.
    unresolved = _without_highest(agents)
    # KC-35: the overlay the registered round runs on, and the ids it got
    content: str | None = None
    registered: tuple = ()
    spawned: list = []
    logs: list = []
    server = attached
    try:
        if server is None and config.server == "spawn":
            server, log_path = _offer_server(config.kilo_bin)
            logs.append(log_path)
            if server is not None:
                spawned.append(server)
        if server is None:
            return _Offer([], unresolved, [], [])
        try:
            kilo_bin = find_kilo_binary(config.kilo_bin)
        except (FileNotFoundError, OSError):
            kilo_bin = "kilo"
        providers = KiloClient(server, str(repo)).providers()
        missing = roster_missing(providers, agents)

        # The attached server cannot be changed here, so the flag refuses; other agents still get their check (KC-25).
        if missing and register_missing and attached is not None:
            others = tuple(agent for agent in agents if agent not in missing)
            return _Offer(
                ["--register-missing needs server = spawn: an attached server reads its own "
                 "kilo.jsonc"]
                + roster_on_offer(providers, others, kilo_bin=kilo_bin),
                unresolved, [], [])

        if missing and register_missing and resolve:
            try:
                content = merge_config_content(os.environ.get("KILO_CONFIG_CONTENT"),
                                               registration_overlay(missing))
            except ValueError as exc:
                return _Offer([str(exc)], unresolved, [], [])
            second, second_log = _offer_server(config.kilo_bin,
                                                env={"KILO_CONFIG_CONTENT": content})
            logs.append(second_log)
            if second is None:
                # No registration happened, so the line says the flag did not take effect.
                return _Offer([f"registration did not take: "
                               f"{', '.join(agent.model for agent in missing)} — "
                               "kilo serve with KILO_CONFIG_CONTENT did not start"],
                              unresolved, [], [])
            else:
                server = second
                spawned.append(second)
                try:
                    providers = KiloClient(server, str(repo)).providers()
                except (KiloHttpError, KiloServerError, ValueError) as exc:
                    return _Offer([f"GET /provider failed: {exc}"], unresolved, [], [])
                still = roster_missing(providers, missing)
                if still:
                    return _Offer([f"registration did not take: "
                                   f"{', '.join(agent.model for agent in still)}"],
                                  unresolved, [], [])
                registered = tuple(agent.model for agent in missing)
                missing = ()
        elif missing and register_missing:
            # Nothing is registered for agents refused on other grounds, and no flag hint is given for them.
            others = tuple(agent for agent in agents if agent not in missing)
            return _Offer(roster_on_offer(providers, others, kilo_bin=kilo_bin),
                          unresolved, [], [])

        failures = roster_on_offer(providers, agents, kilo_bin=kilo_bin)
        if missing:
            failures.extend(_missing_hint_lines(missing))
        if failures or not resolve:
            return _Offer(failures, unresolved, [], [])

        # The gate's two checks need no session: the share check on the offer, and one probe call (KC-55).
        warnings = []
        share = gate_share_line(providers, agents, _gate_base_url(config))
        if share:
            warnings.append(share)
        refusals, probe_warnings = _gate_probe(config)
        warnings.extend(probe_warnings)
        if refusals:
            return _Offer(list(refusals), unresolved, [], warnings)

        # The probe uses the round's quota rule, so a dry key answers in about a second (KC-61).
        max_retry_wait = float(getattr(config, "provider_retry_max_wait_sec", 0) or 0)
        quota_re = _quota_re(config)
        probe_kwargs = {}
        if max_retry_wait > 0:
            probe_kwargs["max_retry_wait"] = max_retry_wait
        if quota_re is not None:
            probe_kwargs["quota_re"] = quota_re

        # Cache logic around the live probe: a fresh cached winner is used, a stale or failed one is probed (KC-11).
        _cache = probe_cache if probe_cache is not None else {}
        # probe_ttl_days = 0 means always re-probe, so the zero is kept, not replaced by the default.
        try:
            _ttl = float(getattr(config, "probe_ttl_days", 7))
        except (TypeError, ValueError):
            _ttl = 7.0
        import time as _time

        # Probes run side by side (probe_parallel), so the cache and memory are written from threads (KC-70).
        _lock = threading.Lock()

        def _save_cache():
            if probe_cache_path:
                save_probe_cache(probe_cache_path, _cache)

        # A named variant that answered recently is not asked again, unless --reprobe (KC-70).
        _memory_hours = probe_memory.hours_of(config)
        _memory_path = probe_memory.memory_path(config, repo)
        _memory = ([] if reprobe or _memory_hours <= 0
                   else probe_memory.load(_memory_path, hours=_memory_hours))
        _answered: list = []
        _refused: list = []
        _retries = int(getattr(config, "probe_retries", 0) or 0)
        _retry_wait = float(getattr(config, "probe_retry_wait_sec", 0) or 0)

        def probe_for(agent):
            # A refusal that a second ask can pass is retried probe_retries times first (KC-70).
            base_try_one = with_retries(
                hello_probe(server, agent.provider_id, agent.model_id, **probe_kwargs),
                retries=_retries, wait_sec=_retry_wait, quota_re=quota_re,
                label=agent.model)
            # Only highest agents use the cache check; a named variant takes the single-rung path (KC-70).
            if agent.variant != HIGHEST:
                key3 = (agent.provider_id, agent.model_id, agent.variant)
                if probe_memory.answered(_memory, *key3):
                    return lambda variant: None

                def _remembering(variant):
                    reason = base_try_one(variant)
                    with _lock:
                        (_answered if reason is None else _refused).append(
                            (agent.provider_id, agent.model_id, variant))
                    return reason

                return _remembering

            key = f"{agent.provider_id}/{agent.model_id}"
            rungs = ladder(listed_variants(providers, agent.provider_id, agent.model_id))
            entry = _cache.get(key) if not reprobe else None
            if isinstance(entry, dict) and entry.get("usable", True):
                try:
                    probed_at = float(entry.get("probed_at", 0))
                except (TypeError, ValueError):
                    probed_at = 0.0
                age_days = (_time.time() - probed_at) / 86400.0
                cached_variant = entry.get("variant")
                # No kilo version is known here, so the age and the current variant list decide.
                if 0 <= age_days < _ttl and cached_variant in rungs:
                    tried_map = {t[0]: t[1] for t in (entry.get("tried") or [])
                                 if isinstance(t, (list, tuple)) and len(t) == 2}

                    def _cached(variant, _cv=cached_variant, _tm=tried_map):
                        if variant == _cv:
                            return None
                        return _tm.get(variant, "probe failed (cached)")

                    return _cached

            # Live probe: the result is saved on the first success and dropped after the last rung fails.
            tried_box: list = []

            def _caching(variant):
                reason = base_try_one(variant)
                with _lock:
                    _record(variant, reason)
                return reason

            def _record(variant, reason):
                if reason is None:
                    _cache[key] = {
                        "variant": variant,
                        "usable": True,
                        "probed_at": _time.time(),
                        "kilo_version": "",
                        "tried": [[r, rs] for r, rs in tried_box],
                        "reasoning_tokens": 0,
                    }
                    _save_cache()
                else:
                    tried_box.append([variant, reason])
                    if variant == rungs[-1] and _cache.pop(key, None) is not None:
                        _save_cache()

            return _caching

        resolved, failures, notes = resolve_variants(
            providers, agents, probe_for, kilo_bin=kilo_bin, quota_re=quota_re,
            parallel=int(getattr(config, "probe_parallel", 1) or 1),
            per_provider=int(getattr(config, "probe_per_provider", 0) or 0))
        if _memory_hours > 0:
            probe_memory.update(_memory_path, _answered, _refused, hours=_memory_hours)
        # The same read gives limit.context, so a full window is told from a spent output budget (KC-56).
        if allow_unprobed:
            # --allow-unprobed starts an agent with no variant instead of refusing the round (KC-11).
            kept = []
            for line in failures:
                if ": no variant answered 'say: hello'" in line:
                    notes.append(f"{line} — started with no variant (--allow-unprobed)")
                else:
                    kept.append(line)
            failures = kept
            resolved = tuple(replace(agent, variant=None) if agent.variant == HIGHEST
                             else agent for agent in resolved)
        resolved = _with_context_limits(resolved, _context_limits(providers))
        if agents and not resolved and not failures:
            # KC-61: every agent was left out for its quota — nothing to start
            failures.append("every agent is out of quota — nothing to start "
                            "(the variant: lines above name each one)")
        return _Offer(failures, resolved, notes, warnings,
                      config_content=content, registered=registered)
    except (KiloHttpError, KiloServerError, ValueError) as exc:
        return _Offer([f"GET /provider failed: {exc}"], unresolved, [], [])
    finally:
        for own_server in spawned:
            own_server.close()
        for log_path in logs:
            try:
                os.unlink(log_path)
            except OSError:
                pass


def intake(repo, tasks_dir, round_no, base_ref, config, argv=None,
           register_missing: bool = False,
           reprobe: bool = False,
           allow_unprobed: bool = False,
           roster_path: str | None = None,
           dry_run: bool = False):
    """Run every pre-round check; return the `Intake`, or `None` with the failures printed.

    All checks run and every failure goes to stderr on its own line before
    anything is created, in this order: the base resolves and `epic-tasks/` is
    clean at it — `workspace.prepare_round` with an empty roster runs exactly
    KC-4's check and builds nothing; the ticket exists and its `**Status:**`
    first word is `open`; no lower-numbered ticket is still on offer, and each
    of those is named with the two ways past it, because the runner's prompt
    does not name a ticket and `scripts/next_task.py` would hand that one to
    the session; the gate's worst case fits the silence clock it holds —
    `policy.gate_worst_case_sec` against `idle_event_timeout_sec` (KC-55 §3);
    the gate's model is not one the roster runs (KC-37 §2);
    the server answers — the `kilo` binary resolves when the
    roster says `spawn`, else `KiloServer.attach` reaches the URL; and the
    roster's `provider/model` pairs are on offer — `KiloClient.providers`
    against `roster_on_offer`, so a display name spelled as an id, a provider
    with no credentials, and a model that is not there are all refused here
    instead of on the first turn (KC-25). With `--register-missing`, a model
    that is known, connected and simply not on the offer is registered for the
    round alone through `KILO_CONFIG_CONTENT` instead of refused: the intake
    proves it took in a second throwaway server and hands the string to
    `cmd_run` as `Intake.config_content` (KC-35).
    `Intake.out_dir` is the default `<out_dir>/<NN>`; `--out` replaces it in
    `cmd_run`.

    Every status read is from the base tree, not from this checkout: the
    sessions read `epic-tasks/` from the worktree built at the base, so the two
    only agree on a word when the base is HEAD. A base that does not resolve
    yields no tree to read, so the checkout stands in for the ticket checks and
    the `WorkspaceError` line is the base's own failure. *argv* is this
    invocation's, for the printed `run` line; `None` means `sys.argv[1:]`.

    KC-7: the ticket must also declare `**File:**` and `**Symbol:**` — one
    `intake:` line per missing label, read from the base tree like the status.
    `scripts/next_task.py` builds the finding it prints under `code to fix` from
    those two, so a ticket without one would hand the sessions no code to fix.

    With *dry_run* the checks that need a server are skipped instead of run — the
    offer check, the variant probe and the gate probe, one `dry-run: skipped …`
    line each, so the plan the caller prints does not read as fully checked. No
    `kilo serve` is started then, intake's throwaway offer server included, and
    nothing is asked of the gate. The pure checks still run: the base, the
    tickets, the gate's worst case against the silence clock and the gate model
    against the roster, so a round that would be refused is refused here too.
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
    # Without a base, the checkout stands in for the base's reads (see the line above).
    at = base_sha or None

    found = gates.ticket_for_round(tasks_dir, round_no)
    name = found[0]
    ticket_path = tasks_dir / name if name else None
    if not name and at:
        # AR-3: a ticket only at the base — `ticket_file`'s temp copy stands in
        name, ticket_path = ticket_file(repo, tasks_dir, round_no, at)
        if ticket_path is not None:
            body = ticket_path.read_text(encoding="utf-8")
            found = (name, body.splitlines()[0].lstrip("# ").strip() if body else "", [])
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
        # A ticket with no File or Symbol label is refused, since the finding needs both (KC-7).
        for label in _missing_labels(body):
            failures.append(
                f"{name} has no **{label}:** line — scripts/next_task.py reads "
                "**File:** and **Symbol:** to name the code the sessions fix"
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

    # The gate's worst case must fit inside the silence clock, or the agent is marked STALLED (KC-55).
    worst_case = gate_worst_case_sec(config)
    idle = config.idle_event_timeout_sec
    if config.gate_settings is not None and isinstance(idle, (int, float)) \
            and not isinstance(idle, bool) and idle > 0 and worst_case >= idle:
        failures.append(
            f"gate retries can outlast the silence clock: worst case "
            f"{_seconds(worst_case)} s ≥ idle_event_timeout_sec {_seconds(idle)} — "
            f"lower gate_deadline_sec in contest.ini, or raise idle_event_timeout_sec")

    # The gate is checked before the offer check, so a refused round uses no model call (KC-37).
    failures.extend(gate_model_refusals(config, config.agents))

    # an openrouter round has no offer to probe: `highest` is no variant there
    agents = _without_highest(config.agents)
    offer = None
    attached = None
    if dry_run:
        # Dry run skips every check that needs a server and says so, so the plan is not read as checked (KC-7).
        print("dry-run: skipped the offer check — no server was started")
        print("dry-run: skipped the variant probe — no session was opened")
        print("dry-run: skipped the gate probe — the gate was not asked")
        offer = _Offer([], agents, [], [])
    elif config.backend == "kilo":
        # An openrouter round has no Kilo server, so there is nothing to resolve or attach to.
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

        # The variant probe costs model calls, so it runs last, after every other check (KC-11).
        _probe_cache_path: str | None = None
        _probe_cache: dict | None = None
        if roster_path:
            _probe_cache_path = str(Path(roster_path).parent / PROBE_CACHE_FILE)
            _probe_cache = load_probe_cache(_probe_cache_path)
        offer = _check_offer(repo, config, attached, resolve=not failures,
                             register_missing=register_missing,
                             probe_cache=_probe_cache,
                             probe_cache_path=_probe_cache_path,
                             reprobe=reprobe,
                             allow_unprobed=allow_unprobed)
        failures.extend(offer.failures)
        for note in offer.notes:
            print(f"variant: {note}")
        for line in offer.warnings:
            print(line)

    if offer is None:
        # No offer was read (openrouter, or a failed backend), so nothing was resolved or registered.
        offer = _Offer([], agents, [], [])

    if failures:
        for line in failures:
            print(f"intake: {line}", file=sys.stderr)
        return None

    return Intake(
        ticket_path=ticket_path,
        title=title,
        base_sha=base_sha,
        out_dir=_round_out_dir(repo, config, round_no),
        agents=offer.agents,
        config_content=offer.config_content,
        registered=offer.registered,
    )


# Patches

#: Terminal states whose worktree diff is exported when the turn ended with no commit (KC-31, KC-41).
_DIFF_STATES = (AgentState.STALLED, AgentState.ERROR)

#: Comment line that names the untracked files, since git diff does not show them (KC-31).
_UNTRACKED_NOTE = "# untracked (not inlined): "

#: The orchestrator's own runs/ rows, which are not agent work and never open a diff (KC-31).
_RUNS_PREFIX = "runs/"


def _git_out(ws, *args: str) -> str | None:
    """One read-only git call in *ws*'s worktree, or `None` when git did not run.

    Read-only subcommands only — `--no-optional-locks` so a sample never takes
    `index.lock` from under a neighbour agent's `git commit`. `None` is this
    ticket's fail-open edge: an absent worktree, a path that is not a
    repository, a held index that the ladder gave up on and a timeout all read
    the same as "no collect data", never as an exception into a round.
    """
    try:
        proc = run_git(["git", "--no-optional-locks", *args], cwd=str(ws.path))
    except Exception:  # noqa: BLE001 — a tree that cannot be read is no data
        return None
    return proc.stdout if proc.returncode == 0 else None


def _uncommitted_diff_body(ws) -> str | None:
    """KC-31: the body of *ws*'s `.diff`, or `None` when there is no diff to write.

    The work has to fail both of KC-21's gates to land here: `git rev-list
    --count <base_sha>..HEAD` is `0`, so the branch holds nothing to format, and
    `git status --porcelain --untracked-files=all` is not empty, so the turn left
    something behind. The body is `git diff <base_sha>` — the worktree against
    the base, tracked files only — with the untracked paths named in a trailing
    comment instead of inlined, because a path that is not in the index has no
    hunk. `runs/` paths are the orchestrator's, not the agent's, and are dropped
    before the emptiness test so a stale `PROGRESS.csv` alone opens no file.

    `None` in every other case: a branch with a commit under it (that is
    KC-21's `.patch`, or KC-41's deadline commit), a clean tree (nothing to
    read, as today), a worktree without a base to diff against, and a status
    that git could not run. A worktree whose tracked diff is empty but which
    holds untracked files still writes a body — the comment is the only view it
    has.
    """
    base = str(getattr(ws, "base_sha", "") or "")
    if not base:
        return None
    count = _git_out(ws, "rev-list", "--count", f"{base}..HEAD")
    if count is None or count.strip() != "0":
        return None
    porcelain = _git_out(ws, "status", "--porcelain", "--untracked-files=all")
    if porcelain is None or not porcelain.strip():
        return None
    untracked = []
    for line in porcelain.splitlines():
        if not line.strip() or len(line) <= 3:
            continue
        path = line[3:].rsplit(" -> ", 1)[-1].strip()
        if not path or path.startswith(_RUNS_PREFIX):
            continue
        if line[:2] == "??":
            untracked.append(path)
    diff = (_git_out(ws, "diff", base) or "").strip()
    if not diff and not untracked:
        return None
    body = diff
    if untracked:
        if body:
            body += "\n"
        else:
            body = "\n"
        body += _UNTRACKED_NOTE + " ".join(sorted(untracked))
    return body


def export_patches(state: RoundState, workspaces: list, out_dir) -> list:
    """One `.patch` per agent whose `run.commit` is set, then the `.diff`s.

    `git format-patch --stdout <base_sha>..HEAD` per worktree — the patch the
    operator then applies by hand (`docs/collect-epics/
    RUN-THE-EPIC-COMPETITION.md` stages 3–5). A `GAVE_UP` keeps its patch too,
    named `<agent>.GAVE_UP.patch`, because the operator still wants to read
    those — as does a `STALLED` or `ERROR` whose branch has a commit on it (KC-21
    harvests it, so `run.commit` is set), named `<agent>.STALLED.patch` and
    `<agent>.ERROR.patch`: the terminal state is the file name. An empty diff
    writes no file and says so: a READY without a commit cannot happen, the
    harvest sets it, so there is no fake sha to fall back on.

    KC-31: a second loop over the agents the first one skipped for having no
    `run.commit` — a `STALLED` or `ERROR` whose turn died with edits in the tree
    and nothing committed writes `git diff <base_sha>` to `<agent>.STALLED.diff`
    or `<agent>.ERROR.diff` instead of losing the turn to the round's end. A
    clean tree still writes nothing, and an agent with a commit gets its patch
    and no `.diff`: the file name says what happened to the work, and the diff
    is not a `READY`, so the caller's exit code is untouched — a diff is one more
    path in the list the run's summary line counts and prints.

    FL-2: every call goes through `tools.git_run.run_git`. `format-patch` never
    takes the index, so the ladder is a no-op here — the point is that a caller
    no longer has to remember which git writes the index.
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
        proc = run_git(
            ["git", "-C", str(ws.path), "format-patch", "--stdout", f"{ws.base_sha}..HEAD"],
        )
        patch = proc.stdout.strip()
        if not patch:
            detail = proc.stderr.strip() or "nothing to format"
            print(f"warning: {name}: no patch for {ws.base_sha}..HEAD — {detail}", file=sys.stderr)
            continue
        out.mkdir(parents=True, exist_ok=True)
        target.write_text(patch + "\n", encoding="utf-8")
        written.append(target)

    # Agents the patch loop skipped have no commit, so their work is in the tree, not the branch (KC-31).
    for run in state.agents:
        if run.commit or run.state not in _DIFF_STATES:
            continue
        name = run.agent.name
        ws = by_agent.get(name)
        if ws is None:
            print(f"warning: {name} ended {run.state.value} with no workspace — no diff",
                  file=sys.stderr)
            continue
        body = _uncommitted_diff_body(ws)
        if body is None:
            continue
        target = out / f"{name}.{run.state.value}.diff"
        try:
            out.mkdir(parents=True, exist_ok=True)
            target.write_text(body + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"warning: {name}: could not write {target}: {exc}", file=sys.stderr)
            continue
        written.append(target)
    return written


def _provider_quota_lines(state: RoundState) -> list:
    """KC-61: one summary line per provider whose agents ended ``provider_quota``.

    The reset time is the round's, read off the first such agent's own
    ``last_reason`` — every agent of one provider was told the same midnight, so
    naming it once per provider is the whole line. A line, not a refusal: the
    round has already run, and the agents did their work around the others.
    Nothing here is a provider name — the id comes off the agent's own spec.
    """
    groups: dict = {}
    for run in state.agents:
        try:
            reason = run.last_reason()
        except Exception:  # noqa: BLE001 — an unreadable run is not a summary failure
            continue
        if not isinstance(reason, str) or not reason.startswith("provider_quota:"):
            continue
        provider = getattr(run.agent, "provider_id", "") or "unknown"
        groups.setdefault(provider, []).append(reason)
    lines = []
    for provider in sorted(groups):
        reasons = groups[provider]
        when = ""
        for reason in reasons:
            # The runner's reason ends in a retry time, which is kept as the timestamp alone.
            match = re.search(r"retry at (.+?)(?:\)\s*)?$", reason)
            if match:
                when = match.group(1)
                break
        lines.append(f"provider_quota: {provider} × {len(reasons)}"
                     + (f" — retry at {when}" if when else ""))
    return lines


# Command

def _start_server(config: ContestConfig, out_dir: Path, env: dict | None = None):
    """`KiloServer.spawn` when `server = spawn`, else `.attach` to the URL.

    KC-35: *env* is added on top of `os.environ` for the child, so the
    round's own server gets the same `KILO_CONFIG_CONTENT` intake registered
    with — `None` spawns exactly as before.
    """
    if config.server == "spawn":
        kwargs = {}
        if env:
            kwargs["env"] = env
        return KiloServer.spawn(find_kilo_binary(config.kilo_bin),
                                log_path=str(out_dir / "kilo-serve.log"), **kwargs)
    return KiloServer.attach(config.server)


def _make_backends(config: ContestConfig, out_dir: Path, env: dict | None = None,
                   workspaces=()):
    """``(server, make_backend)`` for ``run_round``: one ``ContestBackend`` per worktree.

    ``backend = kilo`` starts (or attaches to) the ``kilo serve`` process the
    whole round shares and hands each agent a ``KiloBackend`` over it; the
    server outlives every agent, so it is returned for ``cmd_run`` to close.
    ``backend = openrouter`` starts no server at all — one ``OpenRouterBackend``
    subprocess per agent, and nothing else left open — so it returns
    ``server=None`` and closes with the round.

    *env* goes to both: the server for `KILO_CONFIG_CONTENT` (KC-35), and the
    OpenRouter agents' subprocess for KC-65's worker count — without it the
    sizing rule would be a Kilo-only rule.

    *workspaces* get their stale ``.kilo`` project file dropped before the server
    is started (Round 151): Kilo reads a workspace's copy of the config on
    spawn, so a file an earlier attempt left there would size the resumed
    agents by a window this round never chose. The drop is untracked-only — a
    file the checkout tracks is the agent's — and keeps the cleanup
    `runner.finish` does at the end of the run.
    """
    if config.backend == "openrouter":
        settings = config.openrouter_settings
        if settings is None:
            raise KiloServerError(
                "[contest] backend = openrouter needs openrouter_llm_profile — "
                "the agents' own base_url and api_key")
        def make_backend(workspace):
            return OpenRouterBackend(settings.api_key, settings.base_url,
                                      str(workspace.path), extra_env=env or None)
        return None, make_backend

    for workspace in workspaces:
        drop_stale_kilo_file(str(workspace.path), assume_stale=True)
    server = _start_server(config, out_dir, env=env)

    def make_backend(workspace):
        return KiloBackend(server, str(workspace.path),
                           events_log=str(out_dir / workspace.agent / "events.jsonl"))
    return server, make_backend


def _kilo_neighbour_note(config: ContestConfig, server, *,
                         proc_root: str = "/proc") -> str | None:
    """KC-62: intake's one line about the box, or `None` to stay silent.

    Above `neighbour_kilo_warn` other Kilo processes share the server's store,
    so `Failed to execute statement` is likely: the line names the count and the
    store, and it is a warning, not a refusal. The data dir comes off the
    server's own environment — `kilo_neighbours` reads it from `/proc/<pid>/
    environ` — never off a constant here. No server (an openrouter round), no
    pid (an attached server), no threshold, or an unreadable `/proc` all print
    nothing: the round starts either way.
    """
    pid = getattr(server, "pid", None) if server is not None else None
    try:
        warn = int(getattr(config, "neighbour_kilo_warn", 4) or 0)
    except (TypeError, ValueError):
        return None
    if not pid or warn < 0:
        return None
    try:
        count, data_dir = kilo_neighbours(pid, proc_root=proc_root)
    except OSError:
        # A scan that cannot read /proc gives no line and never refuses the round.
        return None
    if count <= warn or not data_dir:
        return None
    return (f"kilo: {count} other Kilo processes share {data_dir} — "
            "database errors likely")


def _apply_flags(config: ContestConfig, args: argparse.Namespace) -> ContestConfig:
    """`--models` (with `--provider` behind its bare ids), `--variant`,
    `--max-parallel` and `--no-gate` on top of the roster.

    `--backend` is deliberately not here: it is applied by `load_roster`
    (`cmd_run` passes `args.backend`), because the backend decides whether the
    OpenRouter profile is expanded and resolved.
    """
    if args.models:
        # An openrouter round defaults to the gateway provider, not kenary (the roster's backend picks it).
        provider = args.provider or ("openrouter" if config.backend == "openrouter"
                                     else DEFAULT_PROVIDER)
        config = replace(config, agents=agents_from_models(args.models, provider=provider))
    # Agents with no variant of their own get --variant, else [contest] variant, else highest (KC-49).
    variant = getattr(args, "variant", None) or config.variant or HIGHEST
    config = replace(config, agents=tuple(
        replace(agent, variant=None) if (agent.variant or variant) == DEFAULT
        else agent if agent.variant else replace(agent, variant=variant)
        for agent in config.agents))
    if args.max_parallel is not None:
        config = replace(config, max_parallel=int(args.max_parallel))
    # KC-43: `--legs N` for one command, else `[contest] legs`
    if getattr(args, "legs", None) is not None:
        config = replace(config, legs=max(1, int(args.legs)))
    if args.no_gate:
        # The mechanical layer still decides; any ask it cannot decide is a recorded gate-failed reject.
        config = replace(config, gate_settings=None)
    return config


def _gate_plan_label(config) -> str:
    """The plan's gate line: `hy3:free @ kenari.id`, or `off`.

    KC-37 §3 / KC-55 §6: the model id and the host, and nothing else — no key,
    no path. The host is there because a gate on a different *model* can still
    share a *key and endpoint* with the roster, which is the round 66 loss.
    """
    if config.gate_settings is None:
        return "off"
    model = str(getattr(config.gate_settings, "model", "") or "").strip()
    host = _host_of(_gate_base_url(config))
    if not model and not host:
        return "off"
    return f"{model} @ {host}" if host else model


def _worker_plan_label(workers, fixed: bool, slots: int = 0) -> str:
    """KC-65: the plan's worker line — the count of the first prompts and the
    rule behind it, so the operator sees the sizing before the round starts.

    ``auto, min 2`` is the rule the runner rewrites as agents finish;
    ``fixed N`` is an operator-pinned `pytest_workers_per_agent`. KC-68:
    ``auto, N suite slots`` is the same rule split by the suites that can run
    at once, when `agent_suite_slots` is armed.
    """
    if workers is None:
        return "-"
    if fixed:
        rule = f"fixed {workers}"
    elif slots > 0:
        rule = f"auto, {slots} suite slot{'s' if slots != 1 else ''}"
    else:
        rule = "auto, min 2"
    return f"{workers} each ({rule})"


def _agent_env(workers: int) -> dict:
    """KC-65: the env entries that size the agents' pytest, without the count.

    The environment carries *where to read* the count, not the count: the runner
    rewrites `<out_dir>/pytest-workers` after every transition, and a number
    fixed when the server spawned would be wrong for most of the round.
    `PYTEST_XDIST_AUTO_NUM_WORKERS` is the same start value, as the fallback
    xdist >= 3.2 honours when the plugin cannot load — 3.8.0 here.

    `PYTHONPATH` is the plugin's own directory plus the inherited value, in that
    order: only that one directory, never the runner's repo root.
    """
    inherited = os.environ.get("PYTHONPATH", "")
    parts = [part for part in inherited.split(os.pathsep) if part]
    return {
        "PYTEST_PLUGINS": PYTEST_WORKERS_PLUGIN,
        "PYTHONPATH": os.pathsep.join((str(PYTEST_WORKERS_PLUGIN_DIR), *parts)),
        "PYTEST_XDIST_AUTO_NUM_WORKERS": str(workers),
    }


def _context_memory_lines(config: ContestConfig, out_dir: Path) -> list[str]:
    """KC-67: one plan line per model that has a remembered context size.

    Read fail-open: a memory that is missing, unreadable or broken is no line,
    and the plan prints what it already prints rather than refusing to print.
    """
    try:
        records = context_memory.load(context_memory.memory_path(config, out_dir),
                                      days=context_memory.days_of(config))
    except Exception:  # noqa: BLE001 — no memory is no line, never a failed plan
        return []
    return context_memory.plan_lines(
        records, config.agents,
        percent=context_memory.compact_at_percent(config),
        min_window=context_memory.min_window(config),
        fallback=_context_limit_fallback(getattr(config, "context_limit_fallback", None)),
        share_pct=context_memory.full_refusal_percent(config))


def _with_remembered_limits(config: ContestConfig, out_dir: Path,
                            content: str | None) -> tuple:
    """KC-69: a remembered context size handed to Kilo as ``limit.context``.

    ``(config, content)``: every agent intake found no ``limit.context`` for
    (KC-56) and the KC-67 memory has a size for gets that size — the prompt
    budget, ``limit - output`` — on its spec, and the model gets
    `context_memory.kilo_limit` in the `KILO_CONFIG_CONTENT` the round's server
    is spawned with, merged over *content* (intake's KC-35 overlay) or else the
    operator's own value. Its ``input`` is ``compact_at_percent`` of the budget
    plus Kilo's reserve, so Kilo compacts after the step that crosses that share,
    inside a turn — the KC-67 gate between prompts only ever saw the turn's edges
    (round 70: 263 159 input tokens in the first turn).

    A model whose size intake knows keeps intake's number unless the memory
    holds a smaller one — a provider that refused below the window it declares
    (round 145) — and then the smaller one is handed over. Round 149: a loose
    memory record only wins when it is close enough to the declared window to
    be evidence of it (`context_memory.remembered` measures that against
    *agent*'s own ``context_limit``, the fallback and
    ``context_full_refusal_percent``). An attached server reads its own config
    and never sees the overlay, so it gets nothing. ``compact_at_percent = 0``
    sends the window with ``input`` at the full budget: Kilo keeps its own
    compact there. Fail-open like the rest of the memory: a memory or an
    operator value that cannot be read is the config and *content* unchanged.
    """
    if config.server != "spawn":
        return config, content
    percent = context_memory.compact_at_percent(config)
    share_pct = context_memory.full_refusal_percent(config)
    fallback = _context_limit_fallback(getattr(config, "context_limit_fallback", None))
    try:
        records = context_memory.load(context_memory.memory_path(config, out_dir),
                                      days=context_memory.days_of(config))
    except Exception:  # noqa: BLE001 — no memory is today's round, never a failed one
        return config, content
    agents, providers = [], {}
    for agent in config.agents:
        size, output = context_memory.remembered(
            records, agent.provider_id, agent.model_id,
            context_memory.min_window(config),
            declared=agent.context_limit, fallback=fallback, share_pct=share_pct)
        if agent.context_limit and (size is None or size >= agent.context_limit):
            # Kilo's declared window stands unless the provider refused below it (round 145).
            size = None
            output = None
        limit = context_memory.kilo_limit(size, output, percent)
        if limit is None:
            agents.append(agent)
            continue
        agents.append(replace(agent, context_limit=size))
        models = providers.setdefault(agent.provider_id, {"models": {}})["models"]
        models[agent.model_id] = {"limit": limit}
    if not providers:
        return config, content
    base = content if content is not None else os.environ.get("KILO_CONFIG_CONTENT")
    try:
        merged = merge_config_content(base, {"provider": providers})
    except ValueError:
        return config, content
    return replace(config, agents=tuple(agents)), merged


def _legs_of(config: ContestConfig) -> int:
    """KC-43: how many legs the round runs — 1 for a config without the key."""
    try:
        return max(1, int(getattr(config, "legs", 1) or 1))
    except (TypeError, ValueError):
        return 1


def _legs_choice(config: ContestConfig, args, size: str | None) -> tuple:
    """KC-44: `(the legs the round runs, where the number came from)` — `flag`,
    `size` or `config`.

    The `--legs` flag wins whenever it is passed, `--legs 1` on an L ticket
    included; then the ticket's own `**Size:**`, when `[contest]
    legs_by_size` names a count for it — `None` for a size the mapping has no
    entry for, the fail-open rule, and no mapping at all the same; then
    `[contest] legs`, as today.
    """
    if getattr(args, "legs", None) is not None:
        return max(1, int(args.legs)), "flag"
    by_size = getattr(config, "legs_by_size", None) or {}
    chosen = by_size.get(size) if size else None
    if chosen is not None:
        return max(1, int(chosen)), "size"
    return _legs_of(config), "config"


def _leg_out_dir(out_dir: Path, leg: int | None) -> Path:
    """KC-43: `contest-out/65.2` for leg 2 of round 65; *out_dir* itself for a round
    that is not a relay (`leg is None`), the folder every earlier round used."""
    return out_dir if leg is None else out_dir.with_name(f"{out_dir.name}.{leg}")


def _print_plan(result: Intake, config: ContestConfig, out_dir: Path, *, run_tests: bool,
                workers: int | None = None, workers_fixed: bool = False,
                agent_tmp: Path | None = None, legs: int | None = None,
                legs_from: str | None = None) -> None:
    """The plan, one line per fact: what the round will do, before it does it."""
    models = ", ".join(agent.model + (f"@{agent.variant}" if agent.variant else "")
                       for agent in config.agents)
    facts = (
        ("ticket", f"{result.ticket_path.name} — {result.title}"),
        ("base", result.base_sha[:12]),
        ("agents", f"{len(config.agents)}: {models}"),
        ("parallel", str(config.max_parallel)),
        ("workers", _worker_plan_label(workers, workers_fixed,
                                       int(getattr(config, "agent_suite_slots", 0) or 0))),
        ("tmpdir", str(agent_tmp) if agent_tmp is not None else "-"),
        ("tests", "on" if run_tests else "off"),
        ("gate", _gate_plan_label(config)),
        ("out", str(out_dir)),
    )
    if legs is None:
        legs = _legs_of(config)
    if legs > 1:
        # Legs are named only for a relay, with where the number came from (KC-43, KC-44).
        where = f" ({legs_from})" if legs_from else ""
        facts = facts + (("legs", f"{legs}{where} — {out_dir.name}.1 … {out_dir.name}.{legs}"),)
    width = max(len(key) for key, _ in facts)
    for key, value in facts:
        print(f"{key:<{width}} {value}")
        if key == "agents" and result.registered:
            # The registered ids are logged here, since kilo.jsonc itself is not touched (KC-35).
            print("registered for this round (kilo.jsonc untouched): "
                  + ", ".join(result.registered))
        if key == "agents":
            # KC-67: the models whose size is known from a past overflow, the only ones compacted by the runner.
            for line in _context_memory_lines(config, out_dir):
                print(line)


def _first_prompt(config: ContestConfig, workspace, ticket_path) -> str:
    """The prompt the runner would send *workspace*'s agent — `--dry-run` prints it.

    The same call the runner makes for a first turn: `round_prompt` with the agent's
    own scratch dir and the round's `tmp_roots`, and no `dirty` — the worktree is
    the one `prepare_round` just built, clean. Without it a `--dry-run` would print
    a prompt that is not the one the first agent gets.
    """
    tmp_roots = tuple(getattr(config, "tmp_roots", ()) or ())
    scratch_dir = agent_tmp_dir(tmp_roots, workspace.agent)
    return round_prompt(workspace.agent, ticket_path, workspace.base_sha,
                        tmp_dir=str(scratch_dir) if scratch_dir is not None else "",
                        tmp_roots=tmp_roots)


def _dry_run(repo, result: Intake, config: ContestConfig, out_dir: Path, args, *,
             run_tests: bool, workers: int, fixed: bool,
             agent_tmp: Path | None,
             legs: int | None = None, legs_from: str | None = None) -> int:
    """KC-7: `run --dry-run` — the plan and the first prompt, then exit 0.

    `intake` has already run with its server checks skipped, so the only things
    left that would touch the network are the worktrees, which are the round's
    own, and the print. `prepare_round` runs with `force=args.fresh`, exactly as
    the real path does, so a later `run` without `--fresh` meets these worktrees
    as KC-23 does today and says so, and `--dry-run --fresh` discards them. The
    plan prints after the worktrees exist, so it names a round the operator can
    then `--resume` or `--fresh` into.

    Exit 0 on the way out: the round has done what it set out to do — show the
    plan and the prompt — and `EXIT_NO_READY` would claim an unmet expectation.
    """
    try:
        workspaces = prepare_round(repo, config, args.ticket, args.base,
                                   force=args.fresh)
    except WorkspaceError as exc:
        print(f"intake: {exc}", file=sys.stderr)
        return EXIT_FAILED
    _print_plan(result, config, out_dir, run_tests=run_tests, workers=workers,
                workers_fixed=fixed, agent_tmp=agent_tmp, legs=legs, legs_from=legs_from)
    if not workspaces:
        print("dry-run: no agent to prompt", file=sys.stderr)
        return EXIT_OK
    first = workspaces[0]
    text = _first_prompt(config, first, result.ticket_path)
    print(f"prompt ({first.agent}):")
    print(text)
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    """`_cmd_run`, with AR-3's base-only ticket copies removed when it returns."""
    try:
        return _cmd_run(args)
    finally:
        while _TEMP_TICKET_DIRS:
            shutil.rmtree(_TEMP_TICKET_DIRS.pop(), ignore_errors=True)


def _cmd_run(args: argparse.Namespace) -> int:
    """`run --ticket NN …` — the round on the real repo, the patches in `<out>/`.

    `intake` first (exit 1 with one line per failure); then `prepare_round` at
    the base, or `<out>/state.json` under `--resume` with the workspaces taken
    from it; then one `ContestBackend` per agent — a shared `kilo serve`
    process for `backend = kilo`, an `OpenRouterBackend` subprocess with no
    server at all for `backend = openrouter` (KC-34); then `run_round(...,
    make_backend=…, run_tests=…)`; then `export_patches` and one JSON line per
    agent from `state.table_rows()`. `server.close()` in `finally`: on Ctrl-C
    `run_round` has already saved `state.json` and re-raised, so the
    KeyboardInterrupt propagates after the close. 0 with a READY, 2 with none.

    KC-7: after `export_patches` the round's folder is finished — `export.write_entrants`
    writes the `entrants.json` `contest-bench` reads and `export.write_summary`
    writes the `SUMMARY.md` the operator reads, and both paths print with the
    `patch:` lines. A failure to write either is a `warn:` line and never a
    different exit code: the patches are the round's result, and the two files
    are how the operator gets them to the next stage.

    A worktree left by a crashed attempt is not reset silently (KC-23): the
    refusal is the same `intake:` line as every other `WorkspaceError`, exit 1,
    no server started — `--fresh` discards the work, `--resume` continues it.

    `--dry-run` runs `intake` and `prepare_round`, prints the plan and the exact
    prompt the first agent would get, and exits 0: no `kilo serve`, no session,
    no gate call, `intake`'s `dry-run: skipped …` lines naming what was not
    checked. The worktrees it creates are the round's own, so a later `run`
    without `--fresh` meets them as KC-23 does today; `--dry-run --fresh` is
    allowed.

    KC-44: the ticket's own `**Size:**` line decides the leg count — the
    `--legs` flag always wins when passed, else `[contest] legs_by_size` says
    what the size is worth, else `[contest] legs` as today. An L ticket that
    comes out one leg is refused at intake, naming the size, the count and the
    `--legs 1` that overrides it, before a worktree is prepared. The chosen
    count and its source land in the relay's `state.json` and the plan's
    `legs` line; a round of one leg keeps the state and the plan it always
    had.
    """
    # The gate key placeholder is set so the roster loads; a fake key only gives gate-failed rejects (KC-7).
    os.environ.setdefault("CONTEST_GATE_API_KEY", "unset-for-the-round")

    repo = _target_repo(args.target)
    tasks_dir = repo / TASKS_DIR

    if args.target:
        failures = _target_failures(repo, args.base)
        if failures:
            for line in failures:
                print(f"intake: {line}", file=sys.stderr)
            return EXIT_FAILED

    try:
        # --backend is passed to load_roster, since it decides how the roster is expanded.
        config = load_roster(_roster_path(repo, args.roster), backend=args.backend)
    except RosterError as exc:
        print(f"intake: {exc}", file=sys.stderr)
        return EXIT_FAILED

    if getattr(args, "legs", None) is not None and args.legs < 1:
        print(f"intake: --legs must be at least 1, got {args.legs}", file=sys.stderr)
        return EXIT_FAILED
    config = _apply_flags(config, args)
    run_tests = not args.no_tests
    # Legs come from the flag, else the ticket size with legs_by_size, else [contest] legs (KC-44).
    ticket_name = None
    try:
        ticket_name = gates.ticket_for_round(tasks_dir, args.ticket)[0]
    except OSError:
        ticket_name = None    # no tasks dir to read is no size, as an unreadable file is
    if ticket_name:
        size = ticket_size(tasks_dir / ticket_name)
    else:
        # AR-3: a ticket only at the base is sized from its temp copy
        ticket_name, base_ticket = ticket_file(repo, tasks_dir, args.ticket, args.base)
        size = ticket_size(base_ticket) if base_ticket else None
    legs, legs_from = _legs_choice(config, args, size)
    # KC-43: a relay's legs are not resumable — a round of one leg is
    if args.resume and legs > 1:
        print(f"intake: --resume cannot continue a round of {legs} legs — resume one "
              f"leg's folder with `--out <folder> --legs 1`", file=sys.stderr)
        return EXIT_FAILED
    # An L ticket that would run in one leg is refused unless --legs says so (KC-44).
    if size == "L" and legs == 1 and getattr(args, "legs", None) is None:
        detail = (f"legs_by_size says L={legs}" if legs_from == "size"
                  else f"legs_by_size names no L, so [contest] legs = {legs} stands")
        print(f"intake: {ticket_name} is **Size:** L and would run in {legs} leg "
              f"({detail}) — an L ticket is refused in one leg; pass --legs 1 "
              "to run it in one leg", file=sys.stderr)
        return EXIT_FAILED

    _roster_ini_path = str(_roster_path(repo, args.roster))
    result = intake(repo, tasks_dir, args.ticket, args.base, config,
                    argv=getattr(args, "argv", None),
                    register_missing=args.register_missing,
                    reprobe=getattr(args, "reprobe", False),
                    allow_unprobed=getattr(args, "allow_unprobed", False),
                    roster_path=_roster_ini_path,
                    dry_run=getattr(args, "dry_run", False))
    if result is None:
        return EXIT_FAILED
    if result.agents:
        # `highest` resolved at intake (KC-49): the round runs what answered
        config = replace(config, agents=result.agents)
    out_dir = Path(args.out).resolve() if args.out else result.out_dir
    # KC-69: after `--out`, so the memory read is the one the runner writes to
    config, config_content = _with_remembered_limits(config, out_dir, result.config_content)

    resume = None
    if args.resume:
        # A --resume without a readable state.json fails before the plan is printed (KC-65).
        state_path = out_dir / "state.json"
        if not state_path.is_file():
            print(f"intake: --resume: no {state_path} — nothing to resume", file=sys.stderr)
            return EXIT_FAILED
        try:
            resume = RoundState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
        except (ValueError, KeyError, OSError) as exc:
            print(f"intake: --resume: {state_path} is unreadable: {exc}", file=sys.stderr)
            return EXIT_FAILED

    # Worker count the first prompts read, from the round's agents, capped at max_parallel (KC-65).
    live = min(int(config.max_parallel), round_live_agents(config, resume))
    workers = agent_pytest_workers(live, core_count(), config)
    fixed = int(config.pytest_workers_per_agent) > 0

    # The agents' temp dir is the round's own and is made just before the server spawns (KC-65).
    agent_tmp = agent_tmp_path(config, args.ticket)
    agent_tmp_created = False

    env = _agent_env(workers)
    if config_content is not None:
        # The intake overlay goes to the round's own server too, with the remembered context limit (KC-35, KC-69).
        env["KILO_CONFIG_CONTENT"] = config_content
    if agent_tmp is not None:
        for key in ("TMPDIR", "TEMP", "TMP"):
            env[key] = str(agent_tmp)

    if getattr(args, "dry_run", False):
        # Intake already skipped every server check, so this is the plan and the prompt only (KC-7).
        return _dry_run(repo, result, config, out_dir, args, run_tests=run_tests,
                        workers=workers, fixed=fixed, agent_tmp=agent_tmp,
                        legs=legs, legs_from=legs_from)

    _print_plan(result, config, out_dir, run_tests=run_tests, workers=workers,
                workers_fixed=fixed, agent_tmp=agent_tmp,
                legs=legs, legs_from=legs_from)

    if args.resume:
        workspaces = [run.workspace for run in resume.agents]
    else:
        try:
            workspaces = prepare_round(repo, config, args.ticket, args.base,
                                       force=args.fresh)
        except WorkspaceError as exc:
            print(f"intake: {exc}", file=sys.stderr)
            return EXIT_FAILED

    # A relay is a round of whole turns, one folder per leg; one leg is the round as it always ran (KC-43).
    relay = legs > 1
    leg = 1 if relay else None
    prior: RoundState | None = None
    leg_records: dict = {}          # agent -> the leg records so far, newest first
    final_out = out_dir
    while True:
        leg_out = _leg_out_dir(out_dir, leg)
        leg_out.mkdir(parents=True, exist_ok=True)
        # Worker count file is written before the server spawns; a failed write falls back to xdist (KC-65).
        workers_file = leg_out / WORKERS_FILE
        try:
            write_pytest_workers(workers_file, workers)
            env["CONTEST_PYTEST_WORKERS_FILE"] = str(workers_file)
        except OSError as exc:
            print(f"warn: cannot write {workers_file}: {exc} — the agents' pytest "
                  "falls back to xdist's own worker count", file=sys.stderr)
        if agent_tmp is not None:
            try:
                agent_tmp_created = not agent_tmp.exists()
                agent_tmp.mkdir(parents=True, exist_ok=True)
                os.chmod(agent_tmp, 0o700)
            except OSError as exc:
                print(f"intake: agent_tmpdir: cannot create {agent_tmp}: {exc}", file=sys.stderr)
                if not relay or leg == 1:
                    return EXIT_FAILED
                print(f"warn: leg {leg} was not run — the round is exported as leg "
                      f"{leg - 1} left it", file=sys.stderr)
                break
        try:
            server, make_backend = _make_backends(config, leg_out, env=env or None,
                                                  workspaces=workspaces)
        except (KiloServerError, FileNotFoundError, OSError) as exc:
            print(f"server: {exc}", file=sys.stderr)
            if agent_tmp_created:
                shutil.rmtree(agent_tmp, ignore_errors=True)
            if not relay or leg == 1:
                return EXIT_FAILED
            print(f"warn: leg {leg} was not run — the round is exported as leg "
                  f"{leg - 1} left it", file=sys.stderr)
            break

        # One intake line says the Kilo store is shared, since agents retry on their own (KC-62).
        note = _kilo_neighbour_note(config, server, proc_root=_PROC_ROOT)
        if note:
            print(note, file=sys.stderr)

        # the agents this leg runs: every one on leg 1, then only those that ran out
        expected = (None if prior is None else
                    {run.agent.name for run in prior.agents if run.state in RELAY_STATES})
        # The server log the heartbeat names; an attached or openrouter server has none (KC-81).
        server_log = getattr(server, "log_path", None) if server is not None else None
        try:
            if relay:
                state = run_leg(config, args.ticket, result.ticket_path, workspaces,
                                make_backend=make_backend, out_dir=leg_out, leg=leg,
                                carry=prior, records=leg_records, run_tests=run_tests,
                                server_pid=server.pid if server else None,
                                legs=legs, legs_from=legs_from,
                                log_path=server_log)
            else:
                # A one-leg round keeps the state it always had, with no legs keys (KC-44).
                state = run_round(config, args.ticket, result.ticket_path, workspaces,
                                  make_backend=make_backend, out_dir=leg_out, resume=resume,
                                  run_tests=run_tests, server_pid=server.pid if server else None,
                                  log_path=server_log)
        finally:
            if server is not None:
                server.close()
            # The temp dir is the round's own: removed here with the server, and only if this round made it (KC-65).
            if agent_tmp_created:
                shutil.rmtree(agent_tmp, ignore_errors=True)
        final_out = leg_out
        if not relay:
            break

        # What a leg left is written as a record for every agent that ran, and never stops the round (KC-43).
        for run in state.agents:
            if expected is not None and run.agent.name not in expected:
                continue
            try:
                text = leg_record(run, run.workspace, leg_out,
                                  ticket_path=result.ticket_path).read_text(
                    encoding="utf-8").strip()
            except Exception as exc:  # noqa: BLE001 — a record failing never fails a round
                print(f"warn: could not write the leg record of {run.agent.name}: {exc}",
                      file=sys.stderr)
                continue
            leg_records.setdefault(run.agent.name, []).insert(0, text)
        going = [run.agent.name for run in state.agents if run.state in RELAY_STATES]
        print(f"leg {leg} of {legs}: "
              + " · ".join(f"{run.agent.name} {run.state.value}" for run in state.agents)
              + (f" — leg {leg + 1} continues {', '.join(going)}"
                 if going and leg < legs else " — the relay ends"), file=sys.stderr)
        if not going or leg >= legs:
            break
        try:
            workspaces = prepare_round(
                repo, config, args.ticket, args.base, leg=leg + 1,
                carry_from=LegCarry(args.ticket, leg,
                                    tuple(run.workspace for run in state.agents)))
        except WorkspaceError as exc:
            print(f"warn: leg {leg + 1} was not run: {exc}", file=sys.stderr)
            break
        prior, leg = state, leg + 1

    patches = export_patches(state, workspaces, final_out)
    # Writes the two files that make the output folder the input of contest-bench (KC-7).
    exports_written: list = []
    try:
        target = export.write_entrants(final_out, state.base_sha, state, patches, repo=repo)
    except Exception as exc:  # noqa: BLE001 — a failed export never changes the exit
        print(f"warn: could not write {export.ENTRANTS_FILE}: {exc}", file=sys.stderr)
    else:
        if target is not None:
            exports_written.append((export.ENTRANTS_FILE, target))
    try:
        target = export.write_summary(final_out, state, state.base_sha, patches, repo=repo,
                                      gate=_gate_plan_label(config))
    except Exception as exc:  # noqa: BLE001 — as above
        print(f"warn: could not write {export.SUMMARY_FILE}: {exc}", file=sys.stderr)
    else:
        if target is not None:
            exports_written.append((export.SUMMARY_FILE, target))
    for label, target in exports_written:
        print(f"{label}: {target}")
    for row in state.table_rows():
        print(json.dumps(row, ensure_ascii=False))
    # One quota line per provider that ran out, with its reset time (KC-61).
    for line in _provider_quota_lines(state):
        print(line)
    for patch in patches:
        print(f"patch: {patch}")

    ready = sum(1 for run in state.agents if run.state is AgentState.READY)
    return EXIT_OK if ready else EXIT_NO_READY


def _same_model(a, b) -> bool:
    """Whether two LlmSettings name one model id — whatever the provider.

    The same weights behind two URLs are still the same reviewer, so the base
    URL does not make them different.
    """
    if a is None or b is None:
        return False
    model_a = str(getattr(a, "model", "") or "").strip().lower()
    model_b = str(getattr(b, "model", "") or "").strip().lower()
    return bool(model_a) and model_a == model_b


class DraftSetupError(ValueError):
    """The draft's writer or its reviewer cannot be prepared.

    The message is the whole line `cmd_draft` prints after `draft: ` — a draft
    profile that is unset or does not resolve, a reviewer profile that does not
    resolve, no reviewer profile without `--no-review`, a reviewer that is the
    writer. A `ValueError` so a caller that already refuses `ValueError` can
    reuse this check: `arena issue create` raises no model name, URL or key here
    either, and no model is called until the callables are asked for a draft.
    """


def draft_callables(config, no_review: bool) -> tuple:
    """`(llm_call, review_call)` for one draft; `DraftSetupError` when either is unset.

    The preparation `cmd_draft` used to keep inline, so the two commands that
    draft a ticket — `contest draft` and `arena issue create` — cannot disagree
    about who writes and who reviews. The writer is the profile `[contest]
    draft_llm_profile` names; the reviewer is `[contest] draft_review_llm_profile`
    when it is set, else the gate's model. Both are built by `draft.llm_call_for`,
    so no model name, URL or key lives in this module or in the caller. Nothing
    is called here — the callables are handed to `draft.draft_ticket`, which asks
    them for the draft.
    """
    if config.draft_settings is None:
        # A missing draft profile is refused in run, not at load, so a typo never fails the round.
        if not config.draft_llm_profile:
            reason = ("[contest] draft_llm_profile is not set — name an LlmSettings "
                      "section (base_url, api_key, model) in " + LOCAL_FILENAME +
                      " and set it under [contest]")
        else:
            reason = ("[contest] draft_llm_profile = " + config.draft_llm_profile +
                      " does not resolve — that section needs base_url, api_key "
                      "and model")
        raise DraftSetupError(reason + " — `run` needs no draft profile")

    review_call = None
    if not no_review:
        # The reviewer is the draft review profile if set, else the gate's model and transport (round 148).
        review_settings = config.gate_settings
        review_key = "gate_llm_profile"
        if config.draft_review_llm_profile:
            if config.draft_review_settings is None:
                raise DraftSetupError("[contest] draft_review_llm_profile = "
                                      + config.draft_review_llm_profile + " does not "
                                      "resolve — that section needs base_url, api_key "
                                      "and model")
            review_settings = config.draft_review_settings
            review_key = "draft_review_llm_profile"
        elif not config.gate_llm_profile:
            raise DraftSetupError("[contest] gate_llm_profile is not set — the review "
                                  "runs on the gate's model, so name it in "
                                  + LOCAL_FILENAME + " as run does, or pass --no-review")
        # The reviewer must be a different model from the drafter, not only a different section (KC-79).
        if _same_model(review_settings, config.draft_settings):
            raise DraftSetupError("the review model is the draft model ("
                                  + str(getattr(config.draft_settings, "model", "")) +
                                  ") — set a different model under [contest] " + review_key +
                                  " or draft_llm_profile in " + LOCAL_FILENAME +
                                  ", or pass --no-review")
        review_call = draft.llm_call_for(review_settings,
                                         system=draft.REVIEW_SYSTEM_PROMPT)

    return draft.llm_call_for(config.draft_settings), review_call


def cmd_draft(args: argparse.Namespace) -> int:
    """`draft --target REPO "brief" [--round NN] [--out FILE] [--run] [--no-review]`.

    KC-79: `draft.draft_ticket` over the target — `action_collect` there (Pass A
    only, no Pass B, no LLM), the three maps cut to `[contest] draft_map_budget`,
    one call to the profile `[contest] draft_llm_profile` names, the lint, one
    rework with the problem list appended.

    KC-80 adds two steps after it. The review is one call to the profile
    `[contest] gate_llm_profile` names — the round's own second model, resolved
    and transported exactly as the gate is, so this module holds no model name,
    URL or key — and the commit lands the ticket on `contest-legs`, so a drafted
    ticket is one `run --ticket NN --target REPO` away from a round. `--no-review`
    skips the review and says so; without a gate profile and without that flag the
    command refuses, naming the key. `--run` starts the round right after the
    commit instead of the message, carrying this command's `run` flags.

    Exit 0 with the path written and committed, 2 with the lint's or the
    review's problems one per line and the last draft saved as `.rejected.md`
    next to the output, 1 on a target or a roster problem — including a profile
    that is unset or does not resolve, which names the key and makes no LLM call.
    """
    repo = _target_repo(args.target)
    if not repo.is_dir():
        print(f"draft: --target {repo} is not a directory", file=sys.stderr)
        return EXIT_FAILED
    try:
        config = load_roster(_roster_path(repo, args.roster))
    except RosterError as exc:
        print(f"draft: {exc}", file=sys.stderr)
        return EXIT_FAILED
    try:
        llm_call, review_call = draft_callables(config, getattr(args, "no_review", False))
    except DraftSetupError as exc:
        print(f"draft: {exc}", file=sys.stderr)
        return EXIT_FAILED

    try:
        result = draft.draft_ticket(
            args.brief,
            repo=repo,
            config=config,
            round_no=args.round,
            out=args.out,
            llm_call=llm_call,
            review_call=review_call,
            commit=True,
        )
    except ValueError as exc:
        print(f"draft: {exc}", file=sys.stderr)
        return EXIT_FAILED
    if result.rejected:
        for problem in result.problems:
            print(f"draft: {problem}", file=sys.stderr)
        if result.rejected_path:
            print(f"draft: rejected draft saved to {result.rejected_path}", file=sys.stderr)
        # Exit code 2 means the lint or review refused; the problems are printed for a re-run.
        return EXIT_NO_READY
    if getattr(args, "no_review", False):
        print("draft: --no-review: the gate model's review was skipped", file=sys.stderr)
    print(f"ticket {result.number} ready: {result.path} — check it, then: "
          f"python3 -m tools.contest run --ticket {result.number} --target {repo}")
    if getattr(args, "run", False):
        return _run_after_draft(args, repo, result.number)
    return EXIT_OK


def _run_after_draft(args: argparse.Namespace, repo: Path, number: int) -> int:
    """`--run`: the round right after the commit, this command's run flags carried.

    The ticket number is the one the draft just wrote and `--target` the repo it
    committed to; everything else is the namespace as parsed, so a flag `run`
    knows about is never dropped by the pass-through. `--out` is dropped: on
    `draft` it is the ticket's file, on `run` it is the round's output folder.
    """
    run_args = copy.copy(args)
    run_args.cmd = "run"
    run_args.ticket = number
    run_args.target = str(repo) if getattr(args, "target", None) else None
    run_args.out = None
    return cmd_run(run_args)


def cmd_status(args: argparse.Namespace) -> int:
    """`status --ticket NN [--out DIR]` — the SUMMARY table off `<out>/state.json`.

    Prints `export.render_table` for the round's saved state and touches nothing:
    no worktree, no server, no file written. That is what makes it safe mid-round,
    where `state.json` is one transition behind the truth, and after the round,
    where it is the whole round — the same table `SUMMARY.md` carries, so the two
    cannot disagree about who ended how.

    KC-81 adds one line, not a table row, when `state.json` carries
    `server_silent` — the spell the heartbeat named and its pid — so a hung
    server is visible without the round's log. `SUMMARY.md` has no such line, so
    the two still agree on the table.

    `--out` names the round's folder outright; without it the default
    `<out_dir>/<NN>` is derived from the roster exactly as `run` does. Exit 1 with
    one line when there is no `state.json` to read: a round that never started has
    no state to print, and a round number that was never run is the same thing.
    """
    repo = Path.cwd().resolve()
    if args.out:
        out_dir = Path(args.out).resolve()
    else:
        try:
            config = load_roster(_roster_path(repo, args.roster))
            out_dir = _round_out_dir(repo, config, args.ticket)
        except (RosterError, OSError) as exc:
            print(f"status: cannot find the round's folder: {exc}", file=sys.stderr)
            return EXIT_FAILED
    # A round of legs has no state.json of its own; the last leg's file is the whole round (round 148).
    if not (out_dir / "state.json").is_file():
        legs = sorted(
            (int(leg), sibling)
            for sibling in out_dir.parent.glob(f"{out_dir.name}.*")
            for stem, dot, leg in [sibling.name.rpartition(".")]
            if dot and stem == out_dir.name and leg.isdigit() and sibling.is_dir()
        )
        if legs:
            out_dir = legs[-1][1]
            print(f"status: a round of legs — {out_dir.name}", file=sys.stderr)
    state_path = out_dir / "state.json"
    if not state_path.is_file():
        print(f"status: no {state_path} — nothing to report", file=sys.stderr)
        return EXIT_FAILED
    try:
        state = RoundState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f"status: {state_path} is unreadable: {exc}", file=sys.stderr)
        return EXIT_FAILED
    for line in export.render_table(state, export.round_patches(out_dir, state)):
        print(line)
    # One line for the server's silent spell (KC-81); state.json is its only source.
    silent = state.server_silent
    if isinstance(silent, dict):
        try:
            age = _age(float(silent.get("seconds")))
        except (TypeError, ValueError):
            age = "?"
        print(f"kilo serve silent {age}: pid {silent.get('pid') or '?'} — the heartbeat "
              f"saw no event and a log that had not grown; the server may be hung")
    return EXIT_OK


def _run_options(parser: argparse.ArgumentParser) -> None:
    """The round's options, shared by `run` and `draft`'s `--run` pass-through.

    `cmd_run` reads every one of these off the namespace — as attributes or with
    `getattr`'s default — so a `draft --run` that dropped a flag would fail deep
    inside the round rather than at the parser. `--ticket`, `--out` and
    `--roster` stay in the parser that needs them: `draft` fills `--ticket` and
    clears `--out`, and `draft --roster` is the file its profile is read from.
    """
    parser.add_argument("--base", default="HEAD",
                        help="the base ref the worktrees start from (default HEAD)")
    parser.add_argument("--models", default="",
                        help="comma-separated model ids run INSTEAD of the roster's agents "
                             "(a:free,b:free → --provider unless the id names its own)")
    parser.add_argument("--backend", default=None, choices=["kilo", "openrouter"],
                        help="override the roster's backend (kilo | openrouter)")
    parser.add_argument("--provider", default=None, metavar="ID",
                        help="the provider id behind a --models id that names no provider of "
                             "its own (default kenary for backend = kilo, openrouter for "
                             "backend = openrouter; the roster spells a model provider/model, "
                             "and --roster's agents are unaffected)")
    parser.add_argument("--variant", default=None, metavar="NAME",
                        help="the reasoning variant of every agent that names none: "
                             "high, max, … as GET /provider lists it; 'highest' (the default, "
                             "[contest] variant) — the top one that answers 'say: hello', "
                             "probed at intake; 'default' — send none. A --models item names "
                             "its own as model@variant")
    parser.add_argument("--register-missing", action="store_true",
                        help="register a roster model that is not in Kilo's own model list "
                             "for this round, through KILO_CONFIG_CONTENT (kilo.jsonc is not "
                             "edited; needs server = spawn)")
    parser.add_argument("--reprobe", action="store_true",
                        help="re-run the variant probe even when contest-probe.json holds a "
                             "fresh entry for the same model and Kilo version (KC-11)")
    parser.add_argument("--allow-unprobed", action="store_true",
                        help="start the round even when a model's variant probe failed or "
                             "the cache has no entry for it (KC-11)")
    parser.add_argument("--max-parallel", type=int, default=None, metavar="N",
                        help="override the roster's max_parallel")
    parser.add_argument("--legs", type=int, default=None, metavar="N",
                        help="run the round as N numbered legs (KC-43): each a whole turn on a "
                             "new session in the same worktree, handed a record of the legs "
                             "before it; overrides [contest] legs (default 1)")
    parser.add_argument("--no-tests", action="store_true",
                        help="do not run the pytest roots in the harvest (the default is on)")
    parser.add_argument("--no-gate", action="store_true",
                        help="no gate model: the mechanical layer decides, the rest is gate-failed")
    parser.add_argument("--resume", action="store_true",
                        help="resume from <out>/state.json — only the mid-flight agents restart")
    parser.add_argument("--dry-run", action="store_true",
                        help="intake, prepare the worktrees, print the plan and the first "
                             "agent's prompt, and stop: no kilo serve, no session, no gate call")
    parser.add_argument("--fresh", action="store_true",
                        help="reset the round's worktrees even when they hold uncommitted "
                             "work or commits")


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
    run.add_argument("--target", default=None, metavar="REPO_PATH",
                     help="the git repo the round runs on (default: the current "
                          "directory). Its epic-tasks/ holds the ticket and its base "
                          "must carry scripts/next_task.py and scripts/append_task.py; "
                          "worktrees, out_dir and rounds_dir resolve against it")
    run.add_argument("--roster", default=DEFAULT_ROSTER,
                     help="the roster ini (default contest.ini in the current "
                          "directory; a relative path is taken from the current "
                          "directory, not from --target; contest.local.ini next to "
                          "it overrides it)")
    _run_options(run)
    run.add_argument("--out", default=None, metavar="DIR",
                     help="the round's output directory (default <out_dir>/<NN>)")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser(
        "status",
        help="print the round's SUMMARY table off <out>/state.json",
        description="One table off <out>/state.json — the same table SUMMARY.md carries, "
                    "safe mid-round as well as after the round, and it touches nothing.",
    )
    status.add_argument("--ticket", type=int, required=True, metavar="NN",
                        help="the round number, NN from epic-tasks/NN-*.md")
    status.add_argument("--roster", default=DEFAULT_ROSTER,
                        help="the roster ini the round's <out_dir> comes from "
                             "(default contest.ini at the repo root)")
    status.add_argument("--out", default=None, metavar="DIR",
                        help="the round's output directory (default <out_dir>/<NN>)")
    status.set_defaults(func=cmd_status)

    drafting = sub.add_parser(
        "draft",
        help="write one ticket from a plain brief, reviewed and committed",
        description="`--collect` over the target (Pass A only, no Pass B), the three "
                    "maps cut to [contest] draft_map_budget, one call to the profile "
                    "[contest] draft_llm_profile names, the lint, one rework, then a review "
                    "by the profile [contest] gate_llm_profile names — one round of "
                    "problems at a time, [contest] draft_review_rounds of them — and the "
                    "ticket committed on contest-legs. `--run` starts the round with the "
                    "flags this command knows about.",
    )
    drafting.add_argument("--target", default=None, metavar="REPO_PATH",
                          help="the git repo the brief is about (default: the current "
                               "directory); `--collect` runs there, epic-tasks/ is "
                               "written there, and the commit lands there")
    drafting.add_argument("brief", metavar="BRIEF",
                          help="the task in one line, verbatim — the brief is never "
                               "rewritten into a different task")
    drafting.add_argument("--round", dest="round", type=int, default=None, metavar="NN",
                          help="the ticket number (default: the next free one in "
                               "epic-tasks/)")
    drafting.add_argument("--out", default=None, metavar="FILE",
                          help="where the ticket goes (default "
                               "epic-tasks/<NN>-<slug>.md, from the ticket's own title)")
    drafting.add_argument("--roster", default=DEFAULT_ROSTER,
                          help="the roster ini [contest] draft_llm_profile and "
                               "[contest] gate_llm_profile are read from (default "
                               "contest.ini in the current directory)")
    drafting.add_argument("--no-review", action="store_true",
                          help="skip the gate model's review of the draft (it still needs "
                               "the draft profile; the skip is printed to stderr)")
    drafting.add_argument("--run", action="store_true",
                          help="run the round right after the commit: `run --ticket NN "
                               "--target REPO` with every run flag this command was given")
    _run_options(drafting)
    drafting.set_defaults(func=cmd_draft)
    return parser


def main(argv=None) -> int:
    """`python3 -m tools.contest …` — subcommands, so KC-7 adds to this list only."""
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_usage(sys.stderr)
        return 2
    # The runner's progress lines are the operator's only live view of a round (live_smoke.py sets the same).
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(message)s")
    # This invocation's argv, so intake can print the run line with the blocked ticket's number.
    args.argv = list(argv) if argv else list(sys.argv[1:])
    return args.func(args)
