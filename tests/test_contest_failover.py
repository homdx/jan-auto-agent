"""KC-185: an agent falls over to its next provider instead of going to ERROR.

Round 328 ran `glm-4-7-flash` through kenari.id; when kenari's provider
rejected the request over and over the agent went to `ERROR`, though the same
model was free on z.ai. A `|` list per agent — `kenary/glm-4-7-flash:free|
zai/glm-4.7-flash` — lets the runner switch provider in the same kilo session
on a provider error: `provider_unavailable` (KC-64), a spent `max_error_retries`
on a rejected request or a dropped stream, or `provider_quota`. The list is a
ring with one pass, the model in use survives `--resume`, and `revive_round.py`
puts an `ERROR` agent back on the first model.

The cases here, from the ticket:

  * A: `agents_from_models` splits `|` into one agent with two models, and a
    list without `|` is byte-identical to today's spec.
  * B: the first model ends every turn `ProviderUnavailable`, the second
    answers — the agent reaches `READY` on the second model, in the same
    session, and the second model got `RETRY_PROMPT`. With one model, `ERROR`.
  * B: both models are down — `ERROR`, the text names both, nothing loops.
  * B: a context overflow on the first model does not switch the model.
  * C: a leg stopped on the second model resumes there; a revived `ERROR`
    agent starts at the first model; an old one-model `state.json` loads.
  * D: the `status` row shows `(2/2)` after a switch.

Nothing here calls a provider: the run is driven over a scripted `ContestBackend`
and the harvest is a stub, so no test waits on a real model or a wall clock.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
for _p in (str(REPO_ROOT), str(TESTS_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tools.contest.cli as cli_module  # noqa: E402
import tools.contest.runner as runner_module  # noqa: E402
from tools.auto.llm_profile import LlmSettings  # noqa: E402
from tools.contest.harvest import Harvest  # noqa: E402
from tools.contest.kilo_client import IdleResult, SessionRef  # noqa: E402
from tools.contest.policy import Policy  # noqa: E402
from tools.contest.roster import AgentSpec, ModelRef  # noqa: E402
from tools.contest.runner import (  # noqa: E402
    AgentRun,
    AgentState,
    RETRY_PROMPT,
    RoundState,
    run_agent,
)
from tools.contest.workspace import Workspace  # noqa: E402
import revive_round  # noqa: E402

#: The ticket's own list: two providers of one model, then one more agent.
LIST_SPEC = "kenary/glm-4-7-flash:free|zai/glm-4.7-flash,sensenova/x"

UNAVAILABLE = IdleResult(
    status="error", elapsed=12.0,
    error={"name": "ProviderUnavailable",
           "data": {"message": "The model service is temporarily unavailable.",
                    "attempts": 10}})

IDLE = IdleResult(status="idle", elapsed=1.0)

OVERFLOW = IdleResult(
    status="error", elapsed=1.0,
    error={"name": "ContextOverflowError",
           "data": {"message": "maximum context length exceeded"}})


class _ScriptedBackend:
    """`ContestBackend` over a scripted queue of `IdleResult`s, nothing live.

    Every prompt is recorded with the model it named, so a test sees which
    provider a turn went to. `mark()` is ``None``, which is the no-shared-tap
    answer: a settle never waits on a real stream.
    """

    def __init__(self, results):
        self._results = list(results)
        self.prompts = []      # (provider_id, model_id, text)
        self.wait_calls = []   # (provider_id, model_id) per wait_idle
        self.sessions = []
        self.aborts = []

    def wait_ready(self):
        return True

    def create_session(self, provider_id, model_id, *, rules, title,
                       agent=None, variant=None):
        ref = SessionRef(id=f"ses-{len(self.sessions) + 1}", provider_id=provider_id,
                         model_id=model_id, directory="/nowhere", agent=agent,
                         variant=variant)
        self.sessions.append(ref)
        return ref

    def prompt(self, session, text):
        self.prompts.append((session.provider_id, session.model_id, text))

    def mark(self):
        return None

    def wait_idle(self, session, timeout, **kwargs):
        self.wait_calls.append((session.provider_id, session.model_id))
        if self._results:
            return self._results.pop(0)
        return IdleResult(status="idle", elapsed=1.0)

    def abort(self, session):
        self.aborts.append(session.id)

    def interrupt(self, session=None):
        pass

    def interrupted(self):
        return False

    def close(self):
        pass

    def session_info(self, session):
        return {}

    def messages(self, session):
        return []

    def tool_parts(self, session):
        return []

    def compact(self, session):
        pass


def _git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


def _repo(tmp_path):
    """One base commit and nothing above it: a clean tree, no KC-21 harvest."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "pkg").mkdir()
    (root / "pkg" / "thing.py").write_text("def thing():\n    return 1\n",
                                           encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return Workspace(agent="glm-4-7-flash", path=root.resolve(),
                     branch="contest/329/glm-4-7-flash",
                     base_sha=_git(root, "rev-parse", "HEAD"), kind="clone")


def _ticket(tmp_path):
    path = tmp_path / "epic-tasks" / "329-kc185-test.md"
    path.parent.mkdir(parents=True)
    path.write_text("# KC-185 test ticket\n\n**File:** `pkg/thing.py`\n\nbody\n",
                    encoding="utf-8")
    return path


def _config(spec):
    return runner_module.ContestConfig(
        agents=(spec,),
        turn_timeout_sec=300,
        turn_extend_sec=0,
        idle_event_timeout_sec=900,
        max_error_retries=2,
        error_retry_backoff_sec=0,
        max_continues_per_attempt=0,
        max_local_store_retries=0,
        provider_retry_max_attempts=10,
        provider_retry_max_wait_sec=300.0,
        quota_patterns="",
        gate_settings=LlmSettings(base_url="", api_key="", model="",
                                  api_format="openai", response_format=True,
                                  temperature=0.0, max_tokens=256),
    )


def _run(tmp_path, results, spec, monkeypatch):
    """One agent through `run_agent` against a scripted backend and a stub harvest."""
    ws = _repo(tmp_path)
    config = _config(spec)
    backend = _ScriptedBackend(results)

    def stub_harvest(worktree, ticket, run_tests, config=None):
        return Harvest(verdict="READY", reasons=(), commit=worktree.base_sha,
                       facts={}, elapsed=0.1)

    monkeypatch.setattr(runner_module, "_harvest", stub_harvest)
    run = AgentRun(agent=spec, workspace=ws)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    run_agent(run, backend=backend, policy=Policy(config), config=config,
              ticket_path=_ticket(tmp_path), out_dir=out_dir,
              on_transition=lambda r: None)
    return run, backend


# A — the model list

def test_a_list_makes_one_agent_with_two_models():
    """`kenary/glm-4-7-flash:free|zai/glm-4.7-flash,sensenova/x` is two agents:
    the first is named after the first model and carries both models in order."""
    first, second = cli_module.agents_from_models(LIST_SPEC)
    assert first.name == "glm-4-7-flash"
    assert [r.model for r in first.models] == ["kenary/glm-4-7-flash:free",
                                               "zai/glm-4.7-flash"]
    assert first.model == "kenary/glm-4-7-flash:free"
    assert first.model_index == 0
    assert second.name == "x"
    assert second.models == ()


def test_a_list_without_a_pipe_is_todays_spec():
    """An item without `|` behaves exactly as today: one model, an empty list."""
    (spec,) = cli_module.agents_from_models("kenary/glm-4-7-flash:free")
    assert spec == AgentSpec(name="glm-4-7-flash", provider_id="kenary",
                             model_id="glm-4-7-flash:free")
    assert spec.models == ()
    assert spec.model_index == 0


def test_a_variant_belongs_to_each_model_on_its_own():
    """`a/m@high|b/m` gives `high` to the first model only."""
    (spec,) = cli_module.agents_from_models("a/m@high|b/m")
    assert spec.models[0].variant == "high"
    assert spec.models[1].variant is None
    assert spec.variant == "high"


# B — fall over instead of ERROR

def _two_model_spec():
    (spec,) = cli_module.agents_from_models("kenary/glm-4-7-flash:free|zai/glm-4.7-flash")
    return spec


def test_the_agent_falls_over_to_the_second_model(tmp_path, monkeypatch):
    """The first model ends every turn `ProviderUnavailable`, the second answers:
    the agent reaches `READY` on the second model, in the same session, and the
    second model got `RETRY_PROMPT`."""
    run, backend = _run(tmp_path, [UNAVAILABLE, IDLE], _two_model_spec(), monkeypatch)

    assert run.state is AgentState.READY
    assert run.agent.model_index == 1
    assert run.agent.provider_id == "zai"
    assert run.agent.model_id == "glm-4.7-flash"
    # one session for the whole run, and the failover went through it
    assert len(backend.sessions) == 1
    assert run.session_id == backend.sessions[0].id
    assert backend.wait_calls == [("kenary", "glm-4-7-flash:free"),
                                  ("zai", "glm-4.7-flash")]
    # the second prompt is the RETRY_PROMPT, not a round prompt
    assert len(backend.prompts) == 2
    assert RETRY_PROMPT.split("{reason}")[0].strip() in backend.prompts[1][2]
    # the switch is recorded on the turn and in turns.jsonl
    first_turn = run.turns[0]
    assert first_turn["cause"] == "failover"
    assert first_turn["provider_from"] == "kenary/glm-4-7-flash:free"
    assert first_turn["provider_to"] == "zai/glm-4.7-flash"


def test_one_model_still_goes_to_error(tmp_path, monkeypatch):
    """With a single model the agent ends `ERROR` as today — no fallover exists."""
    (spec,) = cli_module.agents_from_models("kenary/glm-4-7-flash:free")
    run, backend = _run(tmp_path, [UNAVAILABLE], spec, monkeypatch)

    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("provider_unavailable after 10 retries:")
    assert len(backend.prompts) == 1
    assert RETRY_PROMPT.split("{reason}")[0].strip() not in backend.prompts[0][2]


def test_both_models_down_names_them_all_and_does_not_loop(tmp_path, monkeypatch):
    """Both models are down: the agent ends `ERROR`, the text names both, and
    the list is not walked twice — one switch, then the end."""
    run, backend = _run(tmp_path, [UNAVAILABLE, UNAVAILABLE], _two_model_spec(),
                        monkeypatch)

    assert run.state is AgentState.ERROR
    assert run.last_error.startswith("provider_unavailable after 10 retries:")
    assert "tried: kenary/glm-4-7-flash:free, zai/glm-4.7-flash" in run.last_error
    # exactly one failover, then the end — never a wrap to the first model
    assert len(backend.prompts) == 2
    assert [t.get("cause") for t in run.turns] == ["failover", None]


def test_a_context_overflow_does_not_switch_the_model(tmp_path, monkeypatch):
    """A context overflow on the first model is not a provider error: the model
    stays the first one, whatever the overflow path decides for the turn."""
    run, backend = _run(tmp_path, [OVERFLOW, IDLE], _two_model_spec(), monkeypatch)

    assert run.agent.model_index == 0
    assert run.agent.provider_id == "kenary"
    assert "failover" not in [t.get("cause") for t in run.turns]
    # the overflow was not read as a provider error anywhere in the run
    assert backend.wait_calls[0] == ("kenary", "glm-4-7-flash:free")


# C — the model an agent is on survives a restart

def test_a_stopped_leg_resumes_on_the_second_model():
    """`state.json` records the list and the index; a run on the second model
    round-trips to the same model."""
    (spec,) = cli_module.agents_from_models("kenary/glm-4-7-flash:free|zai/glm-4.7-flash")
    run = AgentRun(
        agent=AgentSpec(name=spec.name, provider_id="zai", model_id="glm-4.7-flash",
                        models=spec.models, model_index=1),
        workspace=Workspace(agent=spec.name, path=Path("/tmp/x"), branch="b",
                            base_sha="c" * 40, kind="worktree"))
    again = AgentRun.from_dict(run.to_dict())
    assert again.agent.model_index == 1
    assert again.agent.provider_id == "zai"
    assert again.agent.model_id == "glm-4.7-flash"
    assert [r.model for r in again.agent.models] == ["kenary/glm-4-7-flash:free",
                                                     "zai/glm-4.7-flash"]


def test_a_revived_error_agent_starts_at_the_first_model():
    """`revive_round.py` puts an `ERROR` agent back on the first model of its list."""
    (spec,) = cli_module.agents_from_models("kenary/glm-4-7-flash:free|zai/glm-4.7-flash")
    row = {
        "agent": {"name": spec.name, "provider_id": "zai", "model_id": "glm-4.7-flash",
                  "models": [{"provider_id": r.provider_id, "model_id": r.model_id,
                              "variant": r.variant} for r in spec.models],
                  "model_index": 1},
        "state": "ERROR", "attempt": 1, "last_error": "provider_unavailable",
    }
    revived = revive_round.revive_agents([row])
    assert revived == [spec.name]
    agent = row["agent"]
    assert agent["model_index"] == 0
    assert agent["provider_id"] == "kenary"
    assert agent["model_id"] == "glm-4-7-flash:free"


def test_revive_prints_the_model_a_list_agent_resumes_on(tmp_path, capsys):
    """`revive_round.py` names the model each agent will resume on — the first
    of its list, since a revived `ERROR` agent starts over."""
    (spec,) = cli_module.agents_from_models("kenary/glm-4-7-flash:free|zai/glm-4.7-flash")
    path = tmp_path / "out" / "329" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"round_no": 329, "ticket": "t", "base_sha": "' + "b" * 40 + '", "agents": ['
        '{"agent": {"name": "glm-4-7-flash", "provider_id": "zai", '
        '"model_id": "glm-4.7-flash", "models": [{"provider_id": "kenary", '
        '"model_id": "glm-4-7-flash:free", "variant": null}, {"provider_id": "zai", '
        '"model_id": "glm-4.7-flash", "variant": null}], "model_index": 1}, '
        '"state": "ERROR", "attempt": 1, "last_error": "provider_unavailable"}]}',
        encoding="utf-8")
    assert revive_round.main([str(path), "--dry-run"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "glm-4-7-flash: ERROR -> WAITING on kenary/glm-4-7-flash:free"


def test_an_old_one_model_state_json_loads_as_a_list_of_one():
    """A `state.json` written before this change, with one model, loads as a
    list of one — the legacy spec, byte for byte."""
    spec = AgentSpec.from_dict({"name": "glm-4-7-flash", "provider_id": "kenary",
                                "model_id": "glm-4-7-flash:free"})
    assert spec.models == ()
    assert spec.model_index == 0
    assert spec.model == "kenary/glm-4-7-flash:free"


def test_a_single_model_keeps_the_pre_change_state_bytes():
    """A single-model run writes the exact pre-KC-185 `state.json`: no `models`
    and no `model_index` keys on the agent row."""
    (spec,) = cli_module.agents_from_models("kenary/glm-4-7-flash:free")
    run = AgentRun(agent=spec, workspace=Workspace(
        agent=spec.name, path=Path("/tmp/x"), branch="b", base_sha="c" * 40,
        kind="worktree"))
    agent = run.to_dict()["agent"]
    assert "models" not in agent
    assert "model_index" not in agent


# D — the operator sees it

def test_the_status_row_shows_the_position_after_a_switch():
    """`status` shows the model in use with its position: `zai/glm-4.7-flash
    (2/2)` once the agent moved off the first model."""
    (spec,) = cli_module.agents_from_models("kenary/glm-4-7-flash:free|zai/glm-4.7-flash")
    switched = AgentSpec(name=spec.name, provider_id="zai", model_id="glm-4.7-flash",
                         models=spec.models, model_index=1)
    run = AgentRun(agent=switched, workspace=Workspace(
        agent=spec.name, path=Path("/tmp/x"), branch="b", base_sha="c" * 40,
        kind="worktree"))
    state = RoundState(round_no=329, ticket="t", base_sha="b", started_at=0.0,
                       agents=[run])
    (row,) = state.table_rows()
    assert row["model"] == "zai/glm-4.7-flash (2/2)"
    assert row["models"] == "kenary/glm-4-7-flash:free|zai/glm-4.7-flash"


def test_a_single_model_status_row_is_unchanged():
    """A single-model agent's `status` row names just its model, as before."""
    (spec,) = cli_module.agents_from_models("kenary/glm-4-7-flash:free")
    run = AgentRun(agent=spec, workspace=Workspace(
        agent=spec.name, path=Path("/tmp/x"), branch="b", base_sha="c" * 40,
        kind="worktree"))
    state = RoundState(round_no=329, ticket="t", base_sha="b", started_at=0.0,
                       agents=[run])
    (row,) = state.table_rows()
    assert row["model"] == "kenary/glm-4-7-flash:free"
    assert row["models"] is None
