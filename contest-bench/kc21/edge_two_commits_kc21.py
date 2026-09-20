"""KC-21 acceptance, part 2 — the two edges the ticket names in prose.

`run.commit` is the branch's one commit, and `None` when the branch has two
(item 1: "None when the branch has two or none"); a harvest that rejects the
tree never re-prompts the dead session.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_contest_runner import Harness, Sandbox, _BenchFake, _git, _prompts, make_config  # noqa: E402
from test_kc21_accept import work_claimed  # noqa: E402
from tools.contest.runner import AgentState  # noqa: E402

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")


def _silent_cfg():
    return make_config(["agent-a"], turn_timeout_sec=30, idle_event_timeout_sec=1)


def _drive(tmp_path, on_prompt):
    sb = Sandbox(tmp_path)
    scenario = {"turns": [{"on_prompt": on_prompt, "events": ["busy"], "idle": False}]}
    with _BenchFake(scenario) as fake:
        run = Harness(sb, fake, _silent_cfg()).go()
        prompts = _prompts(fake)
    return sb, run, prompts


def test_two_commits_on_the_branch_leave_no_commit_to_point_at(tmp_path):
    def two(directory, text):
        work_claimed(directory, text)
        _git(directory, "commit", "-q", "--allow-empty", "-m", "KC-21: second")

    sb, run, prompts = _drive(tmp_path, two)
    ws = sb.ws("agent-a")
    assert _git(ws.path, "rev-list", "--count", f"{ws.base_sha}..HEAD") == "2"
    assert run.state is AgentState.STALLED
    assert run.commit is None, "an ambiguous branch must not name a sha"
    (turn,) = run.turns
    assert turn["harvest"]["verdict"] == "REWORK"
    assert "commits_ne_1" in turn["harvest"]["reasons"]
    assert len(prompts) == 1
