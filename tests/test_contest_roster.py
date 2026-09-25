"""tests/test_contest_roster.py — KC-2: ``contest.ini`` — the roster, the limits, the gate.

`tools/contest/roster.py` turns `contest.ini` into the one place the round's
information lives: the limits the runner (KC-6) reads, the roster the policy
(KC-3) judges and the gate's own LLM profile, resolved through
`tools.auto.llm_profile.resolve_llm_profile` the way `[gate1]
presence_llm_profile = gate1_llm` does. Without the module every test below
fails at import.

Nothing here calls a provider or a kilo server: every roster is written into
`tmp_path` with a literal api_key, and the one committed file is loaded read
only. The committed `contest.ini` points `api_key` at `${CONTEST_GATE_API_KEY}`,
so the tests export a dummy for it — never a real secret.

The cases, from the ticket's acceptance list:

  1. the committed `contest.ini` loads, with the three probe models and their
     provider/model split;
  2. `kilo/~anthropic/claude-haiku-latest` splits at the first slash;
  3. an unset `${ENV}` errors naming the key;
  4. an unknown `[contest]` key errors naming it;
  5. a duplicate agent name errors;
  6. `contest.local.ini` overrides `api_key`;
  7. `session_rules()` is the probe's three rules first, then one `bash` deny
     per `deny_commands` entry, in order;
  8. `gate_settings` comes from `resolve_llm_profile` (asserted by
     monkeypatching it), with `response_format` true by default;
  9. `.gitignore` ignores `contest.local.ini` and `contest-out/`.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.auto.llm_profile import LlmSettings
from tools.contest import roster
from tools.contest.roster import (
    AGENT_KEYS,
    AGENT_SECTION_PREFIX,
    BASE_RULES,
    CONTEST_KEYS,
    AgentSpec,
    ContestConfig,
    RosterError,
    load_roster,
)

COMMITTED = REPO_ROOT / "contest.ini"
LOCAL = "contest.local.ini"

#: The api_key the committed contest.ini asks the environment for.
GATE_KEY_ENV = "CONTEST_GATE_API_KEY"

#: The rules the probe sent in every session (PROBE.md) — the first three of
#: every rule list, before the deny_commands are appended.
PROBE_RULES = [
    {"permission": "*", "pattern": "*", "action": "allow"},
    {"permission": "external_directory", "pattern": "*", "action": "ask"},
    {"permission": "doom_loop", "pattern": "*", "action": "ask"},
]

#: A roster that loads with nothing exotic: one agent, a literal api_key.
MINIMAL = """
[contest]
max_parallel = 2
gate_llm_profile = gate
deny_commands = git push*, sudo *

[gate]
base_url = https://example/v1
api_key = test-key
model = test/model
response_format = true

[contest.agent.alpha]
model = kenary/hy3:free
"""


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def write_ini(directory: Path, text: str, name: str = "contest.ini") -> Path:
    """Write *text* as a roster in *directory* and return its path."""
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def add_to_contest(text: str, line: str) -> str:
    """One more key in the `[contest]` section of *text*."""
    return text.replace("[contest]\n", "[contest]\n" + line + "\n")


def add_to_agent(text: str, line: str) -> str:
    """One more key in the `[contest.agent.alpha]` section of *text*."""
    return text.replace("model = kenary/hy3:free\n", "model = kenary/hy3:free\n" + line + "\n")


def roster_only() -> str:
    """MINIMAL with its agent section removed: limits and a gate, no roster."""
    return MINIMAL.split("[contest.agent.alpha]")[0]


@pytest.fixture
def gate_key(monkeypatch):
    """The committed roster's ${CONTEST_GATE_API_KEY}, as a dummy value."""
    monkeypatch.setenv(GATE_KEY_ENV, "test-gate-key")
    return "test-gate-key"


# ─────────────────────────────────────────────────────────────────────────────
# the committed contest.ini
# ─────────────────────────────────────────────────────────────────────────────

def test_committed_contest_ini_loads(gate_key):
    cfg = load_roster(COMMITTED)

    assert isinstance(cfg, ContestConfig)
    assert [agent.name for agent in cfg.agents] == ["laguna", "mistral", "hy3"]
    assert cfg.agents[0].provider_id == "kenary"
    assert cfg.agents[0].model_id == "laguna-s-2-1:free"
    assert cfg.agents[1].model_id == "mistral-medium-3-5:free"
    assert cfg.agents[2].model_id == "hy3:free"
    # `kilo_agent =` is empty in the committed file: the server's default
    assert cfg.agents[0].kilo_agent is None
    assert cfg.agents[0].variant is None
    assert cfg.agents[0].model == "kenary/laguna-s-2-1:free"

    assert cfg.kilo_bin == "auto"
    assert cfg.server == "spawn"
    assert cfg.max_parallel == 3
    assert cfg.max_rework == 2
    assert cfg.turn_timeout_sec == 3600
    assert cfg.idle_event_timeout_sec == 900
    assert cfg.max_questions_per_turn == 3
    assert cfg.tmp_roots == ("/tmp/kilo/*", "/tmp/contest/*")
    assert cfg.ask_commands == (
        "*>*", "*|*tee *", "cp *", "mv *", "ln *", "rsync *", "install *", "dd *",
    )
    assert cfg.gate_max_calls_per_session == 20
    assert cfg.out_dir == "contest-out"
    assert cfg.rounds_dir == "../rounds"
    assert cfg.gate_llm_profile == "contest_gate_llm"


def test_committed_limits_are_ints(gate_key):
    cfg = load_roster(COMMITTED)
    for key in ("max_parallel", "max_rework", "turn_timeout_sec",
                "idle_event_timeout_sec", "max_questions_per_turn",
                "progress_every_sec", "gate_max_calls_per_session"):
        assert isinstance(getattr(cfg, key), int), key


def test_committed_ask_commands_keep_their_text_and_order(gate_key):
    assert load_roster(COMMITTED).ask_commands == (
        "*>*", "*|*tee *", "cp *", "mv *", "ln *", "rsync *", "install *", "dd *",
    )


def test_committed_denied_commands_keep_their_text_and_order(gate_key):
    assert load_roster(COMMITTED).deny_commands == (
        "git push*", "sudo *", "rm -rf /*", "curl * | sh", "wget * | sh",
    )


def test_committed_api_key_is_an_env_reference_not_a_secret():
    """The committed file must never carry a credential."""
    text = COMMITTED.read_text(encoding="utf-8")
    assert f"${{{GATE_KEY_ENV}}}" in text
    assert "sk-" not in text
    assert "<token>" not in text


# ─────────────────────────────────────────────────────────────────────────────
# the model split
# ─────────────────────────────────────────────────────────────────────────────

def test_model_splits_at_the_first_slash(tmp_path):
    path = write_ini(
        tmp_path,
        """
[contest]

[gate]
base_url = https://example/v1
api_key = test-key
model = test/model

[contest.agent.haiku]
model = kilo/~anthropic/claude-haiku-latest
kilo_agent = build
variant = high
""",
    )
    cfg = load_roster(path)

    (agent,) = cfg.agents
    assert agent.name == "haiku"
    assert isinstance(agent, AgentSpec)
    assert agent.provider_id == "kilo"
    assert agent.model_id == "~anthropic/claude-haiku-latest"
    assert agent.model == "kilo/~anthropic/claude-haiku-latest"
    assert agent.kilo_agent == "build"
    assert agent.variant == "high"


def test_model_without_a_slash_names_its_section(tmp_path):
    text = MINIMAL.replace("model = kenary/hy3:free", "model = hy3:free")
    with pytest.raises(ValueError, match=r"\[contest\.agent\.alpha\]"):
        load_roster(write_ini(tmp_path, text))


# ─────────────────────────────────────────────────────────────────────────────
# fail closed: typos are found at load time, naming themselves
# ─────────────────────────────────────────────────────────────────────────────

def test_unset_env_reference_names_the_section_and_key(tmp_path):
    text = add_to_contest(MINIMAL, "out_dir = ${SURELY_NOT_SET_ROSTER}")
    with pytest.raises(RosterError, match=r"\[contest\] out_dir"):
        load_roster(write_ini(tmp_path, text))
    with pytest.raises(ValueError, match="SURELY_NOT_SET_ROSTER"):
        load_roster(write_ini(tmp_path, text))


def test_unknown_contest_key_names_it(tmp_path):
    text = add_to_contest(MINIMAL, "max_paralle = 3")
    with pytest.raises(RosterError, match=r"\[contest\] has unknown key\(s\) max_paralle"):
        load_roster(write_ini(tmp_path, text))


def test_unknown_agent_key_names_it(tmp_path):
    text = add_to_agent(MINIMAL, "max_rework = 1")
    with pytest.raises(RosterError, match=r"\[contest\.agent\.alpha\] has unknown key\(s\) max_rework"):
        load_roster(write_ini(tmp_path, text))


def test_duplicate_agent_name_errors(tmp_path):
    text = MINIMAL + "\n[contest.agent.alpha]\nmodel = kenary/laguna-s-2-1:free\n"
    with pytest.raises(RosterError, match="contest.agent.alpha"):
        load_roster(write_ini(tmp_path, text))


def test_duplicate_key_in_a_source_names_it(tmp_path):
    text = add_to_contest(MINIMAL, "max_parallel = 4")
    with pytest.raises(RosterError, match="max_parallel"):
        load_roster(write_ini(tmp_path, text))


def test_agent_name_that_cannot_be_a_branch_errors(tmp_path):
    text = MINIMAL.replace("[contest.agent.alpha]", "[contest.agent.Alpha]")
    with pytest.raises(RosterError, match="Alpha"):
        load_roster(write_ini(tmp_path, text))


def test_empty_roster_errors(tmp_path):
    with pytest.raises(RosterError, match="empty"):
        load_roster(write_ini(tmp_path, roster_only()))


def test_missing_model_names_its_section(tmp_path):
    text = MINIMAL.replace("model = kenary/hy3:free", "model =")
    with pytest.raises(RosterError, match=r"\[contest\.agent\.alpha\] is missing 'model'"):
        load_roster(write_ini(tmp_path, text))


def test_missing_roster_file_names_it(tmp_path):
    with pytest.raises(RosterError, match="does not exist"):
        load_roster(tmp_path / "no-such.ini")


def test_unreadable_overlay_names_it(tmp_path):
    with pytest.raises(RosterError, match="could not be read"):
        load_roster(write_ini(tmp_path, MINIMAL),
                    overlay=tmp_path / "no-such-overlay.ini")


# ─────────────────────────────────────────────────────────────────────────────
# fail open: an absent or malformed value falls back, it never aborts a round
# ─────────────────────────────────────────────────────────────────────────────

def test_absent_keys_take_their_documented_defaults(tmp_path):
    text = """
[contest]

[contest.agent.alpha]
model = kenary/hy3:free
"""
    cfg = load_roster(write_ini(tmp_path, text))

    assert cfg.kilo_bin == "auto"
    assert cfg.server == "spawn"
    assert cfg.max_parallel == 3
    assert cfg.max_rework == 2
    assert cfg.turn_timeout_sec == 1800
    assert cfg.idle_event_timeout_sec == 300
    assert cfg.max_questions_per_turn == 3
    assert cfg.max_error_retries == 2
    assert cfg.error_retry_backoff_sec == 15
    assert cfg.error_retry_max_backoff_sec == 60
    assert cfg.agent_max_sec == 0
    assert cfg.progress_every_sec == 60
    assert cfg.tmp_roots == ()
    assert cfg.deny_commands == ()
    assert cfg.gate_max_calls_per_session == 20
    assert cfg.out_dir == "contest-out"
    assert cfg.rounds_dir == "../rounds"
    # no gate profile at all: the defaults, and response_format stays true
    assert cfg.gate_settings is roster.DEFAULTS_GATE
    assert cfg.gate_settings.response_format is True
    assert cfg.agents[0].kilo_agent is None
    assert cfg.agents[0].variant is None


def test_progress_every_sec_parses_from_contest_and_zero_is_off(tmp_path):
    """KC-18: the heartbeat's period; 0 disables it."""
    base = """
[contest]
progress_every_sec = %s

[contest.agent.alpha]
model = kenary/hy3:free
"""
    assert load_roster(write_ini(tmp_path, base % "15")).progress_every_sec == 15
    assert load_roster(write_ini(tmp_path, base % "0")).progress_every_sec == 0
    assert "progress_every_sec" in CONTEST_KEYS


def test_max_error_retries_and_error_retry_backoff_sec_parse_and_default(tmp_path):
    """KC-19: both keys parse, default, and are in CONTEST_KEYS."""
    assert "max_error_retries" in CONTEST_KEYS
    assert "error_retry_backoff_sec" in CONTEST_KEYS
    base = """
[contest]
max_error_retries = %s
error_retry_backoff_sec = %s

[contest.agent.alpha]
model = kenary/hy3:free
"""
    cfg = load_roster(write_ini(tmp_path, base % ("5", "30")))
    assert cfg.max_error_retries == 5
    assert cfg.error_retry_backoff_sec == 30
    cfg0 = load_roster(write_ini(tmp_path, base % ("0", "0")))
    assert cfg0.max_error_retries == 0
    assert cfg0.error_retry_backoff_sec == 0
    for key in ("max_error_retries", "error_retry_backoff_sec"):
        assert isinstance(getattr(cfg, key), int)


def test_error_retry_max_backoff_sec_parses_and_is_a_contest_key(tmp_path):
    """The cap on the wait between retries; 0 = no cap."""
    assert "error_retry_max_backoff_sec" in CONTEST_KEYS
    assert "agent_max_sec" in CONTEST_KEYS
    base = """
[contest]
error_retry_max_backoff_sec = %s

[contest.agent.alpha]
model = kenary/hy3:free
"""
    assert load_roster(write_ini(tmp_path, base % "90")).error_retry_max_backoff_sec == 90
    assert load_roster(write_ini(tmp_path, base % "0")).error_retry_max_backoff_sec == 0


def test_max_continues_per_attempt_defaults_to_two_and_parses(tmp_path):
    """KC-22: the key, its default, and that it is in CONTEST_KEYS."""
    assert "max_continues_per_attempt" in CONTEST_KEYS
    cfg = load_roster(write_ini(tmp_path, MINIMAL))
    assert cfg.max_continues_per_attempt == 2
    base = """
[contest]
max_continues_per_attempt = %s

[contest.agent.alpha]
model = kenary/hy3:free
"""
    assert load_roster(write_ini(tmp_path, base % "1")).max_continues_per_attempt == 1
    # 0 disables the mechanism; a negative value parses to itself and the
    # runner's `> 0` guard reads it as off, like the other [contest] ints.
    assert load_roster(write_ini(tmp_path, base % "0")).max_continues_per_attempt == 0
    assert load_roster(write_ini(tmp_path, base % "-1")).max_continues_per_attempt == -1


def test_max_continues_per_attempt_malformed_falls_back_to_the_default(tmp_path):
    """KC-22: like the other [contest] ints, a malformed value is fail-open — it
    falls back to the default (2), never aborting a round."""
    for bad in ("three", "1.5"):
        text = add_to_contest(MINIMAL, f"max_continues_per_attempt = {bad}")
        assert load_roster(write_ini(tmp_path, text)).max_continues_per_attempt == 2


def test_committed_max_continues_per_attempt_is_two(gate_key):
    assert load_roster(COMMITTED).max_continues_per_attempt == 2


def test_malformed_limit_falls_back_to_the_default(tmp_path):
    text = add_to_contest(MINIMAL, "turn_timeout_sec = three")
    assert load_roster(write_ini(tmp_path, text)).turn_timeout_sec == 1800


def test_malformed_gate_boolean_falls_back_inside_the_profile(tmp_path):
    text = MINIMAL.replace("response_format = true", "response_format = maybe")
    assert load_roster(write_ini(tmp_path, text)).gate_settings.response_format is True


# ─────────────────────────────────────────────────────────────────────────────
# layers: contest.ini -> contest.local.ini -> overlay
# ─────────────────────────────────────────────────────────────────────────────

def test_local_ini_overrides_the_api_key(tmp_path):
    base = write_ini(tmp_path, MINIMAL.replace("api_key = test-key",
                                               "api_key = base-key"))
    assert load_roster(base).gate_settings.api_key == "base-key"

    write_ini(tmp_path, "[gate]\napi_key = local-key\n", name=LOCAL)
    assert load_roster(base).gate_settings.api_key == "local-key"


def test_local_ini_overrides_a_limit_and_adds_an_agent(tmp_path):
    base = write_ini(tmp_path, MINIMAL.replace("max_parallel = 2", "max_parallel = 1"))
    write_ini(
        tmp_path,
        "[contest]\nmax_parallel = 7\n"
        "[contest.agent.gamma]\nmodel = kenary/mistral-medium-3-5:free\n",
        name=LOCAL,
    )

    cfg = load_roster(base)
    assert cfg.max_parallel == 7
    assert [agent.name for agent in cfg.agents] == ["alpha", "gamma"]


def test_overlay_is_applied_after_the_local_file(tmp_path):
    base = write_ini(tmp_path, MINIMAL.replace("model = kenary/hy3:free",
                                               "model = kenary/laguna-s-2-1:free"))
    assert load_roster(base).agents[0].model_id == "laguna-s-2-1:free"

    write_ini(tmp_path, "[gate]\napi_key = local-key\n", name=LOCAL)
    write_ini(tmp_path, "[contest.agent.alpha]\nmodel = kenary/hy3:free\n",
              name="overlay.ini")

    cfg = load_roster(base, overlay=tmp_path / "overlay.ini")
    assert cfg.agents[0].model_id == "hy3:free"
    # and the local file is still honoured underneath the overlay
    assert cfg.gate_settings.api_key == "local-key"


def test_overlay_can_supply_the_whole_roster(tmp_path):
    """A [contest] with no agents is an empty roster; the overlay fills it in."""
    base = write_ini(tmp_path, roster_only())
    with pytest.raises(RosterError, match="empty"):
        load_roster(base)

    overlay = write_ini(tmp_path, "[contest.agent.alpha]\nmodel = kenary/hy3:free\n",
                        name="overlay.ini")
    cfg = load_roster(base, overlay=overlay)
    assert [agent.name for agent in cfg.agents] == ["alpha"]


def test_unknown_sections_are_ignored(tmp_path):
    """agents.ini can be passed as an overlay without complaint."""
    cfg = load_roster(write_ini(tmp_path, MINIMAL), overlay=REPO_ROOT / "agents.ini")
    assert [agent.name for agent in cfg.agents] == ["alpha"]
    assert cfg.gate_settings.api_key == "test-key"


def test_env_reference_in_an_ignored_section_is_not_expanded(tmp_path):
    """Only sections this module reads are expanded, so an overlay may carry ${}."""
    text = MINIMAL + "\n[unrelated]\ntoken = ${SURELY_NOT_SET_ROSTER}\n"
    assert load_roster(write_ini(tmp_path, text)).gate_settings.api_key == "test-key"


# ─────────────────────────────────────────────────────────────────────────────
# the session rules: one place, the probe's shape
# ─────────────────────────────────────────────────────────────────────────────

def test_session_rules_are_the_probe_rules_then_one_deny_per_command():
    rules = ContestConfig(deny_commands=("git push*", "sudo *", "rm -rf /*")).session_rules()

    assert rules[:3] == PROBE_RULES
    assert rules == PROBE_RULES + [
        {"permission": "bash", "pattern": "git push*", "action": "deny"},
        {"permission": "bash", "pattern": "sudo *", "action": "deny"},
        {"permission": "bash", "pattern": "rm -rf /*", "action": "deny"},
    ]


def test_session_rules_follow_the_order_of_deny_commands():
    rules = ContestConfig(deny_commands=("z*", "a*")).session_rules()
    assert [rule["pattern"] for rule in rules[3:]] == ["z*", "a*"]
    assert all(rule["permission"] == "bash" and rule["action"] == "deny"
               for rule in rules[3:])


def test_session_rules_ask_before_deny(tmp_path):
    cfg = ContestConfig(
        ask_commands=("*>*", "cp *"),
        deny_commands=("git push*",),
    )
    rules = cfg.session_rules()

    assert rules[:3] == PROBE_RULES
    assert rules == PROBE_RULES + [
        {"permission": "bash", "pattern": "*>*", "action": "ask"},
        {"permission": "bash", "pattern": "cp *", "action": "ask"},
        {"permission": "bash", "pattern": "git push*", "action": "deny"},
    ]


def test_session_rules_ask_commands_empty_keeps_probe_rules(tmp_path):
    cfg = ContestConfig()
    assert cfg.session_rules() == PROBE_RULES


def test_session_rules_ask_and_deny_in_file_order(tmp_path):
    rules = ContestConfig(
        ask_commands=("z*", "a*"),
        deny_commands=("z*", "a*"),
    ).session_rules()
    asks = [r for r in rules if r["action"] == "ask" and r["permission"] == "bash"]
    denies = [r for r in rules if r["action"] == "deny" and r["permission"] == "bash"]
    assert [r["pattern"] for r in asks] == ["z*", "a*"]
    assert [r["pattern"] for r in denies] == ["z*", "a*"]
    assert all(r["permission"] == "bash" for r in asks)
    assert all(r["permission"] == "bash" for r in denies)
    # asks come before denies
    assert rules.index(asks[0]) < rules.index(denies[0])


def test_absent_ask_commands_key_defaults_to_empty(tmp_path):
    text = """
[contest]

[contest.agent.alpha]
model = kenary/hy3:free
"""
    cfg = load_roster(write_ini(tmp_path, text))
    assert cfg.ask_commands == ()
    assert cfg.session_rules() == PROBE_RULES


def test_ask_commands_in_contest_keys():
    assert "ask_commands" in CONTEST_KEYS
    assert "deny_commands" in CONTEST_KEYS


def test_session_rules_returns_a_fresh_list_every_call():
    cfg = ContestConfig(deny_commands=("git push*",))
    cfg.session_rules().append({"permission": "bash", "pattern": "added", "action": "deny"})
    assert cfg.session_rules() == PROBE_RULES + [
        {"permission": "bash", "pattern": "git push*", "action": "deny"},
    ]


def test_committed_rules_match_the_probe(gate_key):
    cfg = load_roster(COMMITTED)
    rules = cfg.session_rules()
    assert rules[:3] == PROBE_RULES
    assert len(rules) == 3 + len(cfg.ask_commands) + len(cfg.deny_commands)
    assert [rule["pattern"] for rule in rules[3:3 + len(cfg.ask_commands)]] == list(cfg.ask_commands)
    assert [rule["pattern"] for rule in rules[3 + len(cfg.ask_commands):]] == list(cfg.deny_commands)
    assert list(BASE_RULES) == PROBE_RULES


# ─────────────────────────────────────────────────────────────────────────────
# the gate profile
# ─────────────────────────────────────────────────────────────────────────────

def test_gate_settings_comes_from_resolve_llm_profile(monkeypatch, tmp_path):
    calls = []
    settings = LlmSettings(base_url="https://x/v1", api_key="k", model="m",
                           response_format=True)

    def fake_resolve(config, section, key, *, defaults):
        calls.append((config, section, key, defaults))
        return settings, "gate"

    monkeypatch.setattr(roster, "resolve_llm_profile", fake_resolve)
    cfg = load_roster(write_ini(tmp_path, MINIMAL))

    assert len(calls) == 1
    config, section, key, defaults = calls[0]
    assert section == "contest"
    assert key == "gate_llm_profile"
    assert config.get("contest", "gate_llm_profile") == "gate"
    # the fallback profile is the gate's own, not another caller's
    assert defaults.response_format is True
    assert defaults.api_format == "openai"
    assert cfg.gate_settings is settings


def test_gate_defaults_answer_in_json():
    assert roster.DEFAULTS is roster.DEFAULTS_GATE
    assert roster.DEFAULTS_GATE.response_format is True
    assert roster.DEFAULTS_GATE.api_format == "openai"


def test_gate_profile_reads_the_committed_section(gate_key, tmp_path):
    # The operator's real gate lives in a git-ignored contest.local.ini next to
    # the committed file, so `load_roster(COMMITTED)` layers it on and reddens
    # these placeholders on exactly the machines configured to run rounds. Read
    # the committed section straight from HEAD, into a directory with no local
    # override, so the test asserts what is committed and nothing the operator
    # put beside it.
    committed = subprocess.run(
        ["git", "show", "HEAD:contest.ini"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    settings = load_roster(write_ini(tmp_path, committed)).gate_settings
    assert settings.base_url == "https://example/v1"
    assert settings.api_key == gate_key
    assert settings.model == "some/model"
    assert settings.api_format == "openai"
    assert settings.response_format is True
    assert settings.temperature == 0.0
    assert settings.max_tokens == 256


def test_gate_profile_without_its_section_names_it(tmp_path):
    text = MINIMAL.replace("[gate]", "[not-gate]")
    with pytest.raises(RosterError, match="gate_llm_profile"):
        load_roster(write_ini(tmp_path, text))


def test_gate_profile_missing_a_required_field_names_it(tmp_path):
    text = MINIMAL.replace("model = test/model\n", "")
    with pytest.raises(RosterError, match="required option"):
        load_roster(write_ini(tmp_path, text))


def test_gate_profile_expands_an_env_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("ROSTER_GATE_TEST_KEY", "expanded-key")
    text = MINIMAL.replace("api_key = test-key", "api_key = ${ROSTER_GATE_TEST_KEY}")
    assert load_roster(write_ini(tmp_path, text)).gate_settings.api_key == "expanded-key"


# ─────────────────────────────────────────────────────────────────────────────
# the round's backend — KC-34
# ─────────────────────────────────────────────────────────────────────────────

#: The agents' own profile when `backend = openrouter`: the gate's section with
#: the OpenRouter endpoint and a key that is not the gate's.
OPENROUTER_PROFILE = """
[openrouter]
base_url = https://openrouter.ai/api/v1
api_key = test-openrouter-key
model =
api_format = openai
"""


def with_backend(text: str, backend: str, profile: bool = True) -> str:
    """*text* with a `[contest] backend =` key, plus its OpenRouter profile."""
    line = f"[contest]\nbackend = {backend}\n"
    if profile:
        line += "openrouter_llm_profile = openrouter\n"
    text = text.replace("[contest]\n", line)
    return text + (OPENROUTER_PROFILE if profile else "")


def test_the_committed_backend_is_kilo_without_an_openrouter_key(gate_key):
    """The committed roster carries a ${CONTEST_OPENROUTER_API_KEY} reference
    but never resolves it: `backend = kilo` needs no OpenRouter key at all, so
    the committed file loads on a machine that has no such key exported."""
    cfg = load_roster(COMMITTED)

    assert cfg.backend == "kilo"
    assert cfg.openrouter_llm_profile == "contest_openrouter_llm"
    assert cfg.openrouter_settings is None


def test_backend_defaults_to_kilo(tmp_path):
    text = MINIMAL.split("[contest.agent.alpha]")[0] + "[contest.agent.alpha]\nmodel = kenary/hy3:free\n"
    cfg = load_roster(write_ini(tmp_path, text))

    assert cfg.backend == "kilo"
    assert cfg.openrouter_settings is None


def test_backend_openrouter_parses_and_resolves_its_own_profile(tmp_path):
    cfg = load_roster(write_ini(tmp_path, with_backend(MINIMAL, "openrouter")))

    assert cfg.backend == "openrouter"
    assert cfg.openrouter_llm_profile == "openrouter"
    assert cfg.openrouter_settings.base_url == "https://openrouter.ai/api/v1"
    assert cfg.openrouter_settings.api_key == "test-openrouter-key"
    assert cfg.openrouter_settings.model == ""
    assert cfg.openrouter_settings.api_format == "openai"
    # the gate keeps its own profile: the backend never borrows its credential
    assert cfg.gate_settings.api_key == "test-key"
    assert cfg.openrouter_settings is not cfg.gate_settings


def test_backend_openrouter_without_a_profile_names_the_key(tmp_path):
    text = MINIMAL.replace("[contest]\n", "[contest]\nbackend = openrouter\n") + OPENROUTER_PROFILE
    with pytest.raises(RosterError, match="openrouter_llm_profile"):
        load_roster(write_ini(tmp_path, text))


def test_backend_openrouter_without_its_section_names_it(tmp_path):
    text = MINIMAL.replace("[contest]\n", "[contest]\nbackend = openrouter\nopenrouter_llm_profile = openrouter\n")
    with pytest.raises(RosterError, match=r"\[openrouter\]"):
        load_roster(write_ini(tmp_path, text))


def test_backend_openrouter_missing_a_required_field_names_it(tmp_path):
    text = with_backend(MINIMAL, "openrouter").replace("api_key = test-openrouter-key\n", "")
    with pytest.raises(RosterError, match="required option"):
        load_roster(write_ini(tmp_path, text))


def test_backend_expands_its_own_env_reference(tmp_path, monkeypatch):
    """The real key goes in `contest.local.ini`; the committed reference is
    expanded from the environment the same way the gate's is."""
    monkeypatch.setenv("ROSTER_OPENROUTER_TEST_KEY", "expanded-openrouter-key")
    text = with_backend(MINIMAL, "openrouter").replace(
        "api_key = test-openrouter-key", "api_key = ${ROSTER_OPENROUTER_TEST_KEY}")
    assert load_roster(write_ini(tmp_path, text)).openrouter_settings.api_key == "expanded-openrouter-key"


def test_backend_openrouter_falls_back_to_its_own_defaults(tmp_path):
    """The gate's and the OpenRouter profile's defaults differ (temperature,
    max_tokens, response_format); the OpenRouter profile must fall back to
    its own, not to the gate's."""
    section = "[openrouter]\nbase_url = https://openrouter.ai/api/v1\napi_key = test-openrouter-key\nmodel = \n"
    text = MINIMAL.replace("[contest]\n", "[contest]\nbackend = openrouter\nopenrouter_llm_profile = openrouter\n") + section
    settings = load_roster(write_ini(tmp_path, text)).openrouter_settings

    assert settings is not roster.DEFAULTS_GATE
    assert settings.temperature == roster.DEFAULTS_OPENROUTER.temperature == 0.2
    assert settings.max_tokens == roster.DEFAULTS_OPENROUTER.max_tokens == 4096
    assert settings.response_format is roster.DEFAULTS_OPENROUTER.response_format is False
    assert settings.base_url == "https://openrouter.ai/api/v1"


def test_load_roster_accepts_a_backend_override(tmp_path):
    """`--backend` is applied while the roster is read, not after: a file that
    says `backend = kilo` still resolves its OpenRouter profile when the
    command asks for `openrouter`, because the override is in place before the
    ${ENV} expansion and the profile resolution."""
    text = with_backend(MINIMAL, "kilo")
    assert load_roster(write_ini(tmp_path, text)).backend == "kilo"
    assert load_roster(write_ini(tmp_path, text)).openrouter_settings is None

    cfg = load_roster(write_ini(tmp_path, text), backend="openrouter")
    assert cfg.backend == "openrouter"
    assert cfg.openrouter_settings.api_key == "test-openrouter-key"
    assert cfg.gate_settings.api_key == "test-key"


def test_backend_override_without_a_profile_in_the_file_names_the_key(tmp_path):
    text = MINIMAL.replace("[contest]\n", "[contest]\nbackend = kilo\n")
    with pytest.raises(RosterError, match="openrouter_llm_profile"):
        load_roster(write_ini(tmp_path, text), backend="openrouter")


def test_backend_override_does_not_mask_a_bad_backend_in_the_file(tmp_path):
    """The override replaces the file's value, but the file's own value is still
    validated: a typo there is reported as a typo, not silently ignored."""
    text = MINIMAL.replace("[contest]\n", "[contest]\nbackend = ollama\n") + OPENROUTER_PROFILE
    with pytest.raises(RosterError, match=r"got 'ollama'"):
        load_roster(write_ini(tmp_path, text), backend="openrouter")


def test_backend_of_an_unknown_value_names_it(tmp_path):
    text = with_backend(MINIMAL, "ollama", profile=False)
    with pytest.raises(RosterError, match=r"backend must be one of kilo \| openrouter, got 'ollama'"):
        load_roster(write_ini(tmp_path, text))


def test_backend_values_are_listed_in_backends_and_contest_keys():
    assert roster.BACKENDS == ("kilo", "openrouter")
    assert "backend" in CONTEST_KEYS
    assert "openrouter_llm_profile" in CONTEST_KEYS
    assert "backend" in ContestConfig.__dataclass_fields__
    assert "openrouter_settings" in ContestConfig.__dataclass_fields__


def test_backend_case_and_whitespace_are_squeezed(tmp_path):
    cfg = load_roster(write_ini(tmp_path, with_backend(MINIMAL, "  openrouter  ")))
    assert cfg.backend == "openrouter"


def test_kilo_needs_no_openrouter_key_even_when_the_section_says_an_env_ref(tmp_path, monkeypatch):
    """The committed roster's shape: the section exists with an env reference,
    the backend is `kilo`, the variable is unset, and the load still succeeds."""
    monkeypatch.delenv(GATE_KEY_ENV, raising=False)
    text = MINIMAL.replace("[contest]\n", "[contest]\nbackend = kilo\nopenrouter_llm_profile = openrouter\n")
    text += OPENROUTER_PROFILE.replace("api_key = test-openrouter-key", "api_key = ${UNSET_OPENROUTER_KEY}")
    cfg = load_roster(write_ini(tmp_path, text))

    assert cfg.openrouter_settings is None


# ─────────────────────────────────────────────────────────────────────────────
# the shape of what this module exports
# ─────────────────────────────────────────────────────────────────────────────

def test_config_and_agent_spec_are_frozen():
    with pytest.raises(FrozenInstanceError):
        ContestConfig().max_parallel = 99
    with pytest.raises(FrozenInstanceError):
        AgentSpec(name="alpha", provider_id="kenary", model_id="hy3:free").model_id = "other"


def test_agents_keep_the_file_order(tmp_path):
    text = """
[contest]

[contest.agent.zeta]
model = kenary/hy3:free

[contest.agent.alpha]
model = kenary/laguna-s-2-1:free
"""
    names = [agent.name for agent in load_roster(write_ini(tmp_path, text)).agents]
    assert names == ["zeta", "alpha"]


def test_exported_key_sets_match_the_dataclass():
    assert set(CONTEST_KEYS) == set(ContestConfig.__dataclass_fields__) - {
        "agents", "gate_settings", "openrouter_settings",
    }
    assert set(AGENT_KEYS) == {"model", "kilo_agent", "variant"}
    assert AGENT_SECTION_PREFIX == "contest.agent."


def test_gitignore_ignores_the_local_roster_and_the_out_dir():
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert any(line.strip() == LOCAL for line in lines)
    assert any(line.strip() in ("contest-out", "contest-out/") for line in lines)


def test_variant_key_defaults_to_highest_and_parses(tmp_path):
    """KC-49: `[contest] variant` — `highest` when absent or empty, as written otherwise."""
    assert "variant" in CONTEST_KEYS
    assert ContestConfig().variant == "highest"
    for text, want in (("", "highest"), ("variant =\n", "highest"),
                       ("variant = high\n", "high"), ("variant = default\n", "default")):
        ini = tmp_path / f"c{len(text)}.ini"
        ini.write_text("[contest]\n" + text
                       + "[contest.agent.a]\nmodel = kenary/a:free\n", encoding="utf-8")
        assert load_roster(ini).variant == want


def test_workspace_kind_defaults_to_clone_and_parses(tmp_path):
    """KC-59: `[contest] workspace_kind` — `clone` when absent or empty, as written
    otherwise, and one of `WORKSPACE_KINDS` always."""
    assert "workspace_kind" in CONTEST_KEYS
    assert ContestConfig().workspace_kind == "clone"
    for value, want in (("clone", "clone"), ("worktree", "worktree")):
        ini = write_ini(tmp_path, add_to_contest(MINIMAL, f"workspace_kind = {value}"),
                        f"wk_{value}.ini")
        assert load_roster(ini).workspace_kind == want
    # absent, and present but empty: the documented default
    assert load_roster(write_ini(tmp_path, MINIMAL)).workspace_kind == "clone"
    for i in range(2):
        ini = write_ini(tmp_path,
                        "[contest]\nworkspace_kind =\n[contest.agent.a]\nmodel = kenary/a:free\n",
                        f"wk_empty_{i}.ini")
        assert load_roster(ini).workspace_kind == "clone"


def test_workspace_kind_refuses_a_value_that_is_not_a_kind(tmp_path):
    """A typo is found at load time, naming the key — as `backend` does."""
    ini = write_ini(tmp_path, add_to_contest(MINIMAL, "workspace_kind = clne"), "wk_bad.ini")
    with pytest.raises(RosterError, match=r"\[contest\] workspace_kind must be one of "
                                          r"clone \| worktree, got 'clne'"):
        load_roster(ini)


# ─────────────────────────────────────────────────────────────────────────────
# KC-55: the gate's retry budget
# ─────────────────────────────────────────────────────────────────────────────

GATE_LIMIT_KEYS = ("gate_retries", "gate_retry_wait_sec",
                   "gate_retry_max_wait_sec", "gate_deadline_sec")


def test_gate_limit_keys_are_committed_with_their_defaults(gate_key):
    """KC-66: 4 retries (5 tries in all), 30 s between them, a 60 s cap on a
    Retry-After, a 600 s deadline — a worst case of 660 s under the committed
    900 s silence clock."""
    config = load_roster(COMMITTED)

    assert config.gate_retries == 4
    assert config.gate_retry_wait_sec == 30.0
    assert config.gate_retry_max_wait_sec == 60.0
    assert config.gate_deadline_sec == 600.0
    assert isinstance(config.gate_retries, int)
    for key in GATE_LIMIT_KEYS:
        assert key in CONTEST_KEYS


def test_absent_gate_limit_keys_take_their_documented_defaults(tmp_path):
    config = load_roster(write_ini(tmp_path, MINIMAL))

    assert (config.gate_retries, config.gate_retry_wait_sec,
            config.gate_retry_max_wait_sec, config.gate_deadline_sec) == (
        4, 30.0, 60.0, 600.0)


@pytest.mark.parametrize("key,value", [
    ("gate_retries", "7"),
    ("gate_retry_wait_sec", "2.5"),
    ("gate_retry_max_wait_sec", "15"),
    ("gate_deadline_sec", "120"),
])
def test_gate_limit_keys_parse_from_contest(tmp_path, key, value):
    text = add_to_contest(MINIMAL, f"{key} = {value}")
    assert getattr(load_roster(write_ini(tmp_path, text)), key) == float(value)


@pytest.mark.parametrize("key,value", [
    ("gate_retries", "-1"),
    ("gate_retry_wait_sec", "-2.5"),
    ("gate_retry_max_wait_sec", "-15"),
    ("gate_deadline_sec", "-120"),
])
def test_a_negative_gate_limit_names_its_key(tmp_path, key, value):
    text = add_to_contest(MINIMAL, f"{key} = {value}")
    with pytest.raises(RosterError, match=key):
        load_roster(write_ini(tmp_path, text))


@pytest.mark.parametrize("key", ["gate_retry_wait_sec", "gate_retry_max_wait_sec",
                                 "gate_deadline_sec"])
def test_a_malformed_gate_time_names_its_key(tmp_path, key):
    """Not a fallback: a typo in the wait budget must not silently become the
    default, and a RosterError names the key so the operator finds it."""
    text = add_to_contest(MINIMAL, f"{key} = soon")
    with pytest.raises(RosterError, match=key):
        load_roster(write_ini(tmp_path, text))


def test_a_malformed_gate_retries_falls_back_to_the_default(tmp_path):
    """The count is an int key, read with the same helper as every other limit."""
    text = add_to_contest(MINIMAL, "gate_retries = three")
    assert load_roster(write_ini(tmp_path, text)).gate_retries == 4


# ─────────────────────────────────────────────────────────────────────────────
# KC-36: turn_extend_sec and turn_max_sec — the turn clock is a floor
# ─────────────────────────────────────────────────────────────────────────────

def test_turn_extend_sec_and_turn_max_sec_parse_and_default(tmp_path):
    """KC-36: both keys parse, default 600 / 7200, and are in CONTEST_KEYS."""
    assert "turn_extend_sec" in CONTEST_KEYS
    assert "turn_max_sec" in CONTEST_KEYS
    assert load_roster(write_ini(tmp_path, MINIMAL)).turn_extend_sec == 600
    assert load_roster(write_ini(tmp_path, MINIMAL)).turn_max_sec == 7200

    base = """
[contest]
turn_timeout_sec = %s
turn_extend_sec  = %s
turn_max_sec     = %s

[contest.agent.alpha]
model = kenary/hy3:free
"""
    cfg = load_roster(write_ini(tmp_path, base % ("1800", "300", "9000")))
    assert (cfg.turn_timeout_sec, cfg.turn_extend_sec, cfg.turn_max_sec) == (1800, 300, 9000)
    # 0 disables extension — today's clock — and a cap equal to the floor is legal
    cfg0 = load_roster(write_ini(tmp_path, base % ("1800", "0", "1800")))
    assert cfg0.turn_extend_sec == 0 and cfg0.turn_max_sec == cfg0.turn_timeout_sec
    for key in ("turn_extend_sec", "turn_max_sec"):
        assert isinstance(getattr(cfg, key), int)


def test_committed_turn_extension_limits_are_a_floor_below_a_ceiling(gate_key):
    """3600 s floor, 600 s per extension, 7200 s ceiling."""
    cfg = load_roster(COMMITTED)
    assert (cfg.turn_timeout_sec, cfg.turn_extend_sec, cfg.turn_max_sec) == (3600, 600, 7200)
    assert cfg.turn_max_sec >= cfg.turn_timeout_sec
    for key in ("turn_timeout_sec", "turn_extend_sec", "turn_max_sec"):
        assert isinstance(getattr(cfg, key), int)


def test_turn_max_sec_below_turn_timeout_sec_names_both_keys(tmp_path):
    """A ceiling below the floor is unextendable: the error names both keys."""
    text = add_to_contest(MINIMAL, "turn_timeout_sec = 1800\nturn_max_sec = 1200")
    with pytest.raises(RosterError) as exc:
        load_roster(write_ini(tmp_path, text))
    for key in ("turn_timeout_sec", "turn_max_sec"):
        assert key in str(exc.value)


def test_a_negative_or_malformed_turn_limit(tmp_path):
    """Both are read through `limit()`: a malformed value falls back, and a
    negative one names its key instead of becoming a zero-second turn."""
    assert load_roster(write_ini(tmp_path,
                                 add_to_contest(MINIMAL, "turn_extend_sec = three"))
                       ).turn_extend_sec == 600
    assert load_roster(write_ini(tmp_path,
                                 add_to_contest(MINIMAL, "turn_max_sec = three"))
                       ).turn_max_sec == 7200
    for key in ("turn_extend_sec", "turn_max_sec"):
        with pytest.raises(RosterError, match=key):
            load_roster(write_ini(tmp_path, add_to_contest(MINIMAL, f"{key} = -1")))


def test_the_turn_limits_survive_a_models_override(tmp_path):
    """KC-36 §6: `--models` replaces the roster and nothing else — the two
    limits are round limits, read from the file like every other one."""
    text = add_to_contest(MINIMAL, "turn_timeout_sec = 3600\nturn_extend_sec = 900\nturn_max_sec = 18000")
    cfg = load_roster(write_ini(tmp_path, text))
    models = replace(cfg, agents=tuple(
        AgentSpec(name="hy3-var1", provider_id=a.provider_id, model_id=a.model_id)
        for a in cfg.agents))
    assert [agent.name for agent in models.agents] == ["hy3-var1"]
    assert (models.turn_timeout_sec, models.turn_extend_sec, models.turn_max_sec) == (3600, 900, 18000)


# ─────────────────────────────────────────────────────────────────────────────
# KC-65: pytest_workers_* and agent_tmpdir — the agents' pytest sizes to the box
# ─────────────────────────────────────────────────────────────────────────────

KC65_KEYS = ("pytest_workers_per_agent", "pytest_workers_few_agents",
             "pytest_workers_few", "pytest_workers_min")


def test_pytest_workers_keys_parse_and_default(tmp_path):
    """KC-65: the four keys parse, are in CONTEST_KEYS, and default to
    0 / 4 / 4 / 2 — no override, `pytest_workers_few_agents` live, and 2."""
    for key in KC65_KEYS:
        assert key in CONTEST_KEYS
    cfg = load_roster(write_ini(tmp_path, MINIMAL))
    assert (cfg.pytest_workers_per_agent, cfg.pytest_workers_few_agents,
            cfg.pytest_workers_few, cfg.pytest_workers_min) == (0, 4, 4, 2)

    text = add_to_contest(MINIMAL, "pytest_workers_per_agent = 3\n"
                                  "pytest_workers_few_agents = 8\n"
                                  "pytest_workers_few = 6\n"
                                  "pytest_workers_min = 1")
    cfg = load_roster(write_ini(tmp_path, text))
    assert (cfg.pytest_workers_per_agent, cfg.pytest_workers_few_agents,
            cfg.pytest_workers_few, cfg.pytest_workers_min) == (3, 8, 6, 1)
    for key in KC65_KEYS:
        assert isinstance(getattr(cfg, key), int), key


def test_committed_pytest_workers_keys_are_the_documented_defaults(gate_key):
    """0 / 4 / 4 / 2 and no temp dir of their own: the committed roster is the
    rule the ticket writes down."""
    cfg = load_roster(COMMITTED)
    assert (cfg.pytest_workers_per_agent, cfg.pytest_workers_few_agents,
            cfg.pytest_workers_few, cfg.pytest_workers_min) == (0, 4, 4, 2)
    assert cfg.agent_tmpdir == ""


def test_a_negative_pytest_workers_key_names_it(tmp_path):
    """A negative count would cap a whole agent at no workers: it names its key."""
    for key in KC65_KEYS:
        with pytest.raises(RosterError, match=key):
            load_roster(write_ini(tmp_path, add_to_contest(MINIMAL, f"{key} = -1")))


def test_pytest_workers_few_and_min_must_be_at_least_one(tmp_path):
    """`pytest_workers_few` and `pytest_workers_min` feed a worker count, so 0
    refuses them; `pytest_workers_min = 1` and `pytest_workers_per_agent = 0`
    stay legal — one worker is slow but real, and 0 means the rule decides."""
    for key in ("pytest_workers_few", "pytest_workers_min"):
        with pytest.raises(RosterError, match=key):
            load_roster(write_ini(tmp_path, add_to_contest(MINIMAL, f"{key} = 0")))
    cfg = load_roster(write_ini(tmp_path, add_to_contest(
        MINIMAL, "pytest_workers_per_agent = 0\npytest_workers_min = 1")))
    assert cfg.pytest_workers_per_agent == 0 and cfg.pytest_workers_min == 1


def test_a_malformed_pytest_workers_key_falls_back(tmp_path):
    """Like every other limit: a typo is the default, not a refusal — the box
    still gets sized."""
    for key, default in (("pytest_workers_per_agent", 0), ("pytest_workers_few_agents", 4),
                         ("pytest_workers_few", 4), ("pytest_workers_min", 2)):
        cfg = load_roster(write_ini(tmp_path, add_to_contest(MINIMAL, f"{key} = three")))
        assert getattr(cfg, key) == default, key


def test_agent_tmpdir_defaults_to_empty_and_expands_its_env_reference(tmp_path, monkeypatch):
    """`agent_tmpdir` unset means the round inherits TMPDIR, so it is `""`;
    `${VAR}` expands like every other [contest] value, and an unset variable
    names itself."""
    assert "agent_tmpdir" in CONTEST_KEYS
    assert ContestConfig().agent_tmpdir == ""
    assert load_roster(write_ini(tmp_path, MINIMAL)).agent_tmpdir == ""

    monkeypatch.setenv("KC65_TMP", str(tmp_path / "scratch"))
    cfg = load_roster(write_ini(tmp_path, add_to_contest(MINIMAL, "agent_tmpdir = ${KC65_TMP}")))
    assert cfg.agent_tmpdir == str(tmp_path / "scratch")
    # a value with a trailing space is squeezed like every other scalar
    cfg = load_roster(write_ini(tmp_path, add_to_contest(MINIMAL, "agent_tmpdir =   /kept   ")))
    assert cfg.agent_tmpdir == "/kept"

    with pytest.raises(RosterError, match="agent_tmpdir"):
        load_roster(write_ini(tmp_path, add_to_contest(MINIMAL, "agent_tmpdir = ${KC65_UNSET}")))
