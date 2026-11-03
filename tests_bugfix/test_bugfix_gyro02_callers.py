"""GYRO-02 (round 331): the runner and `--dry-run` judge the repo by the agent's checkout.

Before: `round_prompt` read the repo off the ticket's folder. AR-3's copy of a
base-only ticket sits in a temp folder git cannot read the base from, so it failed
open, and an agent on an external --target got `CollectBridge`, `agents_128k.ini`
and `68b78a0` in its first prompt, its --resume prompt and the dry run's print.

`round_prompt(repo=)` itself is pinned in test_contest_prompt_external_target.py;
these drive its callers: `run_agent` with a scripted backend and `cli._first_prompt`,
each with the ticket copied out of the checkout as `cli.ticket_file` does.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from types import SimpleNamespace

from tools.auto.llm_profile import LlmSettings
from tools.contest.cli import _first_prompt, agents_from_models
from tools.contest.gates import BRIDGE
from tools.contest.kilo_client import IdleResult, SessionRef
from tools.contest.policy import Policy
from tools.contest.roster import ContestConfig
from tools.contest.runner import AgentRun, run_agent
from tools.contest.workspace import Workspace

HOUSE = ("CollectBridge", "agents_128k.ini", "fail-open", "68b78a0")
ROOT = Path(__file__).resolve().parent.parent


class _Backend:
    def __init__(self):
        self.prompts = []

    def wait_ready(self):
        return True

    def create_session(self, provider_id, model_id, *, rules, title, agent=None, variant=None, **kw):
        return SessionRef(id="ses1", provider_id=provider_id, model_id=model_id, directory="/nowhere",
                          agent=agent, variant=variant)

    def prompt(self, session, text, *a, **kw):
        self.prompts.append(text)

    def wait_idle(self, session, timeout, **kw):
        return IdleResult(status="error", elapsed=1.0,
                          error={"name": "ProviderUnavailable", "data": {"message": "down", "attempts": 10}})

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
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def _checkout(tmp_path, bridge: bool):
    """A one-commit checkout and its ticket copied to a folder outside it: (workspace, copied ticket)."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "thing.py").write_text("def thing():\n    return 1\n")
    (repo / "epic-tasks").mkdir()
    ticket = repo / "epic-tasks" / "331-x.md"
    ticket.write_text("# X-01 — x\n\n**File:** `pkg/thing.py`\n\nbody\n", encoding="utf-8")
    if bridge:
        (repo / BRIDGE).parent.mkdir(parents=True)
        (repo / BRIDGE).write_text("def _shrink():\n    pass\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    copied = tmp_path / "copy" / ticket.name
    copied.parent.mkdir()
    copied.write_text(ticket.read_text(encoding="utf-8"), encoding="utf-8")
    ws = Workspace(agent="a", path=repo.resolve(), branch="contest/331/a", base_sha=_git(repo, "rev-parse", "HEAD"),
                   kind="clone")
    return ws, copied


def _first_prompts(tmp_path, *, bridge=False, dirty=""):
    ws, copied = _checkout(tmp_path, bridge)
    spec = agents_from_models("kenary/m-a:free")[0]
    gate = LlmSettings(base_url="", api_key="", model="", api_format="openai", response_format=True,
                       temperature=0.0, max_tokens=256)
    config = ContestConfig(agents=(spec,), turn_timeout_sec=300, turn_extend_sec=0, idle_event_timeout_sec=900,
                           max_error_retries=1, error_retry_backoff_sec=0, provider_retry_max_attempts=10,
                           provider_retry_max_wait_sec=300.0, quota_patterns="quota exceeded", gate_settings=gate,
                           max_rework=0, agent_max_sec=40)
    ws = Workspace(agent=spec.name, path=ws.path, branch=ws.branch, base_sha=ws.base_sha, kind="clone")
    run = AgentRun(agent=spec, workspace=ws)
    run.dirty_on_resume = dirty
    backend = _Backend()
    out = tmp_path / "out"
    out.mkdir()
    run_agent(run, backend=backend, policy=Policy(config), config=config, ticket_path=copied, out_dir=out,
              on_transition=lambda r: None)
    return backend.prompts, ws, copied


def test_first_turn_on_an_external_checkout_has_no_house_rules(tmp_path):
    prompts, _, _ = _first_prompts(tmp_path)
    assert prompts and not [w for w in HOUSE if w in prompts[0]]


def test_resume_into_dirty_work_on_an_external_checkout_has_no_house_rules(tmp_path):
    prompts, _, _ = _first_prompts(tmp_path, dirty=" M pkg/thing.py")
    assert prompts and "pkg/thing.py" in prompts[0]
    assert not [w for w in HOUSE if w in prompts[0]]


def test_dry_run_prints_the_first_prompt_the_agent_gets(tmp_path):
    prompts, ws, copied = _first_prompts(tmp_path)
    assert _first_prompt(SimpleNamespace(tmp_roots=()), ws, copied) == prompts[0]


def test_every_round_prompt_call_passes_the_checkout():
    """All eight callers (seven in run_agent, the dry run's one) hand round_prompt the checkout."""
    missing, count = [], 0
    for rel in ("tools/contest/runner.py", "tools/contest/cli.py"):
        for node in ast.walk(ast.parse((ROOT / rel).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "round_prompt":
                count += 1
                if not any(k.arg == "repo" for k in node.keywords):
                    missing.append(f"{rel}:{node.lineno}")
    assert count == 8 and not missing, missing


def test_guard_first_turn_on_a_bridge_checkout_keeps_house_rules(tmp_path):
    prompts, _, _ = _first_prompts(tmp_path, bridge=True)
    assert prompts and "CollectBridge._shrink" in prompts[0]
