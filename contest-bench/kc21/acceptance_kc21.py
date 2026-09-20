"""KC-21 acceptance — the judge's own suite, written from the ticket, not from any entry.

Every scenario is the ticket's Acceptance list read literally, through the public
contract only: `run_agent` against `tests/_kilo_fake.py` and `cli.export_patches`
against four one-commit worktrees. Helpers come from `tests/test_contest_runner.py`
(the sandbox, the fake, the harness) — never an entry's own new helper.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_contest_runner import (  # noqa: E402
    Harness,
    Sandbox,
    _BenchFake,
    _git,
    _jsonl,
    _prompts,
    _write,
    make_config,
)
from tools.contest import cli  # noqa: E402
from tools.contest import runner as runner_mod  # noqa: E402
from tools.contest.roster import AgentSpec  # noqa: E402
from tools.contest.runner import AgentRun, AgentState, RoundState  # noqa: E402
from tools.contest.workspace import Workspace  # noqa: E402

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")

TICKET = "45-kc6-test.md"
ERROR_PAYLOAD = {"name": "ProviderError", "message": "boom-42"}


# ── the turn's work, written here so no entry's helper is in the judge ───────

def _commit_work(directory: str, *, claim: bool) -> str:
    """A change, a test, one commit — with or without the `PROGRESS.csv` row."""
    d = Path(directory)
    _write(d / "pkg" / "thing.py", f"def thing():\n    return 42  # {time.time()}\n")
    _write(d / "tests" / "test_thing.py",
           "from pkg.thing import thing\n\n\ndef test_thing():\n    assert thing() == 42\n")
    _git(directory, "add", "-A")
    _git(directory, "commit", "-q", "-m", "KC-21: thing")
    sha = _git(directory, "rev-parse", "HEAD")
    if claim:
        agent = Path(directory).name
        csv_path = d / "runs" / agent / "PROGRESS.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(f"ticket,finding,outcome,commit,note\n{TICKET},,FIXED,{sha},test\n",
                            encoding="utf-8")
    return sha


def work_claimed(directory, text):
    _commit_work(directory, claim=True)


def work_unclaimed(directory, text):
    _commit_work(directory, claim=False)


def _drive(tmp_path, scenario, cfg, caplog=None):
    sb = Sandbox(tmp_path)
    with _BenchFake(scenario) as fake:
        h = Harness(sb, fake, cfg)
        if caplog is not None:
            with caplog.at_level(logging.INFO, logger="tools.contest.runner"):
                run = h.go()
        else:
            run = h.go()
        prompts = _prompts(fake)
    return sb, run, prompts


def _silent(on_prompt):
    """A turn that works, emits one event and then never idles — KC-12's silence."""
    return {"turns": [{"on_prompt": on_prompt, "events": ["busy"], "idle": False}]}


def _errored(on_prompt):
    return {"turns": [{"on_prompt": on_prompt, "events": ["busy"], "error": ERROR_PAYLOAD}]}


def _cfg_silence():
    return make_config(["agent-a"], turn_timeout_sec=30, idle_event_timeout_sec=1)


def _cfg_plain():
    return make_config(["agent-a"], turn_timeout_sec=30, idle_event_timeout_sec=60)


def _head(ws) -> str:
    return _git(ws.path, "rev-parse", "HEAD")


# ── 1. a stall on a valid entry is READY ─────────────────────────────────────

def test_stall_after_a_valid_commit_is_ready_with_the_sha_and_the_after_note(tmp_path, caplog):
    sb, run, prompts = _drive(tmp_path, _silent(work_claimed), _cfg_silence(), caplog)
    ws = sb.ws("agent-a")
    assert run.state is AgentState.READY, (run.state, run.last_error)
    assert run.commit == _head(ws)
    (turn,) = run.turns
    assert turn["idle_status"] == "stalled"
    assert turn["harvest"]["verdict"] == "READY"
    assert len(prompts) == 1
    line = f"agent-a: READY — {run.commit[:12]} after no event for 1s"
    assert any(r.getMessage() == line for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


def test_the_stalled_turn_is_written_once_with_both_idle_status_and_harvest(tmp_path):
    sb, run, _ = _drive(tmp_path, _silent(work_claimed), _cfg_silence())
    rows = _jsonl(sb.out_dir / "agent-a" / "turns.jsonl")
    assert len(rows) == 1, rows
    (row,) = rows
    assert row["idle_status"] == "stalled"
    assert row["harvest"]["verdict"] == "READY"


# ── 2. a stall on a commit the harvest rejects stays STALLED, patch exported ──

def test_stall_on_a_rejected_commit_is_stalled_with_the_sha_and_a_rework_verdict(tmp_path):
    sb, run, prompts = _drive(tmp_path, _silent(work_unclaimed), _cfg_silence())
    ws = sb.ws("agent-a")
    assert run.state is AgentState.STALLED, (run.state, run.last_error)
    assert run.commit == _head(ws), "the operator's patch needs the sha of the one commit"
    (turn,) = run.turns
    assert turn["idle_status"] == "stalled"
    assert turn["harvest"]["verdict"] == "REWORK"
    assert "no_progress_row" in turn["harvest"]["reasons"]
    assert run.last_error == "no event for 1s"
    assert run.attempt == 0
    assert len(prompts) == 1, "a dead session is never re-prompted"


# ── 3. the same two outcomes for a session.error ─────────────────────────────

def test_session_error_on_a_valid_commit_is_ready(tmp_path):
    sb, run, _ = _drive(tmp_path, _errored(work_claimed), _cfg_plain())
    assert run.state is AgentState.READY, (run.state, run.last_error)
    assert run.commit == _head(sb.ws("agent-a"))
    (turn,) = run.turns
    assert turn["idle_status"] == "error"
    assert turn["harvest"]["verdict"] == "READY"


def test_session_error_on_a_rejected_commit_stays_error_with_the_sha(tmp_path):
    sb, run, prompts = _drive(tmp_path, _errored(work_unclaimed), _cfg_plain())
    assert run.state is AgentState.ERROR, (run.state, run.last_error)
    assert run.commit == _head(sb.ws("agent-a"))
    (turn,) = run.turns
    assert turn["harvest"]["verdict"] == "REWORK"
    assert "boom-42" in run.last_error
    assert run.attempt == 0 and len(prompts) == 1


# ── 4. a stall with no commit keeps today's path byte-for-byte ───────────────

def test_a_stall_with_no_commit_never_harvests(tmp_path, monkeypatch):
    def explode(*a, **kw):
        raise AssertionError("_harvest was called for a worktree with no commit")

    monkeypatch.setattr(runner_mod, "_harvest", explode)
    sb, run, _ = _drive(tmp_path, {"turns": [{"events": [], "idle": False}]}, _cfg_silence())
    assert run.state is AgentState.STALLED
    assert run.commit is None
    (turn,) = run.turns
    assert "harvest" not in turn
    assert run.last_error == "no event for 1s"
    assert len(_jsonl(sb.out_dir / "agent-a" / "turns.jsonl")) == 1


# ── 5. export_patches names the file by the terminal state ──────────────────

def _repo_with_one_commit(root: Path, agent: str) -> tuple:
    path = root / agent
    path.mkdir(parents=True)
    _git(str(path), "init", "-q", "-b", "main")
    _git(str(path), "config", "user.email", "t@t")
    _git(str(path), "config", "user.name", "t")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(str(path), "add", "-A")
    _git(str(path), "commit", "-q", "-m", "base")
    base = _git(str(path), "rev-parse", "HEAD")
    (path / "thing.py").write_text("x = 1\n", encoding="utf-8")
    _git(str(path), "add", "-A")
    _git(str(path), "commit", "-q", "-m", "the entry")
    sha = _git(str(path), "rev-parse", "HEAD")
    return Workspace(agent=agent, path=path, branch=f"contest/60/{agent}",
                     base_sha=base, kind="worktree"), sha


@pytest.mark.parametrize("state,suffix", [
    (AgentState.READY, ".patch"),
    (AgentState.GAVE_UP, ".GAVE_UP.patch"),
    (AgentState.STALLED, ".STALLED.patch"),
    (AgentState.ERROR, ".ERROR.patch"),
])
def test_export_patches_names_the_patch_by_the_terminal_state(tmp_path, state, suffix):
    agent = f"agent-{state.value.lower()}"
    ws, sha = _repo_with_one_commit(tmp_path / "wt", agent)
    run = AgentRun(agent=AgentSpec(agent, "kenary", f"{agent}:free"), workspace=ws,
                   state=state, commit=sha)
    out = tmp_path / "out"
    written = cli.export_patches(
        RoundState(round_no=60, ticket=TICKET, base_sha=ws.base_sha, started_at=1.0,
                   agents=[run]), [ws], out)
    assert [Path(w).name for w in written] == [f"{agent}{suffix}"]
    body = (out / f"{agent}{suffix}").read_text(encoding="utf-8")
    assert "the entry" in body and "thing.py" in body
