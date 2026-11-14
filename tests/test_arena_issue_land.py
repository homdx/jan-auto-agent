"""tests/test_arena_issue_land.py — AR-8: `issue land`, the closing commit lands the ticket."""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from tools.arena import merge, rounds

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
NN = 195
INI = "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = test/a\n"
TICKET = f"{NN}-ar-8-entry-merge.md"
STATUS_LINE = f"**Status:** landed — round {NN}, winner glm-4-7 (19/19 on contest-bench/{NN}), " \
              "7e7e7e7 as-is"
HEAD = "def thing():\n    return 1\n"
ROW_195 = (f"| {NN} | `AR-8` | open — waiting | LARGE | S | [t](x) | "
           "`tools/arena/merge.py` |")
ROW_131 = "| 131 | `AR-1` | open — old row | MEDIUM | S | [t](x) | `a.py` |"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _arg(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A clean checkout on `main` with round 195's ticket, INDEX row and base commit."""
    r = tmp_path / "repo"
    r.mkdir()
    _write(r / "contest.ini", INI)
    _write(r / ".gitignore", "__pycache__/\n.arena/\nout/\ncontest-out/\n")
    _write(r / "epic-tasks" / TICKET,
           f"# AR-8 — {NN}\n\n**Status:** open\n**Size:** M\n\nbody of {NN}\n")
    _write(r / "epic-tasks" / "INDEX.md", f"# Index\n\n{ROW_131}\n{ROW_195}\n")
    _write(r / "src" / "thing.py", HEAD)
    _write(r / "src" / "x.py", "x = 0\n")
    _write(r / ".arena" / "rounds" / f"{NN}.json", json.dumps({"branch": "main"}))
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _roster(repo: Path, agent: str = "glm-4-7", state: str = "READY") -> None:
    """Round 195's two agents in `out/195/`, the READY/TOTAL the body is built from."""
    out = repo / "out" / str(NN)
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "entrants.json", json.dumps(
        [{"agent": agent, "state": state, "branch": "round-195-glm",
          "entry": f"out/{NN}/{agent}.patch"}]))
    _write(out / "state.json", json.dumps(
        {"agents": [{"agent": agent, "state": state}, {"agent": "other", "state": "DONE"}]}))


def _record(repo: Path, sha: str = "7e7e7e7e7e7e7e7e", agent: str = "glm-4-7") -> None:
    _write(repo / ".arena" / "merged.json",
           json.dumps({"131": {"agent": "other", "sha": "a" * 40, "subject": "other"},
                       str(NN): {"agent": agent, "sha": sha,
                                 "subject": "AR-8: make thing add up"}}))


def _land(repo: Path, capsys, **kw) -> int:
    kw.setdefault("passthrough", [])
    kw.setdefault("score", "")
    kw.setdefault("message", "")
    kw.setdefault("note", "")
    return merge.issue_land(repo, _arg(number=str(NN), **kw), prof={"trailer": ""})


def test_status_line_index_and_bench_in_one_commit(repo: Path, capsys) -> None:
    """The ticket's `**Status:**` line, the INDEX row's word, and `contest-bench/NN/*` land
    together; the ticket's other lines, every other INDEX line and the winner stay out."""
    _roster(repo)
    _record(repo)
    _write(repo / "contest-bench" / str(NN) / "test_bench_195.py", "def test_bench():\n    pass\n")
    _write(repo / "contest-bench" / str(NN) / "__pycache__" / "thing.pyc", "pyc\n")

    assert _land(repo, capsys, score="19/19 on contest-bench/195") == merge.EXIT_OK
    sha = _git(repo, "rev-parse", "HEAD")[:7]
    shown = _git(repo, "show", "--name-only", "--format=", "--no-renames", "HEAD").splitlines()
    assert shown == [f"contest-bench/{NN}/test_bench_195.py",
                     f"epic-tasks/{TICKET}", "epic-tasks/INDEX.md"]

    text = (repo / "epic-tasks" / TICKET).read_text(encoding="utf-8")
    assert f"**Status:** {STATUS_LINE.split('**Status:** ')[1]}" in text
    assert "**Size:** M" in text and f"body of {NN}\n" in text

    rows = (repo / "epic-tasks" / "INDEX.md").read_text(encoding="utf-8")
    assert rows.splitlines()[2] == ROW_131
    assert rows.splitlines()[3] == ROW_195.replace("open — waiting", "landed")

    log = _git(repo, "log", "-1", "--format=%s%n%b")
    assert log.startswith(f"{NN}: ticket landed, and contest-bench/{NN} holds the round's")
    assert f"Round {NN}: 1/2 READY; winner glm-4-7 taken as-is in " \
           f"7e7e7e7." in log
    assert "def thing():" not in log and "7e7e7e7" not in log.splitlines()[0]

    out = capsys.readouterr().out
    assert f"landed {NN} @ {sha} — epic-tasks/{TICKET}, epic-tasks/INDEX.md, " \
           f"contest-bench/{NN}/test_bench_195.py" in out
    assert "next: python3 -m pytest tests -n 8 -q" in out
    assert "then: python3 -m pytest tests_bugfix -n 8 -q" in out
    assert f"push: git push origin main" in out
    # the bench file is tracked, the pyc is not, the round's record is not
    tracked = _git(repo, "ls-files")
    assert f"contest-bench/{NN}/test_bench_195.py" in tracked.splitlines()
    assert "__pycache__" not in tracked and ".arena/" not in tracked


def test_without_a_score_there_is_no_parenthesis(repo: Path, capsys) -> None:
    _roster(repo)
    _record(repo)
    assert _land(repo, capsys) == merge.EXIT_OK
    text = (repo / "epic-tasks" / TICKET).read_text(encoding="utf-8")
    assert f"**Status:** landed — round {NN}, winner glm-4-7, 7e7e7e7 as-is" in text


def test_no_record_leaves_only_the_status_word(repo: Path, capsys) -> None:
    """Acceptance 11: with no merge record the line is `landed — round NN`, there is no
    winner sentence in the body, and `--note` is appended as given."""
    _roster(repo)
    assert _land(repo, capsys, note="the round ran twice") == merge.EXIT_OK
    text = (repo / "epic-tasks" / TICKET).read_text(encoding="utf-8")
    assert f"**Status:** landed — round {NN}" in text
    assert "winner" not in text

    log = _git(repo, "log", "-1", "--format=%b")
    assert log.startswith(f"Round {NN}: 1/2 READY.")
    assert "winner" not in log and log.endswith("the round ran twice")


def test_trailer_and_custom_subject(repo: Path, capsys) -> None:
    _roster(repo)
    _record(repo)
    assert merge.issue_land(repo, _arg(number=str(NN), score="", note="", passthrough=[],
                                    message="my subject"),
                            prof={"trailer": "Signed-off-by: t <t@example.invalid>"}) == merge.EXIT_OK
    log = _git(repo, "log", "-1", "--format=%s%n%b")
    assert log.startswith("my subject\n")
    assert "Round 195: 1/2 READY; winner glm-4-7 taken as-is in 7e7e7e7." in log
    assert log.endswith("Signed-off-by: t <t@example.invalid>")


def test_extra_paths_ride_in_the_same_commit(repo: Path, capsys) -> None:
    """Acceptance 7: `-- tools/x.py` joins the commit and the sha ends `+ follow-up`."""
    _roster(repo)
    _record(repo)
    _write(repo / "src" / "x.py", "x = 1\n")

    assert _land(repo, capsys, passthrough=["src/x.py"]) == merge.EXIT_OK
    shown = _git(repo, "show", "--name-only", "--format=", "--no-renames", "HEAD").splitlines()
    assert shown == [f"epic-tasks/{TICKET}", "epic-tasks/INDEX.md", "src/x.py"]
    assert f"**Status:** landed — round {NN}, winner glm-4-7, 7e7e7e7 + follow-up" in \
           (repo / "epic-tasks" / TICKET).read_text(encoding="utf-8")
    assert "src/x.py" in capsys.readouterr().out


def test_refusals(repo: Path, capsys) -> None:
    """Each refusal is exit 2 and lands nothing."""
    _roster(repo)
    _record(repo)

    # a tracked change that is not the ticket, the INDEX row or an extra
    _write(repo / "src" / "thing.py", "def thing():\n    return 5\n")
    capsys.readouterr()
    assert _land(repo, capsys) == merge.EXIT_USAGE
    assert "changes not in the commit" in capsys.readouterr().err
    assert _git(repo, "status", "--porcelain").count("src/thing.py") == 1

    # an extra path with no change, and an extra path that does not exist
    _git(repo, "checkout", "-q", "--", "src/thing.py")
    for extra, why in (("src/thing.py", "no change to commit"),
                       ("src/nope.py", "no such file")):
        capsys.readouterr()
        assert _land(repo, capsys, passthrough=[extra]) == merge.EXIT_USAGE
        assert why in capsys.readouterr().err

    # not a round number, and a round that is still running
    capsys.readouterr()
    assert merge.issue_land(repo, _arg(number="x", score="", message="", note="",
                                       passthrough=[]), prof={}) == merge.EXIT_USAGE
    assert "not a round number" in capsys.readouterr().err

    fake = repo / "fake-proc"
    (fake / "4242").mkdir(parents=True)
    (fake / "4242" / "cmdline").write_bytes(
        f"python3\x00-m\x00tools.contest\x00run\x00--ticket\x00{NN}\x00".encode())
    (fake / "4242" / "cwd").symlink_to(str(repo.resolve()))
    proc_root = rounds.PROC_ROOT
    rounds.PROC_ROOT = str(fake)
    try:
        capsys.readouterr()
        assert _land(repo, capsys) == merge.EXIT_USAGE
        assert "is running" in capsys.readouterr().err
    finally:
        rounds.PROC_ROOT = proc_root

    # the wrong branch, and no ticket anywhere
    _write(repo / ".arena" / "rounds" / f"{NN}.json", json.dumps({"branch": "elsewhere"}))
    capsys.readouterr()
    assert _land(repo, capsys) == merge.EXIT_USAGE
    assert "elsewhere" in capsys.readouterr().err
    _write(repo / ".arena" / "rounds" / f"{NN}.json", json.dumps({"branch": "main"}))

    _git(repo, "rm", "-q", "-r", "epic-tasks")
    capsys.readouterr()
    assert _land(repo, capsys) == merge.EXIT_USAGE
    assert "no ticket" in capsys.readouterr().err


def test_refusals_stage_nothing(repo: Path, capsys) -> None:
    """The bench is only staged after every refusal, so a refused land leaves it untracked."""
    _roster(repo)
    _record(repo)
    bench = repo / "contest-bench" / str(NN)
    _write(bench / "test_bench_195.py", "def test_bench():\n    pass\n")
    _write(repo / "src" / "thing.py", "def thing():\n    return 5\n")

    capsys.readouterr()
    assert _land(repo, capsys) == merge.EXIT_USAGE
    assert "changes not in the commit" in capsys.readouterr().err
    status = _git(repo, "status", "--porcelain").splitlines()
    assert "M src/thing.py" in status and "?? contest-bench/" in status
    # the index is untouched: the bench is untracked, not staged
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_bench_dir_and_paths(repo: Path) -> None:
    """`contest-bench/NN` is found by name and by the two-digit form, and the
    `.gitignore`d `__pycache__` never shows up in the bench's path list."""
    (repo / "contest-bench" / "02" / "__pycache__").mkdir(parents=True)
    _write(repo / "contest-bench" / "02" / "test_bench_02.py", "x = 1\n")
    _write(repo / "contest-bench" / "02" / "__pycache__" / "x.pyc", "pyc\n")

    bench = merge._bench_dir(repo, 2)
    assert bench is not None and bench.name == "02"
    assert merge._bench_paths(repo, bench) == ["contest-bench/02/test_bench_02.py"]
    assert merge._bench_dir(repo, 3) is None


def test_landed_note(repo: Path) -> None:
    rec = {"agent": "glm-4-7", "sha": "7" * 40}
    assert merge.landed_note(NN, rec) == "— round 195, winner glm-4-7, 7777777 as-is"
    assert merge.landed_note(NN, rec, "19/19 on contest-bench/195") == \
        "— round 195, winner glm-4-7 (19/19 on contest-bench/195), 7777777 as-is"
    assert merge.landed_note(NN, rec, follow_up=True) == "— round 195, winner glm-4-7, 7777777 + follow-up"
    assert merge.landed_note(NN, None) == "— round 195"


def test_update_index_row_touches_one_cell(repo: Path) -> None:
    rows = (repo / "epic-tasks" / "INDEX.md").read_text(encoding="utf-8")
    new = merge.update_index_row(rows, NN, "landed")
    assert new is not None and new.splitlines()[2] == ROW_131
    assert new.splitlines()[3] == ROW_195.replace("open — waiting", "landed")
    assert merge.update_index_row(rows, 999, "landed") is None


def test_an_uncommitted_edit_of_the_ticket_is_not_overwritten(repo: Path, capsys) -> None:
    """The ticket text may come from `arena-round/NN`; a dirty checkout copy must not be lost."""
    _roster(repo)
    _record(repo)
    path = repo / "epic-tasks" / TICKET
    mine = path.read_text(encoding="utf-8") + "my own note\n"
    path.write_text(mine, encoding="utf-8")
    capsys.readouterr()
    assert _land(repo, capsys) == merge.EXIT_USAGE
    assert "uncommitted changes" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == mine
    assert _git(repo, "log", "--oneline").count("\n") == 0


def test_a_landed_ticket_is_not_landed_twice(repo: Path, capsys) -> None:
    """A second `issue land` would only repeat the commit; it is refused in one line."""
    _roster(repo)
    _record(repo)
    assert _land(repo, capsys) == merge.EXIT_OK
    commits = _git(repo, "rev-list", "--count", "HEAD")
    capsys.readouterr()
    assert _land(repo, capsys) == merge.EXIT_USAGE
    err = capsys.readouterr().err
    assert "already landed" in err and len(err.strip().splitlines()) == 1
    assert _git(repo, "rev-list", "--count", "HEAD") == commits


# ── round 363: a rollback that cannot finish says so ──────────────────────────
def _reject_commits(repo: Path, message: str = "no commits today") -> None:
    """A `pre-commit` hook that refuses every commit with *message*."""
    _git(repo, "config", "core.hooksPath", ".git/hooks")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\necho '{message}' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)


def _fail_ticket_restore(monkeypatch, ticket: Path) -> None:
    """Make the rollback's `Path.write_bytes()` of *ticket* raise, and nothing else."""
    real = Path.write_bytes

    def write_bytes(self, data):
        if self == ticket:
            raise OSError(errno.EIO, "injected restore error")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)


def _fail_unstage(monkeypatch) -> None:
    """Make the rollback's `git reset` raise, and every other git call work."""
    real = merge.git

    def git(repo, *args, **kwargs):
        if args and args[0] == "reset":
            raise merge.GitRefError("injected reset error")
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(merge, "git", git)


def test_a_refused_land_reports_the_refusal_and_restores_everything(repo: Path, capsys) -> None:
    """Acceptance 1: the hook's refusal is the only error, HEAD stays, and the ticket,
    INDEX row and staging all return to where they were; a retry then succeeds."""
    _roster(repo)
    _record(repo)
    _reject_commits(repo)
    before = _git(repo, "rev-parse", "HEAD")
    ticket_before = (repo / "epic-tasks" / TICKET).read_text(encoding="utf-8")
    rows_before = (repo / "epic-tasks" / "INDEX.md").read_text(encoding="utf-8")

    assert _land(repo, capsys) == merge.EXIT_USAGE
    err = capsys.readouterr().err
    assert "no commits today" in err
    assert "rollback" not in err
    assert _git(repo, "rev-parse", "HEAD") == before
    assert (repo / "epic-tasks" / TICKET).read_text(encoding="utf-8") == ticket_before
    assert (repo / "epic-tasks" / "INDEX.md").read_text(encoding="utf-8") == rows_before
    assert _git(repo, "diff", "--cached", "--name-only") == ""

    (repo / ".git" / "hooks" / "pre-commit").unlink()
    assert _land(repo, capsys) == merge.EXIT_OK


def test_a_failed_ticket_restore_is_reported_and_the_rest_still_runs(
        repo: Path, capsys, monkeypatch) -> None:
    """Acceptance 2: the original refusal, the failed path and error, and the incomplete
    warning are all shown; the INDEX row is still restored and the staging still undone."""
    _roster(repo)
    _record(repo)
    _reject_commits(repo)
    ticket = repo / "epic-tasks" / TICKET
    rows_before = (repo / "epic-tasks" / "INDEX.md").read_text(encoding="utf-8")
    _fail_ticket_restore(monkeypatch, ticket)

    assert _land(repo, capsys) == merge.EXIT_USAGE
    err = capsys.readouterr().err
    assert "no commits today" in err
    assert f"could not restore epic-tasks/{TICKET}" in err
    assert "injected restore error" in err
    assert "rollback incomplete" in err and "inspect the working tree and index" in err
    # the independent steps still ran: the row is back and the staging is gone
    assert (repo / "epic-tasks" / "INDEX.md").read_text(encoding="utf-8") == rows_before
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    # the ticket could not be restored, so it stays landed on disk
    assert "**Status:** landed" in ticket.read_text(encoding="utf-8")


def test_a_failed_unstage_is_reported_and_the_files_are_still_restored(
        repo: Path, capsys, monkeypatch) -> None:
    """Acceptance 3: the original refusal and the failed git operation with its paths are
    shown, and the file restorations still happen."""
    _roster(repo)
    _record(repo)
    _reject_commits(repo)
    ticket_before = (repo / "epic-tasks" / TICKET).read_text(encoding="utf-8")
    rows_before = (repo / "epic-tasks" / "INDEX.md").read_text(encoding="utf-8")
    _fail_unstage(monkeypatch)

    assert _land(repo, capsys) == merge.EXIT_USAGE
    err = capsys.readouterr().err
    assert "no commits today" in err
    assert "could not unstage" in err
    assert f"epic-tasks/{TICKET}" in err and "epic-tasks/INDEX.md" in err
    assert "injected reset error" in err
    assert "rollback incomplete" in err
    # the files were restored even though the unstage failed
    assert (repo / "epic-tasks" / TICKET).read_text(encoding="utf-8") == ticket_before
    assert (repo / "epic-tasks" / "INDEX.md").read_text(encoding="utf-8") == rows_before
    # the staging is still there, because the unstage is what failed
    assert _git(repo, "diff", "--cached", "--name-only") != ""


def test_both_rollback_failures_are_visible(repo: Path, capsys, monkeypatch) -> None:
    """Acceptance 4: one failed step does not hide the other."""
    _roster(repo)
    _record(repo)
    _reject_commits(repo)
    _fail_ticket_restore(monkeypatch, repo / "epic-tasks" / TICKET)
    _fail_unstage(monkeypatch)
    _write(repo / "src" / "x.py", "x = 1\n")
    _git(repo, "add", "--", "src/x.py")
    staged = _git(repo, "show", ":src/x.py")
    _write(repo / "src" / "x.py", "x = 2\n")

    assert _land(repo, capsys, passthrough=["src/x.py"]) == merge.EXIT_USAGE
    err = capsys.readouterr().err
    assert "no commits today" in err
    assert "could not restore" in err and "injected restore error" in err
    assert "could not unstage" in err and "injected reset error" in err
    assert "rollback incomplete" in err
    assert _git(repo, "show", ":src/x.py") == staged
    assert (repo / "src" / "x.py").read_text() == "x = 2\n"


@pytest.mark.parametrize("path, staged, extras", [
    ("src/x.py", "x = 1\n", ["src/x.py"]),
    ("src/x.py", "x = 1\n", ["./src/x.py", "src/x.py"]),
    ("src/x.py", "x = 1\n", ["src", "src/x.py"]),
    ("epic-tasks/INDEX.md", f"# Index\n\n{ROW_131}\n{ROW_195}\n# staged edit\n", []),
    (f"contest-bench/{NN}/new evidence.txt", "staged evidence\n", []),
])
def test_a_refused_land_preserves_the_exact_staged_and_worktree_versions(
        repo: Path, capsys, path, staged, extras) -> None:
    _write(repo / path, staged)
    _git(repo, "add", "--", path)
    working = staged + "# later edit\n"
    _write(repo / path, working)
    before = _git(repo, "diff", "--cached", "--binary")
    _reject_commits(repo)

    assert _land(repo, capsys, passthrough=extras) == merge.EXIT_USAGE
    assert "no commits today" in capsys.readouterr().err
    assert _git(repo, "diff", "--cached", "--binary") == before
    assert (repo / path).read_text() == working

    (repo / ".git/hooks/pre-commit").unlink()
    assert _land(repo, capsys, passthrough=extras) == merge.EXIT_OK
    assert "# later edit" in _git(repo, "show", f"HEAD:{path}")


@pytest.mark.parametrize("staging", ["delete", "intent"])
def test_a_refused_land_preserves_staged_deletion_and_intent_to_add(
        repo: Path, capsys, staging) -> None:
    path = "src/x.py" if staging == "delete" else "src/new.py"
    if staging == "delete":
        _git(repo, "rm", "--", path)
    _write(repo / path, "x = 2\n")
    if staging == "intent":
        _git(repo, "add", "-N", "--", path)
    before = _git(repo, "status", "--porcelain")
    _reject_commits(repo)

    assert _land(repo, capsys, passthrough=[path]) == merge.EXIT_USAGE
    assert "no commits today" in capsys.readouterr().err
    assert _git(repo, "status", "--porcelain") == before
    assert (repo / path).read_text() == "x = 2\n"


def test_snapshot_error_refuses_before_any_file_is_changed(repo: Path, capsys, monkeypatch) -> None:
    ticket = repo / "epic-tasks" / TICKET
    original = ticket.read_bytes()
    read_bytes = Path.read_bytes
    reads = 0

    def read(path):
        nonlocal reads
        if path == repo / "epic-tasks/INDEX.md":
            reads += 1
            if reads == 2:
                raise OSError(errno.EIO, "injected snapshot error")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    assert _land(repo, capsys) == merge.EXIT_USAGE
    assert "injected snapshot error" in capsys.readouterr().err
    assert ticket.read_bytes() == original
    assert _git(repo, "status", "--porcelain") == ""


def test_partial_initial_write_restores_only_the_file_it_touched(
        repo: Path, capsys, monkeypatch) -> None:
    ticket = repo / "epic-tasks" / TICKET
    original = ticket.read_bytes()
    write_text, write_bytes = Path.write_text, Path.write_bytes

    def write(path, *args, **kwargs):
        if path == ticket:
            write_bytes(path, b"partial")
            raise OSError(errno.EIO, "injected initial write error")
        return write_text(path, *args, **kwargs)

    def restore(path, data):
        assert path == ticket, "rollback touched a file that the command had not written"
        return write_bytes(path, data)

    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr(Path, "write_bytes", restore)
    assert _land(repo, capsys) == merge.EXIT_USAGE
    assert "injected initial write error" in capsys.readouterr().err
    assert ticket.read_bytes() == original
    assert _git(repo, "status", "--porcelain") == ""
