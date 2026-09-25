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

`backend = openrouter` runs the same command on a non-Kilo backend:
`--backend openrouter` skips the `kilo serve` start, the `KiloServer.attach`
health check and the `GET /provider` offer check alike, gives each agent an
`OpenRouterBackend` subprocess instead of a `KiloClient` session, and makes
`openrouter` the provider behind a bare `--models` id — an operator with a
gateway key and no `kilo` binary can run the round. That credential is
`[contest] openrouter_llm_profile`, resolved at load time for that backend
only, and it is not the gate profile.

`main(argv)` takes subcommands so KC-7 (round 46) adds `status` and `--dry-run`
without moving anything. Exit codes: 0 when at least one agent is READY, 2
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
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from tools.contest import gates
from tools.contest.backend import KiloBackend, OpenRouterBackend
from tools.contest.kilo_client import (
    KiloClient,
    KiloHttpError,
    KiloServerError,
    KiloServer,
    find_kilo_binary,
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
    AgentState,
    RoundState,
    _is_quota,
    _quota_re,
    run_round,
)
from tools.contest.variant import (
    DEFAULT,
    HIGHEST,
    hello_probe,
    ladder,
    listed_variants,
    needs_login,
    pick_variant,
)
from tools.contest.workspace import WorkspaceError, prepare_round
from tools.git_run import run_git

__all__ = [
    "DEFAULT_PROVIDER",
    "DEFAULT_ROSTER",
    "GATE_PLACEHOLDER_MODEL",
    "TASKS_DIR",
    "Intake",
    "agents_from_models",
    "cmd_run",
    "export_patches",
    "gate_share_line",
    "intake",
    "main",
    "resolve_variants",
    "roster_on_offer",
    "roster_missing",
    "registration_overlay",
    "merge_config_content",
]

#: The ticket folder the round reads; `scripts/next_task.py` hands it out.
TASKS_DIR = "epic-tasks"

#: The committed roster at the repo root; a `contest.local.ini` next to it
#: overrides it, `load_roster`'s own rule (KC-2).
DEFAULT_ROSTER = "contest.ini"

#: The provider id behind a `--models` id that does not name its own. The id
#: `POST /session` wants — not the display name the id may have been read from.
DEFAULT_PROVIDER = "kenary"

#: The committed `contest.ini`'s gate model. It is not a model anyone can call,
#: so a round that still runs on it has no gate to probe (KC-55 §5).
GATE_PLACEHOLDER_MODEL = "some/model"

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
        # split at the FIRST "/", the roster's own rule: `kilo/nex-agi/nex-n2.5-pro:free`
        # is provider `kilo`, model `nex-agi/nex-n2.5-pro:free`
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
                     kilo_bin: str | None = None, quota_re=None) -> tuple:
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
    """
    resolved = []
    failures: list = []
    notes: list = []
    picks: dict = {}
    probed: dict = {}
    for agent in agents:
        wanted = agent.variant
        if not wanted:
            resolved.append(agent)
            continue
        listed = listed_variants(providers, agent.provider_id, agent.model_id)
        if wanted != HIGHEST:
            if wanted not in listed:
                # the refusal is the list; nothing is asked, and the run does
                # not start, so a probe here would only burn a request against
                # the key that is about to be refused
                failures.append(
                    f"[{agent.name}] {agent.model}: no variant '{wanted}' — listed: "
                    f"{', '.join(listed) if listed else '(none)'}")
                resolved.append(agent)
                continue
            # KC-61: a named variant used to be sent unasked, so an exhausted
            # key reached the round when the variant was named. One request per
            # provider/model@variant is the whole cost — OpenRouter counts a
            # failed request against the free quota too. Without a probe there
            # is nothing to ask, and the named variant is sent as today.
            if probe_for is not None:
                key = (agent.provider_id, agent.model_id, wanted)
                if key not in probed:
                    probed[key] = _probe_answer(probe_for, agent, wanted)
                answer = probed[key]
                if answer is None:
                    resolved.append(agent)      # hello came back, as today
                elif _is_quota(answer, quota_re):
                    # the key is out until its reset: one console line, and the
                    # agent is left out — the round runs one agent short
                    # instead of being refused for one dry key
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
            pick = pick_variant(ladder(listed), probe_for(agent))
            picks[agent.model] = pick
            notes.append(f"{agent.model}@{HIGHEST} → {pick.describe()}")
        pick = picks[agent.model]
        if not pick.usable and pick.tried and all(
                _is_quota(reason, quota_re) for _rung, reason in pick.tried):
            # KC-61: every rung answered a quota — the same dry key, not a
            # model that cannot talk: left out like a named variant's
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
    #: the roster with every `highest` resolved (KC-49); empty = the config's as is
    agents: tuple = ()

    #: the `KILO_CONFIG_CONTENT` a `--register-missing` round runs on: the
    #: operator's own value with the missing models merged in (KC-35)
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
            # keyed like `roster_missing` reads the offer: by the `models` key,
            # which is what `AgentSpec.model_id` names
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
                 register_missing: bool = False) -> _Offer:
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
    # no offer to read: `highest` — the round's default — quietly becomes no
    # variant, and the round's own `server:` line reports why there was none
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

        # the attached server reads the operator's own kilo.jsonc and this
        # process cannot change it: the flag refuses, and the other agents still
        # get their KC-25 check so the round is not refused twice
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
                # the overlay would not even start a server: no registration
                # happened, and the hint's own advice — re-run with the flag —
                # is what the operator just did, so the line says what failed
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
            # refused on other grounds already (`resolve=False`): nothing is
            # registered, but the ids the flag would register are still not
            # failures, and no hint tells the operator to pass a flag they passed
            others = tuple(agent for agent in agents if agent not in missing)
            return _Offer(roster_on_offer(providers, others, kilo_bin=kilo_bin),
                          unresolved, [], [])

        failures = roster_on_offer(providers, agents, kilo_bin=kilo_bin)
        if missing:
            failures.extend(_missing_hint_lines(missing))
        if failures or not resolve:
            return _Offer(failures, unresolved, [], [])

        # KC-55 §5: the gate's two checks, both of them free of a session — the
        # share check off the offer just read, and one probe call of its own
        warnings = []
        share = gate_share_line(providers, agents, _gate_base_url(config))
        if share:
            warnings.append(share)
        refusals, probe_warnings = _gate_probe(config)
        warnings.extend(probe_warnings)
        if refusals:
            return _Offer(list(refusals), unresolved, [], warnings)

        # KC-61: the probe carries the round's own quota rule, so a dry key
        # answers in about a second instead of burning HELLO_TIMEOUT_SEC per
        # model — and its answer reads as a quota at intake, not at the runner
        max_retry_wait = float(getattr(config, "provider_retry_max_wait_sec", 0) or 0)
        quota_re = _quota_re(config)
        probe_kwargs = {}
        if max_retry_wait > 0:
            probe_kwargs["max_retry_wait"] = max_retry_wait
        if quota_re is not None:
            probe_kwargs["quota_re"] = quota_re

        def probe_for(agent):
            return hello_probe(server, agent.provider_id, agent.model_id,
                               **probe_kwargs)
        resolved, failures, notes = resolve_variants(providers, agents, probe_for,
                                                     kilo_bin=kilo_bin,
                                                     quota_re=quota_re)
        # KC-56: the same read's `limit.context`, so the runner can tell a full
        # context window from a spent output budget without asking again
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
           register_missing: bool = False):
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

    # KC-55 §3: the gate's worst case must fit inside the silence clock it
    # holds. The gate runs synchronously inside `wait_idle`'s loop, so while it
    # waits the session produces no events and every second counts; a worst
    # case at or over `idle_event_timeout_sec` makes the runner declare its own
    # agent `STALLED`. `gate_worst_case_sec` is the one place the number comes
    # from, so the intake line and the tests read the same value.
    worst_case = gate_worst_case_sec(config)
    idle = config.idle_event_timeout_sec
    if config.gate_settings is not None and isinstance(idle, (int, float)) \
            and not isinstance(idle, bool) and idle > 0 and worst_case >= idle:
        failures.append(
            f"gate retries can outlast the silence clock: worst case "
            f"{_seconds(worst_case)} s ≥ idle_event_timeout_sec {_seconds(idle)} — "
            f"lower gate_deadline_sec in contest.ini, or raise idle_event_timeout_sec")

    # an openrouter round has no offer to probe: `highest` is no variant there
    agents = _without_highest(config.agents)
    offer = None
    attached = None
    if config.backend == "kilo":
        # an openrouter round has no Kilo server at all: nothing to resolve,
        # nothing to attach to, and no offer to check the roster against
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
        # the variant probe spends model calls, so it only runs for a round
        # that has passed every other check
        offer = _check_offer(repo, config, attached, resolve=not failures,
                             register_missing=register_missing)
        failures.extend(offer.failures)
        for note in offer.notes:
            print(f"variant: {note}")
        for line in offer.warnings:
            print(line)

    if offer is None:
        # no offer was read: an openrouter round, or a backend that failed
        # before the check — nothing was resolved and nothing was registered
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

    FL-2: the call goes through `tools.git_run.run_git`. `format-patch` never
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
            # the runner's reason ends `(retry at YYYY-MM-DD HH:MM UTC)`, so the
            # group is the timestamp alone, its closing paren and all dropped
            match = re.search(r"retry at (.+?)(?:\)\s*)?$", reason)
            if match:
                when = match.group(1)
                break
        lines.append(f"provider_quota: {provider} × {len(reasons)}"
                     + (f" — retry at {when}" if when else ""))
    return lines


# ─────────────────────────────────────────────────────────────────────────────
# the command
# ─────────────────────────────────────────────────────────────────────────────

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


def _make_backends(config: ContestConfig, out_dir: Path, env: dict | None = None):
    """``(server, make_backend)`` for ``run_round``: one ``ContestBackend`` per worktree.

    ``backend = kilo`` starts (or attaches to) the ``kilo serve`` process the
    whole round shares and hands each agent a ``KiloBackend`` over it; the
    server outlives every agent, so it is returned for ``cmd_run`` to close.
    ``backend = openrouter`` starts no server at all — one ``OpenRouterBackend``
    subprocess per agent, and nothing else left open — so it returns
    ``server=None`` and closes with the round.
    """
    if config.backend == "openrouter":
        settings = config.openrouter_settings
        if settings is None:
            raise KiloServerError(
                "[contest] backend = openrouter needs openrouter_llm_profile — "
                "the agents' own base_url and api_key")
        def make_backend(workspace):
            return OpenRouterBackend(settings.api_key, settings.base_url,
                                     str(workspace.path))
        return None, make_backend

    server = _start_server(config, out_dir, env=env)

    def make_backend(workspace):
        return KiloBackend(server, str(workspace.path),
                           events_log=str(out_dir / workspace.agent / "events.jsonl"))
    return server, make_backend


def _apply_flags(config: ContestConfig, args: argparse.Namespace) -> ContestConfig:
    """`--models` (with `--provider` behind its bare ids), `--variant`,
    `--max-parallel` and `--no-gate` on top of the roster.

    `--backend` is deliberately not here: it is applied by `load_roster`
    (`cmd_run` passes `args.backend`), because the backend decides whether the
    OpenRouter profile is expanded and resolved.
    """
    if args.models:
        # the roster's backend picks the provider behind a bare id: an
        # openrouter round has no Kilo to route through, so the default there
        # is the gateway, not `kenary`
        provider = args.provider or ("openrouter" if config.backend == "openrouter"
                                     else DEFAULT_PROVIDER)
        config = replace(config, agents=agents_from_models(args.models, provider=provider))
    # KC-49: every agent that names no variant of its own gets `--variant`,
    # else `[contest] variant` (`highest` unless the roster says otherwise);
    # `default` is no variant at all
    variant = getattr(args, "variant", None) or config.variant or HIGHEST
    config = replace(config, agents=tuple(
        replace(agent, variant=None) if (agent.variant or variant) == DEFAULT
        else agent if agent.variant else replace(agent, variant=variant)
        for agent in config.agents))
    if args.max_parallel is not None:
        config = replace(config, max_parallel=int(args.max_parallel))
    if args.no_gate:
        # the mechanical layer still decides; every ask it cannot decide is the
        # existing `gate-failed` reject, recorded like any decision
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


def _print_plan(result: Intake, config: ContestConfig, out_dir: Path, *, run_tests: bool) -> None:
    """The plan, one line per fact: what the round will do, before it does it."""
    models = ", ".join(agent.model + (f"@{agent.variant}" if agent.variant else "")
                       for agent in config.agents)
    facts = (
        ("ticket", f"{result.ticket_path.name} — {result.title}"),
        ("base", result.base_sha[:12]),
        ("agents", f"{len(config.agents)}: {models}"),
        ("parallel", str(config.max_parallel)),
        ("tests", "on" if run_tests else "off"),
        ("gate", _gate_plan_label(config)),
        ("out", str(out_dir)),
    )
    width = max(len(key) for key, _ in facts)
    for key, value in facts:
        print(f"{key:<{width}} {value}")
        if key == "agents" and result.registered:
            # KC-35: what --register-missing added for this round alone — named
            # here because kilo.jsonc was not touched, so the log is the only
            # record that the ids were ever registered
            print("registered for this round (kilo.jsonc untouched): "
                  + ", ".join(result.registered))


def cmd_run(args: argparse.Namespace) -> int:
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
        # `--backend` goes into `load_roster`, not into `_apply_flags`: the
        # backend decides whether the OpenRouter profile is expanded and
        # resolved, so the flag has to be in place before the roster is read —
        # applied afterwards, `config.openrouter_settings` would still be None.
        config = load_roster(_roster_path(repo, args.roster), backend=args.backend)
    except RosterError as exc:
        print(f"intake: {exc}", file=sys.stderr)
        return EXIT_FAILED

    config = _apply_flags(config, args)
    run_tests = not args.no_tests

    result = intake(repo, tasks_dir, args.ticket, args.base, config,
                    argv=getattr(args, "argv", None),
                    register_missing=args.register_missing)
    if result is None:
        return EXIT_FAILED
    if result.agents:
        # `highest` resolved at intake (KC-49): the round runs what answered
        config = replace(config, agents=result.agents)
    out_dir = Path(args.out).resolve() if args.out else result.out_dir
    _print_plan(result, config, out_dir, run_tests=run_tests)
    if result.config_content is not None:
        # KC-35: the overlay intake proved in its throwaway goes to the round's
        # own server too, or the round would run on an unregistered list
        env = {"KILO_CONFIG_CONTENT": result.config_content}
    else:
        env = None

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
        server, make_backend = _make_backends(config, out_dir, env=env)
    except (KiloServerError, FileNotFoundError, OSError) as exc:
        print(f"server: {exc}", file=sys.stderr)
        return EXIT_FAILED

    try:
        state = run_round(config, args.ticket, result.ticket_path, workspaces,
                          make_backend=make_backend, out_dir=out_dir, resume=resume,
                          run_tests=run_tests)
    finally:
        if server is not None:
            server.close()

    patches = export_patches(state, workspaces, out_dir)
    for row in state.table_rows():
        print(json.dumps(row, ensure_ascii=False))
    # KC-61: one line per provider out of quota, with its reset time — the
    # reason the agents ended is in each row's last_reason, and it is the same
    # reason five times over, so the summary says it once per provider.
    for line in _provider_quota_lines(state):
        print(line)
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
    run.add_argument("--backend", default=None, choices=["kilo", "openrouter"],
                     help="override the roster's backend (kilo | openrouter)")
    run.add_argument("--provider", default=None, metavar="ID",
                     help="the provider id behind a --models id that names no provider of "
                          "its own (default kenary for backend = kilo, openrouter for "
                          "backend = openrouter; the roster spells a model provider/model, "
                          "and --roster's agents are unaffected)")
    run.add_argument("--variant", default=None, metavar="NAME",
                     help="the reasoning variant of every agent that names none: "
                          "high, max, … as GET /provider lists it; 'highest' (the default, "
                          "[contest] variant) — the top one that answers 'say: hello', "
                          "probed at intake; 'default' — send none. A --models item names "
                          "its own as model@variant")
    run.add_argument("--register-missing", action="store_true",
                     help="register a roster model that is not in Kilo's own model list "
                          "for this round, through KILO_CONFIG_CONTENT (kilo.jsonc is not "
                          "edited; needs server = spawn)")
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
