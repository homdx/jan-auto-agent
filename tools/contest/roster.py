"""tools/contest/roster.py — KC-2: ``contest.ini`` — the roster, the limits, the gate.

The probe (``scripts/kilo_hello.py``, commit 67e834d; recorded in
``docs/kilo-contest/PROBE.md``) ran three models by hand, each as a separate
``--model`` flag. This module keeps that information once, in ``contest.ini``,
in the shape the runner (KC-6) and the policy (KC-3) read it:

  * the limits — one set per round, not per agent;
  * the roster — one ``[contest.agent.<name>]`` per competing model, in file
    order, with ``model`` split at the FIRST ``/`` the way ``kilo models``
    prints it (``kilo/~anthropic/claude-haiku-latest`` is provider ``kilo``,
    not ``~anthropic``);
  * the safety gate's own model — a named LLM profile resolved through
    ``tools.auto.llm_profile.resolve_llm_profile``, exactly as ``[gate1]
    presence_llm_profile = gate1_llm`` does, so ``api_key``, ``base_url``,
    ``api_format``, ``response_format`` and ``think`` behave here as they do
    in ``agents.ini``'s ``[gate1_llm]``;
  * the round's backend — ``backend = kilo`` (default) or ``backend =
    openrouter``, one value for the whole round. ``openrouter`` needs a second
    named profile, ``openrouter_llm_profile = contest_openrouter_llm``, resolved
    through the same ``resolve_llm_profile`` call: that is the competing
    agents' own credential, and it is not the gate's profile — the gate is a
    different model called on a risky command;
  * the session rules KC-1's ``KiloClient.create_session`` sends — the probe's
    three fixed rules, then one ``bash`` deny per ``deny_commands`` entry.
    :meth:`ContestConfig.session_rules` is the single source of truth for that
    list.

:func:`load_roster` reads ``path``, then ``contest.local.ini`` next to it if
present (git-ignored — that is where a real ``api_key`` lives), then ``overlay``
last, so a later file overrides an earlier one key by key. ``${ENV}`` references
in any value are expanded from the environment, and an unresolved one is an
error naming the section and key. Unknown keys in ``[contest]`` or in an agent
section are an error naming them, so a typo is found at load time and not at
the end of a round; unknown sections are ignored, so ``agents.ini`` can be
passed as an overlay without complaint. An absent value falls back to its
documented default, and a malformed number falls back the same way through
``tools.config_safe`` — a stray edit to a limit cannot abort a run.

Standard library plus the repo's own ``tools.auto.llm_profile``; nothing here
opens a network connection.
"""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from tools.auto.llm_profile import LlmSettings, resolve_llm_profile
from tools.config_safe import safe_getint

__all__ = [
    "AGENT_KEYS",
    "AGENT_SECTION_PREFIX",
    "BASE_RULES",
    "CONTEST_KEYS",
    "BACKENDS",
    "WORKSPACE_KINDS",
    "DEFAULTS",
    "DEFAULTS_GATE",
    "DEFAULTS_OPENROUTER",
    "LOCAL_FILENAME",
    "AgentSpec",
    "ContestConfig",
    "RosterError",
    "load_roster",
]

#: The file that holds the secrets. It is git-ignored: the committed
#: ``contest.ini`` references ``${CONTEST_GATE_API_KEY}`` instead.
LOCAL_FILENAME = "contest.local.ini"

#: ``[contest.agent.<name>]`` — the roster section.
AGENT_SECTION_PREFIX = "contest.agent."

#: Every key ``[contest]`` may carry, in the order ``contest.ini`` lists them.
CONTEST_KEYS = (
    "kilo_bin",
    "server",
    "backend",
    "max_parallel",
    "max_rework",
    "max_continues_per_attempt",
    "turn_timeout_sec",
    "turn_extend_sec",
    "turn_max_sec",
    "idle_event_timeout_sec",
    "max_questions_per_turn",
    "max_error_retries",
    "error_retry_backoff_sec",
    "error_retry_max_backoff_sec",
    "agent_max_sec",
    "max_local_store_retries",
    "local_store_retry_backoff_sec",
    "neighbour_kilo_warn",
    "provider_retry_max_attempts",
    "provider_retry_max_wait_sec",
    "quota_patterns",
    "progress_every_sec",
    "harvest_budget_sec",
    "pytest_workers_per_agent",
    "pytest_workers_few_agents",
    "pytest_workers_few",
    "pytest_workers_min",
    "agent_tmpdir",
    "tmp_roots",
    "deny_commands",
    "ask_commands",
    "gate_llm_profile",
    "openrouter_llm_profile",
    "gate_max_calls_per_session",
    "gate_retries",
    "gate_retry_wait_sec",
    "gate_retry_max_wait_sec",
    "gate_deadline_sec",
    "out_dir",
    "rounds_dir",
    "workspace_kind",
    "variant",
    "probe_ttl_days",
)

#: Every key an agent section may carry.
AGENT_KEYS = ("model", "kilo_agent", "variant")

#: The three rules the probe sent in every session (PROBE.md): everything
#: allowed, and the two permissions the contest answers itself. Kept as a tuple
#: of dicts so a caller comparing against it sees exactly what was sent.
BASE_RULES = (
    {"permission": "*", "pattern": "*", "action": "allow"},
    {"permission": "external_directory", "pattern": "*", "action": "ask"},
    {"permission": "doom_loop", "pattern": "*", "action": "ask"},
)

#: The gate's profile defaults. ``base_url`` / ``api_key`` / ``model`` are the
#: fields ``resolve_llm_profile`` requires and never inherits, so they carry no
#: usable value — a profile that omits one of them fails loudly at load time,
#: which is what an empty default buys here. ``response_format`` is true, as
#: the contest's gate profile needs it.
DEFAULTS_GATE = LlmSettings(
    base_url="",
    api_key="",
    model="",
    api_format="openai",
    temperature=0.0,
    max_tokens=256,
    response_format=True,
)

#: ``DEFAULTS_GATE`` under the name the ticket uses for the symbol.
DEFAULTS = DEFAULTS_GATE

#: The OpenRouter agents' own profile defaults, resolved the same way the gate'
#: profile is: ``[contest] openrouter_llm_profile`` names an LlmSettings section in
#: this file. Same shape as ``[contest_gate_llm]``, different taste — a coding
#: agent wants a wide token budget and free-form output, not the gate's 256-token
#: JSON verdict. ``model`` is empty on purpose: ``OpenRouterBackend`` gets its
#: model id per agent from ``AgentSpec``, not from this section.
DEFAULTS_OPENROUTER = LlmSettings(
    base_url="",
    api_key="",
    model="",
    api_format="openai",
    temperature=0.2,
    max_tokens=4096,
    response_format=False,
)

#: The values ``[contest] backend =`` accepts, in the order ``contest.ini`` lists them.
BACKENDS = ("kilo", "openrouter")

#: ``[a-z0-9][a-z0-9_-]*`` — an agent name becomes a branch and a folder.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: The values ``[contest] workspace_kind =`` accepts. ``clone`` is the default
#: since KC-59: a local clone has its own ``refs/stash``, index, ``HEAD`` and
#: branches, so one agent's ``git stash`` can never pop another agent's work
#: off the stack the repo's worktrees share. ``worktree`` is the pre-KC-59
#: behaviour, kept for anyone who needs it.
WORKSPACE_KINDS = ("clone", "worktree")

#: ``${NAME}`` in a value: expanded from the environment.
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class RosterError(ValueError):
    """``contest.ini`` cannot be loaded.

    Raised for a missing roster file, an unreadable source, a ``${ENV}``
    reference to an unset variable, a duplicate section or option, an unknown
    key in ``[contest]`` or an agent section, a ``backend`` that is not
    ``kilo`` or ``openrouter``, a ``backend = openrouter`` whose
    ``openrouter_llm_profile`` does not resolve, an agent name that cannot
    become a branch, a ``model`` with no ``/``, and an empty roster. Always
    says which section and key is at fault — the operator has to be able to
    find the typo.
    Subclasses :class:`ValueError`, which is what every other config problem in
    this repo is.
    """


@dataclass(frozen=True)
class AgentSpec:
    """One competing model, from one ``[contest.agent.<name>]`` section.

    ``name`` becomes a branch and a folder, so it must match
    ``[a-z0-9][a-z0-9_-]*``. ``provider_id`` / ``model_id`` are the two halves
    of the section's ``model`` value, split at its first ``/``.
    """

    name: str
    provider_id: str
    model_id: str
    kilo_agent: str | None = None
    variant: str | None = None
    #: KC-56: the model's ``limit.context`` from ``GET /provider``, put here at
    #: intake (never read from ``contest.ini``); ``None`` = unknown. The runner
    #: tells a reply cut off by a full context window from one cut off by its
    #: output budget with it.
    context_limit: int | None = None

    @property
    def model(self) -> str:
        """``providerID/modelID`` — the shape the probe sent and ``kilo models`` prints."""
        return f"{self.provider_id}/{self.model_id}"


@dataclass(frozen=True)
class ContestConfig:
    """The round: its limits, its roster and the gate's LLM profile.

    Every ``[contest]`` key, typed. ``agents`` is in file order — the roster
    order a round runs in. ``gate_settings`` is resolved through
    ``resolve_llm_profile`` at load time, so a misconfigured gate profile is
    found then rather than on the gate's first call.
    """

    kilo_bin: str = "auto"
    server: str = "spawn"
    max_parallel: int = 3
    max_rework: int = 2
    max_continues_per_attempt: int = 2
    turn_timeout_sec: int = 1800
    #: KC-36: seconds a turn's deadline is pushed, per extension, when the
    #: worktree's churn has grown since the last deadline. 0 = the turn clock
    #: is a hard kill again: today's behaviour, no callback asked.
    turn_extend_sec: int = 600
    #: KC-36: the hard ceiling for one turn, whatever the churn.
    #: ``turn_timeout_sec`` plus every extension stops no later than this, so a
    #: model in an infinite edit loop is bounded.
    turn_max_sec: int = 7200
    idle_event_timeout_sec: int = 300
    max_questions_per_turn: int = 3
    max_error_retries: int = 2
    error_retry_backoff_sec: int = 15
    #: the longest wait between two retries; 0 = no cap (the wait keeps doubling)
    error_retry_max_backoff_sec: int = 60
    #: one agent's hard limit from its start, whatever the turns add up to; the
    #: session is aborted as a stall and the tree is scored as it stands. 0 = off
    agent_max_sec: int = 0
    #: KC-62: Kilo's own store errors ("Failed to execute statement"): retried in
    #: the same session on this budget, which the provider's does not touch.
    #: 0 turns it off — the pre-KC-62 path.
    max_local_store_retries: int = 5
    #: KC-62: base backoff for those retries, doubled per retry and jittered ±30 %.
    local_store_retry_backoff_sec: int = 10
    #: KC-62: intake warns, and the heartbeat names them, when more than this many
    #: other Kilo processes of the same user share the server's store.
    neighbour_kilo_warn: int = 4
    #: KC-64: Kilo retries in a row with no model output before the agent
    #: ends ERROR provider_unavailable; 0 = off (wait for the turn deadline)
    provider_retry_max_attempts: int = 10
    #: KC-61: a ``session.status`` retry scheduled further out than this many
    #: seconds is a quota reset, not a blip: the agent ends
    #: ``ERROR provider_quota`` at once instead of sitting out the silence
    #: clock. 0 turns it off — today's behaviour, for a provider that names no
    #: reset time at all.
    provider_retry_max_wait_sec: float = 300.0
    #: KC-61: ``|``-separated quota phrases, case-insensitive literals, empty
    #: when unset. Matched against a provider error's text when Kilo names no
    #: retry time, and against every ``session.error``. Transient texts must
    #: not live here — they belong to KC-19's retry path.
    quota_patterns: str = ""
    progress_every_sec: int = 60
    #: KC-57: the wall-clock budget for one harvest's pytest roots.
    #: 0 turns it off, which keeps today's unbounded behaviour.
    harvest_budget_sec: int = 900
    #: KC-65: pytest-xdist workers each agent gets. 0 = by the agents that are
    #: live: one alone gets every core, up to `pytest_workers_few_agents` get
    #: `pytest_workers_few`, more get `pytest_workers_min` — always at most
    #: `cpu_count`. A value above 0 is fixed for the round.
    pytest_workers_per_agent: int = 0
    #: KC-65: up to this many live agents each get `pytest_workers_few`.
    pytest_workers_few_agents: int = 4
    #: KC-65: the workers each of those gets.
    pytest_workers_few: int = 4
    #: KC-65: the workers each gets above `pytest_workers_few_agents`; 1 is
    #: allowed, 0 is rejected.
    pytest_workers_min: int = 2
    #: KC-65: temp dir for the agents' commands, `${VAR}` expanded; "" =
    #: inherit TMPDIR. The round makes `<agent_tmpdir>/contest-<NN>` under it
    #: mode 0700 before the server spawns, and removes that dir — never the
    #: parent — when the round ends.
    agent_tmpdir: str = ""
    tmp_roots: tuple[str, ...] = ()
    deny_commands: tuple[str, ...] = ()
    ask_commands: tuple[str, ...] = ()
    gate_llm_profile: str = ""
    gate_max_calls_per_session: int = 20
    #: KC-55: how many times a gate call that hit a rate limit or a dropped
    #: connection is retried by ``tools.llm_stream.request_completion``'s own
    #: loop. 0 is today's fail-fast gate.
    gate_retries: int = 4
    #: KC-55: the wait between two of those retries when the server names no
    #: ``Retry-After``. KC-66: 30 s, 5 tries in all — a free gate key shared
    #: with eight agents needs more than 10 s to cool.
    gate_retry_wait_sec: float = 30.0
    #: KC-55: a ``Retry-After`` longer than this is a quota reset, not a blip:
    #: the call fails at once rather than waiting it out.
    gate_retry_max_wait_sec: float = 60.0
    #: KC-55: one whole gate decision, every retry and re-ask, stops no later
    #: than this many seconds in. ``policy.gate_worst_case_sec`` adds
    #: ``GATE_TIMEOUT`` to it and intake compares that with
    #: ``idle_event_timeout_sec``.
    gate_deadline_sec: float = 600.0
    out_dir: str = "contest-out"
    rounds_dir: str = "../rounds"
    #: KC-59: how ``prepare_round`` builds each agent's checkout — one of
    #: ``WORKSPACE_KINDS``. ``clone`` (the default) makes a fresh local clone
    #: per agent, ``worktree`` keeps the pre-KC-59 worktree per agent.
    workspace_kind: str = "clone"
    #: KC-49: the reasoning variant of every agent that names none — a name
    #: (``high``, ``max``), ``highest`` (the top one that answers, probed at
    #: intake) or ``default`` (no variant sent: the provider's own default).
    variant: str = "highest"
    #: KC-11: how many days a probe result is trusted before re-probing. 0 = always re-probe.
    probe_ttl_days: int = 7
    agents: tuple[AgentSpec, ...] = ()
    gate_settings: LlmSettings = field(default_factory=lambda: DEFAULTS_GATE)
    #: The backend the round runs on: one of ``BACKENDS``, one value per round.
    backend: str = "kilo"
    #: The OpenRouter agents' own credential, resolved from
    #: ``[contest] openrouter_llm_profile``. ``None`` unless ``backend ==
    #: "openrouter"``, where a profile that does not resolve is a
    #: ``RosterError`` at load time — the gate profile above is the safety
    #: gate's model, never the competing agents' credential, so the two are
    #: read separately.
    openrouter_settings: LlmSettings | None = None
    #: The profile name that ``openrouter_settings`` was resolved from.
    openrouter_llm_profile: str = ""

    def session_rules(self) -> list[dict]:
        """The rule list ``KiloClient.create_session`` sends, per session.

        The probe's three fixed rules first, then one ``bash`` ask per
        ``ask_commands`` entry in the order ``contest.ini`` lists them, then
        one ``bash`` deny per ``deny_commands`` entry. A command matching both
        is denied — Kilo's last match wins, which is what the probe relied on
        for ``*`` allow followed by ``external_directory`` ask.
        """
        rules = [dict(rule) for rule in BASE_RULES]
        for pattern in self.ask_commands:
            rules.append({"permission": "bash", "pattern": pattern, "action": "ask"})
        for pattern in self.deny_commands:
            rules.append({"permission": "bash", "pattern": pattern, "action": "deny"})
        return rules


# ─────────────────────────────────────────────────────────────────────────────
# reading
# ─────────────────────────────────────────────────────────────────────────────

def _new_parser() -> configparser.ConfigParser:
    """The parser shape for a roster file.

    ``interpolation=None``: a ``${ENV}`` reference is a literal this module
    expands itself (and a stray ``%`` in a ``deny_commands`` entry would
    otherwise be a format character). ``inline_comment_prefixes`` because
    ``contest.ini``, like ``agents.ini``, explains each value on its own line.
    """
    return configparser.ConfigParser(
        interpolation=None, inline_comment_prefixes=(";", "#")
    )


def _expand(parser: configparser.ConfigParser, sections) -> None:
    """Expand ``${ENV}`` in every value of every section named.

    Only sections this module actually reads are expanded, so an overlay such
    as ``agents.ini`` may carry ``${...}`` in a section nobody asks for without
    being told off. Values are written back into the parser, so
    ``resolve_llm_profile`` reads the expanded ``api_key`` rather than the
    reference.
    """
    for section in sections:
        if not section or not parser.has_section(section):
            continue
        for key in parser.options(section):
            parser.set(section, key, _expand_env(parser.get(section, key), section, key))


def _expand_env(raw: str, section: str, key: str) -> str:
    """``${NAME}`` in *raw* from the environment; an unset NAME names itself."""
    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise RosterError(
                f"[{section}] {key} references ${{{name}}}, which is not set "
                f"in the environment — export it, or move the value to "
                f"{LOCAL_FILENAME}"
            )
        return os.environ[name]

    return _ENV_RE.sub(replace, raw)


def _agent_sections(parser: configparser.ConfigParser) -> list[str]:
    """The roster sections, in file order."""
    return [s for s in parser.sections() if s.startswith(AGENT_SECTION_PREFIX)]


def _unknown_keys(parser: configparser.ConfigParser, section: str, known) -> list[str]:
    """The keys in *section* that are not in *known*, sorted."""
    if not parser.has_section(section):
        return []
    return sorted(k for k in parser.options(section) if k not in set(known))


def _split_list(raw: str) -> tuple[str, ...]:
    """A comma-separated value as a tuple of stripped, non-empty entries."""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _optional(parser: configparser.ConfigParser, section: str, key: str) -> str | None:
    """A value that is ``None`` when absent or blank — the probe's empty ``kilo_agent =``."""
    value = parser.get(section, key, fallback="").strip()
    return value or None


def _parse_agents(parser: configparser.ConfigParser) -> tuple[AgentSpec, ...]:
    """Every ``[contest.agent.<name>]`` section, in file order."""
    sections = _agent_sections(parser)
    agents: list[AgentSpec] = []
    seen: set[str] = set()
    for section in sections:
        name = section[len(AGENT_SECTION_PREFIX):]
        unknown = _unknown_keys(parser, section, AGENT_KEYS)
        if unknown:
            raise RosterError(
                f"[{section}] has unknown key(s) {', '.join(unknown)} — "
                f"known keys: {', '.join(AGENT_KEYS)}"
            )
        if not _NAME_RE.match(name):
            raise RosterError(
                f"[{section}] name {name!r} must match [a-z0-9][a-z0-9_-]* — it "
                f"becomes a branch name and a folder"
            )
        if name in seen:
            raise RosterError(f"duplicate agent name {name!r} in [{section}]")
        seen.add(name)

        model = parser.get(section, "model", fallback="").strip()
        if not model:
            raise RosterError(f"[{section}] is missing 'model' (providerID/modelID)")
        provider_id, slash, model_id = model.partition("/")
        if not slash or not model_id:
            raise RosterError(
                f"[{section}] model {model!r} must be providerID/modelID — it is "
                f"split at the first '/', as `kilo models` prints it "
                f"(kilo/~anthropic/x is provider 'kilo')"
            )
        agents.append(
            AgentSpec(
                name=name,
                provider_id=provider_id,
                model_id=model_id,
                kilo_agent=_optional(parser, section, "kilo_agent"),
                variant=_optional(parser, section, "variant"),
            )
        )

    if not agents:
        raise RosterError(
            "no [contest.agent.<name>] sections found — the roster is empty"
        )
    return tuple(agents)


def _build(parser: configparser.ConfigParser, backend: str | None = None) -> ContestConfig:
    """The config, from a parsed and merged roster.

    *backend* overrides the file's own ``[contest] backend =`` — ``--backend``
    is exactly such an override, so it is applied here, before the OpenRouter
    profile is expanded and resolved, not after: a roster that says ``kilo``
    still resolves the profile its ``openrouter_llm_profile`` names when the
    command asks for ``openrouter``.
    """
    unknown = _unknown_keys(parser, "contest", CONTEST_KEYS)
    if unknown:
        raise RosterError(
            f"[contest] has unknown key(s) {', '.join(unknown)} — "
            f"known keys: {', '.join(CONTEST_KEYS)}"
        )

    if backend is not None:
        file_backend = parser.get("contest", "backend", fallback="kilo").strip()
        if file_backend not in BACKENDS:
            raise RosterError(
                f"[contest] backend must be one of {' | '.join(BACKENDS)}, "
                f"got {file_backend!r}")
        backend = backend.strip()
    else:
        backend = parser.get("contest", "backend", fallback="kilo").strip()
    gate_profile = parser.get("contest", "gate_llm_profile", fallback="").strip()
    openrouter_profile = parser.get("contest", "openrouter_llm_profile", fallback="").strip()
    # The OpenRouter profile's own ${ENV} references are expanded only when the
    # backend needs them: a kilo round must not fail to load the roster because
    # an OpenRouter key is not exported, and the committed contest.ini carries
    # the ${CONTEST_OPENROUTER_API_KEY} reference for a machine that has one.
    profiles = [gate_profile]
    if backend == "openrouter":
        profiles.append(openrouter_profile)
    _expand(parser, ("contest", *_agent_sections(parser), *profiles))

    def scalar(key: str, default: str) -> str:
        return parser.get("contest", key, fallback=default).strip()

    def limit(key: str, default: int) -> int:
        return safe_getint(parser, "contest", key, fallback=default)

    def seconds(key: str, default: float) -> float:
        """KC-55: a time in seconds. Unlike ``limit``, a malformed value is a
        ``RosterError`` naming the key rather than a fallback: the gate's wait
        budget is exactly what a typo must not silently reset, and a negative
        one would cap a whole decision at zero seconds."""
        raw = parser.get("contest", key, fallback=str(default)).strip()
        try:
            value = float(raw)
        except ValueError:
            raise RosterError(f"[contest] {key} is not a number of seconds: {raw}") from None
        if value < 0:
            raise RosterError(f"[contest] {key} must be >= 0 seconds, got {raw}")
        return value

    def list_(key: str) -> tuple[str, ...]:
        return _split_list(parser.get("contest", key, fallback=""))

    if backend not in BACKENDS:
        raise RosterError(
            f"[contest] backend must be one of {' | '.join(BACKENDS)}, got {backend!r}")

    workspace_kind = scalar("workspace_kind", "clone") or "clone"
    if workspace_kind not in WORKSPACE_KINDS:
        raise RosterError(
            f"[contest] workspace_kind must be one of {' | '.join(WORKSPACE_KINDS)}, "
            f"got {workspace_kind!r}")

    agents = _parse_agents(parser)

    gate_retries = limit("gate_retries", 4)
    if gate_retries < 0:
        raise RosterError(f"[contest] gate_retries must be >= 0, got {gate_retries}")

    turn_timeout = limit("turn_timeout_sec", 1800)
    turn_extend = limit("turn_extend_sec", 600)
    turn_max = limit("turn_max_sec", 7200)
    if turn_extend < 0:
        raise RosterError(f"[contest] turn_extend_sec must be >= 0, got {turn_extend}")
    if turn_max < 0:
        raise RosterError(f"[contest] turn_max_sec must be >= 0, got {turn_max}")
    # KC-36: the ceiling cannot sit below the floor — an unextendable turn
    # would be killed before it had existed.
    if turn_max < turn_timeout:
        raise RosterError(
            f"[contest] turn_max_sec must be >= turn_timeout_sec — got "
            f"turn_max_sec={turn_max} and turn_timeout_sec={turn_timeout}")

    try:
        settings, _profile = resolve_llm_profile(
            parser, "contest", "gate_llm_profile", defaults=DEFAULTS_GATE
        )
    except ValueError as exc:
        raise RosterError(f"[contest] gate_llm_profile: {exc}") from exc

    # KC-65: a worker count that is zero would hand every agent's `-n auto` a
    # number it cannot use, so the two bands that feed it refuse 0; `1` is
    # allowed for `pytest_workers_min` (a crowded box beats a slow one) and
    # negatives are refused for all four.
    pytest_workers_per_agent = limit("pytest_workers_per_agent", 0)
    pytest_workers_few_agents = limit("pytest_workers_few_agents", 4)
    pytest_workers_few = limit("pytest_workers_few", 4)
    pytest_workers_min = limit("pytest_workers_min", 2)
    for key, value in (("pytest_workers_per_agent", pytest_workers_per_agent),
                       ("pytest_workers_few_agents", pytest_workers_few_agents),
                       ("pytest_workers_few", pytest_workers_few),
                       ("pytest_workers_min", pytest_workers_min)):
        if value < 0:
            raise RosterError(f"[contest] {key} must be >= 0, got {value}")
    if pytest_workers_few < 1:
        raise RosterError(f"[contest] pytest_workers_few must be >= 1, got {pytest_workers_few}")
    if pytest_workers_min < 1:
        raise RosterError(f"[contest] pytest_workers_min must be >= 1, got {pytest_workers_min}")

    openrouter_settings = None
    if backend == "openrouter":
        # the agents' own credential: required only for that backend, resolved
        # the same way the gate's is and only then, so a kilo round never needs
        # an OpenRouter key to load the roster
        if not openrouter_profile:
            raise RosterError(
                "[contest] backend = openrouter needs openrouter_llm_profile — "
                "the agents' own base_url and api_key, as [contest_gate_llm] is "
                "for the gate")
        try:
            openrouter_settings, _ = resolve_llm_profile(
                parser, "contest", "openrouter_llm_profile", defaults=DEFAULTS_OPENROUTER
            )
        except ValueError as exc:
            raise RosterError(f"[contest] openrouter_llm_profile: {exc}") from exc

    return ContestConfig(
        kilo_bin=scalar("kilo_bin", "auto"),
        server=scalar("server", "spawn"),
        max_parallel=limit("max_parallel", 3),
        max_rework=limit("max_rework", 2),
        max_continues_per_attempt=limit("max_continues_per_attempt", 2),
        turn_timeout_sec=turn_timeout,
        turn_extend_sec=turn_extend,
        turn_max_sec=turn_max,
        idle_event_timeout_sec=limit("idle_event_timeout_sec", 300),
        max_questions_per_turn=limit("max_questions_per_turn", 3),
        max_error_retries=limit("max_error_retries", 2),
        error_retry_backoff_sec=limit("error_retry_backoff_sec", 15),
        error_retry_max_backoff_sec=limit("error_retry_max_backoff_sec", 60),
        agent_max_sec=limit("agent_max_sec", 0),
        max_local_store_retries=limit("max_local_store_retries", 5),
        local_store_retry_backoff_sec=limit("local_store_retry_backoff_sec", 10),
        neighbour_kilo_warn=limit("neighbour_kilo_warn", 4),
        provider_retry_max_attempts=limit("provider_retry_max_attempts", 10),
        provider_retry_max_wait_sec=seconds("provider_retry_max_wait_sec", 300.0),
        quota_patterns=scalar("quota_patterns", ""),
        progress_every_sec=limit("progress_every_sec", 60),
        harvest_budget_sec=max(0, limit("harvest_budget_sec", 900)),
        pytest_workers_per_agent=pytest_workers_per_agent,
        pytest_workers_few_agents=pytest_workers_few_agents,
        pytest_workers_few=pytest_workers_few,
        pytest_workers_min=pytest_workers_min,
        agent_tmpdir=scalar("agent_tmpdir", ""),
        tmp_roots=list_("tmp_roots"),
        deny_commands=list_("deny_commands"),
        ask_commands=list_("ask_commands"),
        gate_llm_profile=gate_profile,
        gate_max_calls_per_session=limit("gate_max_calls_per_session", 20),
        gate_retries=gate_retries,
        gate_retry_wait_sec=seconds("gate_retry_wait_sec", 30.0),
        gate_retry_max_wait_sec=seconds("gate_retry_max_wait_sec", 60.0),
        gate_deadline_sec=seconds("gate_deadline_sec", 600.0),
        out_dir=scalar("out_dir", "contest-out"),
        rounds_dir=scalar("rounds_dir", "../rounds"),
        workspace_kind=workspace_kind,
        variant=scalar("variant", "highest") or "highest",
        probe_ttl_days=int(scalar("probe_ttl_days", "7") or "7"),
        agents=agents,
        gate_settings=settings,
        backend=backend,
        openrouter_settings=openrouter_settings,
        openrouter_llm_profile=openrouter_profile,
    )


def load_roster(path, *, overlay=None, backend=None) -> ContestConfig:
    """Load *path*, then ``contest.local.ini`` next to it, then *overlay*.

    Later sources override earlier ones key by key, which is where a real
    ``api_key`` overrides the committed ``${CONTEST_GATE_API_KEY}`` reference.
    Raises :class:`RosterError` — naming the section and key — on a missing or
    unreadable source, an unset ``${ENV}`` reference, a duplicate section or
    option, an unknown key in ``[contest]`` or an agent section, a ``backend``
    that is neither ``kilo`` nor ``openrouter``, a ``backend = openrouter``
    whose ``openrouter_llm_profile`` does not resolve, an agent name that
    cannot become a branch, a ``model`` without a ``/`` or an empty roster.
    Unknown sections are ignored, so an overlay may be ``agents.ini``.
    """
    roster = Path(path)
    if not roster.is_file():
        raise RosterError(f"roster file does not exist: {roster}")

    sources = [roster]
    local = roster.with_name(LOCAL_FILENAME)
    if local.is_file():
        sources.append(local)
    if overlay is not None:
        sources.append(Path(overlay))

    parser = _new_parser()
    try:
        read = set(parser.read([str(source) for source in sources]))
    except configparser.Error as exc:
        raise RosterError(f"{exc}") from exc
    unread = [str(source) for source in sources if str(source) not in read]
    if unread:
        raise RosterError(f"roster source could not be read: {', '.join(unread)}")

    return _build(parser, backend)
