"""HARDEN-30 (round 328): the four bugs of the 2026-11-02 review, pinned.

A: an acceptance check run by `Executor.run` from a subdirectory imported the source
repository's modules (its `PYTHONPATH` had `base_dir` first) and wrote into the source tree.
B: a submission that deleted every test scored READY: a deleted file counted as a shipped
test, and a run in which every root was `absent` passed. C: an interrupt between two
`upsert_task` calls of a plan emission left a partial `plan.json` that resume took for a
finished plan. D: reopening a regression ticket did not reset a spent time budget when the
fix task had no `feedback_round_*.md` files.

Each test below fails on the round's base (3a73c55) except the guards, which pin what must
keep working.
"""

from __future__ import annotations

import configparser
import csv
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.auto.bug_fix_loop import BugFixLoop
from tools.auto.commit_on_success import CommitOnSuccess
from tools.auto.executor import Executor
from tools.auto.git_manager import make_git_manager
from tools.auto.outer_loop import OuterLoopResult
from tools.auto.state import STATUS_DONE, StateStore
from tools.auto.ticket_store import TicketStore
from tools.contest.gates import judge_worktree
from tools.contest.harvest import harvest
from tools.contest.workspace import Workspace


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo(path: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(path, "init", "-q")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    return path


# ── A ───────────────────────────────────────────────────────────────────────

_APP = ('from pathlib import Path\n'
        'def check():\n'
        '    Path(__file__).with_name("side_effect.txt").write_text("changed")\n'
        '    print("FILE=" + __file__)\n')


def _imported(res) -> str:
    return next((l[5:] for l in (res.stdout or "").splitlines() if l.startswith("FILE=")), "")


def _run_check(base: Path, command: str, target: str = "app.py"):
    return Executor(base, timeout_sec=60).run(
        {"id": "T1", "target_files": [target], "acceptance_check": command})


@pytest.fixture
def check_repo(tmp_path):
    return _repo(tmp_path / "repo", {"app.py": _APP, "checks/check.py": "import app\napp.check()\n",
                                     ".gitignore": ".agent/\n"})


def test_a_a_subdir_check_imports_the_workspace_and_leaves_the_source(check_repo, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    res = _run_check(check_repo, "python checks/check.py")
    assert res.exit_code == 0, res.stderr
    assert (check_repo / ".agent" / "workspace") in Path(_imported(res)).parents
    assert not (check_repo / "side_effect.txt").exists()


def test_a_an_inherited_pythonpath_to_the_source_does_not_win(check_repo, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(check_repo))
    res = _run_check(check_repo, "python checks/check.py")
    assert res.exit_code == 0, res.stderr
    assert not (check_repo / "side_effect.txt").exists()


def test_a_a_src_layout_entry_is_mapped_into_the_workspace(tmp_path, monkeypatch):
    base = _repo(tmp_path / "repo", {"src/app.py": _APP, "checks/check.py": "import app\napp.check()\n",
                                     ".gitignore": ".agent/\n"})
    monkeypatch.setenv("PYTHONPATH", str(base / "src"))
    res = _run_check(base, "python checks/check.py", target="src/app.py")
    assert res.exit_code == 0, res.stderr
    assert (base / ".agent" / "workspace") in Path(_imported(res)).parents
    assert not (base / "src" / "side_effect.txt").exists()


def test_a_guard_an_unrelated_inherited_entry_still_reaches_the_check(check_repo, tmp_path, monkeypatch):
    lib = tmp_path / "site"
    lib.mkdir()
    (lib / "extlib.py").write_text("NAME = 'outside'\n")
    (check_repo / "checks" / "ext.py").write_text("import extlib\nprint(extlib.NAME)\n")
    monkeypatch.setenv("PYTHONPATH", str(lib))
    res = _run_check(check_repo, "python checks/ext.py")
    assert res.exit_code == 0 and "outside" in res.stdout, res.stderr


def test_a_guard_run_raw_still_runs_and_imports_in_base_dir(check_repo, monkeypatch):
    import sys
    monkeypatch.delenv("PYTHONPATH", raising=False)
    res = Executor(check_repo, timeout_sec=30).run_raw(f"{sys.executable} checks/check.py")
    assert res.exit_code == 0, res.stderr
    assert Path(_imported(res)).resolve() == (check_repo / "app.py").resolve()


# ── B ───────────────────────────────────────────────────────────────────────

_TICKET = "# R1 — probe\n\n**File:** `app.py`\n\n**Also touches:** `tests/`\n"


def _round(tmp_path, files: dict):
    base = _repo(tmp_path / "repo", {"epic-tasks/01-r1.md": _TICKET, "app.py": "X = 1\n", **files})
    sha = _git(base, "rev-parse", "HEAD")
    path = tmp_path / "wt-a"
    _git(base, "worktree", "add", "-q", str(path), sha)
    _git(path, "checkout", "-q", "-b", "contest/01/a", sha)
    return (Workspace(agent="a", path=path, branch="contest/01/a", base_sha=sha, kind="worktree"),
            base / "epic-tasks" / "01-r1.md")


def _submit(ws, ticket, *, rm=(), write=None, mv=()):
    for src, dst in mv:
        _git(ws.path, "mv", src, dst)
    for rel in rm:
        _git(ws.path, "rm", "-q", "-r", rel)
    for rel, text in (write or {}).items():
        (ws.path / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws.path / rel).write_text(text)
    _git(ws.path, "add", "-A")
    _git(ws.path, "commit", "-qm", "submission")
    sha = _git(ws.path, "rev-parse", "HEAD")
    ws.progress_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(ws.progress_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticket", "finding", "outcome", "commit", "note"])
        w.writeheader()
        w.writerow({"ticket": ticket.name, "finding": "", "outcome": "FIXED", "commit": sha, "note": "n"})


_FAILING = "def test_app():\n    assert False\n"
_PASSING = "def test_app():\n    assert True\n"


def test_b_deleting_the_only_test_is_rework(tmp_path):
    ws, ticket = _round(tmp_path, {"tests/test_app.py": _FAILING})
    _submit(ws, ticket, rm=["tests"], write={"app.py": "X = 2\n"})
    h = harvest(ws, ticket, run_tests=True)
    assert h.verdict == "REWORK", h.facts
    assert any(r.blocking for r in h.reasons)


def test_b_a_deleted_test_is_not_a_shipped_test(tmp_path):
    ws, ticket = _round(tmp_path, {"tests/test_app.py": _FAILING})
    _submit(ws, ticket, rm=["tests/test_app.py"], write={"app.py": "X = 2\n"})
    row = judge_worktree("a", str(ws.path), ws.base_sha, ["app.py"], False)
    assert row["test_files"] == 0 and row["gate"] == "FAIL"


def test_b_a_run_in_which_no_root_ran_is_rework(tmp_path):
    ws, ticket = _round(tmp_path, {"tests/test_app.py": _FAILING})
    _submit(ws, ticket, rm=["tests"], write={"app.py": "X = 2\n", "test_top.py": _PASSING})
    assert harvest(ws, ticket, run_tests=True).verdict == "REWORK"


def test_b_guard_a_renamed_passing_test_is_ready(tmp_path):
    ws, ticket = _round(tmp_path, {"tests/test_app.py": _PASSING})
    _submit(ws, ticket, mv=[("tests/test_app.py", "tests/test_renamed.py")], write={"app.py": "X = 2\n"})
    assert harvest(ws, ticket, run_tests=True).verdict == "READY"


# ── C ───────────────────────────────────────────────────────────────────────

class _Ctrl:
    def __init__(self, base: Path, with_git: bool):
        self.base_dir = base
        self.config_path = str(base / "agents.ini")
        self.goal = "improve current code"
        self.state = StateStore(base / ".agent")
        self.state.initialise(self.goal, base)
        self.run_trace = None
        self.progress_display = None
        self.task_mode = "code"
        self.git = None
        if with_git:
            cfg = configparser.ConfigParser()
            cfg["auto"] = {"git_user": "t", "git_email": "t@t"}
            self.git = make_git_manager(base, cfg)


def _plan(ctrl, interrupt_at=None) -> int:
    """`_run_plan_phase` offline; a KeyboardInterrupt on the Nth task write. Returns ingest calls."""
    from tools.auto import pipeline
    from tools.auto.architect import CandidateTask, CitedLocation
    from tools.auto.repo_ingest import RepoCluster
    cands = [CandidateTask(title=f"Fix thing {i}", instruction=f"Improve thing {i}.",
                           target_files=[f"m{i}.py"], acceptance_check="python -m pytest tests/ -q",
                           cited_location=CitedLocation(file=f"m{i}.py", symbol="f", line_start=1, line_end=2),
                           cluster="agents") for i in (1, 2, 3)]
    real, calls, ingested = StateStore.upsert_task, {"n": 0}, {"n": 0}

    def upsert(self, task, *a, **kw):
        calls["n"] += 1
        if calls["n"] == interrupt_at:
            raise KeyboardInterrupt
        return real(self, task, *a, **kw)

    def ingest(*a, **kw):
        ingested["n"] += 1
        return [RepoCluster(name="agents", files=[c.target_files[0] for c in cands], patterns=[])]

    cfg = configparser.ConfigParser()
    cfg["auto"] = {}
    with patch.object(pipeline, "ingest_repo", ingest), \
         patch.object(pipeline, "review_clusters", lambda *a, **kw: cands), \
         patch.object(pipeline, "filter_candidates", lambda *a, **kw: (cands, [])), \
         patch.object(StateStore, "upsert_task", upsert):
        try:
            pipeline._run_plan_phase(ctrl, cfg)
        except KeyboardInterrupt:
            pass
    return ingested["n"]


def _plan_repo(tmp_path, with_git: bool) -> Path:
    files = {f"m{i}.py": "def f():\n    return 1\n" for i in (1, 2, 3)}
    files[".gitignore"] = ".agent/\n"
    base = tmp_path / "repo"
    if with_git:
        return _repo(base, files)
    for rel, text in files.items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text(text)
    return base


@pytest.mark.parametrize("with_git", [True, False], ids=["git", "no-git"])
def test_c_resume_after_an_interrupted_emission_has_every_task(tmp_path, with_git):
    base = _plan_repo(tmp_path, with_git)
    _plan(_Ctrl(base, with_git), interrupt_at=2)
    ctrl = _Ctrl(base, with_git)
    _plan(ctrl)
    titles = sorted(t["title"] for t in ctrl.state.all_tasks())
    assert titles == ["Fix thing 1", "Fix thing 2", "Fix thing 3"]


def test_c_a_done_task_stays_done_through_recovery(tmp_path):
    base = _plan_repo(tmp_path, True)
    first = _Ctrl(base, True)
    _plan(first, interrupt_at=2)
    done_id = first.state.all_tasks()[0]["id"]
    first.state.set_task_status(done_id, STATUS_DONE)
    ctrl = _Ctrl(base, True)
    _plan(ctrl)
    assert {t["id"]: t["status"] for t in ctrl.state.all_tasks()}[done_id] == STATUS_DONE
    assert len(ctrl.state.all_tasks()) == 3


def test_c_the_no_git_path_skips_a_task_the_schema_rejects(tmp_path):
    """Found by sensenova var2's test: the no-git emission raised on a bad task, so the
    rest of the backlog was lost (and with the emission marker, every resume replanned)."""
    from tools.auto.pipeline import _emit_without_git
    from tools.auto.state import make_task
    base = _plan_repo(tmp_path, False)
    ctrl = _Ctrl(base, False)
    good = make_task(id="AUTO-T1", title="t1", instruction="i")
    bad = dict(make_task(id="AUTO-T2", title="t2", instruction="i"), round="not an int")
    later = make_task(id="AUTO-T3", title="t3", instruction="i")
    backlog = SimpleNamespace(to_state_tasks=lambda: [good, bad, later], auto_tasks=[good, bad, later])
    with patch("tools.auto.pipeline.to_improvements_md", lambda b: "# plan\n"):
        _emit_without_git(ctrl, backlog, None)
    assert [t["id"] for t in ctrl.state.all_tasks()] == ["AUTO-T1", "AUTO-T3"]
    assert _plan(_Ctrl(base, False)) == 0


def test_c_guard_a_complete_plan_is_not_planned_again(tmp_path):
    base = _plan_repo(tmp_path, True)
    _plan(_Ctrl(base, True), interrupt_at=2)
    _plan(_Ctrl(base, True))
    assert _plan(_Ctrl(base, True)) == 0


# ── D ───────────────────────────────────────────────────────────────────────

_LEDGER = json.dumps({"consumed_s": 2, "session_started_at": None})
_TASK = {"id": "T1", "title": "t", "target_files": ["a.py"], "acceptance_check": "true"}


def _loop(tmp_path, *, spend: bool):
    base = _repo(tmp_path / "repo", {".gitignore": ".agent/\n", "a.py": "x = 1\n"})
    state = StateStore(base / ".agent")
    state.initialise("g", base)
    tickets = TicketStore(base / ".agent" / "tickets")
    seen: list = []

    class Outer:
        def run_task(self, task, base_dir, **_):
            p = state.task_dir(task["id"]) / "deadline_started_at.txt"
            seen.append(p.read_text() if p.exists() else None)
            if spend:
                p.write_text(_LEDGER)       # the attempt spends its budget before any feedback round
            return OuterLoopResult(task_id=task["id"], passed=False, rounds_used=0,
                                   exhausted=True, feedback_files=[], inner_results=[])

    cfg = configparser.ConfigParser()
    cfg["auto"] = {"git_user": "t", "git_email": "t@t"}
    loop = BugFixLoop(Outer(), CommitOnSuccess(make_git_manager(base, cfg), state), tickets, state)
    return loop, base, state, tickets, seen


def _fail():
    return SimpleNamespace(passed=False, stdout="F", stderr="", returncode=1, output="F")


def test_d_a_reopened_ticket_without_rounds_gets_a_fresh_budget(tmp_path):
    loop, base, _, tickets, seen = _loop(tmp_path, spend=True)
    first = loop.handle_regression(dict(_TASK), _fail(), base)
    tickets.update(first.ticket_id, status="open", fix_attempts=1)
    loop.handle_regression(dict(_TASK), _fail(), base)
    assert len(seen) == 2 and seen[1] is None


def test_d_the_reset_helper_clears_the_ledger_without_rounds(tmp_path):
    loop, _, state, _, _ = _loop(tmp_path, spend=False)
    state.write_task_file("BUG-FIX-T1", "deadline_started_at.txt", _LEDGER)
    loop._clear_stale_fix_rounds("BUG-FIX-T1")
    assert not (state.task_dir("BUG-FIX-T1") / "deadline_started_at.txt").exists()


def test_d_guard_an_ordinary_resume_keeps_the_spent_budget(tmp_path):
    loop, base, state, _, seen = _loop(tmp_path, spend=False)
    state.write_task_file("BUG-FIX-T1", "deadline_started_at.txt", _LEDGER)
    loop.handle_regression(dict(_TASK), _fail(), base)
    assert seen == [_LEDGER]
