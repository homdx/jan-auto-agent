"""KC-185 (round 329): an agent with a `|` list of models falls over to the next one.

Before: one agent was one model. A provider that went down (`ProviderUnavailable`), ran
out of quota, or kept failing past `max_error_retries` ended the agent in ERROR, though
the same model was free on another provider; `kenary/m|zai/m` was not even parsed as
one agent. Round 328 moved glm by hand-editing state.json, and z.ai then went down too.

Each test drives `run_agent` with a scripted backend and looks at the model every
prompt went out with; the guards pin what a one-model agent does today.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from tools.auto.llm_profile import LlmSettings
from tools.contest.cli import agents_from_models
from tools.contest.kilo_client import IdleResult, SessionRef
from tools.contest.policy import Policy
from tools.contest.roster import ContestConfig
from tools.contest.runner import AgentRun, RoundState, run_agent
from tools.contest.workspace import Workspace


class _Backend:
    def __init__(self, results):
        self.results, self.i, self.prompts, self.sessions = list(results), 0, [], []

    def wait_ready(self):
        return True

    def create_session(self, provider_id, model_id, *, rules, title, agent=None, variant=None, **kw):
        self.sessions.append((provider_id, model_id))
        return SessionRef(id=f"ses{len(self.sessions)}", provider_id=provider_id, model_id=model_id,
                          directory="/nowhere", agent=agent, variant=variant)

    def prompt(self, session, text, *a, **kw):
        self.prompts.append((f"{session.provider_id}/{session.model_id}", session.id))

    def wait_idle(self, session, timeout, **kw):
        r = self.results[min(self.i, len(self.results) - 1)]
        self.i += 1
        return r

    def mark(self):
        return None

    def abort(self, session):
        pass

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


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


def _run(tmp_path, models, script, *, max_rework=1, max_error_retries=1):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "thing.py").write_text("def thing():\n    return 1\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    ticket = tmp_path / "epic-tasks" / "111-kc185.md"
    ticket.parent.mkdir()
    ticket.write_text("# KC-185\n\n**File:** `pkg/thing.py`\n\nbody\n")
    spec = agents_from_models(models)[0]
    gate = LlmSettings(base_url="", api_key="", model="", api_format="openai", response_format=True,
                       temperature=0.0, max_tokens=256)
    config = ContestConfig(agents=(spec,), turn_timeout_sec=300, turn_extend_sec=0, idle_event_timeout_sec=900,
                           max_error_retries=max_error_retries, error_retry_backoff_sec=0,
                           provider_retry_max_attempts=10, provider_retry_max_wait_sec=300.0,
                           quota_patterns="quota exceeded", gate_settings=gate, max_rework=max_rework,
                           agent_max_sec=40)
    ws = Workspace(agent=spec.name, path=repo.resolve(), branch=f"contest/111/{spec.name}", base_sha=sha,
                   kind="clone")
    backend = _Backend(script)
    run = AgentRun(agent=spec, workspace=ws)
    out = tmp_path / "out"
    out.mkdir()
    run_agent(run, backend=backend, policy=Policy(config), config=config, ticket_path=ticket, out_dir=out,
              on_transition=lambda r: None)
    return run, backend


def _down():
    return IdleResult(status="error", elapsed=1.0,
                      error={"name": "ProviderUnavailable", "data": {"message": "down", "attempts": 10}})


def _ok():
    return IdleResult(status="idle", elapsed=1.0)


def _switches(backend):
    models = [p[0] for p in backend.prompts]
    return [models[0]] + [m for i, m in enumerate(models[1:], 1) if m != models[i - 1]]


def test_a_pipe_items_are_one_agent_named_by_the_first_model():
    agents = agents_from_models("kenary/glm-4-7-flash:free|zai/glm-4.7-flash,sensenova/x")
    assert [(a.name, a.provider_id, a.model_id) for a in agents] == [
        ("glm-4-7-flash", "kenary", "glm-4-7-flash:free"), ("x", "sensenova", "x")]


def test_b_a_down_provider_hands_the_same_session_to_the_next_model(tmp_path):
    run, b = _run(tmp_path, "kenary/m-a:free|zai/m-b", [_down(), _ok()])
    assert [p[0] for p in b.prompts[:2]] == ["kenary/m-a:free", "zai/m-b"]
    assert len(b.sessions) == 1 and b.prompts[0][1] == b.prompts[1][1]


def test_b_both_down_is_error_naming_both_without_looping(tmp_path):
    run, b = _run(tmp_path, "kenary/m-a:free|zai/m-b", [_down()])
    assert run.state.value == "ERROR"
    assert "m-a" in run.last_error and "m-b" in run.last_error
    assert len(b.prompts) == 2


def test_b_a_retry_scheduled_twenty_minutes_out_moves_on_now(tmp_path):
    """The user's case: the provider says retry in 20 minutes. wait_idle returns ProviderQuota
    for a retry past provider_retry_max_wait_sec (KC-61); the agent takes the next model."""
    quota = IdleResult(status="error", elapsed=1.0, error={"name": "ProviderQuota", "data": {
        "message": "retry in 20 minutes", "retryAt": int((time.time() + 1200) * 1000)}})
    run, b = _run(tmp_path, "kenary/m-a:free|zai/m-b", [quota, _ok()])
    assert [p[0] for p in b.prompts[:2]] == ["kenary/m-a:free", "zai/m-b"]


def test_b_the_ring_keeps_turning_while_turns_finish(tmp_path):
    script = [_down(), _ok(), _down(), _ok(), _down(), _ok(), _down(), _ok()]
    run, b = _run(tmp_path, "kenary/m-a:free|zai/m-b", script, max_rework=8)
    assert _switches(b)[:5] == ["kenary/m-a:free", "zai/m-b", "kenary/m-a:free", "zai/m-b", "kenary/m-a:free"]


def test_c_the_model_in_use_survives_a_restart(tmp_path):
    run, _ = _run(tmp_path, "kenary/m-a:free|zai/m-b", [_down(), _ok()])
    back = AgentRun.from_dict(json.loads(json.dumps(run.to_dict())))
    assert (back.agent.provider_id, back.agent.model_id) == ("zai", "m-b")


def test_d_status_shows_the_model_in_use_and_its_place(tmp_path):
    run, _ = _run(tmp_path, "kenary/m-a:free|zai/m-b", [_down(), _ok()])
    rs = RoundState(round_no=1, ticket="111-kc185.md", base_sha="a" * 40, started_at=0.0, agents=[run])
    cell = str(rs.table_rows()[0]["model"])
    assert "zai/m-b" in cell and "2/2" in cell


def test_guard_one_model_down_is_error_after_one_prompt(tmp_path):
    run, b = _run(tmp_path, "kenary/m-a:free", [_down()])
    assert run.state.value == "ERROR" and len(b.prompts) == 1


def test_b_a_working_model_is_never_left(tmp_path):
    run, b = _run(tmp_path, "kenary/m-a:free|zai/m-b", [_ok(), _ok(), _ok()])
    assert {p[0] for p in b.prompts} == {"kenary/m-a:free"}


def test_guard_no_pipe_names_are_unchanged():
    agents = agents_from_models("kenary/hy3:free,kenary/hy3:free,sensenova/x@high")
    assert [(a.name, a.model_id, a.variant) for a in agents] == [
        ("hy3-var1", "hy3:free", None), ("hy3-var2", "hy3:free", None), ("x", "x", "high")]


def test_guard_an_old_one_model_state_json_loads():
    old = {"agent": {"name": "x", "provider_id": "kenary", "model_id": "m:free", "kilo_agent": None,
                     "variant": None, "context_limit": None},
           "workspace": {"agent": "x", "path": "/tmp/x", "branch": "contest/1/x", "base_sha": "a" * 40,
                         "kind": "clone"},
           "state": "WAITING", "attempt": 0, "turns": []}
    back = AgentRun.from_dict(old)
    assert (back.agent.provider_id, back.agent.model_id) == ("kenary", "m:free")
