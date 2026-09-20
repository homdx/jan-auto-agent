"""Classify (not grade) what an entry does when the runner's own policy stalls a
turn whose worktree already holds a valid entry: harvested, or dropped?"""
import sys, tempfile
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent
for p in (str(REPO_ROOT), str(REPO_ROOT / "tests")):
    sys.path.insert(0, p)
from tests.test_contest_runner import Harness, Sandbox, _BenchFake, _git, make_config
from tests.test_kc21_accept import work_claimed
from tools.contest.runner import AgentState

with tempfile.TemporaryDirectory() as td:
    sb = Sandbox(Path(td))
    ws = sb.ws("agent-a")
    work_claimed(str(ws.path), "")
    head = _git(ws.path, "rev-parse", "HEAD")
    with _BenchFake({"turns": [{"events": ["busy"], "questions": 3, "delay": 0.5}]}) as fake:
        run = Harness(sb, fake, make_config(["agent-a"])).go()
    turn = run.turns[0] if run.turns else {}
    print("state=%-8s commit=%-5s harvest=%-6s error=%s" % (
        run.state.value,
        "yes" if run.commit == head else ("no" if run.commit is None else "other"),
        (turn.get("harvest") or {}).get("verdict"),
        run.last_error))
