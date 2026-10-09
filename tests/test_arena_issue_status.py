"""tests/test_arena_issue_status.py — AR-14: `issue queue|open|close|reopen|edit`, `closed`, intake."""

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

NN, OTHER, NEXT = 148, 144, 150

HEADER = """**Status:** {status}
**Severity:** 2
**File:** pkg/x.py
**Symbol:** x
**Round:** {nn}
**Size:** M
**Also touches:** -

"""

# One $EDITOR per assertion: a here-doc that never has to quote itself.
APPEND = """#!/bin/sh
python3 - "$1" <<'PY'
import os, sys
open(sys.argv[1], "a", encoding="utf-8").write(os.environ["APPEND"])
PY
"""

REPLACE = """#!/bin/sh
python3 - "$1" <<'PY'
import os, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
open(path, "w", encoding="utf-8").write(text.replace(os.environ["OLD"], os.environ["NEW"]))
PY
"""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _md(nn: int = NN, status: str | None = "open", closed: str = "", ar: str = "the work") -> str:
    """One ticket: a title, the header, and a body — *status* may be missing."""
    head = HEADER.format(nn=nn, status=status or "open")
    if status is None:
        head = head.split("\n", 1)[1]
    if closed:
        head = head.replace(f"**Status:** {status}\n",
                            f"**Status:** {status}\n**Closed:** {closed}\n")
    return f"# AR-{nn} — {ar}\n\n{head}body of {nn}\n"


def _ticket(repo: Path, nn: int = NN, **kw) -> None:
    _write(repo / "epic-tasks" / f"{nn}-ticket-{nn}.md", _md(nn, **kw))


def _commit(repo: Path, subject: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", subject)


def _round(repo: Path, folder: str) -> None:
    _write(repo / "out" / folder / "state.json", json.dumps({"agents": []}))


def _ref(repo: Path, spec: str) -> str:
    return _git(repo, "rev-parse", "--verify", "-q", spec)


def _body(repo: Path, nn: int = NN) -> str:
    return (repo / "epic-tasks" / f"{nn}-ticket-{nn}.md").read_text(encoding="utf-8")


def _branch(repo: Path, name: str = "arena") -> str:
    return _git(repo, "rev-parse", f"refs/heads/{name}")


def _on_branch(repo: Path, name: str, nn: int = NN) -> str:
    return _git(repo, "show", f"refs/heads/{name}:epic-tasks/{nn}-ticket-{nn}.md")


def _changed(repo: Path, a: str, b: str) -> list[str]:
    """The paths whose blob differs between the trees at *a* and *b*."""
    return _git(repo, "diff", "--name-only", a, b).splitlines()


def _run(capsys, *argv: str, yes: bool = True) -> tuple:
    """`cli.main(argv)` with `-y`: the verbs ask `apply? [y/N]` first, a prompt a test
    does not answer — `yes=False` is for the tests that do."""
    code = cli.main((["-y"] if yes else []) + list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def _refuse(capsys, *argv: str, want=(), step: str = "", nn=None, branch: str = "") -> list:
    """Assert *argv* refuses in the §7 shape and return its stderr lines.

    One `arena:` line, one failed step in `flow:`, no placeholder, the real NN
    and branch, and a first hint that is both a reason and a command.
    """
    code, out, err = _run(capsys, *argv)
    lines = err.splitlines()
    assert code == 2, err
    assert out == ""
    head = lines[0]
    assert head.startswith("arena: "), lines
    for fragment in want:
        assert fragment in head, (fragment, head)
    fails = [line for line in lines if "[✗]" in line]
    assert len(fails) == 1, lines
    assert fails[0].startswith("  flow:"), lines
    if step:
        assert step in fails[0], (step, fails)
    assert "<" not in err and ">" not in err, err
    if nn is not None:
        assert str(nn) in err, err
    if branch:
        assert branch in err, err
    hints = [line for line in lines if line.startswith("  → ")]
    assert hints, err
    assert ":  " in hints[0], hints[0]
    return lines


def _json_refuse(capsys, *argv: str, want=()) -> dict:
    code, out, err = _run(capsys, "-o", "json", *argv)
    assert code == 2 and out == "", (code, out, err)
    lines = err.splitlines()
    assert len(lines) == 1, lines          # one object a script can load, no text block
    data = json.loads(lines[0])
    head = data["error"]
    for fragment in want:
        assert fragment in head, (fragment, head)
    assert data["where"] and data["ticket"]
    assert [step["state"] for step in data["flow"]].count("fail") == 1
    assert data["hints"] and all(h["command"] for h in data["hints"])
    return data


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """On branch `arena`: 144 landed with a `144:` commit, 148 open, clean."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "arena")
    _write(r / "contest.ini", INI)
    _write(r / ".gitignore", ".arena/\nout/\n")
    _ticket(r, OTHER, status="landed", ar="done work")
    _ticket(r, NN)
    _commit(r, "tickets")
    _commit(r, f"{OTHER}: done work")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, ENV[key])
    monkeypatch.setattr(cli, "REPO_ROOT", r)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return r


def _alive(repo: Path, tmp_path, monkeypatch, nn: int = NN) -> None:
    """A `tools.contest run --ticket NN` process in *repo*: the round is running."""
    proc = tmp_path / "proc" / "99"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"\0".join(
        [b"python3", b"-m", b"tools.contest", b"run", b"--ticket", str(nn).encode()]))
    os.symlink(repo, proc / "cwd")
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "proc"))


def _editor(tmp_path, monkeypatch, script_text: str, name: str = "ed.sh") -> Path:
    """*script_text* as $EDITOR: one script per assertion, so no quoting in it."""
    script = tmp_path / name
    script.write_text(script_text, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    return script


# 1 ─ §5: what blocks a `run start`
def test_run_start_blocks_on_every_lower_open_ticket(repo, capsys):
    _ticket(repo, 145, ar="ctxfix")
    _ticket(repo, 147, ar="overflow")
    _commit(repo, "more tickets")
    code, out, err = _run(capsys, "run", "start", str(NN))
    assert code == rounds.EXIT_USAGE and out == ""
    blocks = [line for line in err.splitlines() if line.startswith("arena: ")]
    assert len(blocks) == 2, err
    assert "145" in blocks[0] and "147" in blocks[1], blocks
    assert err.count("flow:") == 2 and "[✗] intake" in err
    assert "build arena-round/148" in err
    assert "arena issue queue 145 --branch arena" in err
    assert "arena issue close 147 --branch arena" in err
    assert not (repo / "out" / str(NN)).exists()


def test_parking_the_blockers_gets_past_intake(repo, capsys, monkeypatch):
    _ticket(repo, 145, ar="ctxfix")
    _commit(repo, "more tickets")
    monkeypatch.setattr(
        rounds, "build_round_ref",
        lambda *a, **kw: (_ for _ in ()).throw(rounds.RoundError("intake cleared")))
    _refuse(capsys, "run", "start", str(NN), want=("AR-145 — ctxfix) is open on arena",), nn=145)
    assert _run(capsys, "issue", "queue", "145")[0] == rounds.EXIT_OK
    code, out, err = _run(capsys, "run", "start", str(NN))
    assert code == rounds.EXIT_USAGE and out == "" and err.strip() == "arena: intake cleared"


def test_a_higher_open_ticket_never_blocks(repo):
    _ticket(repo, NEXT, ar="later")
    _commit(repo, "more tickets")
    assert tickets.blocking_tickets(repo, "arena", NN) == []
    assert tickets.report_intake_blockers(repo, "arena", NN) is False


def test_queued_and_closed_are_not_blockers(repo, capsys):
    _ticket(repo, 145, ar="ctxfix")
    _commit(repo, "more tickets")
    assert _run(capsys, "issue", "queue", "145")[0] == rounds.EXIT_OK
    assert _run(capsys, "issue", "close", "145", "--reason", "moved to 149")[0] == rounds.EXIT_OK
    assert tickets.blocking_tickets(repo, "arena", NN) == []


def test_a_recorded_progress_entry_is_not_a_blocker(repo):
    _ticket(repo, 145, ar="ctxfix")
    _commit(repo, "more tickets")
    _write(repo / "epic-tasks" / "PROGRESS.csv", "ticket,outcome\n145-ticket-145.md,FIXED\n")
    assert tickets.blocking_tickets(repo, "arena", NN) == []


def test_a_progress_file_that_will_not_decode_is_read_as_empty(repo, capsys):
    _ticket(repo, 145, ar="ctxfix")
    _commit(repo, "more tickets")
    (repo / "epic-tasks" / "PROGRESS.csv").write_bytes(b"ticket,outcome\n145,\xff\xfe\n")
    assert [b["number"] for b in tickets.blocking_tickets(repo, "arena", NN)] == [145]
    _refuse(capsys, "run", "start", str(NN), want=("is open on arena",), nn=145)


def test_a_blocker_without_a_status_is_named_as_open(repo, capsys):
    _ticket(repo, 145, status=None, ar="ctxfix")
    _commit(repo, "more tickets")
    assert [b["status"] for b in tickets.blocking_tickets(repo, "arena", NN)] == [""]
    _refuse(capsys, "run", "start", str(NN), want=("is open on arena",), nn=145)


def test_an_unknown_status_word_is_a_blocker_that_names_the_word(repo, capsys):
    _ticket(repo, 145, status="fixed", ar="ctxfix")
    _commit(repo, "more tickets")
    assert [b["status"] for b in tickets.blocking_tickets(repo, "arena", NN)] == ["fixed"]
    lines = _refuse(capsys, "run", "start", str(NN), want=("is fixed on arena",), nn=145)
    assert "'fixed' is not open, queued, closed, landed" in " ".join(lines)


def test_the_blockers_are_reported_in_json(repo, capsys):
    _ticket(repo, 145, ar="ctxfix")
    _commit(repo, "more tickets")
    code, _, err = _run(capsys, "-o", "json", "run", "start", str(NN))
    assert code == rounds.EXIT_USAGE
    data = json.loads(err.splitlines()[-1])
    assert data["flow"][-1]["step"] == "start sessions"
    assert any("queue 145" in hint["command"] for hint in data["hints"])


# 2 ─ §6: the branch
def test_no_branch_on_a_detached_head_names_the_last_branch(repo, capsys):
    _git(repo, "checkout", "-q", "--detach", _branch(repo))
    lines = _refuse(capsys, "issue", "queue", str(NN), want=("HEAD is detached",), nn=NN,
                    step="branch resolved")
    assert f"arena issue queue {NN} --branch arena" in " ".join(lines)


def test_an_origin_only_branch_is_never_written(repo, capsys):
    origin = repo.parent / "origin.git"
    _git(repo, "init", "--bare", "-q", str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "arena")
    tip = _branch(repo, "arena")
    _git(repo, "checkout", "-q", "-b", "other")
    _git(repo, "branch", "-D", "arena")
    _refuse(capsys, "issue", "queue", str(NN), "--branch", "arena",
            want=("only on origin", "never written"), step="branch resolved")
    assert _ref(repo, "HEAD") == _branch(repo, "other")
    assert _ref(repo, "refs/remotes/origin/arena") == tip


def test_a_missing_branch_refuses_and_offers_a_close_match(repo, capsys):
    lines = _refuse(capsys, "issue", "queue", str(NN), "--branch", "arenax",
                    want=("does not exist",), step="branch resolved")
    assert f"arena issue queue {NN} --branch arena" in " ".join(lines)


def test_a_branch_checked_out_elsewhere_is_named(repo, capsys):
    other = repo.parent / "worktree"
    _git(repo, "worktree", "add", "--quiet", str(other), "-b", "ctxfix")
    _refuse(capsys, "issue", "queue", str(NN), "--branch", "ctxfix",
            want=("checked out in", "would go stale"), step="where B is checked out")
    assert _branch(repo, "ctxfix") == _branch(repo)


def test_a_branch_checked_out_elsewhere_points_at_its_own_checkout(repo, capsys):
    other = repo.parent / "worktree"
    _git(repo, "worktree", "add", "--quiet", str(other), "-b", "ctxfix")
    lines = _refuse(capsys, "issue", "queue", str(NN), "--branch", "ctxfix")
    text = " ".join(lines)
    assert str(other) in text and f"cd {other} && arena issue queue {NN}" in text


def test_the_worktree_read_failure_is_refused(repo, capsys, monkeypatch):
    monkeypatch.setattr(tickets, "_worktree_of",
                        lambda r, branch: (_ for _ in ()).throw(
                            tickets.GitRefError("git worktree list: unreadable")))
    lines = _refuse(capsys, "issue", "queue", str(NN),
                    want=("cannot tell where arena is checked out",
                          "`git worktree list` said:"),
                    step="where B is checked out")
    assert "git worktree list" in " ".join(lines)


def test_a_branch_that_is_not_checked_out_is_written_by_plumbing(repo, capsys):
    _git(repo, "branch", "ctxfix")
    before = (_branch(repo), _branch(repo, "ctxfix"))
    code, out, _ = _run(capsys, "issue", "queue", str(NN), "--branch", "ctxfix",
                        "--note", "judged on ctxfix")
    assert code == rounds.EXIT_OK
    assert f"{NN}: open → queued on ctxfix @ " in out and "git push origin ctxfix" in out
    assert _branch(repo) == before[0]
    after = _branch(repo, "ctxfix")
    assert after != before[1] and _ref(repo, f"{after}^") == before[1]
    assert "queued (judged on ctxfix)" in _on_branch(repo, "ctxfix")
    assert tickets.status_word(_body(repo)) == "open"


def test_the_tree_of_a_plumbing_commit_differs_only_in_the_ticket(repo, capsys):
    _git(repo, "branch", "ctxfix")
    parent = _branch(repo, "ctxfix")
    _run(capsys, "issue", "queue", str(NN), "--branch", "ctxfix")
    names = _changed(repo, parent, _branch(repo, "ctxfix"))
    assert names == [f"epic-tasks/{NN}-ticket-{NN}.md"]


def test_the_index_and_head_are_untouched_by_plumbing(repo, capsys):
    _git(repo, "branch", "ctxfix")
    index = repo / ".git" / "index"
    before = (_branch(repo), _git(repo, "status", "--porcelain"), index.read_bytes())
    _run(capsys, "issue", "queue", str(NN), "--branch", "ctxfix")
    assert (_branch(repo), _git(repo, "status", "--porcelain"), index.read_bytes()) == before


def test_the_compare_and_swap_refuses_a_moved_branch(repo, capsys, monkeypatch):
    _git(repo, "branch", "ctxfix")
    real_git = tickets.git

    def racy_git(r, *args, **kw):
        if args[:1] == ("update-ref",):
            # a second arena call commits to the same branch in the meantime
            sha = real_git(r, "commit-tree", real_git(r, "write-tree"), "-p", "ctxfix",
                           "-m", "noise")
            real_git(r, "update-ref", "refs/heads/ctxfix", sha, _branch(r, "ctxfix"))
        return real_git(r, *args, **kw)

    monkeypatch.setattr(tickets, "git", racy_git)
    _refuse(capsys, "issue", "queue", str(NN), "--branch", "ctxfix",
            want=("git update-ref",), step="commit")
    assert "queued" not in _on_branch(repo, "ctxfix")
    assert _git(repo, "log", "-1", "--format=%s", "ctxfix") == "noise"


def test_the_commit_is_local_and_never_pushes(repo, capsys):
    _git(repo, "branch", "ctxfix")
    code, out, _ = _run(capsys, "issue", "queue", str(NN), "--branch", "ctxfix")
    assert code == rounds.EXIT_OK and "git push origin ctxfix" in out
    assert "origin" not in _git(repo, "remote")


# 3 ─ §6: the ticket
def test_no_ticket_refuses_and_says_what_is_there(repo, capsys):
    lines = _refuse(capsys, "issue", "queue", "199", want=("no ticket 199",), nn=199,
                    step="ticket found on B")
    assert "arena issue list --branch arena" in " ".join(lines)


def test_a_draft_only_ticket_refuses_and_points_at_create(repo, capsys):
    _write(repo / ".arena/drafts/149-draft.md", _md(149, ar="drafted"))
    lines = _refuse(capsys, "issue", "queue", "149",
                    want=("only in .arena/drafts/", "no **Status:** line yet"), nn=149,
                    step="ticket found on B")
    assert "arena issue create --file .arena/drafts/149-draft.md" in " ".join(lines)


def test_a_ticket_only_in_the_checkout_refuses(repo, capsys):
    _ticket(repo, 149, ar="uncommitted")
    lines = _refuse(capsys, "issue", "queue", "149", want=("not committed on arena",), nn=149,
                    step="ticket found on B")
    text = " ".join(lines)
    assert "--only" in text and "149: ticket" in text


def test_two_files_with_the_same_number_refuse_and_name_both(repo, capsys):
    _write(repo / "epic-tasks/149-one.md", _md(149, ar="one"))
    _write(repo / "epic-tasks/149-two.md", _md(149, ar="two"))
    _commit(repo, "two files")
    lines = _refuse(capsys, "issue", "queue", "149",
                    want=("two tickets 149 on arena", "find_ticket would refuse"), nn=149,
                    step="ticket found on B")
    text = " ".join(lines)
    assert "149-one.md" in text and "149-two.md" in text
    assert "rm epic-tasks/149-two.md" in text


def test_a_rejected_file_is_not_a_second_ticket(repo, capsys):
    _ticket(repo, 149, ar="real")
    _write(repo / "epic-tasks/149-ticket-149.md.rejected", _md(149, ar="rejected"))
    _commit(repo, "with a rejected file")
    code, out, _ = _run(capsys, "issue", "queue", "149")
    assert code == rounds.EXIT_OK and "open → queued" in out


def test_no_status_line_refuses_and_points_at_edit(repo, capsys):
    _ticket(repo, 149, status=None, ar="no status")
    _commit(repo, "no status")
    lines = _refuse(capsys, "issue", "queue", "149",
                    want=("no **Status:** line", "does not invent where it goes"), nn=149,
                    step="status readable")
    assert "arena issue edit 149 --branch arena" in " ".join(lines)


def test_an_unknown_status_word_refuses_and_names_it(repo, capsys):
    _ticket(repo, 149, status="fixed", ar="fixed")
    _commit(repo, "wrong word")
    _refuse(capsys, "issue", "queue", "149",
            want=("**Status:** 'fixed' is not one of open, queued, closed, landed",), nn=149,
            step="status readable")


def test_landed_refuses_every_verb(repo, capsys):
    _ticket(repo, 149, status="landed", ar="landed")
    _commit(repo, "landed")
    for verb in ("queue", "open", "close", "reopen"):
        flag = ("--reason", "why") if verb == "close" else ()
        lines = _refuse(capsys, "issue", verb, "149", *flag, want=("is landed",), nn=149,
                        step="transition allowed")
        text = " ".join(lines)
        assert f"not {verb}" in text and "issue land" in text


def test_landed_is_read_but_never_written_by_a_verb(repo, capsys):
    assert {target for _, target in tickets.TRANSITIONS.values()} == {"open", "queued", "closed"}
    _ticket(repo, 149, status="landed", ar="landed")
    _commit(repo, "landed")
    _refuse(capsys, "issue", "queue", "149", want=("is landed",), step="transition allowed")
    assert tickets.status_word(_on_branch(repo, "arena", 149)) == "landed"


def test_uncommitted_edits_are_never_swept_and_are_kept(repo, capsys):
    (repo / "epic-tasks/148-ticket-148.md").write_text(_body(repo) + "operator edit\n",
                                                       encoding="utf-8")
    lines = _refuse(capsys, "issue", "queue", str(NN),
                    want=("uncommitted edits", "never overwritten or swept"), nn=NN,
                    step="write")
    text = " ".join(lines)
    assert "diff -- epic-tasks/148-ticket-148.md" in text
    assert "restore -- epic-tasks/148-ticket-148.md" in text
    assert _body(repo).endswith("operator edit\n")
    raw = subprocess.run(["git", "status", "--porcelain", "--", f"epic-tasks/{NN}-ticket-{NN}.md"],
                         cwd=str(repo), capture_output=True, text=True, env=ENV).stdout
    assert raw == f" M epic-tasks/{NN}-ticket-{NN}.md\n"


def test_a_staged_edit_of_the_ticket_is_refused_too(repo, capsys):
    (repo / f"epic-tasks/{NN}-ticket-{NN}.md").write_text(_body(repo) + "staged edit\n",
                                                          encoding="utf-8")
    _git(repo, "add", f"epic-tasks/{NN}-ticket-{NN}.md")
    _refuse(capsys, "issue", "queue", str(NN), want=("uncommitted edits",), nn=NN,
            step="write")
    assert _body(repo).endswith("staged edit\n")
    assert _branch(repo) == _ref(repo, "HEAD")


def test_a_running_round_refuses_the_write(repo, tmp_path, capsys, monkeypatch):
    _alive(repo, tmp_path, monkeypatch)
    _refuse(capsys, "issue", "queue", str(NN), want=("is running",), nn=NN,
            step="round not running")


def test_a_done_round_is_allowed(repo, capsys):
    _round(repo, str(NN))
    code, out, _ = _run(capsys, "issue", "queue", str(NN))
    assert code == rounds.EXIT_OK and "open → queued" in out


# 4 ─ §6: the line's format
def test_queue_writes_the_word_and_one_commit(repo, capsys):
    before = _body(repo)
    code, out, _ = _run(capsys, "issue", "queue", str(NN))
    assert code == rounds.EXIT_OK and out.startswith(f"{NN}: open → queued on arena @ ")
    assert before.replace("**Status:** open", "**Status:** queued", 1) == _body(repo)
    assert _git(repo, "log", "-1", "--format=%s") == f"{NN}: status open → queued"
    assert len(_git(repo, "log", "-1", "--format=%P").split()) == 1


def test_the_note_after_the_word(repo, capsys):
    code, _, _ = _run(capsys, "issue", "queue", str(NN), "--note", "judged on arena")
    assert code == rounds.EXIT_OK
    assert "**Status:** queued (judged on arena)" in _body(repo)
    assert tickets.status_word(_body(repo)) == "queued"
    code, _, _ = _run(capsys, "issue", "open", str(NN))
    assert code == rounds.EXIT_OK and "**Status:** open" in _body(repo)


def test_a_note_without_parentheses_is_read_as_a_note(repo, capsys):
    assert tickets.status_note("**Status:** queued, judged on arena\n") == "judged on arena"
    assert tickets.status_note("**Status:** queued (judged on arena)\n") == "judged on arena"
    assert tickets.status_note("**Status:** open\n") == ""
    (repo / "epic-tasks/148-ticket-148.md").write_text(
        _body(repo).replace("**Status:** open", "**Status:** queued, judged on arena"),
        encoding="utf-8")
    _commit(repo, "a note without parentheses")
    code, out, _ = _run(capsys, "issue", "open", str(NN))
    assert code == rounds.EXIT_OK and "→ open" in out


def test_close_requires_reason(repo, capsys):
    _refuse(capsys, "issue", "close", str(NN), want=("needs --reason",), nn=NN,
            step="transition allowed")


def test_close_writes_closed_and_its_reason(repo, capsys):
    code, _, _ = _run(capsys, "issue", "close", str(NN), "--reason", "moved to 149")
    assert code == rounds.EXIT_OK
    assert "**Status:** closed\n**Closed:** moved to 149" in _body(repo)
    assert _git(repo, "log", "-1", "--format=%s") == f"{NN}: status open → closed"
    assert tickets.closed_reason(_body(repo)) == "moved to 149"


def test_reopen_removes_the_closed_line(repo, capsys):
    before = _body(repo)
    _run(capsys, "issue", "close", str(NN), "--reason", "moved to 149")
    code, _, _ = _run(capsys, "issue", "reopen", str(NN), "--note", "back on offer")
    assert code == rounds.EXIT_OK
    assert "**Status:** open (back on offer)" in _body(repo)
    assert "**Closed:**" not in _body(repo)
    assert _body(repo).replace("**Status:** open (back on offer)", "**Status:** open", 1) == before


def test_closing_again_does_not_duplicate_the_reason(repo, capsys):
    _run(capsys, "issue", "close", str(NN), "--reason", "moved to 149")
    code, _, _ = _run(capsys, "issue", "close", str(NN), "--reason", "moved to 150")
    assert code == rounds.EXIT_OK
    assert _body(repo).count("**Closed:**") == 1
    assert tickets.closed_reason(_body(repo)) == "moved to 150"


def test_reopening_twice_is_a_no_op(repo, capsys):
    _run(capsys, "issue", "close", str(NN), "--reason", "why")
    # `open` is from `queued` only: a closed ticket comes back with `reopen`, so
    # the `**Closed:**` line is never removed but the verb that wrote it.
    _refuse(capsys, "issue", "open", str(NN), want=("open is not allowed from closed",),
            nn=NN, step="transition allowed")
    assert "**Closed:**" in _body(repo)
    code, _, _ = _run(capsys, "issue", "reopen", str(NN))
    assert code == rounds.EXIT_OK and "**Closed:**" not in _body(repo)
    head, before = _branch(repo), _body(repo)
    _refuse(capsys, "issue", "reopen", str(NN), want=("reopen is not allowed from open",),
            nn=NN, step="transition allowed")
    assert (_branch(repo), _body(repo)) == (head, before)


def test_queue_on_a_queued_ticket_is_a_no_op(repo, capsys):
    _run(capsys, "issue", "queue", str(NN))
    before = (_branch(repo), _body(repo))
    code, out, _ = _run(capsys, "issue", "queue", str(NN))
    assert code == rounds.EXIT_OK and out == f"{NN}: already queued\n"
    assert (_branch(repo), _body(repo)) == before


def test_queue_with_a_new_note_commits(repo, capsys):
    _run(capsys, "issue", "queue", str(NN))
    head = _branch(repo)
    code, _, _ = _run(capsys, "issue", "queue", str(NN), "--note", "judged on arena")
    assert code == rounds.EXIT_OK and _branch(repo) != head
    assert "queued (judged on arena)" in _body(repo)


def test_a_note_without_parentheses_still_makes_the_word_the_state(repo, capsys):
    assert tickets.status_word("**Status:** queued, judged on arena\n") == "queued"


def test_the_status_line_is_the_only_line_that_changes(repo, capsys):
    before = _body(repo)
    _run(capsys, "issue", "close", str(NN), "--reason", "why")
    assert _body(repo) == before.replace("**Status:** open", "**Status:** closed\n**Closed:** why",
                                         1)
    _run(capsys, "issue", "reopen", str(NN))
    assert _body(repo) == before


def test_issue_view_still_prints_the_text(repo, capsys):
    code, out, _ = _run(capsys, "issue", "view", str(NN))
    assert code == rounds.EXIT_OK and out.startswith(_body(repo))


# 5 ─ §6: the commit
def test_the_commit_commits_only_the_ticket(repo, capsys):
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "contest.ini").write_text(INI + "\n[side]\n", encoding="utf-8")
    _git(repo, "add", "-A")
    code, _, _ = _run(capsys, "issue", "queue", str(NN), "--branch", "side")
    assert code == rounds.EXIT_OK
    names = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert names == [f"epic-tasks/{NN}-ticket-{NN}.md"]
    assert (repo / "contest.ini").read_text(encoding="utf-8") == INI + "\n[side]\n"


def test_a_refused_local_commit_is_a_refusal_not_a_traceback(repo, capsys):
    hooks = repo / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'pre-commit: no' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(repo, "config", "core.hooksPath", "hooks")
    tip = _branch(repo)
    lines = _refuse(capsys, "issue", "queue", str(NN), want=("pre-commit: no",), step="commit")
    assert _branch(repo) == tip
    # `write` finished and `commit` refused: the text sits there uncommitted.
    assert _git(repo, "status", "--porcelain", "--",
                f"epic-tasks/{NN}-ticket-{NN}.md") == f"M epic-tasks/{NN}-ticket-{NN}.md"
    assert f"git -C {os.path.realpath(str(repo))} status" in " ".join(lines)


def test_the_json_names_the_ticket_the_branch_and_the_commit(repo, capsys):
    _git(repo, "branch", "ctxfix")
    code, out, _ = _run(capsys, "-o", "json", "issue", "queue", str(NN), "--branch", "ctxfix")
    data = json.loads(out)
    assert code == rounds.EXIT_OK and out.endswith("\n")
    assert data["ticket"] == NN and data["branch"] == "ctxfix"
    assert data["from"] == "open" and data["to"] == "queued"
    assert data["commit"] == _branch(repo, "ctxfix") and len(data["commit"]) == 40


def test_the_json_says_when_nothing_was_committed(repo, capsys):
    _run(capsys, "issue", "queue", str(NN))
    code, out, _ = _run(capsys, "-o", "json", "issue", "queue", str(NN))
    assert code == rounds.EXIT_OK
    data = json.loads(out)
    assert data["from"] == "queued" and data["to"] == "queued" and data["commit"] is None


def test_edit_is_the_only_verb_allowed_on_an_unknown_word(repo, capsys, monkeypatch, tmp_path):
    _ticket(repo, 149, status="fixed", ar="fixed")
    _commit(repo, "wrong word")
    for verb in ("queue", "open", "close", "reopen"):
        flag = ("--reason", "why") if verb == "close" else ()
        _refuse(capsys, "issue", verb, "149", *flag,
                want=("is not one of open, queued, closed, landed",), step="status readable")
    _editor(tmp_path, monkeypatch, APPEND)
    monkeypatch.setenv("APPEND", "\nedited\n")
    code, _, _ = _run(capsys, "issue", "edit", "149")
    assert code == rounds.EXIT_OK and "edited" in _body(repo, 149)


# 6 ─ §7: the refusal's shape
def test_a_ticket_number_that_is_not_a_number_has_one_line(repo, capsys):
    code, out, err = _run(capsys, "issue", "queue", "abc")
    assert code == 2 and out == ""
    assert err.count("\n") == 1 and err.startswith("arena: ")
    assert "not a ticket number" in err


def test_every_refusal_carries_the_flow_and_the_json(repo, capsys):
    _json_refuse(capsys, "issue", "queue", str(NN), "--branch", "arenax",
                 want=("does not exist",))


def test_the_flow_marks_are_done_failed_and_todo(repo, capsys):
    _ticket(repo, 149, status="fixed", ar="fixed")
    _commit(repo, "wrong word")
    lines = _refuse(capsys, "issue", "queue", "149", step="status readable")
    flow = [line for line in lines if line.startswith("  flow:")][0]
    assert flow.count("[✓]") == 3 and flow.count("[✗]") == 1 and flow.count("[ ]") == 4
    assert "[✓] branch resolved" in flow and "[ ] write" in flow
    assert " → " in flow


def test_the_where_line_says_where_the_operator_is(repo, capsys):
    (repo / f"epic-tasks/{NN}-ticket-{NN}.md").write_text(
        _body(repo).replace("**Status:** open", "**Status:** fixed"), encoding="utf-8")
    _commit(repo, "wrong word")
    _git(repo, "branch", "ctxfix")
    lines = _refuse(capsys, "issue", "queue", str(NN), "--branch", "ctxfix",
                    want=("is not one of open, queued, closed, landed",), step="status readable")
    where = " ".join(lines)
    assert "checkout " in where and "target branch ctxfix @ " in where
    assert "not checked out" in where and f"round {NN}: not started" in where
    assert f"epic-tasks/{NN}-ticket-{NN}.md" in where and "Status: fixed" in where


# 7 ─ `issue edit`
def test_issue_edit_commits_the_editor_text(repo, tmp_path, capsys, monkeypatch):
    _editor(tmp_path, monkeypatch, APPEND)
    monkeypatch.setenv("APPEND", "\nedited body\n")
    before = _body(repo)
    code, out, _ = _run(capsys, "issue", "edit", str(NN))
    assert code == rounds.EXIT_OK and out.startswith(f"{NN}: edited on arena @ ")
    assert _body(repo) == before + "\nedited body\n"
    assert _git(repo, "log", "-1", "--format=%s") == f"{NN}: ticket edited"
    assert tickets.status_word(_body(repo)) == "open"
    assert not list((repo / ".arena" / "tmp").glob("*"))


def test_issue_edit_with_branch_writes_by_plumbing(repo, tmp_path, capsys, monkeypatch):
    _git(repo, "branch", "ctxfix")
    _editor(tmp_path, monkeypatch, APPEND)
    monkeypatch.setenv("APPEND", "\nplumbed\n")
    head = _branch(repo)
    code, out, _ = _run(capsys, "issue", "edit", str(NN), "--branch", "ctxfix")
    assert code == rounds.EXIT_OK and _branch(repo) == head
    assert "plumbed" in _on_branch(repo, "ctxfix") and "\nplumbed\n" not in _body(repo)
    assert "edited on ctxfix" in out


def test_issue_edit_refuses_a_status_change_and_keeps_the_text(repo, tmp_path, capsys,
                                                                monkeypatch):
    _editor(tmp_path, monkeypatch, REPLACE)
    monkeypatch.setenv("OLD", "**Status:** open")
    monkeypatch.setenv("NEW", "**Status:** closed")
    lines = _refuse(capsys, "issue", "edit", str(NN),
                    want=("the editor changed **Status:**", "use `arena issue close`"),
                    nn=NN, step="status unchanged")
    kept = [line for line in lines if "kept here" in line]
    assert kept and Path(kept[0].rsplit("  ", 1)[-1]).exists()
    assert "changed the status" not in _body(repo)
    assert tickets.status_word(_body(repo)) == "open"


def test_issue_edit_names_the_verb_not_the_status(repo, tmp_path, capsys, monkeypatch):
    _editor(tmp_path, monkeypatch, REPLACE)
    monkeypatch.setenv("OLD", "**Status:** open")
    monkeypatch.setenv("NEW", "**Status:** queued")
    lines = _refuse(capsys, "issue", "edit", str(NN), want=("the editor changed",))
    assert "arena issue queue" in " ".join(lines)


def test_issue_edit_adds_a_missing_status_line(repo, tmp_path, capsys, monkeypatch):
    _ticket(repo, 149, status=None, ar="no status")
    _commit(repo, "no status")
    _editor(tmp_path, monkeypatch, APPEND)
    monkeypatch.setenv("APPEND", "\n**Status:** open\n")
    code, _, _ = _run(capsys, "issue", "edit", "149")
    assert code == rounds.EXIT_OK and "**Status:** open" in _body(repo, 149)


def test_issue_edit_refuses_a_missing_header_and_keeps_the_text(repo, tmp_path, capsys,
                                                                monkeypatch):
    (repo / "epic-tasks/148-ticket-148.md").write_text(
        _body(repo).replace("**Symbol:** x\n", ""), encoding="utf-8")
    _commit(repo, "no symbol")
    _editor(tmp_path, monkeypatch, APPEND)
    monkeypatch.setenv("APPEND", "\nedited\n")
    lines = _refuse(capsys, "issue", "edit", str(NN),
                    want=("missing **Symbol:** header field",), step="lint")
    kept = [line for line in lines if "kept here" in line]
    assert kept and Path(kept[0].rsplit("  ", 1)[-1]).exists()


def test_issue_edit_with_no_change_commits_nothing(repo, tmp_path, capsys, monkeypatch):
    _editor(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    head = _branch(repo)
    code, out, _ = _run(capsys, "issue", "edit", str(NN))
    assert code == rounds.EXIT_OK and out == f"{NN}: no change\n"
    assert _branch(repo) == head


def test_issue_edit_refuses_when_the_editor_exits_nonzero(repo, tmp_path, capsys, monkeypatch):
    _editor(tmp_path, monkeypatch, "#!/bin/sh\nexit 3\n")
    _refuse(capsys, "issue", "edit", str(NN), want=("exited 3",), step="editor")


def test_issue_edit_refuses_when_the_editor_is_not_a_command(repo, capsys, monkeypatch):
    monkeypatch.setenv("EDITOR", "/no/such/editor")
    _refuse(capsys, "issue", "edit", str(NN), want=("did not run",), step="editor")


def test_issue_edit_refuses_a_running_round(repo, tmp_path, capsys, monkeypatch):
    _alive(repo, tmp_path, monkeypatch)
    _refuse(capsys, "issue", "edit", str(NN), want=("is running",), step="round not running")


# 8 ─ the cross-checks
def test_next_task_skips_closed_and_reads_a_note_as_queued(tmp_path):
    from scripts import next_task
    folder = tmp_path / "tasks"
    folder.mkdir()
    for num, status in ((1, "open"), (2, "closed"), (3, "queued"), (4, "queued (judged)"),
                        (5, None)):
        (folder / f"{num}-t.md").write_text(
            _md(num, status=status).split("\n", 2)[2], encoding="utf-8")
    rows = {t["num"]: t["offered"] for t in next_task.load_tickets(str(folder))}
    assert rows == {1: True, 2: False, 3: False, 4: False, 5: True}
    assert next_task.SKIP_STATUS == ("landed", "queued", "closed")


def test_the_contest_and_next_task_agree_on_the_parked_words():
    from tools.contest import cli as contest_cli
    from scripts import next_task
    assert contest_cli.PARKED == next_task.SKIP_STATUS == ("landed", "queued", "closed")


def test_issue_list_shows_a_closed_ticket_with_its_reason(repo, capsys):
    _ticket(repo, 149, status="closed", closed="moved to 150", ar="closed")
    _commit(repo, "closed ticket")
    code, out, _ = _run(capsys, "-o", "json", "issue", "list")
    assert code == rounds.EXIT_OK
    rows = {row["number"]: row for row in json.loads(out)}
    assert rows[149]["state"] == "closed"                 # the state stays one word
    assert rows[149]["reason"] == "moved to 150"
    assert rows[149]["flags"] == []                       # a closed ticket is no mismatch
    assert rows[NN]["reason"] == ""
    code, out, _ = _run(capsys, "issue", "list")
    row = next(line for line in out.splitlines() if line.startswith("149"))
    assert "closed (moved to 150)" in row and not row.rstrip().endswith("!")


def test_a_closed_ticket_without_a_reason_line_is_still_just_closed(repo, capsys):
    _ticket(repo, 149, status="closed", ar="closed")
    _commit(repo, "closed, no reason")
    code, out, _ = _run(capsys, "issue", "list")
    row = next(line for line in out.splitlines() if line.startswith("149"))
    assert "closed" in row and "(" not in row.split("closed", 1)[1].split("closed")[0]


def test_issue_list_keeps_its_columns(repo, capsys):
    code, out, _ = _run(capsys, "issue", "list")
    assert code == rounds.EXIT_OK
    assert out.splitlines()[0].split() == ["NN", "STATE", "TITLE", "!"]


def test_the_verb_is_registered_with_its_flags(repo, capsys):
    for verb, ar in (("queue", "AR-14"), ("open", "AR-14"), ("close", "AR-14"),
                     ("reopen", "AR-14"), ("edit", "AR-14")):
        assert cli.OBJECTS["issue"].verbs[verb].ticket == ar
    for argv, flag in ((["issue", "queue", "--help"], "--note"),
                       (["issue", "close", "--help"], "--reason"),
                       (["issue", "reopen", "--help"], "--note"),
                       (["issue", "edit", "--help"], "--branch")):
        code, out, _ = _run(capsys, *argv)
        assert code == 0 and flag in out, (argv, out)
    code, out, _ = _run(capsys, "issue", "--help")
    assert code == 0
    for verb in ("create", "list", "view", "queue", "close", "edit", "land"):
        assert verb in out


def test_land_has_a_handler_in_ar8(capsys):
    """AR-8: `issue land` is a handler with its own arguments, not a placeholder."""
    verb = cli.OBJECTS["issue"].verbs["land"]
    assert verb.handler is not None and verb.add_arguments is not None
    code, out, _ = _run(capsys, "issue", "land", "--help")
    assert code == 0 and "--score" in out and "--note" in out and "NN" in out


# 8 ─ what the judge of round 150 found missing in the winner, each now pinned
def _hints(lines: list) -> list:
    return [line for line in lines if line.startswith("  → ")]


def test_a_landed_ticket_is_sent_to_issue_view_and_to_issue_land(repo, capsys):
    """§7: both hints, whether or not AR-8's handler exists yet."""
    _ticket(repo, 149, status="landed", ar="landed")
    _commit(repo, "149: landed")
    lines = _refuse(capsys, "issue", "queue", "149", step="transition allowed")
    hints = "\n".join(_hints(lines))
    assert "arena issue view 149" in hints and "arena issue land 149" in hints


@pytest.mark.parametrize("verb", ["queue", "edit"])
def test_two_files_with_one_number_get_a_log_hint_and_a_rm_hint_each(repo, capsys, verb):
    _write(repo / "epic-tasks/149-first.md", _md(149, ar="first"))
    _write(repo / "epic-tasks/149-second.md", _md(149, ar="second"))
    _commit(repo, "two 149")
    lines = _refuse(capsys, "issue", verb, "149", step="ticket found on B")
    hints = "\n".join(_hints(lines))
    for name in ("149-first.md", "149-second.md"):
        assert f"git log -1 --format=%h\\ %s arena -- epic-tasks/{name}" in hints
        assert f" rm epic-tasks/{name}" in hints


def test_edit_fixes_an_unknown_status_word(repo, tmp_path, capsys, monkeypatch):
    """§6: an unknown word refuses every verb but `edit` — which has to be able to fix it."""
    _ticket(repo, 149, status="fixed", ar="wrong word")
    _commit(repo, "wrong word")
    before = _ref(repo, "HEAD")
    _editor(tmp_path, monkeypatch, REPLACE)
    monkeypatch.setenv("OLD", "**Status:** fixed")
    monkeypatch.setenv("NEW", "**Status:** open")
    code, out, err = _run(capsys, "issue", "edit", "149")
    assert code == rounds.EXIT_OK, err
    assert "**Status:** open" in _body(repo, 149) and "fixed" not in _body(repo, 149)
    assert _git(repo, "rev-list", "--count", f"{before}..HEAD") == "1"


def test_edit_adds_a_missing_status_line(repo, tmp_path, capsys, monkeypatch):
    """§6: no `**Status:**` line — the operator adds it with `issue edit`."""
    _ticket(repo, 149, status=None, ar="no status")
    _commit(repo, "no status")
    _editor(tmp_path, monkeypatch, REPLACE)
    monkeypatch.setenv("OLD", "**Severity:**")
    monkeypatch.setenv("NEW", "**Status:** open\n**Severity:**")
    code, out, err = _run(capsys, "issue", "edit", "149")
    assert code == rounds.EXIT_OK, err
    assert _body(repo, 149).count("**Status:** open") == 1


def test_edit_never_writes_landed_by_hand_not_even_over_an_unknown_word(
        repo, tmp_path, capsys, monkeypatch):
    _ticket(repo, 149, status="fixed", ar="wrong word")
    _commit(repo, "wrong word")
    before = _ref(repo, "HEAD")
    _editor(tmp_path, monkeypatch, REPLACE)
    monkeypatch.setenv("OLD", "**Status:** fixed")
    monkeypatch.setenv("NEW", "**Status:** landed")
    code, out, err = _run(capsys, "issue", "edit", "149")
    assert code == 2 and "landed" in err
    assert _ref(repo, "HEAD") == before and "fixed" in _body(repo, 149)


def test_edit_with_no_editor_and_no_terminal_is_refused_before_anything_is_made(
        repo, capsys, monkeypatch):
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    before = _ref(repo, "HEAD")
    code, out, err = _run(capsys, "issue", "edit", str(NN))
    assert code == 2 and "$EDITOR" in err.splitlines()[0]
    assert _ref(repo, "HEAD") == before
    assert not (repo / ".arena" / "tmp").exists(), "no temp file for an editor that never ran"


@pytest.mark.parametrize("answer, written", [("y", True), ("yes", True), ("n", False), ("", False)])
def test_without_dash_y_the_verb_asks_and_writes_only_on_yes(
        repo, capsys, monkeypatch, answer, written):
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    before = _ref(repo, "HEAD")
    code, out, err = _run(capsys, "issue", "queue", str(NN), yes=False)
    assert f"{NN}: open → queued on arena" in out        # the before → after line
    if written:
        assert code == rounds.EXIT_OK, err
        assert "**Status:** queued" in _body(repo)
    else:
        assert code == 2 and "not applied" in err
        assert _ref(repo, "HEAD") == before and "**Status:** open" in _body(repo)


def test_a_closed_stdin_is_a_no_not_a_traceback(repo, capsys, monkeypatch):
    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    before = _ref(repo, "HEAD")
    code, out, err = _run(capsys, "issue", "queue", str(NN), yes=False)
    assert code == 2 and "not applied" in err and _ref(repo, "HEAD") == before


def test_dash_y_asks_nothing_and_says_the_change_once(repo, capsys, monkeypatch):
    def never(prompt=""):
        raise AssertionError("asked despite -y")

    monkeypatch.setattr("builtins.input", never)
    code, out, err = _run(capsys, "issue", "queue", str(NN))
    assert code == rounds.EXIT_OK, err
    assert out.count("open → queued") == 1


def test_a_refusal_in_json_is_one_object_and_nothing_else(repo, capsys):
    code, out, err = _run(capsys, "-o", "json", "issue", "close", str(NN))   # no --reason
    assert code == 2 and out == ""
    data = json.loads(err)                      # the whole of stderr is that one object
    assert data["error"] and data["hints"] and data["flow"]


def test_the_issue_help_lists_every_verb_the_object_has(capsys):
    assert cli.main(["issue", "--help"]) == rounds.EXIT_OK
    shown = capsys.readouterr().out
    for verb in ("create", "list", "view", "queue", "open", "close", "reopen", "edit", "land"):
        assert verb in shown
