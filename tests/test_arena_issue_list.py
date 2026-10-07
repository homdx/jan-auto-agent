"""tests/test_arena_issue_list.py — AR-6: `arena issue list` / `issue view`, the computed ticket state."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tools.arena import cli, rounds

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

INI = """[contest]
out_dir = out

[contest.agent.a]
model = test/a
"""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _md(title: str, status: str) -> str:
    return f"# {title}\n\n**Status:** {status}\n\nbody\n"


def _tracked(repo: Path, name: str, title: str, status: str) -> None:
    _write(repo / "epic-tasks" / name, _md(title, status))


def _commit(repo: Path, subject: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", subject)


def _round(repo: Path, folder: str) -> None:
    _write(repo / "out" / folder / "state.json", json.dumps({"agents": []}))


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "arena")
    _write(repo / "contest.ini", INI)
    _commit(repo, "init")
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return repo


def _run(capsys, *argv):
    code = cli.main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def _states(capsys, *extra) -> dict[int, str]:
    code, out, _ = _run(capsys, "-o", "json", "issue", "list", *extra)
    assert code == 0
    return {row["number"]: row["state"] for row in json.loads(out)}


def test_one_ticket_per_state(repo, tmp_path, monkeypatch, capsys):
    _write(repo / ".arena" / "drafts" / "01-draft.md", _md("D", "open"))
    _tracked(repo, "02-open.md", "O", "open")
    _tracked(repo, "03-queued.md", "Q", "queued")
    _tracked(repo, "04-landed.md", "L", "landed")
    _tracked(repo, "05-closed.md", "C", "closed")
    _tracked(repo, "06-done.md", "Done", "open")
    _tracked(repo, "07-running.md", "R", "open")
    _tracked(repo, "08-leg.md", "Leg", "open")
    _commit(repo, "04: landed work")
    _round(repo, "06")
    _round(repo, "07")
    _round(repo, "08.2")
    proc = tmp_path / "proc" / "99"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"\0".join(
        [b"python3", b"-m", b"tools.contest", b"run", b"--ticket", b"07"]))
    os.symlink(repo, proc / "cwd")
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "proc"))
    assert _states(capsys) == {1: "draft", 2: "open", 3: "queued", 4: "landed", 5: "closed",
                               6: "done", 7: "running", 8: "done"}


def test_a_ticket_only_on_the_branch_is_listed_with_where_branch(repo, capsys):
    _tracked(repo, "09-onbranch.md", "B", "open")
    _commit(repo, "add 9")
    _git(repo, "checkout", "-q", "-b", "work")
    _git(repo, "rm", "-q", "epic-tasks/09-onbranch.md")
    _commit(repo, "drop 9 in the checkout")
    code, out, _ = _run(capsys, "-o", "json", "issue", "list", "--branch", "arena")
    row = json.loads(out)[0]
    assert code == 0 and row["number"] == 9
    assert row["where"] == "branch" and row["path"] == "arena:epic-tasks/09-onbranch.md"


def test_each_flag(repo, capsys):
    _tracked(repo, "10-a.md", "A", "open")
    _commit(repo, "10: the work")
    _tracked(repo, "11-b.md", "B", "landed")
    _write(repo / ".arena" / "drafts" / "12-c.md", _md("C", "open"))
    _tracked(repo, "12-c.md", "C", "open")
    _tracked(repo, "13-d.md", "D", "open")
    _tracked(repo, "13-e.md", "E", "open")
    _commit(repo, "files")
    code, out, _ = _run(capsys, "-o", "json", "issue", "list")
    flags = {row["number"]: row["flags"] for row in json.loads(out)}
    assert code == 0
    assert flags[10] == ["commit 10: on arena but status open"]
    assert flags[11] == ["status landed but no 11: commit on arena"]
    assert flags[12] == ["draft and epic-tasks both hold 12"]
    assert flags[13] == ["two files for 13: 13-d.md, 13-e.md"]


def test_subject_numbers_reads_one_ticket_or_a_list():
    """176: `151, 152: …` names two tickets; the single forms are unchanged."""
    from tools.arena.tickets import subject_numbers
    assert subject_numbers("151, 152: tickets for the tap reconnect") == {151, 152}
    assert subject_numbers("7,08,9: x") == {7, 8, 9}
    assert subject_numbers("144: x") == {144}
    assert subject_numbers("0144: x") == {144}
    assert subject_numbers("144 — x") == {144}
    assert subject_numbers("RUN-3: x") == set()
    assert subject_numbers("151, 152 tickets") == set()


def test_a_list_subject_clears_the_landed_without_commit_flag(repo, capsys):
    _tracked(repo, "20-a.md", "A", "landed")
    _tracked(repo, "21-b.md", "B", "landed")
    _commit(repo, "20, 21: both tickets")
    code, out, _ = _run(capsys, "-o", "json", "issue", "list")
    assert code == 0
    assert {row["number"]: row["flags"] for row in json.loads(out)} == {20: [], 21: []}


def test_a_file_named_like_the_branch_does_not_make_git_log_ambiguous(repo, capsys):
    # This repo's own `./arena` script sits next to branch `arena`: a bare
    # `git log arena` was "ambiguous argument" and `issue list` refused.
    _write(repo / "arena", "#!/bin/sh\n")
    _tracked(repo, "14-x.md", "X", "open")
    _commit(repo, "14: the work")
    code, out, err = _run(capsys, "-o", "json", "issue", "list")
    assert code == 0, err
    assert json.loads(out)[0]["flags"] == ["commit 14: on arena but status open"]


def test_state_filter(repo, capsys):
    _tracked(repo, "02-open.md", "O", "open")
    _tracked(repo, "03-queued.md", "Q", "queued")
    _commit(repo, "tickets")
    assert _states(capsys, "--state", "open") == {2: "open"}
    code, _, err = _run(capsys, "issue", "list", "--state", "nosuch")
    assert code == 2 and len(err.splitlines()) == 1
    code, _, err = _run(capsys, "issue", "list", "--state", "landed")
    assert code == 3 and len(err.splitlines()) == 1


def test_no_tickets_at_all_is_exit_3(repo, capsys):
    code, out, err = _run(capsys, "issue", "list")
    assert code == 3 and out == "" and len(err.splitlines()) == 1


def test_json_flags_is_a_list_and_the_table_marks_it(repo, capsys):
    _tracked(repo, "11-b.md", "B", "landed")
    _commit(repo, "tickets")
    code, out, _ = _run(capsys, "-o", "json", "issue", "list")
    rows = json.loads(out)
    assert code == 0 and isinstance(rows[0]["flags"], list) and rows[0]["flags"]
    code, out, _ = _run(capsys, "issue", "list")
    lines = out.splitlines()
    assert lines[0].split() == ["NN", "STATE", "TITLE", "!"] and lines[1].endswith("!")


def test_view_prints_the_text_and_the_flag_lines(repo, capsys):
    _tracked(repo, "11-b.md", "B", "landed")
    _commit(repo, "tickets")
    code, out, _ = _run(capsys, "issue", "view", "11")
    assert code == 0
    assert out == _md("B", "landed") + "! status landed but no 11: commit on arena\n"
    code, out, _ = _run(capsys, "-o", "json", "issue", "view", "11")
    data = json.loads(out)
    assert data["text"] == _md("B", "landed") and data["number"] == 11


def test_view_refusals(repo, capsys):
    code, _, err = _run(capsys, "issue", "view", "77")
    assert code == 1 and len(err.splitlines()) == 1
    code, _, err = _run(capsys, "issue", "view", "abc")
    assert code == 2 and len(err.splitlines()) == 1


def test_land_is_implemented(capsys):
    # `issue create` landed in round 145 (AR-7); `issue land` is AR-8, and it has
    # a handler now — a missing NN is a usage error, not the placeholder's message.
    code, _, err = _run(capsys, "issue", "land")
    assert code == 2 and "NN" in err and "not implemented" not in err
    code, out, _ = _run(capsys, "issue", "land", "--help")
    assert code == 0 and "--score" in out and "--note" in out and "NN" in out


def _snapshot(repo: Path):
    files = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*")
                   if ".git" not in p.relative_to(repo).parts)
    return files, _git(repo, "for-each-ref")


def test_nothing_in_the_repo_changes(repo, capsys):
    _tracked(repo, "02-open.md", "O", "open")
    _write(repo / ".arena" / "drafts" / "03-d.md", _md("D", "open"))
    _commit(repo, "tickets")
    before = _snapshot(repo)
    _run(capsys, "issue", "list")
    _run(capsys, "issue", "view", "2")
    _run(capsys, "issue", "view", "77")
    assert _snapshot(repo) == before
