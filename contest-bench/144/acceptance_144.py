"""Judge's acceptance suite for round 144 (AR-6), written from the ticket alone.

`arena issue list [--state S]` / `arena issue view NN` through
`tools.arena.cli.main`: a throw-away git repo on branch `arena` with
`contest.ini` (`out_dir = out`), tickets in `epic-tasks/` (checkout and branch)
and `.arena/drafts/`, round folders with a `state.json`, `REPO_ROOT` patched,
`rounds.PROC_ROOT` a fake `/proc`. Nothing runs a round, a kilo or a model.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/144/acceptance_144.py -n 8 -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.arena import cli  # noqa: E402
from tools.arena import rounds  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, env=ENV, check=True, capture_output=True,
                          text=True).stdout


def ticket(title: str, status: str) -> str:
    return (f"# {title}\n\n**Status:** {status}\n**Severity:** LOW\n**File:** x.py\n"
            f"**Symbol:** f\n**Round:** 1\n**Size:** S\n**Also touches:** -\n\nbody of {title}\n")


def put(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def state_json(folder: Path, nn: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "state.json").write_text(json.dumps(
        {"round_no": nn, "ticket": f"{nn}-x.md", "base_sha": "a" * 40, "started_at": 1.0,
         "agents": []}))


def fake_proc(root: Path, pid: int, argv: list[str], cwd: Path) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    os.symlink(cwd, d / "cwd")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "arena")
    put(r, "contest.ini", "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = p/m-a\n")
    put(r, "README", "x\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    proc = tmp_path / "proc"
    proc.mkdir()
    for mod in (cli, rounds):
        if isinstance(getattr(mod, "REPO_ROOT", None), Path):
            monkeypatch.setattr(mod, "REPO_ROOT", r)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(proc))
    monkeypatch.chdir(tmp_path)  # cwd must not matter
    return r


def run(capsys, *argv):
    try:
        rc = cli.main(list(argv))
    except SystemExit as e:
        rc = e.code
    c = capsys.readouterr()
    return rc, c.out, c.err


def listing(capsys, *extra) -> tuple[int, list[dict], str]:
    rc, out, err = run(capsys, "-o", "json", "issue", "list", *extra)
    data = json.loads(out) if rc == 0 else []
    return rc, data, err


def by_no(data: list[dict]) -> dict[int, dict]:
    return {int(t["number"]): t for t in data}


def states_repo(repo: Path, proc: Path) -> None:
    """One ticket per state."""
    put(repo, "epic-tasks/21-open.md", ticket("open one", "open"))
    put(repo, "epic-tasks/22-queued.md", ticket("queued one", "queued"))
    put(repo, "epic-tasks/23-landed.md", ticket("landed one", "landed"))
    put(repo, "epic-tasks/24-closed.md", ticket("closed one", "closed"))
    put(repo, "epic-tasks/25-done.md", ticket("done one", "open"))
    put(repo, "epic-tasks/26-running.md", ticket("running one", "open"))
    put(repo, "epic-tasks/27-legs.md", ticket("legs one", "open"))
    put(repo, ".arena/drafts/28-draft.md", ticket("draft one", "open"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "23: landed one")
    state_json(repo / "out" / "25", 25)
    state_json(repo / "out" / "26", 26)
    state_json(repo / "out" / "27.2", 27)
    fake_proc(proc, 4242, ["python3", "-m", "tools.contest", "run", "--ticket", "26"], repo)
    lock = repo / ".arena" / "locks" / "26.pid"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("4242\n")


# 1
def test_one_ticket_per_state(repo, capsys, tmp_path):
    states_repo(repo, tmp_path / "proc")
    rc, data, err = listing(capsys)
    assert rc == 0, err
    got = {n: t["state"] for n, t in by_no(data).items()}
    assert got == {21: "open", 22: "queued", 23: "landed", 24: "closed", 25: "done",
                   26: "running", 27: "done", 28: "draft"}


def test_numeric_order_and_titles(repo, capsys, tmp_path):
    states_repo(repo, tmp_path / "proc")
    put(repo, "epic-tasks/100-big.md", ticket("big one", "open"))
    rc, data, _ = listing(capsys)
    nums = [int(t["number"]) for t in data]
    assert nums == sorted(nums) and nums[-1] == 100
    assert by_no(data)[21]["title"] == "open one"


def test_table_columns(repo, capsys, tmp_path):
    states_repo(repo, tmp_path / "proc")
    rc, out, err = run(capsys, "issue", "list")
    assert rc == 0, err
    head = out.splitlines()[0].split()
    assert head[:3] == ["NN", "STATE", "TITLE"]
    row = next(l for l in out.splitlines() if "queued one" in l)
    assert row.split()[0].lstrip("0") == "22" and "queued" in row


def test_running_needs_live_pid(repo, capsys, tmp_path):
    states_repo(repo, tmp_path / "proc")
    (repo / ".arena" / "locks" / "26.pid").write_text("999999\n")
    import shutil
    shutil.rmtree(tmp_path / "proc" / "4242")
    rc, data, _ = listing(capsys)
    assert by_no(data)[26]["state"] == "done"


# 2
def test_branch_only_ticket(repo, capsys):
    put(repo, "epic-tasks/31-branch.md", ticket("branch one", "open"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "epic-tasks: ticket 31")
    (repo / "epic-tasks" / "31-branch.md").unlink()
    rc, data, err = listing(capsys)
    assert rc == 0, err
    t = by_no(data)[31]
    assert t["where"] == "branch" and t["state"] == "open" and t["title"] == "branch one"


def test_checkout_wins_over_branch(repo, capsys):
    put(repo, "epic-tasks/32-x.md", ticket("on branch", "open"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "epic-tasks: ticket 32")
    put(repo, "epic-tasks/32-x.md", ticket("in checkout", "queued"))
    rc, data, _ = listing(capsys)
    t = by_no(data)[32]
    assert t["where"] == "checkout" and t["state"] == "queued" and t["title"] == "in checkout"


# 3
def test_flag_commit_but_open(repo, capsys):
    put(repo, "epic-tasks/41-x.md", ticket("fixed but open", "open"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "41: the fix")
    rc, data, _ = listing(capsys)
    t = by_no(data)[41]
    assert isinstance(t["flags"], list) and len(t["flags"]) == 1
    assert "41" in t["flags"][0]


def test_flag_landed_without_commit(repo, capsys):
    put(repo, "epic-tasks/42-x.md", ticket("landed no commit", "landed"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "epic-tasks: ticket 42")
    rc, data, _ = listing(capsys)
    t = by_no(data)[42]
    assert t["state"] == "landed" and len(t["flags"]) == 1


def test_no_flags_when_consistent(repo, capsys, tmp_path):
    states_repo(repo, tmp_path / "proc")
    rc, data, _ = listing(capsys)
    d = by_no(data)
    assert d[23]["flags"] == [] and d[21]["flags"] == [] and d[22]["flags"] == []


def test_flag_draft_and_epic(repo, capsys):
    put(repo, "epic-tasks/43-x.md", ticket("epic side", "open"))
    put(repo, ".arena/drafts/43-x.md", ticket("draft side", "open"))
    rc, data, _ = listing(capsys)
    t = by_no(data)[43]
    assert t["state"] == "open" and t["title"] == "epic side" and len(t["flags"]) == 1
    assert len([x for x in data if int(x["number"]) == 43]) == 1


def test_flag_two_files(repo, capsys):
    put(repo, "epic-tasks/44-a.md", ticket("a", "open"))
    put(repo, "epic-tasks/44-b.md", ticket("b", "open"))
    rc, data, _ = listing(capsys)
    assert len([x for x in data if int(x["number"]) == 44]) == 1
    t = by_no(data)[44]
    assert len(t["flags"]) == 1 and "44-a.md" in t["flags"][0] and "44-b.md" in t["flags"][0]


def test_rejected_draft_skipped(repo, capsys):
    put(repo, ".arena/drafts/45-x.rejected.md", ticket("rejected", "open"))
    put(repo, "epic-tasks/46-x.md", ticket("other", "open"))
    rc, data, _ = listing(capsys)
    assert 45 not in by_no(data)


def test_bang_column(repo, capsys):
    put(repo, "epic-tasks/41-x.md", ticket("fixed but open", "open"))
    put(repo, "epic-tasks/47-x.md", ticket("clean one", "open"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "41: the fix")
    rc, out, _ = run(capsys, "issue", "list")
    flagged = next(l for l in out.splitlines() if "fixed but open" in l)
    clean = next(l for l in out.splitlines() if "clean one" in l)
    assert flagged.rstrip().endswith("!") and not clean.rstrip().endswith("!")


# 4
def test_state_filter(repo, capsys, tmp_path):
    states_repo(repo, tmp_path / "proc")
    rc, data, _ = listing(capsys, "--state", "open")
    assert rc == 0 and [int(t["number"]) for t in data] == [21]


def test_state_filter_unknown(repo, capsys, tmp_path):
    states_repo(repo, tmp_path / "proc")
    rc, out, err = run(capsys, "issue", "list", "--state", "nosuch")
    assert rc == 2 and len(err.strip().splitlines()) == 1


def test_state_filter_empty(repo, capsys):
    put(repo, "epic-tasks/21-open.md", ticket("open one", "open"))
    rc, out, err = run(capsys, "issue", "list", "--state", "landed")
    assert rc == 3


# 5
def test_json_shape(repo, capsys, tmp_path):
    states_repo(repo, tmp_path / "proc")
    rc, data, _ = listing(capsys)
    for t in data:
        assert {"number", "state", "title", "path", "where", "status_field", "flags"} <= set(t)
        assert isinstance(t["flags"], list)
    assert by_no(data)[28]["where"] == "draft"
    assert by_no(data)[22]["status_field"] == "queued"


# 6
def test_view(repo, capsys):
    put(repo, "epic-tasks/41-x.md", ticket("fixed but open", "open"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "41: the fix")
    rc, out, err = run(capsys, "issue", "view", "41")
    assert rc == 0, err
    assert out.startswith(ticket("fixed but open", "open").rstrip("\n")[:40])
    assert "body of fixed but open" in out
    assert [l for l in out.splitlines() if l.startswith("! ")]


def test_view_branch_only(repo, capsys):
    put(repo, "epic-tasks/31-branch.md", ticket("branch one", "open"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "epic-tasks: ticket 31")
    (repo / "epic-tasks" / "31-branch.md").unlink()
    rc, out, _ = run(capsys, "issue", "view", "31")
    assert rc == 0 and "body of branch one" in out


def test_view_missing(repo, capsys):
    rc, out, err = run(capsys, "issue", "view", "77")
    assert rc == 1 and len(err.strip().splitlines()) == 1
    assert "Traceback" not in err


def test_view_bad_number(repo, capsys):
    rc, out, err = run(capsys, "issue", "view", "abc")
    assert rc == 2


def test_view_json(repo, capsys):
    put(repo, "epic-tasks/21-open.md", ticket("open one", "open"))
    rc, out, _ = run(capsys, "-o", "json", "issue", "view", "21")
    assert rc == 0
    d = json.loads(out)
    if isinstance(d, list):
        d = d[0]
    assert "body of open one" in d["text"] and d["state"] == "open"


# 7
@pytest.mark.parametrize("verb", ["land"])
def test_other_issue_verbs_unimplemented(repo, capsys, verb):
    args = ["issue", verb, "x"] if verb == "create" else ["issue", verb, "21"]
    rc, _, err = run(capsys, *args)
    assert rc != 0 and "not implemented" in err


# 8
def test_read_only(repo, capsys, tmp_path):
    states_repo(repo, tmp_path / "proc")
    files = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if ".git/" not in
                   p.as_posix() + "/")
    refs = git(repo, "for-each-ref")
    status = git(repo, "status", "--porcelain")
    run(capsys, "issue", "list")
    run(capsys, "issue", "view", "21")
    assert sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if ".git/" not in
                  p.as_posix() + "/") == files
    assert git(repo, "for-each-ref") == refs and git(repo, "status", "--porcelain") == status


def test_bad_branch_refused(repo, capsys):
    put(repo, "epic-tasks/21-open.md", ticket("open one", "open"))
    rc, _, err = run(capsys, "issue", "list", "--branch", "nosuch")
    assert rc == 2 and len(err.strip().splitlines()) == 1
