"""tests_bugfix/test_arena_issue_edit_swap_209.py — bugs 42 and 43 (209), the Sonnet 5.5 entry's cases.

`arena issue edit` and a ticket that changed while the editor was open; kept beside
`test_arena_issue_edit_concurrent_209.py` (the Opus entry's) because each pins
something the other does not — this one the commit helper itself.

42 — the compare-and-swap base is the tip and the blob taken when the ticket was read, not
     the ones at the write; a competing change to the ticket is refused, the edited text kept.
43 — a plumbing failure that is not a moved branch is a refusal with three `{why, command}`
     hints, not an AttributeError.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tools.arena import cli, rounds, tickets
from tools.arena.gitref import GitRefError

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
NN = 148
REL = f"epic-tasks/{NN}-ticket-{NN}.md"
HEADER = """**Status:** open
**Severity:** 2
**File:** pkg/x.py
**Symbol:** x
**Round:** {nn}
**Size:** M
**Also touches:** -

"""

#: The editor: first the competing change (what a second terminal or a push would do while
#: the editor stays open), then the operator's own edit.
EDITOR = """#!/bin/sh
python3 - "$1" <<'PY'
import os, subprocess, sys

def git(*args, env=None):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()

kind = os.environ.get("C_KIND", "")
rel, text, branch = os.environ.get("C_REL", ""), os.environ.get("C_TEXT", ""), os.environ.get("C_BRANCH", "")
if kind == "checked-out":
    open(rel, "w", encoding="utf-8").write(text)
    git("add", rel)
    git("commit", "-q", "-m", "competing")
elif kind == "plumbed":
    env = {**os.environ, "GIT_INDEX_FILE": os.path.join(os.environ["C_TMP"], "idx")}
    git("read-tree", branch, env=env)
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], input=text, check=True,
                          capture_output=True, text=True).stdout.strip()
    git("update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}", env=env)
    tree = git("write-tree", env=env)
    commit = git("commit-tree", tree, "-p", branch, "-m", "competing")
    git("update-ref", "refs/heads/" + branch, commit)
open(sys.argv[1], "a", encoding="utf-8").write(os.environ["APPEND"])
PY
"""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _ticket_text(nn: int = NN) -> str:
    return f"# AR-{nn} — the work\n\n{HEADER.format(nn=nn)}body of {nn}\n"


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    r = tmp_path / "repo"
    (r / "epic-tasks").mkdir(parents=True)
    _git(r, "init", "-q", "-b", "arena")
    (r / "contest.ini").write_text("[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = test/a\n",
                                   encoding="utf-8")
    (r / ".gitignore").write_text(".arena/\nout/\n", encoding="utf-8")
    (r / REL).write_text(_ticket_text(), encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "tickets")
    _git(r, "branch", "ctxfix")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, ENV[key])
    script = tmp_path / "ed.sh"
    script.write_text(EDITOR, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    monkeypatch.setenv("C_TMP", str(tmp_path))
    monkeypatch.setenv("APPEND", "\nthe operator's edit\n")
    monkeypatch.setattr(cli, "REPO_ROOT", r)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return r


def _compete(monkeypatch, kind: str, branch: str, rel: str, text: str) -> None:
    monkeypatch.setenv("C_KIND", kind)
    monkeypatch.setenv("C_BRANCH", branch)
    monkeypatch.setenv("C_REL", rel)
    monkeypatch.setenv("C_TEXT", text)


def _edit(capsys, *extra: str):
    code = cli.main(["-y", "issue", "edit", str(NN), *extra])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def _on(repo: Path, branch: str, rel: str = REL) -> str:
    return _git(repo, "show", f"refs/heads/{branch}:{rel}")


def _kept(repo: Path) -> list[str]:
    return [p.read_text(encoding="utf-8") for p in (repo / ".arena" / "tmp").glob("*/*")
            if p.is_file()] + [p.read_text(encoding="utf-8")
                               for p in (repo / ".arena" / "tmp").glob("*") if p.is_file()]


def _hints(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith("  → ")]


COMPETING = _ticket_text() + "a competing change\n"


# ── 42 ───────────────────────────────────────────────────────────────────────
def test_a_change_to_the_checked_out_ticket_during_the_edit_is_not_overwritten(repo, capsys, monkeypatch):
    _compete(monkeypatch, "checked-out", "arena", REL, COMPETING)
    code, out, err = _edit(capsys)
    assert code == 2 and out == ""
    assert _on(repo, "arena") == COMPETING.rstrip("\n")
    assert (repo / REL).read_text(encoding="utf-8") == COMPETING
    assert any("the operator's edit" in text for text in _kept(repo))
    assert "[✗]" in err and "the commit" in err
    assert "git log -3 --oneline arena" in _hints(err)[0]
    assert "the edited text, kept here" in err


def test_a_change_to_the_ticket_on_another_branch_during_the_edit_is_not_overwritten(repo, capsys, monkeypatch):
    _compete(monkeypatch, "plumbed", "ctxfix", REL, COMPETING)
    tip = _git(repo, "rev-parse", "ctxfix")
    code, out, err = _edit(capsys, "--branch", "ctxfix")
    assert code == 2 and out == ""
    moved = _git(repo, "rev-parse", "ctxfix")
    assert moved != tip and _git(repo, "log", "-1", "--format=%s", "ctxfix") == "competing"
    assert _on(repo, "ctxfix") == COMPETING.rstrip("\n")
    assert any("the operator's edit" in text for text in _kept(repo))
    assert "git log -3 --oneline ctxfix" in _hints(err)[0]
    assert len(_hints(err)) == 3


def test_a_branch_that_moved_for_another_file_does_not_stop_the_plumbed_edit(repo, capsys, monkeypatch):
    _compete(monkeypatch, "plumbed", "ctxfix", "epic-tasks/999-other.md", "other\n")
    code, out, _ = _edit(capsys, "--branch", "ctxfix")
    assert code == 0 and out.startswith(f"{NN}: edited on ctxfix @ ")
    assert _on(repo, "ctxfix").endswith("the operator's edit")
    assert _on(repo, "ctxfix", "epic-tasks/999-other.md") == "other"       # the other commit kept
    assert _git(repo, "log", "--format=%s", "ctxfix", "-3").splitlines()[:2] == [
        f"{NN}: ticket edited", "competing"]


def test_a_branch_that_moved_for_another_file_does_not_stop_the_checked_out_edit(repo, capsys, monkeypatch):
    _compete(monkeypatch, "checked-out", "arena", "epic-tasks/999-other.md", "other\n")
    code, out, _ = _edit(capsys)
    assert code == 0 and out.startswith(f"{NN}: edited on arena @ ")
    assert _on(repo, "arena").endswith("the operator's edit")
    assert (repo / "epic-tasks/999-other.md").read_text(encoding="utf-8") == "other\n"


@pytest.mark.parametrize("extra", [[], ["--branch", "ctxfix"]])
def test_an_untouched_ticket_is_still_edited(repo, capsys, monkeypatch, extra):
    code, out, _ = _edit(capsys, *extra)
    assert code == 0 and "edited on" in out
    assert _on(repo, extra[1] if extra else "arena").endswith("the operator's edit")


def test_the_commit_helper_with_no_base_keeps_its_old_swap(repo):
    sha = tickets._commit_plumbed(repo, "ctxfix", REL, COMPETING, "x")
    assert _git(repo, "rev-parse", "ctxfix") == sha


def test_the_commit_helper_refuses_a_changed_blob_and_names_the_move(repo):
    base_sha = _git(repo, "rev-parse", "ctxfix")
    base_blob = _git(repo, "rev-parse", f"{base_sha}:{REL}")
    tickets._commit_plumbed(repo, "ctxfix", REL, COMPETING, "competing")
    with pytest.raises(GitRefError, match="moved from"):
        tickets._commit_plumbed(repo, "ctxfix", REL, "mine\n", "mine", base_sha, base_blob)


# ── 43 ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("message", [
    "git read-tree: cannot read the index",
    "git commit-tree: Author identity unknown",
    "branch 'ctxfix' does not exist",
    "git update-ref refs/heads/ctxfix: the branch moved from aaaaaaa to bbbbbbb — another arena call committed first",
])
def test_a_plumbing_failure_is_a_refusal_with_three_pair_hints(repo, capsys, monkeypatch, message):
    def boom(*a, **k):
        raise GitRefError(message)
    monkeypatch.setattr(tickets, "_commit_plumbed", boom)
    code, out, err = _edit(capsys, "--branch", "ctxfix")
    assert code == 2 and out == "" and "Traceback" not in err and "AttributeError" not in err
    assert err.splitlines()[0] == f"arena: {message}"
    hints = _hints(err)
    assert len(hints) == 3 and all(":  " in h and h.split(":  ", 1)[1].strip() for h in hints)

    code, out, err = _edit_json(capsys, "--branch", "ctxfix")
    data = json.loads(err.splitlines()[0])
    assert code == 2 and data["error"] == message
    assert len(data["hints"]) == 3
    assert all(set(h) == {"why", "command"} and h["command"] for h in data["hints"])


def _edit_json(capsys, *extra: str):
    code = cli.main(["-y", "-o", "json", "issue", "edit", str(NN), *extra])
    cap = capsys.readouterr()
    return code, cap.out, cap.err
