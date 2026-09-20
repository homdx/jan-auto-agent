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
    "DEFAULTS",
    "DEFAULTS_GATE",
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
    "max_parallel",
    "max_rework",
    "turn_timeout_sec",
    "idle_event_timeout_sec",
    "max_questions_per_turn",
    "max_error_retries",
    "error_retry_backoff_sec",
    "progress_every_sec",
    "tmp_roots",
    "deny_commands",
    "gate_llm_profile",
    "gate_max_calls_per_session",
    "out_dir",
    "rounds_dir",
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

#: ``[a-z0-9][a-z0-9_-]*`` — an agent name becomes a branch and a folder.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: ``${NAME}`` in a value: expanded from the environment.
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class RosterError(ValueError):
    """``contest.ini`` cannot be loaded.

    Raised for a missing roster file, an unreadable source, a ``${ENV}``
    reference to an unset variable, a duplicate section or option, an unknown
    key in ``[contest]`` or an agent section, an agent name that cannot become
    a branch, a ``model`` with no ``/``, and an empty roster. Always says which
    section and key is at fault — the operator has to be able to find the typo.
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
    turn_timeout_sec: int = 1800
    idle_event_timeout_sec: int = 300
    max_questions_per_turn: int = 3
    max_error_retries: int = 2
    error_retry_backoff_sec: int = 15
    progress_every_sec: int = 60
    tmp_roots: tuple[str, ...] = ()
    deny_commands: tuple[str, ...] = ()
    gate_llm_profile: str = ""
    gate_max_calls_per_session: int = 20
    out_dir: str = "contest-out"
    rounds_dir: str = "../rounds"
    agents: tuple[AgentSpec, ...] = ()
    gate_settings: LlmSettings = field(default_factory=lambda: DEFAULTS_GATE)

    def session_rules(self) -> list[dict]:
        """The rule list ``KiloClient.create_session`` sends, per session.

        The probe's three fixed rules first, then one ``bash`` deny per
        ``deny_commands`` entry in the order ``contest.ini`` lists them. A new
        list on every call, so a caller can append without affecting another.
        """
        rules = [dict(rule) for rule in BASE_RULES]
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


def _build(parser: configparser.ConfigParser) -> ContestConfig:
    """The config, from a parsed and merged roster."""
    unknown = _unknown_keys(parser, "contest", CONTEST_KEYS)
    if unknown:
        raise RosterError(
            f"[contest] has unknown key(s) {', '.join(unknown)} — "
            f"known keys: {', '.join(CONTEST_KEYS)}"
        )

    gate_profile = parser.get("contest", "gate_llm_profile", fallback="").strip()
    _expand(parser, ("contest", *_agent_sections(parser), gate_profile))

    def scalar(key: str, default: str) -> str:
        return parser.get("contest", key, fallback=default).strip()

    def limit(key: str, default: int) -> int:
        return safe_getint(parser, "contest", key, fallback=default)

    def list_(key: str) -> tuple[str, ...]:
        return _split_list(parser.get("contest", key, fallback=""))

    agents = _parse_agents(parser)

    try:
        settings, _profile = resolve_llm_profile(
            parser, "contest", "gate_llm_profile", defaults=DEFAULTS_GATE
        )
    except ValueError as exc:
        raise RosterError(f"[contest] gate_llm_profile: {exc}") from exc

    return ContestConfig(
        kilo_bin=scalar("kilo_bin", "auto"),
        server=scalar("server", "spawn"),
        max_parallel=limit("max_parallel", 3),
        max_rework=limit("max_rework", 2),
        turn_timeout_sec=limit("turn_timeout_sec", 1800),
        idle_event_timeout_sec=limit("idle_event_timeout_sec", 300),
        max_questions_per_turn=limit("max_questions_per_turn", 3),
        max_error_retries=limit("max_error_retries", 2),
        error_retry_backoff_sec=limit("error_retry_backoff_sec", 15),
        progress_every_sec=limit("progress_every_sec", 60),
        tmp_roots=list_("tmp_roots"),
        deny_commands=list_("deny_commands"),
        gate_llm_profile=gate_profile,
        gate_max_calls_per_session=limit("gate_max_calls_per_session", 20),
        out_dir=scalar("out_dir", "contest-out"),
        rounds_dir=scalar("rounds_dir", "../rounds"),
        agents=agents,
        gate_settings=settings,
    )


def load_roster(path, *, overlay=None) -> ContestConfig:
    """Load *path*, then ``contest.local.ini`` next to it, then *overlay*.

    Later sources override earlier ones key by key, which is where a real
    ``api_key`` overrides the committed ``${CONTEST_GATE_API_KEY}`` reference.
    Raises :class:`RosterError` — naming the section and key — on a missing or
    unreadable source, an unset ``${ENV}`` reference, a duplicate section or
    option, an unknown key in ``[contest]`` or an agent section, an agent name
    that cannot become a branch, a ``model`` without a ``/`` or an empty
    roster. Unknown sections are ignored, so an overlay may be ``agents.ini``.
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

    return _build(parser)
