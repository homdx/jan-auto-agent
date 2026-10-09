"""211: `arena run start NN --fresh` is the explicit one-shot discard; `model use --set` makes a profile in one call."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.arena import cli, models, rounds

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

TICKET = "# AR-9 — round 7\n\n**Status:** draft\n**File:** pkg/thing.py\n**Symbol:** thing\n**Size:** M\n"
INI = "[contest]\nout_dir = contest-out\nrounds_dir = rounds\n\n[contest.agent.agent-a]\nmodel = test/agent-a\n"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    _write(repo / "contest.ini", INI)
    _write(repo / ".gitignore", ".arena/\ncontest-out/\nrounds/\n")
    _write(repo / "epic-tasks" / "01-old.md", "# AR-0 old\n\n**Status:** landed\n")
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _write(repo / ".arena" / "drafts" / "07-x.md", TICKET)
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    return repo


@pytest.fixture
def spawn(monkeypatch):
    seen: list = []

    def fake(line, cwd):
        seen.append(list(line))
        return subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"], cwd=cwd)

    monkeypatch.setattr(rounds, "SPAWN", fake)
    return seen


def _dirty_tree(repo: Path, name: str = "07-x") -> Path:
    tree = repo / "rounds" / name
    tree.mkdir(parents=True)
    _git(tree, "init", "-q")
    _write(tree / "a.txt", "a")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "a")
    _write(tree / "a.txt", "changed")
    _write(tree / "new.txt", "new")
    return tree


def _backups(repo: Path) -> list[Path]:
    return sorted((repo / "contest-out").glob("07.*"))


def test_fresh_yes_lists_backs_up_and_passes_fresh_once(repo, spawn, capsys):
    tree = _dirty_tree(repo)
    _write(repo / "contest-out" / "07" / "state.json", "{}")
    assert cli.main(["run", "start", "7", "--fresh", "-y"]) == 0
    assert [line.count("--fresh") for line in spawn] == [1]
    out = capsys.readouterr().out
    assert "rounds/07-x: M 1, ?? 1" in out
    assert len(_backups(repo)) == 1
    assert (_backups(repo)[0] / "state.json").read_text() == "{}"
    assert tree.exists()  # arena lists, the runner discards


def test_no_yes_and_no_terminal_refuses_and_touches_nothing(repo, spawn, capsys):
    _dirty_tree(repo)
    _write(repo / "contest-out" / "07" / "state.json", "{}")
    assert cli.main(["run", "start", "7", "--fresh"]) == 2
    assert spawn == []
    assert _backups(repo) == []
    assert not (repo / ".arena" / "rounds" / "7.json").exists()
    assert subprocess.run(["git", "rev-parse", "--verify", "-q", "arena-round/7"], cwd=str(repo),
                          capture_output=True).returncode != 0
    assert "discard" in capsys.readouterr().err


def test_a_clean_start_passes_no_fresh(repo, spawn):
    assert cli.main(["run", "start", "7"]) == 0
    assert "--fresh" not in spawn[0]


def test_the_profile_file_is_unchanged_by_a_fresh_start(repo, spawn):
    _write(repo / "contest.local.ini", "[arena.profile.p]\nmodels = a/b\n[arena]\nprofile = p\n")
    _dirty_tree(repo)
    before = (repo / "contest.local.ini").read_bytes(), (repo / "contest.ini").read_bytes()
    assert cli.main(["run", "start", "7", "--fresh", "-y"]) == 0
    assert ((repo / "contest.local.ini").read_bytes(), (repo / "contest.ini").read_bytes()) == before
    assert "--fresh" in spawn[0] and "-p" not in spawn[0]


def test_profile_fresh_yes_warns_and_asks(repo, spawn, capsys):
    _write(repo / "contest.local.ini",
           "[arena.profile.p]\nmodels = a/b\nfresh = yes\n[arena]\nprofile = p\n")
    _dirty_tree(repo)
    assert cli.main(["run", "start", "7"]) == 2          # asked, no terminal: no
    cap = capsys.readouterr()
    assert "profile p has fresh=yes: this start discards uncommitted work" in cap.out
    assert spawn == []
    assert cli.main(["run", "start", "7", "-y"]) == 0
    assert [line.count("--fresh") for line in spawn] == [1]


def test_the_refusal_over_old_worktrees_names_the_arena_command(repo, spawn, capsys):
    _dirty_tree(repo)
    assert cli.main(["run", "start", "7"]) == 2
    err = capsys.readouterr().err
    assert "arena run start 7 --fresh" in err and "arena run rerun 7" in err
    assert err.count("\n") == 1 and spawn == []


def test_a_backup_folder_never_overwrites_an_existing_one(repo):
    _write(repo / "contest-out" / "07" / "state.json", "{}")
    config = rounds.load_config(repo)
    first = rounds.backup_round_folder(repo, config, 7, "p")
    _write(first / "marker", "keep")
    second = rounds.backup_round_folder(repo, config, 7, "p")
    assert first != second and (first / "marker").read_text() == "keep"
    assert not (second / "marker").exists()
    assert rounds.backup_round_folder(repo, config, 8, "p") is None


def test_no_backup_skips_the_copy(repo, spawn):
    _write(repo / "contest-out" / "07" / "state.json", "{}")
    assert cli.main(["run", "start", "7", "--fresh", "--no-backup", "-y"]) == 0
    assert _backups(repo) == []


def test_model_use_set_writes_the_other_keys_in_the_same_call(repo, monkeypatch):
    monkeypatch.setattr(models, "KILO_LIST", lambda r, providers: {
        "p1": {"a:free": {"cost": {"input": 0, "output": 0}}}})
    assert cli.main(["model", "use", "p1/a:free", "-p", "q", "--set", "max_parallel=15",
                     "branch=arena", "-y"]) == 0
    text = (repo / "contest.local.ini").read_text()
    assert "models = p1/a:free" in text and "max_parallel = 15" in text and "branch = arena" in text
    assert cli.main(["model", "use", "p1/a:free", "-p", "q", "--set", "models=x", "-y"]) == 2
