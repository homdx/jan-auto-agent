"""tests_bugfix/test_arena_issue_edit_mid_edit_209.py — ticket 209, bugs 42 and 43 (the Sonnet 5 entry's cases).

`issue_edit` reads the ticket from the branch, opens `$EDITOR` (as long as the
operator keeps it open), then lints and commits the edited text. Bug 42: the
compare-and-swap base used to be taken *after* the editor closed, so a commit
that landed on the same ticket during the edit window was silently lost — the
new commit was built on the new tip with text based on the old one. Bug 43:
the refusal's `hints` ternary nested a list inside the list for any
`GitRefError` that was not a moved branch, and the refusal printer's `.get()`
crashed on it.

Every `$EDITOR` here is a fixed script, driven by a `COMPETE_CFG` JSON env var,
that — mid-edit — makes the competing change through
`tools.arena.gitref.commit_file_on`, the same plumbing `_commit_plumbed` itself
uses (imported by adding this project's root, `PROJECT_ROOT`, to the script's
own `sys.path`: its `cwd` is the throwaway test repo, not this project).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tools.arena import cli, rounds, tickets

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

INI = """[contest]
out_dir = out

[contest.agent.a]
model = test/a
"""

NN, OTHER = 148, 144

HEADER = """**Status:** {status}
**Severity:** 2
**File:** pkg/x.py
**Symbol:** x
**Round:** {nn}
**Size:** M
**Also touches:** -

"""

#: This project's own root — injected into every `$EDITOR` script's
#: `sys.path` so it can reuse `tools.arena.gitref.commit_file_on` to build the
#: competing commit the same way arena itself would.
PROJECT_ROOT = str(Path(__file__).resolve().parents[1])

# A competing commit, built the same way `_commit_plumbed` builds one: read
# the ref's current text for `COMPETE_CFG["rel"]`, append its "marker", commit
# it onto `COMPETE_CFG["ref"]` — then make our own edit to the temp file ($1),
# appending `COMPETE_CFG["append"]`.
COMPETE_THEN_EDIT = """#!/bin/sh
python3 - "$1" <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["PROJECT_ROOT"])
from pathlib import Path
from tools.arena import gitref

cfg = json.loads(os.environ["COMPETE_CFG"])
tmp_path = Path(sys.argv[1])
repo = Path(os.getcwd())
# `update-ref` wants a fully-qualified ref, not a short branch name — a bare
# "arena" would make a stray `.git/arena` file instead of moving the branch.
full_ref = f"refs/heads/{cfg['ref']}"
parent = gitref.git(repo, "rev-parse", full_ref)
old = gitref.git(repo, "show", f"{full_ref}:{cfg['rel']}", strip=False)
gitref.commit_file_on(repo, parent, cfg["rel"], old + cfg["marker"], "competing change",
                      full_ref)

tmp_path.write_text(tmp_path.read_text(encoding="utf-8") + cfg["append"], encoding="utf-8")
PY
"""

# Edits the temp file only — no competing commit — for the "an untouched
# branch is still edited fine" sanity checks.
APPEND = """#!/bin/sh
python3 - "$1" <<'PY'
import os, sys
open(sys.argv[1], "a", encoding="utf-8").write(os.environ["APPEND"])
PY
"""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _md(nn: int, status: str = "open", ar: str = "the work") -> str:
    head = HEADER.format(nn=nn, status=status)
    return f"# AR-{nn} — {ar}\n\n{head}body of {nn}\n"


def _ticket(repo: Path, nn: int, **kw) -> None:
    _write(repo / "epic-tasks" / f"{nn}-ticket-{nn}.md", _md(nn, **kw))


def _commit(repo: Path, subject: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", subject)


def _body(repo: Path, nn: int = NN) -> str:
    return (repo / "epic-tasks" / f"{nn}-ticket-{nn}.md").read_text(encoding="utf-8")


def _on_ref(repo: Path, ref: str, nn: int) -> str:
    return _git(repo, "show", f"{ref}:epic-tasks/{nn}-ticket-{nn}.md")


def _run(capsys, *argv: str) -> tuple:
    code = cli.main(["-y", *argv])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def _editor(tmp_path, monkeypatch, script_text: str, name: str = "ed.sh") -> Path:
    script = tmp_path / name
    script.write_text(script_text, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    monkeypatch.setenv("PROJECT_ROOT", PROJECT_ROOT)
    return script


def _compete(monkeypatch, *, ref: str, rel: str, marker: str, append: str = "\nmy edit\n") -> None:
    """Configures `COMPETE_THEN_EDIT` for one test: the competing write, then ours."""
    monkeypatch.setenv("COMPETE_CFG", json.dumps(
        {"ref": ref, "rel": rel, "marker": marker, "append": append}))


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """On branch `arena`: 144 and 148 open, clean, a sibling `ctxfix` branch."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "arena")
    _write(r / "contest.ini", INI)
    _write(r / ".gitignore", ".arena/\nout/\n")
    _ticket(r, OTHER, ar="other work")
    _ticket(r, NN)
    _commit(r, "tickets")
    _git(r, "branch", "ctxfix")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, ENV[key])
    monkeypatch.setattr(cli, "REPO_ROOT", r)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return r


# ── Bug 42: checked-out path ───────────────────────────────────────────────

def test_checked_out_edit_refuses_when_the_same_ticket_changed_mid_edit(repo, tmp_path,
                                                                        capsys, monkeypatch):
    """A competing commit to *this* ticket while `$EDITOR` is open is not overwritten."""
    _editor(tmp_path, monkeypatch, COMPETE_THEN_EDIT)
    _compete(monkeypatch, ref="arena", rel=f"epic-tasks/{NN}-ticket-{NN}.md",
             marker="\n-- race --\n")
    code, out, err = _run(capsys, "issue", "edit", str(NN))
    assert code == 2 and out == ""
    lines = err.splitlines()
    assert lines[0].startswith("arena: ")
    assert "while the editor was open" in lines[0]
    fails = [line for line in lines if "[✗]" in line]
    assert len(fails) == 1 and "the commit" in fails[0]
    hints = [line for line in lines if line.startswith("  → ")]
    assert hints, err
    assert "git log -3 --oneline arena" in hints[0]
    assert any("kept here" in h for h in hints)
    assert any("edit" in h and str(NN) in h for h in hints)
    # the competing change is still there — nothing was lost. The working
    # tree itself never saw it (the competing commit landed by plumbing,
    # which never touches the index or worktree of a checked-out branch),
    # but the branch's own tree — what a push or the next read sees — has it.
    assert _git(repo, "log", "-1", "--format=%s") == "competing change"
    assert "-- race --" in _on_ref(repo, "arena", NN)
    kept = [h for h in hints if "kept here" in h][0]
    kept_path = Path(kept.rsplit("  ", 1)[-1])
    assert kept_path.exists() and "my edit" in kept_path.read_text(encoding="utf-8")


def test_checked_out_edit_is_not_blocked_by_a_move_on_another_file(repo, tmp_path,
                                                                    capsys, monkeypatch):
    """A branch move during the edit that touches a *different* ticket does not refuse."""
    _editor(tmp_path, monkeypatch, COMPETE_THEN_EDIT)
    _compete(monkeypatch, ref="arena", rel=f"epic-tasks/{OTHER}-ticket-{OTHER}.md",
             marker="\n-- other --\n")
    code, out, err = _run(capsys, "issue", "edit", str(NN))
    assert code == rounds.EXIT_OK, err
    assert out.startswith(f"{NN}: edited on arena @ ")
    assert "my edit" in _body(repo, NN)
    assert "-- other --" in _on_ref(repo, "arena", OTHER)


def test_checked_out_edit_of_an_untouched_ticket_still_works(repo, tmp_path, capsys,
                                                              monkeypatch):
    _editor(tmp_path, monkeypatch, APPEND)
    monkeypatch.setenv("APPEND", "\nplain edit\n")
    code, out, _ = _run(capsys, "issue", "edit", str(NN))
    assert code == rounds.EXIT_OK and "plain edit" in _body(repo, NN)


# ── Bug 42: plumbed path (another branch) ──────────────────────────────────

def test_plumbed_edit_refuses_when_the_same_ticket_changed_mid_edit(repo, tmp_path,
                                                                    capsys, monkeypatch):
    """The same race on a branch that is not checked out — the plumbed writer."""
    _editor(tmp_path, monkeypatch, COMPETE_THEN_EDIT)
    _compete(monkeypatch, ref="ctxfix", rel=f"epic-tasks/{NN}-ticket-{NN}.md",
             marker="\n-- race --\n")
    head = _git(repo, "rev-parse", "arena")
    code, out, err = _run(capsys, "issue", "edit", str(NN), "--branch", "ctxfix")
    assert code == 2 and out == "" and _git(repo, "rev-parse", "arena") == head
    lines = err.splitlines()
    fails = [line for line in lines if "[✗]" in line]
    assert len(fails) == 1 and "the commit" in fails[0]
    hints = [line for line in lines if line.startswith("  → ")]
    assert "git log -3 --oneline ctxfix" in hints[0]
    assert any("kept here" in h for h in hints)
    assert "-- race --" in _on_ref(repo, "ctxfix", NN)
    assert "my edit" not in _on_ref(repo, "ctxfix", NN)


def test_plumbed_edit_is_not_blocked_by_a_move_on_another_file(repo, tmp_path,
                                                                capsys, monkeypatch):
    _editor(tmp_path, monkeypatch, COMPETE_THEN_EDIT)
    _compete(monkeypatch, ref="ctxfix", rel=f"epic-tasks/{OTHER}-ticket-{OTHER}.md",
             marker="\n-- other --\n")
    code, out, err = _run(capsys, "issue", "edit", str(NN), "--branch", "ctxfix")
    assert code == rounds.EXIT_OK, err
    assert out.startswith(f"{NN}: edited on ctxfix @ ")
    assert "my edit" in _on_ref(repo, "ctxfix", NN)
    assert "-- other --" in _on_ref(repo, "ctxfix", OTHER)


def test_plumbed_edit_of_an_untouched_ticket_still_works(repo, tmp_path, capsys,
                                                          monkeypatch):
    _editor(tmp_path, monkeypatch, APPEND)
    monkeypatch.setenv("APPEND", "\nplumbed plain\n")
    code, out, _ = _run(capsys, "issue", "edit", str(NN), "--branch", "ctxfix")
    assert code == rounds.EXIT_OK and "plumbed plain" in _on_ref(repo, "ctxfix", NN)


# ── Bug 43: a non-"moved branch" GitRefError does not crash ────────────────

@pytest.mark.parametrize("message", [
    "git read-tree: cannot read the index",
    "git commit-tree: Author identity unknown",
    "branch 'ctxfix' does not exist",
])
def test_other_plumbing_refusal_is_a_flat_hint_not_a_crash(repo, tmp_path, capsys,
                                                           monkeypatch, message):
    """Any `GitRefError` other than a moved branch used to nest a list inside
    `hints`, and `output.refuse_ctx`'s `.get()` crashed on it (bug 43)."""
    _editor(tmp_path, monkeypatch, APPEND)
    monkeypatch.setenv("APPEND", "\nwhatever\n")
    monkeypatch.setattr(
        tickets, "_commit_plumbed",
        lambda *a, **kw: (_ for _ in ()).throw(tickets.GitRefError(message)))
    code, out, err = _run(capsys, "issue", "edit", str(NN), "--branch", "ctxfix")
    assert code == 2 and out == ""
    lines = err.splitlines()
    assert lines[0] == f"arena: {message}"
    hints = [line for line in lines if line.startswith("  → ")]
    assert len(hints) == 3, err
    for hint in hints:
        assert ":  " in hint, hint          # `_hint_line`'s shape: why, command


@pytest.mark.parametrize("message", [
    "git read-tree: cannot read the index",
    "git commit-tree: Author identity unknown",
    "branch 'ctxfix' does not exist",
])
def test_other_plumbing_refusal_is_a_flat_hint_in_json_too(repo, tmp_path, capsys,
                                                           monkeypatch, message):
    _editor(tmp_path, monkeypatch, APPEND)
    monkeypatch.setenv("APPEND", "\nwhatever\n")
    monkeypatch.setattr(
        tickets, "_commit_plumbed",
        lambda *a, **kw: (_ for _ in ()).throw(tickets.GitRefError(message)))
    code = cli.main(["-y", "-o", "json", "issue", "edit", str(NN), "--branch", "ctxfix"])
    cap = capsys.readouterr()
    assert code == 2 and cap.out == ""
    data = json.loads(cap.err.splitlines()[0])
    assert data["error"] == message
    assert data["hints"] and all(isinstance(h, dict) for h in data["hints"])
    assert all(h["why"] and h["command"] for h in data["hints"])


def test_moved_branch_refusal_still_names_the_log_command_first(repo, tmp_path, capsys,
                                                                 monkeypatch):
    """The one case the old ternary already got right keeps working."""
    _editor(tmp_path, monkeypatch, APPEND)
    monkeypatch.setenv("APPEND", "\nwhatever\n")
    monkeypatch.setattr(
        tickets, "_commit_plumbed",
        lambda *a, **kw: (_ for _ in ()).throw(
            tickets.GitRefError("git update-ref refs/heads/ctxfix: the branch moved from "
                               "aaaaaaa to bbbbbbb — another arena call committed first")))
    code, out, err = _run(capsys, "issue", "edit", str(NN), "--branch", "ctxfix")
    assert code == 2
    hints = [line for line in err.splitlines() if line.startswith("  → ")]
    assert "git log -3 --oneline ctxfix" in hints[0]
