"""tests/test_arena_run_start.py — AR-3: `arena run start NN`, its base ref and step 0."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tools.arena import cli, gitref, rounds

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

TICKET = """# AR-9 — round {num}

**Status:** {status}
**File:** pkg/thing.py
**Symbol:** thing
**Size:** M

body {extra}
"""

INI = """[contest]
out_dir = {out}

[contest.agent.agent-a]
model = test/agent-a
"""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                          env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ticket(num: int = 7, status: str = "draft", extra: str = "") -> str:
    return TICKET.format(num=num, status=status, extra=extra)


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """A throw-away repo on branch `main`, one commit, `cli.REPO_ROOT` patched onto it."""
    repo = tmp_path / "repo"
    _write(repo / "contest.ini", INI.format(out="contest-out"))
    _write(repo / "epic-tasks" / "01-old.md", _ticket(1, "landed"))
    _write(repo / ".gitignore", ".arena/\ncontest-out/\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, ENV[key])
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return repo


def _draft(repo: Path, text: str, name: str = "07-x.md") -> None:
    _write(repo / ".arena" / "drafts" / name, text)


def _ref(repo: Path, nn: int = 7) -> str | None:
    proc = subprocess.run(["git", "rev-parse", "--verify", "-q", f"arena-round/{nn}"],
                          cwd=str(repo), capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _stub(script: str):
    """A `rounds.SPAWN` that runs *script* (python) instead of the runner, recording argv."""
    seen: list = []

    def spawn(line, cwd):
        seen.append(line)
        return subprocess.Popen([sys.executable, "-c", script], cwd=cwd)
    spawn.seen = seen
    return spawn


# 1 ─ commit_file_on never touches the checkout
def test_commit_file_on_builds_the_ref_without_touching_the_checkout(repo):
    index = repo / ".git" / "index"
    before = (_git(repo, "status", "--porcelain"), _git(repo, "rev-parse", "HEAD"),
              index.read_bytes())
    tip = _git(repo, "rev-parse", "main")
    sha = gitref.commit_file_on(repo, tip, "epic-tasks/07-x.md", _ticket(), "7: t",
                                "refs/heads/arena-round/7")
    assert _ref(repo) == sha
    assert _git(repo, "rev-parse", f"{sha}^") == tip
    names = _git(repo, "ls-tree", "-r", "--name-only", sha).splitlines()
    assert sorted(names) == sorted(
        _git(repo, "ls-tree", "-r", "--name-only", tip).splitlines() + ["epic-tasks/07-x.md"])
    after = (_git(repo, "status", "--porcelain"), _git(repo, "rev-parse", "HEAD"),
             index.read_bytes())
    assert after == before


def test_a_git_failure_is_one_line_error(repo):
    with pytest.raises(gitref.GitRefError) as err:
        gitref.commit_file_on(repo, "no-such-ref", "a.md", "x", "m", "refs/heads/x")
    assert "\n" not in str(err.value) and str(err.value).startswith("git read-tree")


# 2 ─ reuse
def test_the_same_ticket_twice_reuses_the_sha(repo):
    content = rounds.open_status(_ticket())
    first = rounds.build_round_ref(repo, 7, "main", "07-x.md", content)
    assert rounds.build_round_ref(repo, 7, "main", "07-x.md", content) == first
    assert "**Status:** open" in _git(repo, "show", "arena-round/7:epic-tasks/07-x.md")


def test_a_ticket_already_on_the_tip_is_the_tip_itself(repo):
    content = rounds.open_status(_ticket())
    _write(repo / "epic-tasks" / "07-x.md", content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ticket")
    tip = _git(repo, "rev-parse", "main")
    assert rounds.build_round_ref(repo, 7, "main", "07-x.md", content) == tip
    assert _ref(repo) == tip


# 3 ─ changed text, --fresh-ticket, -y, a moved tip
def test_changed_text_is_refused_naming_fresh_ticket(repo, capsys):
    _draft(repo, _ticket())
    rounds.build_round_ref(repo, 7, "main", "07-x.md", rounds.open_status(_ticket()))
    old = _ref(repo)
    _draft(repo, _ticket(extra="changed"))
    monkey_spawn = _stub("raise SystemExit(0)")
    rounds.SPAWN, saved = monkey_spawn, rounds.SPAWN
    try:
        assert cli.main(["run", "start", "7"]) == 2
        assert "--fresh-ticket" in capsys.readouterr().err
        assert _ref(repo) == old
        assert cli.main(["run", "start", "7", "--fresh-ticket"]) == 0
        new = _ref(repo)
        assert new != old
        # a state.json in 07/ and no -y: refused, ref unchanged
        _write(repo / "contest-out" / "07" / "state.json", "{}")
        _draft(repo, _ticket(extra="again"))
        assert cli.main(["run", "start", "7", "--fresh-ticket"]) == 2
        assert "-y" in capsys.readouterr().err
        assert _ref(repo) == new
        assert cli.main(["-y", "run", "start", "7", "--fresh-ticket"]) == 0
        assert _ref(repo) != new
    finally:
        rounds.SPAWN = saved


def test_a_moved_tip_is_the_same_refusal(repo):
    content = rounds.open_status(_ticket())
    rounds.build_round_ref(repo, 7, "main", "07-x.md", content)
    _write(repo / "other.txt", "x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "move")
    with pytest.raises(rounds.RoundError, match="--fresh-ticket"):
        rounds.build_round_ref(repo, 7, "main", "07-x.md", content)


# 4 ─ the run line
def test_build_run_line_is_exact():
    assert rounds.build_run_line(7, {"models": "a,a,b", "legs": "2"}, ["--no-gate"]) == [
        sys.executable, "-m", "tools.contest", "run", "--ticket", "7",
        "--base", "arena-round/7", "--models", "a,a,b", "--legs", "2", "--no-gate"]


@pytest.mark.parametrize("passthrough", [["--base", "X"], ["--ticket=3"], ["--out", "d"],
                                         ["--target", "r"]])
def test_an_arena_owned_flag_after_double_dash_is_refused(repo, capsys, passthrough):
    _draft(repo, _ticket())
    assert cli.main(["run", "start", "7", "--", *passthrough]) == 2
    assert capsys.readouterr().err.count("\n") == 1
    assert _ref(repo) is None


# 5 ─ a dirty epic-tasks/
def test_a_dirty_epic_tasks_is_refused_before_anything_is_written(repo, capsys):
    _draft(repo, _ticket())
    _write(repo / "epic-tasks" / "99-draft.md", "x")
    assert cli.main(["run", "start", "7"]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "epic-tasks/99-draft.md" in err and ".arena/drafts/" in err
    assert _ref(repo) is None
    assert not (repo / ".arena" / "rounds" / "7.json").exists()


# 6 ─ step 0: a ticket only at the base, through the real runner's --dry-run
def test_step0_a_ticket_only_at_the_base_passes_intake(tmp_path, monkeypatch, capsys):
    from test_contest_cli import TICKET_01, Sandbox, _ticket as kc_ticket
    from test_contest_cli_export import _no_server
    from test_contest_cli import _plan
    from tools.contest import cli as contest_cli

    sb = Sandbox(tmp_path, tickets=((TICKET_01, kc_ticket("01", "first", "landed")),))
    text = (sb.repo / "contest.ini").read_text(encoding="utf-8")
    _write(sb.repo / "contest.ini",
           text.replace("[contest]\n", "[contest]\nlegs_by_size = M=2\n", 1))
    _git(sb.repo, "commit", "-q", "-am", "legs by size")
    _draft(sb.repo, _ticket())
    rounds.build_round_ref(sb.repo, 7, "main", "07-x.md", rounds.open_status(_ticket()))
    assert not (sb.repo / "epic-tasks" / "07-x.md").exists()

    tmp = tmp_path / "systmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    monkeypatch.chdir(sb.repo)
    _no_server(monkeypatch)
    code = contest_cli.main(["run", "--ticket", "7", "--base", "arena-round/7",
                             "--no-tests", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    plan = _plan(out)
    assert plan["ticket"].startswith("07-x.md")
    # sized from the temp copy's `**Size:** M`: legs_by_size gives M two legs
    assert any(line.split()[:3] == ["legs", "2", "(size)"] for line in out.splitlines())
    assert list(tmp.iterdir()) == []


def test_step0_a_ticket_in_the_checkout_reads_as_before(tmp_path):
    from tools.contest import cli as contest_cli
    tasks = tmp_path / "epic-tasks"
    _write(tasks / "07-x.md", _ticket())
    assert contest_cli.ticket_file(tmp_path, tasks, 7, None) == ("07-x.md", tasks / "07-x.md")
    assert contest_cli.ticket_file(tmp_path, tasks, 8, None) == ("", None)


# 7, 8 ─ the lock and the exit mapping
LOCK_STUB = """
from pathlib import Path
Path("seen-lock").write_text(Path(".arena/locks/7.pid").read_text())
raise SystemExit({code})
"""


@pytest.mark.parametrize("code", [0, 1])
def test_the_lock_exists_while_the_child_runs_and_is_gone_after(repo, monkeypatch, code):
    _draft(repo, _ticket())
    spawn = _stub(LOCK_STUB.format(code=code))
    monkeypatch.setattr(rounds, "SPAWN", spawn)
    assert cli.main(["run", "start", "7"]) == code
    assert (repo / "seen-lock").read_text().strip().isdigit()
    assert not (repo / ".arena" / "locks" / "7.pid").exists()
    record = json.loads((repo / ".arena" / "rounds" / "7.json").read_text())
    assert record["base_ref"] == "arena-round/7" and record["branch"] == "main"
    assert set(record) == {"branch", "base_ref", "base_sha", "ticket_sha256", "started_at"}
    assert spawn.seen[0][:8] == [sys.executable, "-m", "tools.contest", "run",
                                 "--ticket", "7", "--base", "arena-round/7"]


@pytest.mark.parametrize("script,expected", [
    ("from pathlib import Path\np = Path('contest-out/07'); p.mkdir(parents=True)\n"
     "(p / 'state.json').write_text('{}')\nraise SystemExit(2)", 4),
    ("raise SystemExit(2)", 1),
    ("raise SystemExit(1)", 1),
    ("raise SystemExit(0)", 0),
    ("raise SystemExit(9)", 1),
])
def test_the_childs_exit_code_maps(repo, monkeypatch, script, expected):
    _draft(repo, _ticket())
    monkeypatch.setattr(rounds, "SPAWN", _stub(script))
    assert cli.main(["run", "start", "7"]) == expected


# 9, 10 ─ round_alive on a fake /proc
def _proc(root: Path, pid: int, argv: list[str], cwd: Path) -> None:
    folder = root / str(pid)
    folder.mkdir(parents=True)
    (folder / "cmdline").write_bytes(b"\0".join(w.encode() for w in argv) + b"\0")
    os.symlink(cwd, folder / "cwd")


def test_round_alive_on_a_fake_proc(repo, tmp_path):
    proc = tmp_path / "proc"
    other = tmp_path / "other"
    other.mkdir()
    _proc(proc, 10, ["python3", "-m", "tools.contest", "run", "--ticket", "7"], repo)
    assert rounds.round_alive(repo, 7, str(proc))
    assert not rounds.round_alive(repo, 8, str(proc))

    proc2 = tmp_path / "proc2"
    _proc(proc2, 10, ["python3", "-m", "tools.contest", "run", "--ticket", "7"], other)
    assert not rounds.round_alive(repo, 7, str(proc2))

    proc3 = tmp_path / "proc3"
    _proc(proc3, 11, ["python3", "-m", "tools.contest", "run", "--ticket=07"], repo)
    assert rounds.round_alive(repo, 7, str(proc3))

    proc4 = tmp_path / "proc4"
    _proc(proc4, 12, ["vim", "notes"], repo)
    _write(repo / ".arena" / "locks" / "7.pid", "12\n")
    assert not rounds.round_alive(repo, 7, str(proc4))


def test_a_live_round_is_refused_before_any_ref(repo, tmp_path, monkeypatch, capsys):
    proc = tmp_path / "proc"
    _proc(proc, 10, ["python3", "-m", "tools.contest", "run", "--ticket", "7"], repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(proc))
    _draft(repo, _ticket())
    assert cli.main(["run", "start", "7"]) == 2
    assert "already running" in capsys.readouterr().err
    assert _ref(repo) is None


# 11 ─ the ticket search order and the branch
def test_drafts_beat_epic_tasks_and_rejected_is_ignored(repo):
    _write(repo / "epic-tasks" / "07-x.md", _ticket(extra="checkout"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "t")
    _draft(repo, _ticket(extra="draft"))
    _draft(repo, _ticket(extra="rejected"), "07-y.rejected.md")
    name, text = rounds.find_ticket(repo, 7, "main")
    assert name == "07-x.md" and "draft" in text


def test_a_ticket_only_on_the_branch_is_found(repo):
    _write(repo / "epic-tasks" / "07-x.md", _ticket(extra="branch"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "t")
    (repo / "epic-tasks" / "07-x.md").unlink()
    _git(repo, "update-index", "--skip-worktree", "epic-tasks/07-x.md")
    name, text = rounds.find_ticket(repo, 7, "main")
    assert name == "07-x.md" and text.endswith("body branch\n")


def test_two_drafts_are_refused_listing_both(repo, capsys):
    _draft(repo, _ticket())
    _draft(repo, _ticket(), "7-y.md")
    assert cli.main(["run", "start", "7"]) == 2
    err = capsys.readouterr().err
    assert "07-x.md" in err and "7-y.md" in err


def test_no_ticket_is_refused(repo, capsys):
    assert cli.main(["run", "start", "7"]) == 2
    assert "no ticket 7" in capsys.readouterr().err


def test_a_detached_head_without_branch_is_refused(repo, capsys):
    _draft(repo, _ticket())
    _git(repo, "checkout", "-q", "--detach")
    assert cli.main(["run", "start", "7"]) == 2
    assert "detached" in capsys.readouterr().err
    assert _ref(repo) is None


def test_an_unknown_branch_is_refused(repo, capsys):
    _draft(repo, _ticket())
    assert cli.main(["run", "start", "7", "--branch", "nope"]) == 2
    assert "nope" in capsys.readouterr().err


def test_repo_root_is_the_checkout_unpatched():
    import importlib
    fresh = importlib.reload(cli)
    assert (fresh.REPO_ROOT / "tools" / "arena" / "rounds.py").is_file()


def test_a_profile_extra_out_is_refused_before_the_runner_line_is_built():
    """172: `build_run_line` never lets a profile's `--out` reach the runner."""
    from tools.arena.profile import ProfileError
    with pytest.raises(ProfileError, match="--out is set by arena"):
        rounds.build_run_line(3, {"extra": "--out /tmp/elsewhere"}, [])
