"""Round 211 black box: written from ticket 211 alone, never from an entry's code.

Run from a checkout root:  python3 -m pytest contest-bench/211/acceptance_211.py -n0 -q
`arena run start NN --fresh`: explicit, one start only, never stored; the dirty
worktrees are listed; a backup of contest-out/NN; `fresh=yes` in a profile is no
longer silent; `arena model use --set KEY=VALUE`.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.arena import cli, models, rounds

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

TICKET = """# AR-9 — round 7

**Status:** draft
**File:** pkg/thing.py
**Symbol:** thing
**Size:** M

body
"""

INI = """[contest]
out_dir = contest-out
rounds_dir = rounds

[contest.agent.agent-a]
model = test/agent-a
"""

LOCAL = """[arena.profile.p1]
models = test/agent-a
max_parallel = 3
"""


def _git(cwd, *args):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=ENV)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    _write(r / "contest.ini", INI)
    _write(r / "contest.local.ini", LOCAL)
    _write(r / ".gitignore", ".arena/\ncontest-out/\nrounds/\ncontest.local.ini\n")
    _write(r / "epic-tasks" / "01-old.md", TICKET.replace("round 7", "round 1").replace("draft", "landed"))
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    _write(r / ".arena" / "drafts" / "07-x.md", TICKET)
    monkeypatch.setattr(cli, "REPO_ROOT", r)
    return r


@pytest.fixture
def spawn(monkeypatch):
    seen = []

    def fake(line, cwd):
        seen.append(list(line))
        return subprocess.Popen([sys.executable, "-c", "pass"], cwd=cwd)

    monkeypatch.setattr(rounds, "SPAWN", fake)
    return seen


def _dirty_worktree(repo, name="07-agent-a"):
    wt = repo / "rounds" / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
    (wt / "mine.txt").write_text("uncommitted work\n")
    (wt / "epic-tasks" / "01-old.md").write_text("changed\n")
    return wt


def _old_out(repo):
    out = repo / "contest-out" / "07"
    _write(out / "SUMMARY.md", "old results\n")
    return out


def _backups(repo):
    root = repo / "contest-out"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and p.name not in ("07", "7") and "p1" in p.name)


def _fresh_count(line):
    return sum(1 for w in line if w == "--fresh")


# (a) --fresh -y: runner gets --fresh exactly once, backup made, worktrees listed
def test_a_fresh_start_passes_fresh_once_and_backs_up(repo, spawn, capsys):
    _dirty_worktree(repo)
    _old_out(repo)
    assert cli.main(["-p", "p1", "-y", "run", "start", "7", "--fresh"]) == 0
    assert len(spawn) == 1 and _fresh_count(spawn[0]) == 1
    backups = _backups(repo)
    assert len(backups) == 1
    assert (backups[0] / "SUMMARY.md").read_text() == "old results\n"
    assert (repo / "contest-out" / "07" / "SUMMARY.md").exists(), "the original stays for the runner"
    out = capsys.readouterr().out
    assert "07-agent-a" in out, "the dirty worktree must be named before it is discarded"


def test_a_fresh_start_lists_the_dirty_file_counts(repo, spawn, capsys):
    _dirty_worktree(repo)
    assert cli.main(["-p", "p1", "-y", "run", "start", "7", "--fresh"]) == 0
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "07-agent-a" in l)
    assert "M" in line and "??" in line


def test_a_no_backup_flag_makes_no_copy(repo, spawn):
    _dirty_worktree(repo)
    _old_out(repo)
    assert cli.main(["-p", "p1", "-y", "run", "start", "7", "--fresh", "--no-backup"]) == 0
    assert _backups(repo) == []
    assert _fresh_count(spawn[0]) == 1


# (b) no terminal and no -y: refused, nothing touched
def test_b_without_yes_and_without_a_terminal_it_refuses(repo, spawn, monkeypatch, capsys):
    wt = _dirty_worktree(repo)
    _old_out(repo)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    code = cli.main(["-p", "p1", "run", "start", "7", "--fresh"])
    assert code != 0
    assert spawn == []
    assert _backups(repo) == []
    assert (wt / "mine.txt").exists()
    assert not (repo / ".arena" / "locks" / "7.pid").exists()


# (c) a clean start passes no --fresh
def test_c_a_plain_start_has_no_fresh(repo, spawn):
    assert cli.main(["-p", "p1", "-y", "run", "start", "7"]) == 0
    assert _fresh_count(spawn[0]) == 0


def test_c_a_plain_start_makes_no_backup(repo, spawn):
    _old_out(repo)
    assert cli.main(["-p", "p1", "-y", "run", "start", "7"]) == 0
    assert _backups(repo) == []


# (d) the profile file is never changed by --fresh
def test_d_the_profile_file_is_unchanged(repo, spawn):
    before = (repo / "contest.local.ini").read_bytes()
    _dirty_worktree(repo)
    assert cli.main(["-p", "p1", "-y", "run", "start", "7", "--fresh"]) == 0
    assert (repo / "contest.local.ini").read_bytes() == before


def test_d_a_second_plain_start_after_fresh_has_no_fresh(repo, spawn):
    assert cli.main(["-p", "p1", "-y", "run", "start", "7", "--fresh"]) == 0
    assert cli.main(["-p", "p1", "-y", "run", "start", "7", "--fresh-ticket"]) in (0, 2)
    for line in spawn[1:]:
        assert _fresh_count(line) == 0


# (e) fresh=yes in the profile: one warning line, the same confirmation
def test_e_profile_fresh_yes_is_no_longer_silent(repo, spawn, capsys):
    _write(repo / "contest.local.ini", LOCAL + "fresh = yes\n")
    _dirty_worktree(repo)
    assert cli.main(["-p", "p1", "-y", "run", "start", "7"]) == 0
    out = capsys.readouterr().out
    assert "fresh=yes" in out
    assert any("discard" in l.lower() for l in out.splitlines() if "fresh=yes" in l)
    assert _fresh_count(spawn[0]) == 1


def test_e_profile_fresh_yes_without_yes_and_without_a_terminal_refuses(repo, spawn, monkeypatch):
    _write(repo / "contest.local.ini", LOCAL + "fresh = yes\n")
    wt = _dirty_worktree(repo)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert cli.main(["-p", "p1", "run", "start", "7"]) != 0
    assert spawn == [] and (wt / "mine.txt").exists()


# (g) the backup never overwrites an existing folder
def test_g_two_fresh_starts_make_two_backups(repo, spawn):
    _old_out(repo)
    assert cli.main(["-p", "p1", "-y", "run", "start", "7", "--fresh"]) == 0
    first = _backups(repo)
    assert len(first) == 1
    (first[0] / "SUMMARY.md").write_text("marker from the first backup\n")
    assert cli.main(["-p", "p1", "-y", "run", "start", "7", "--fresh"]) == 0
    both = _backups(repo)
    assert len(both) == 2
    assert (first[0] / "SUMMARY.md").read_text() == "marker from the first backup\n"


def test_g_no_old_results_means_no_backup_and_no_error(repo, spawn):
    assert not (repo / "contest-out" / "07").exists()
    assert cli.main(["-p", "p1", "-y", "run", "start", "7", "--fresh"]) == 0
    assert _backups(repo) == []


# the runner's own flag stays out of the passthrough rules
def test_fresh_still_cannot_ride_the_passthrough_as_a_second_one(repo, spawn):
    cli.main(["-p", "p1", "-y", "run", "start", "7", "--fresh", "--", "--fresh"])
    for line in spawn:
        assert _fresh_count(line) <= 1


# (h) model use --set
@pytest.fixture
def kilo(monkeypatch):
    listing = {"kp": {"a:free": {"cost": {"input": 0, "output": 0},
                                 "capabilities": {"toolcall": True, "input": {"text": True},
                                                  "output": {"text": True}},
                                 "limit": {"context": 4000}}}}
    monkeypatch.setattr(models, "KILO_LIST",
                        lambda repo, providers: {p: m for p, m in listing.items()
                                                 if not providers or p in providers})


def test_h_model_use_set_writes_the_keys_in_one_call(repo, kilo, capsys):
    code = cli.main(["-p", "p2", "model", "use", "kp/a:free", "--set", "max_parallel=15",
                     "--set", "branch=arena", "-y"])
    assert code == 0
    text = (repo / "contest.local.ini").read_text()
    section = text.split("[arena.profile.p2]", 1)[1]
    assert "models" in section and "kp/a:free" in section
    assert "max_parallel" in section and "15" in section
    assert "branch" in section and "arena" in section


def test_h_model_use_set_is_repeatable_and_models_stays_refused(repo, kilo):
    code = cli.main(["-p", "p3", "model", "use", "kp/a:free", "--set", "models=other/x", "-y"])
    assert code != 0
    assert "other/x" not in (repo / "contest.local.ini").read_text()


def test_h_model_use_without_set_is_as_before(repo, kilo):
    assert cli.main(["-p", "p4", "model", "use", "kp/a:free", "-y"]) == 0
    section = (repo / "contest.local.ini").read_text().split("[arena.profile.p4]", 1)[1]
    assert "max_parallel" not in section
