"""tests_bugfix/test_arena_issue_edit_concurrent_209.py — bugs 42, 43: `issue edit` loses a change made under $EDITOR; a plumbing refusal crashes.

Bug 42: `arena issue edit NN` read the ticket before `$EDITOR`, but the plumbed
writer took its compare-and-swap base (`rev-parse refs/heads/B`) after the editor
closed — a commit that changed the ticket meanwhile was the base, and the editor's
text (made from the old blob) was committed over it. The checked-out path
(`git commit --only -- <rel>`) did the same over a commit on the checked-out
branch. The editor here is a script that makes the competing change mid-edit.

Bug 43: for a `GitRefError` that was not a moved branch, the hints held a list
inside the list, and the refusal printer's `.get` on it was an `AttributeError`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.arena import cli, rounds, tickets
from tools.arena.gitref import GitRefError

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

INI = "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = test/a\n"

NN = 148
REL = f"epic-tasks/{NN}-ticket.md"
TICKET = f"""# AR-{NN} — the work

**Status:** open
**Severity:** 2
**File:** pkg/x.py
**Symbol:** x
**Round:** {NN}
**Size:** M
**Also touches:** -

body of {NN}
"""
MINE = "\nedited by me\n"
THEIRS = "\nother change\n"

# The $EDITOR: first the competing change ($COMPETE), then the operator's own edit.
#   commit — append THEIRS to $COMPETE_REL in the checkout and commit it there
#   write  — append THEIRS to $COMPETE_REL in the checkout, nothing committed
#   plumb  — one commit on $COMPETE_BRANCH that appends THEIRS to $COMPETE_REL
#            (a temporary index: the branch is not checked out anywhere)
EDITOR = f"""#!{sys.executable}
import os, subprocess, sys, tempfile
path, mode = sys.argv[1], os.environ.get("COMPETE", "")
rel, branch = os.environ.get("COMPETE_REL", ""), os.environ.get("COMPETE_BRANCH", "")
THEIRS = {THEIRS!r}

def g(*args, env=None, stdin=None):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True,
                          env=env, input=stdin).stdout.strip()

if mode in ("commit", "write"):
    with open(rel, "a", encoding="utf-8") as fh:
        fh.write(THEIRS)
    if mode == "commit":
        g("add", "--", rel)
        g("commit", "-q", "-m", "competing change", "--", rel)
elif mode == "plumb":
    tip = "refs/heads/" + branch
    with tempfile.TemporaryDirectory() as tmp:
        env = {{**os.environ, "GIT_INDEX_FILE": os.path.join(tmp, "index")}}
        g("read-tree", tip, env=env)
        try:
            old = subprocess.run(["git", "show", tip + ":" + rel], check=True,
                                 capture_output=True, text=True).stdout
        except subprocess.CalledProcessError:
            old = ""
        blob = g("hash-object", "-w", "--stdin", env=env, stdin=old + THEIRS)
        g("update-index", "--add", "--cacheinfo", "100644," + blob + "," + rel, env=env)
        tree = g("write-tree", env=env)
        sha = g("commit-tree", tree, "-p", tip, "-m", "competing change", env=env)
        g("update-ref", tip, sha)
with open(path, "a", encoding="utf-8") as fh:
    fh.write({MINE!r})
"""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _show(repo: Path, branch: str, rel: str = REL) -> str:
    return _git(repo, "show", f"refs/heads/{branch}:{rel}")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """On branch `arena` with ticket 148 open; `ctxfix` is a second, not checked
    out branch at the same commit; the competing editor is $EDITOR."""
    r = tmp_path / "repo"
    (r / "epic-tasks").mkdir(parents=True)
    _git(r, "init", "-q", "-b", "arena")
    (r / "contest.ini").write_text(INI, encoding="utf-8")
    (r / ".gitignore").write_text(".arena/\nout/\n", encoding="utf-8")
    (r / REL).write_text(TICKET, encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "tickets")
    _git(r, "branch", "ctxfix")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, ENV[key])
    monkeypatch.setattr(cli, "REPO_ROOT", r)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    editor = tmp_path / "ed.py"
    editor.write_text(EDITOR, encoding="utf-8")
    editor.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("COMPETE", raising=False)
    return r


def _compete(monkeypatch, mode: str, branch: str = "arena", rel: str = REL) -> None:
    monkeypatch.setenv("COMPETE", mode)
    monkeypatch.setenv("COMPETE_BRANCH", branch)
    monkeypatch.setenv("COMPETE_REL", rel)


def _run(capsys, *argv: str) -> tuple:
    code = cli.main(["-y", *argv])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def _hints(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith("  → ")]


def _kept(err: str) -> Path:
    """The path the `the edited text, kept here` hint names."""
    line = next(h for h in _hints(err) if "the edited text, kept here" in h)
    return Path(line.split(":  ", 1)[1].strip())


def _assert_refused_and_kept(code: int, out: str, err: str, branch: str) -> None:
    """Exit 2, the usual `commit` refusal, `git log -3` first, the edit kept."""
    assert code == 2 and out == "", (code, out, err)
    fails = [line for line in err.splitlines() if "[✗]" in line]
    assert len(fails) == 1 and "[✗] commit" in fails[0], err
    hints = _hints(err)
    assert f"git log -3 --oneline {branch}" in hints[0], hints
    kept = _kept(err)
    assert kept.read_text(encoding="utf-8").endswith(MINE)
    assert "Traceback" not in err


# 42 ─ the ticket changed while the editor was open
def test_a_commit_on_the_checked_out_branch_mid_edit_is_not_overwritten(repo, capsys,
                                                                         monkeypatch):
    _compete(monkeypatch, "commit")
    code, out, err = _run(capsys, "issue", "edit", str(NN))
    _assert_refused_and_kept(code, out, err, "arena")
    on_branch = _show(repo, "arena")
    assert on_branch.endswith(THEIRS.strip()) and MINE.strip() not in on_branch
    assert _git(repo, "log", "-1", "--format=%s") == "competing change"
    assert (repo / REL).read_text(encoding="utf-8") == TICKET + THEIRS


def test_a_write_to_the_checked_out_file_mid_edit_is_not_overwritten(repo, capsys,
                                                                     monkeypatch):
    _compete(monkeypatch, "write")
    code, out, err = _run(capsys, "issue", "edit", str(NN))
    assert code == 2 and out == "", err
    assert _kept(err).read_text(encoding="utf-8").endswith(MINE)
    assert (repo / REL).read_text(encoding="utf-8") == TICKET + THEIRS
    assert _show(repo, "arena") == TICKET.strip()


def test_a_commit_on_another_branch_mid_edit_is_not_overwritten(repo, capsys, monkeypatch):
    _compete(monkeypatch, "plumb", branch="ctxfix")
    head = _git(repo, "rev-parse", "HEAD")
    code, out, err = _run(capsys, "issue", "edit", str(NN), "--branch", "ctxfix")
    _assert_refused_and_kept(code, out, err, "ctxfix")
    on_branch = _show(repo, "ctxfix")
    assert on_branch.endswith(THEIRS.strip()) and MINE.strip() not in on_branch
    assert _git(repo, "log", "-1", "--format=%s", "ctxfix") == "competing change"
    assert _git(repo, "rev-parse", "HEAD") == head


def test_the_plumbed_refusal_in_json_keeps_the_edit(repo, capsys, monkeypatch):
    _compete(monkeypatch, "plumb", branch="ctxfix")
    code, out, err = _run(capsys, "-o", "json", "issue", "edit", str(NN), "--branch", "ctxfix")
    assert code == 2 and out == "", (code, out, err)
    data = json.loads(err.strip().splitlines()[-1])
    hints = data["hints"]
    assert all(isinstance(h, dict) and h["why"] and h["command"] for h in hints), hints
    assert hints[0]["command"] == "git log -3 --oneline ctxfix"
    kept = [h["command"] for h in hints if h["why"] == "the edited text, kept here"]
    assert kept and Path(kept[0]).read_text(encoding="utf-8").endswith(MINE)


def test_a_branch_moved_for_another_file_does_not_stop_the_edit_plumbed(repo, capsys,
                                                                       monkeypatch):
    _compete(monkeypatch, "plumb", branch="ctxfix", rel="notes.txt")
    code, out, err = _run(capsys, "issue", "edit", str(NN), "--branch", "ctxfix")
    assert code == rounds.EXIT_OK, err
    assert _show(repo, "ctxfix") == (TICKET + MINE).strip()
    assert _show(repo, "ctxfix", "notes.txt") == THEIRS.strip()
    assert _git(repo, "log", "-1", "--format=%s", "ctxfix") == f"{NN}: ticket edited"


def test_a_branch_moved_for_another_file_does_not_stop_the_edit_checked_out(repo, capsys,
                                                                           monkeypatch):
    (repo / "notes.txt").write_text("notes\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-q", "-m", "notes")
    _compete(monkeypatch, "commit", rel="notes.txt")
    code, out, err = _run(capsys, "issue", "edit", str(NN))
    assert code == rounds.EXIT_OK, err
    assert _show(repo, "arena") == (TICKET + MINE).strip()
    assert _show(repo, "arena", "notes.txt") == ("notes\n" + THEIRS).strip()


@pytest.mark.parametrize("branch", ["arena", "ctxfix"])
def test_an_untouched_ticket_is_still_edited(repo, capsys, branch):
    code, out, err = _run(capsys, "issue", "edit", str(NN), "--branch", branch)
    assert code == rounds.EXIT_OK, err
    assert f"edited on {branch}" in out
    assert _show(repo, branch) == (TICKET + MINE).strip()


# 43 ─ a plumbing refusal that is not a moved branch
REFUSALS = ["git read-tree: cannot read the index",
            "git commit-tree: Author identity unknown",
            "branch 'x' does not exist"]


def _inject(monkeypatch, message: str) -> None:
    def refuse(*_a, **_kw):
        raise GitRefError(message)
    monkeypatch.setattr(tickets, "_commit_plumbed", refuse)


@pytest.mark.parametrize("message", REFUSALS)
def test_a_plumbing_refusal_is_a_refusal_not_a_traceback(repo, capsys, monkeypatch, message):
    _inject(monkeypatch, message)
    code, out, err = _run(capsys, "issue", "edit", str(NN), "--branch", "ctxfix")
    assert code == 2 and out == "", (code, out, err)
    assert message in err.splitlines()[0]
    hints = _hints(err)
    assert len(hints) == 3, hints
    for hint in hints:
        why, _, command = hint[len("  → "):].partition(":  ")
        assert why.strip() and command.strip(), hint
    assert "git -C" in hints[0] and "status" in hints[0]
    assert _kept(err).read_text(encoding="utf-8").endswith(MINE)


@pytest.mark.parametrize("message", REFUSALS)
def test_a_plumbing_refusal_in_json_has_three_why_command_hints(repo, capsys, monkeypatch,
                                                                message):
    _inject(monkeypatch, message)
    code, out, err = _run(capsys, "-o", "json", "issue", "edit", str(NN), "--branch", "ctxfix")
    assert code == 2 and out == "", (code, out, err)
    lines = err.strip().splitlines()
    assert len(lines) == 1, lines
    hints = json.loads(lines[0])["hints"]
    assert len(hints) == 3, hints
    assert all(isinstance(h, dict) and set(h) >= {"why", "command"} and h["command"]
               for h in hints), hints
