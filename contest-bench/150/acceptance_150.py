"""Judge's acceptance suite for round 150 (AR-14), written from the ticket alone.

`arena issue queue | open | close | reopen | edit`, on the checked-out branch and on another
branch without a checkout, the refusals of the ticket's situation tables, `closed` as a parked
status, `run start` naming the ticket that blocks intake, and the flow block + hints every
refusal carries. Everything goes through `tools.arena.cli.main` on a throw-away git repository
on branch `arena` (`REPO_ROOT` patched in every `tools.arena` module). Nothing dials a model or
starts a runner: the one `run start` case is a refusal that must build nothing.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/150/acceptance_150.py -n 0 -q
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.arena import cli  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
OPERATOR = "op@bench"

TICKET = """# AR-{nn} — a bench ticket

{status_line}{closed_line}**Severity:** LOW
**File:** pkg/a.py
**Symbol:** a
**Round:** {nn}
**Size:** S
**Also touches:** -

## Why

Because.

## What to build

A thing.

## Acceptance

```bash
python3 -m pytest tests -q
```
"""


def ticket_text(nn: int, status: str | None = "open", closed: str | None = None) -> str:
    return TICKET.format(
        nn=nn,
        status_line="" if status is None else f"**Status:** {status}\n",
        closed_line="" if closed is None else f"**Closed:** {closed}\n")


def tname(nn: int) -> str:
    return f"{nn:02d}-bench-ticket.md"


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, env=ENV, capture_output=True, text=True)
    if check and proc.returncode:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


def put(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def commit_all(repo: Path, msg: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", msg)


def count(repo: Path, rev: str = "HEAD") -> int:
    return int(git(repo, "rev-list", "--count", rev))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "arena")
    git(r, "config", "user.name", "Bench Operator")
    git(r, "config", "user.email", OPERATOR)
    put(r, "contest.ini", "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = p/m-a\n")
    put(r, "pkg/a.py", "def a():\n    return 1\n")
    (r / "scripts").mkdir()
    for name in ("next_task.py", "append_task.py"):
        shutil.copy(ROOT / "scripts" / name, r / "scripts" / name)
    put(r, "epic-tasks/" + tname(7), ticket_text(7, "open"))
    commit_all(r, "base")
    for name in list(os.environ):
        if name.startswith(("GIT_AUTHOR", "GIT_COMMITTER")):
            monkeypatch.delenv(name)
    for modname, mod in list(sys.modules.items()):
        if modname.startswith("tools.arena") and isinstance(getattr(mod, "REPO_ROOT", None), Path):
            monkeypatch.setattr(mod, "REPO_ROOT", r)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return r


def run(capsys, *argv):
    try:
        rc = cli.main(list(argv))
    except SystemExit as e:
        rc = e.code
    c = capsys.readouterr()
    return rc, c.out, c.err


def status_of(text: str) -> str:
    m = re.search(r"^\*\*Status:\*\*\s*(\S+)", text, re.M)
    return m.group(1).lower() if m else ""


def read_ticket(repo: Path, nn: int = 7, branch: str | None = None) -> str:
    if branch is None:
        return (repo / "epic-tasks" / tname(nn)).read_text(encoding="utf-8")
    return git(repo, "show", f"{branch}:epic-tasks/{tname(nn)}") + "\n"


def set_ticket(repo: Path, nn: int, status: str | None, closed: str | None = None,
               msg: str = "ticket") -> None:
    put(repo, "epic-tasks/" + tname(nn), ticket_text(nn, status, closed))
    commit_all(repo, msg)


VERBS = [("queue", "open", "queued"), ("open", "queued", "open"),
         ("close", "open", "closed"), ("close", "queued", "closed"),
         ("reopen", "closed", "open")]


def verb_args(verb: str, branch: str = "arena") -> list[str]:
    extra = ["--reason", "no longer wanted"] if verb == "close" else []
    return ["-y", "issue", verb, "7", "--branch", branch, *extra]


# ── 1. each verb, on the checked-out branch ───────────────────────────────────

@pytest.mark.parametrize("verb,before,after", VERBS)
def test_a_verb_on_the_checked_out_branch_is_one_commit_with_the_right_status(
        repo, capsys, verb, before, after):
    set_ticket(repo, 7, before, closed="old reason" if before == "closed" else None)
    old, n = read_ticket(repo), count(repo)
    rc, out, err = run(capsys, *verb_args(verb))
    assert rc == 0, (out, err)
    assert count(repo) == n + 1
    new = read_ticket(repo)
    assert status_of(new) == after
    if after == "closed":
        assert re.search(r"^\*\*Closed:\*\*.*no longer wanted", new, re.M)
    else:
        assert "**Closed:**" not in new
    keep = lambda t: [ln for ln in t.splitlines()                     # noqa: E731
                      if not ln.startswith(("**Status:**", "**Closed:**"))]
    assert keep(new) == keep(old), "the rest of the file is byte for byte the same"
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("verb,before,after", VERBS)
def test_the_commit_names_the_ticket_and_the_operator_and_nothing_is_pushed(
        repo, capsys, verb, before, after):
    set_ticket(repo, 7, before, closed="old reason" if before == "closed" else None)
    rc, out, err = run(capsys, *verb_args(verb))
    assert rc == 0, (out, err)
    subject = git(repo, "log", "-1", "--format=%s")
    assert re.match(r"^0*7[:\s]", subject), subject
    assert after in subject
    assert git(repo, "log", "-1", "--format=%ae") == OPERATOR
    assert "co-authored-by" not in git(repo, "log", "-1", "--format=%B").lower()
    assert "git push" in out + err, "the push command is printed, never run"
    assert git(repo, "remote") == ""


def test_the_success_line_in_json_carries_the_ticket_the_branch_the_states_and_the_commit(
        repo, capsys):
    rc, out, err = run(capsys, "-o", "json", "-y", "issue", "queue", "7", "--branch", "arena")
    assert rc == 0, (out, err)
    obj = json.loads(out.strip().splitlines()[-1])
    assert {"ticket", "branch", "from", "to", "commit"} <= set(obj), obj
    assert str(obj["ticket"]).lstrip("0") == "7" and obj["branch"] == "arena"
    assert obj["from"] == "open" and obj["to"] == "queued"
    assert git(repo, "rev-parse", "HEAD").startswith(str(obj["commit"])[:7])


def test_a_note_follows_the_status_word_and_the_state_is_still_the_word(repo, capsys):
    rc, out, err = run(capsys, "-y", "issue", "queue", "7", "--branch", "arena",
                       "--note", "judged on arena")
    assert rc == 0, (out, err)
    line = re.search(r"^\*\*Status:\*\*.*$", read_ticket(repo), re.M).group(0)
    assert "queued" in line and "judged on arena" in line
    rc, out, err = run(capsys, "issue", "list", "--branch", "arena")
    assert rc == 0 and re.search(r"\b7\b.*\bqueued\b", out), out


# ── 2. on a branch that is not checked out ───────────────────────────────────

@pytest.mark.parametrize("verb,before,after", VERBS)
def test_a_branch_that_is_not_checked_out_gets_one_commit_and_no_checkout_moves(
        repo, capsys, verb, before, after):
    set_ticket(repo, 7, before, closed="old reason" if before == "closed" else None)
    git(repo, "branch", "fix")
    head, arena_n, fix_n = git(repo, "rev-parse", "HEAD"), count(repo, "arena"), count(repo, "fix")
    rc, out, err = run(capsys, *verb_args(verb, "fix"))
    assert rc == 0, (out, err)
    assert count(repo, "fix") == fix_n + 1
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "symbolic-ref", "--short", "HEAD") == "arena"
    assert git(repo, "status", "--porcelain") == "" and count(repo, "arena") == arena_n
    assert git(repo, "diff", "--cached", "--name-only") == ""
    assert status_of(read_ticket(repo, branch="fix")) == after
    assert status_of(read_ticket(repo)) == before, "the checked-out file is untouched"


def test_a_branch_checked_out_in_another_worktree_is_refused_naming_it(repo, capsys, tmp_path):
    git(repo, "branch", "fix")
    other = tmp_path / "other-checkout"
    git(repo, "worktree", "add", "-q", str(other), "fix")
    before = git(repo, "rev-parse", "fix")
    rc, out, err = run(capsys, *verb_args("queue", "fix"))
    assert rc not in (0, None)
    assert str(other) in err + out
    assert git(repo, "rev-parse", "fix") == before


# ── 3. the refusals ──────────────────────────────────────────────────────────

def refused(repo, capsys, *argv):
    n = count(repo)
    rc, out, err = run(capsys, *argv)
    assert rc not in (0, None), (out, err)
    assert count(repo) == n, "a refusal commits nothing"
    assert err.strip(), "a refusal says why on stderr"
    return out, err


@pytest.mark.parametrize("verb", ["queue", "open", "close", "reopen"])
def test_a_landed_ticket_refuses_every_verb_naming_issue_land(repo, capsys, verb):
    set_ticket(repo, 7, "landed")
    out, err = refused(repo, capsys, *verb_args(verb))
    assert "land" in err.lower()


def test_close_without_a_reason_is_refused(repo, capsys):
    refused(repo, capsys, "-y", "issue", "close", "7", "--branch", "arena")


def test_reopen_of_a_ticket_that_is_not_closed_is_refused(repo, capsys):
    set_ticket(repo, 7, "queued")      # `open` would be "already the status" (§6), exit 0
    refused(repo, capsys, *verb_args("reopen"))


def test_an_unknown_ticket_number_is_refused(repo, capsys):
    refused(repo, capsys, "-y", "issue", "queue", "99", "--branch", "arena")


def test_a_verb_to_the_status_the_ticket_has_commits_nothing_and_says_so(repo, capsys):
    set_ticket(repo, 7, "queued")
    n = count(repo)
    rc, out, err = run(capsys, *verb_args("queue"))
    assert rc == 0, (out, err)
    assert count(repo) == n
    assert "already queued" in (out + err).lower()


def test_a_status_word_that_is_none_of_the_four_is_refused_naming_the_word(repo, capsys):
    set_ticket(repo, 7, "fixed")
    out, err = refused(repo, capsys, *verb_args("queue"))
    assert "fixed" in err


def test_a_ticket_with_no_status_line_is_refused(repo, capsys):
    set_ticket(repo, 7, None)
    refused(repo, capsys, *verb_args("queue"))


def test_a_ticket_only_in_the_drafts_folder_is_refused(repo, capsys):
    put(repo, ".arena/drafts/" + tname(8), ticket_text(8, "open"))
    refused(repo, capsys, "-y", "issue", "queue", "8", "--branch", "arena")


def test_a_ticket_in_the_checkout_but_not_committed_on_the_branch_is_refused(repo, capsys):
    put(repo, "epic-tasks/" + tname(8), ticket_text(8, "open"))
    refused(repo, capsys, "-y", "issue", "queue", "8", "--branch", "arena")
    assert status_of(read_ticket(repo, 8)) == "open"


def test_uncommitted_edits_in_the_ticket_file_are_never_swept_into_the_commit(repo, capsys):
    path = repo / "epic-tasks" / tname(7)
    edited = path.read_text(encoding="utf-8") + "\nAn operator's unsaved thought.\n"
    path.write_text(edited, encoding="utf-8")
    refused(repo, capsys, *verb_args("queue"))
    assert path.read_text(encoding="utf-8") == edited


def test_a_branch_that_exists_only_on_origin_is_refused_naming_origin(repo, capsys, tmp_path):
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "branch", "fix")
    git(repo, "push", "-q", "origin", "fix")
    git(repo, "branch", "-D", "fix")
    git(repo, "fetch", "-q", "origin")
    before = git(repo, "rev-parse", "origin/fix")
    out, err = refused(repo, capsys, *verb_args("queue", "fix"))
    assert "origin" in err
    assert git(repo, "rev-parse", "origin/fix") == before


def test_a_detached_head_with_no_branch_named_is_refused(repo, capsys):
    git(repo, "checkout", "-q", "--detach")
    refused(repo, capsys, "-y", "issue", "queue", "7")


def test_other_staged_files_stay_out_of_the_commit_and_stay_staged(repo, capsys):
    put(repo, "notes.txt", "keep me out\n")
    git(repo, "add", "notes.txt")
    rc, out, err = run(capsys, *verb_args("queue"))
    assert rc == 0, (out, err)
    assert git(repo, "show", "--name-only", "--format=", "HEAD").split() == [f"epic-tasks/{tname(7)}"]
    assert git(repo, "diff", "--cached", "--name-only").split() == ["notes.txt"]


# ── 4. `closed` is parked ────────────────────────────────────────────────────

def run_next_task(repo: Path, tasks: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(repo / "scripts" / "next_task.py"),
                           "--tasks", str(tasks)], cwd=repo, env=ENV, capture_output=True, text=True)


@pytest.mark.parametrize("first", ["closed", "closed", "queued (judged on arena)", "landed"])
def test_next_task_does_not_offer_a_parked_ticket_and_hands_out_the_next_open_one(
        repo, tmp_path, first):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "01-first.md").write_text(ticket_text(1, first), encoding="utf-8")
    (tasks / "02-second.md").write_text(ticket_text(2, "open"), encoding="utf-8")
    proc = run_next_task(repo, tasks)
    assert proc.returncode == 0, proc.stderr
    assert "02-second.md" in proc.stdout and "01-first.md" not in proc.stdout


def test_next_task_finds_the_folder_finished_when_every_ticket_is_closed(repo, tmp_path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "01-first.md").write_text(ticket_text(1, "closed"), encoding="utf-8")
    assert run_next_task(repo, tasks).returncode == 3


def test_issue_list_shows_a_closed_ticket_as_closed(repo, capsys):
    rc, out, err = run(capsys, *verb_args("close"))
    assert rc == 0, (out, err)
    rc, out, err = run(capsys, "issue", "list", "--branch", "arena")
    assert rc == 0 and re.search(r"\b7\b.*\bclosed\b", out), (out, err)


# ── 5. `run start` names what blocks it ──────────────────────────────────────

def test_run_start_names_the_lower_open_tickets_and_builds_nothing(repo, capsys):
    set_ticket(repo, 5, "open")
    set_ticket(repo, 6, "open")
    n = count(repo)
    rc, out, err = run(capsys, "-y", "run", "start", "6", "--branch", "arena")
    assert rc not in (0, None), (out, err)
    assert git(repo, "branch", "--list", "arena-round/6") == "", "nothing is built"
    assert count(repo) == n
    assert re.search(r"\b0?5\b", err), err
    assert re.search(r"issue queue 0?5\b", err), err
    assert re.search(r"issue close 0?5\b", err), err
    assert "arena" in err


def test_a_higher_open_ticket_does_not_block(repo, capsys):
    set_ticket(repo, 9, "open")
    rc, out, err = run(capsys, "-y", "run", "start", "7", "--branch", "arena")
    assert not re.search(r"issue (queue|close) 0?9\b", err), err


# ── 6. edit ──────────────────────────────────────────────────────────────────

def kept_file(err: str) -> Path:
    """The temp file a refused `edit` names: an absolute path ending in `.md`, outside the repo."""
    found = re.findall(r"(/[^\s'\"]+\.md)\b", err)
    assert found, err
    return Path(found[-1])


def editor(tmp_path: Path, monkeypatch, body: str) -> None:
    script = tmp_path / "ed.sh"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(script))
    monkeypatch.setenv("VISUAL", str(script))


def test_edit_commits_what_the_editor_saved(repo, capsys, monkeypatch, tmp_path):
    editor(tmp_path, monkeypatch, 'printf "\\nOne more line.\\n" >> "$1"')
    n = count(repo)
    rc, out, err = run(capsys, "-y", "issue", "edit", "7", "--branch", "arena")
    assert rc == 0, (out, err)
    assert count(repo) == n + 1
    assert "One more line." in read_ticket(repo)
    assert status_of(read_ticket(repo)) == "open"


def test_edit_that_changes_the_status_line_is_refused_and_commits_nothing(
        repo, capsys, monkeypatch, tmp_path):
    editor(tmp_path, monkeypatch, 'sed -i "s/^\\*\\*Status:\\*\\* open/**Status:** closed/" "$1"')
    n = count(repo)
    rc, out, err = run(capsys, "-y", "issue", "edit", "7", "--branch", "arena")
    assert rc not in (0, None), (out, err)
    assert count(repo) == n
    assert status_of(read_ticket(repo)) == "open"
    assert re.search(r"issue (queue|open|close|reopen)", err), err
    kept = kept_file(err)
    assert kept.is_file() and "**Status:** closed" in kept.read_text(encoding="utf-8"), \
        f"the rest of the edit is kept in a temp file whose path is printed: {err}"


# ── 7. every refusal carries where / ticket / flow and a hint ────────────────

def refusal_cases(repo: Path):
    """(name, argv, prepare) — refusals that must all carry the flow block and a hint."""
    return [
        ("landed", verb_args("queue"), lambda: set_ticket(repo, 7, "landed")),
        ("no-reason", ["-y", "issue", "close", "7", "--branch", "arena"], lambda: None),
        ("unknown-nn", ["-y", "issue", "queue", "99", "--branch", "arena"], lambda: None),
        ("unknown-status", verb_args("queue"), lambda: set_ticket(repo, 7, "fixed")),
        ("not-closed", verb_args("reopen"), lambda: set_ticket(repo, 7, "queued")),
    ]


@pytest.mark.parametrize("case", ["landed", "no-reason", "unknown-nn", "unknown-status", "not-closed"])
def test_every_refusal_prints_where_ticket_flow_and_a_pasteable_hint(repo, capsys, case):
    name, argv, prepare = next(c for c in refusal_cases(repo) if c[0] == case)
    prepare()
    rc, out, err = run(capsys, *argv)
    assert rc not in (0, None)
    text = err
    assert re.search(r"^\s*where:", text, re.M), text
    assert re.search(r"^\s*ticket:", text, re.M), text
    flow = re.search(r"^\s*flow:.*?(?=^\s*→ |\Z)", text, re.M | re.S)   # the block, wrapped or not
    assert flow, text
    assert flow.group(0).count("[✗]") == 1, flow.group(0)
    assert re.search(r"^\s*→ \S", text, re.M), text
    assert not re.search(r"<[A-Za-z][A-Za-z _-]*>", text), "no <placeholder> is left in a hint"


@pytest.mark.parametrize("case", ["landed", "unknown-nn", "no-reason"])
def test_a_refusal_in_json_carries_where_ticket_flow_and_hints(repo, capsys, case):
    name, argv, prepare = next(c for c in refusal_cases(repo) if c[0] == case)
    prepare()
    rc, out, err = run(capsys, "-o", "json", *argv)
    assert rc not in (0, None)
    objs = []
    for line in (out + "\n" + err).splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                objs.append(json.loads(line))
            except ValueError:
                pass
    obj = next((o for o in objs if "hints" in o), None)
    assert obj is not None, (out, err)
    assert {"where", "ticket", "flow", "hints"} <= set(obj), obj
    assert obj["hints"] and all({"why", "command"} <= set(h) for h in obj["hints"]), obj["hints"]
    assert all(isinstance(s, dict) and {"step", "state"} <= set(s) for s in obj["flow"]), obj["flow"]


# ═══ layer 2 — the rows of the ticket's situation tables (§1, §2, §5, §6, §7) ═══════════════

def round_state(repo: Path, nn: int = 7) -> None:
    """A round that ran: `out/NN/state.json` (the fixture's `out_dir`)."""
    put(repo, f"out/{nn}/state.json", '{"agents": []}')


@pytest.fixture
def live_runner(repo):
    """A process that looks like `python -m tools.contest run --ticket 7`, with the repo as cwd."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                             "tools.contest", "run", "--ticket", "7"], cwd=repo)
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("verb", ["queue", "close"])
def test_a_running_round_refuses_the_verb_naming_it(repo, capsys, live_runner, verb):
    round_state(repo)
    out, err = refused(repo, capsys, *verb_args(verb))
    assert "running" in err.lower()


def test_a_done_round_is_allowed_a_status_change(repo, capsys):
    round_state(repo)
    rc, out, err = run(capsys, *verb_args("queue"))
    assert rc == 0, (out, err)
    assert status_of(read_ticket(repo)) == "queued"


def test_two_files_with_one_number_on_the_branch_are_refused_naming_both(repo, capsys):
    put(repo, "epic-tasks/07-other-ticket.md", ticket_text(7, "open"))
    commit_all(repo, "a second 7")
    out, err = refused(repo, capsys, *verb_args("queue"))
    assert tname(7) in err and "07-other-ticket.md" in err


def test_a_rejected_copy_of_the_ticket_is_ignored(repo, capsys):
    put(repo, "epic-tasks/07-bench-ticket.rejected.md", ticket_text(7, "open"))
    commit_all(repo, "a rejected 7")
    rc, out, err = run(capsys, *verb_args("queue"))
    assert rc == 0, (out, err)
    assert status_of(read_ticket(repo)) == "queued"


def test_the_closed_line_sits_directly_under_the_status_line(repo, capsys):
    rc, out, err = run(capsys, *verb_args("close"))
    assert rc == 0, (out, err)
    lines = read_ticket(repo).splitlines()
    at = next(i for i, ln in enumerate(lines) if ln.startswith("**Status:**"))
    assert lines[at].split()[1] == "closed"
    assert lines[at + 1].startswith("**Closed:**") and "no longer wanted" in lines[at + 1]


def test_queue_or_open_on_a_closed_ticket_is_refused_pointing_at_reopen(repo, capsys):
    set_ticket(repo, 7, "closed", closed="old reason")
    for verb in ("queue", "open"):
        out, err = refused(repo, capsys, *verb_args(verb))
        assert re.search(r"issue reopen 0?7", err), err


def test_issue_list_shows_a_closed_ticket_with_its_reason(repo, capsys):
    run(capsys, *verb_args("close"))
    rc, out, err = run(capsys, "issue", "list", "--branch", "arena")
    assert rc == 0 and "no longer wanted" in out, (out, err)


def test_the_profiles_branch_is_the_default_branch(repo, capsys):
    git(repo, "branch", "fix")
    put(repo, "contest.local.ini", "[arena]\nprofile = p1\n\n[arena.profile.p1]\nbranch = fix\n"
                                    "models = p/m-a\n")
    rc, out, err = run(capsys, "-y", "issue", "queue", "7")
    assert rc == 0, (out, err)
    assert status_of(read_ticket(repo, branch="fix")) == "queued"
    assert status_of(read_ticket(repo)) == "open"


def test_edit_on_a_branch_that_is_not_checked_out_leaves_every_checkout_alone(
        repo, capsys, monkeypatch, tmp_path):
    git(repo, "branch", "fix")
    editor(tmp_path, monkeypatch, 'printf "\\nEdited on fix.\\n" >> "$1"')
    head, n = git(repo, "rev-parse", "HEAD"), count(repo, "fix")
    rc, out, err = run(capsys, "-y", "issue", "edit", "7", "--branch", "fix")
    assert rc == 0, (out, err)
    assert count(repo, "fix") == n + 1 and "Edited on fix." in read_ticket(repo, branch="fix")
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "status", "--porcelain") == ""
    assert "Edited on fix." not in read_ticket(repo)


def test_edit_that_fails_the_header_lint_is_refused_and_names_the_problem_and_a_temp_file(
        repo, capsys, monkeypatch, tmp_path):
    editor(tmp_path, monkeypatch, 'sed -i "/^\\*\\*Severity:\\*\\*/d" "$1"')
    n = count(repo)
    rc, out, err = run(capsys, "-y", "issue", "edit", "7", "--branch", "arena")
    assert rc not in (0, None), (out, err)
    assert count(repo) == n and "**Severity:**" in read_ticket(repo)
    assert "Severity" in err, err
    kept = kept_file(err)
    assert kept.is_file() and "**Severity:**" not in kept.read_text(encoding="utf-8"), \
        f"the edited text is kept in a temp file whose path is printed: {err}"


def test_run_start_names_a_ticket_with_an_unknown_status_word_and_points_at_edit(repo, capsys):
    set_ticket(repo, 5, "fixed")
    set_ticket(repo, 6, "open")
    n = count(repo)
    rc, out, err = run(capsys, "-y", "run", "start", "6", "--branch", "arena")
    assert rc not in (0, None), (out, err)
    assert git(repo, "branch", "--list", "arena-round/6") == "" and count(repo) == n
    assert re.search(r"issue edit 0?5\b", err), err


def test_the_other_worktree_refusal_gives_a_cd_and_the_same_verb(repo, capsys, tmp_path):
    git(repo, "branch", "fix")
    other = tmp_path / "other-checkout"
    git(repo, "worktree", "add", "-q", str(other), "fix")
    rc, out, err = run(capsys, *verb_args("queue", "fix"))
    assert rc not in (0, None)
    assert re.search(rf"cd {re.escape(str(other))}\b.*issue queue 0?7", err), err


def test_the_where_block_names_this_checkout_and_the_branch(repo, capsys):
    set_ticket(repo, 7, "landed")
    rc, out, err = run(capsys, *verb_args("queue"))
    where = re.search(r"^\s*where:.*?(?=^\s*ticket:)", err, re.M | re.S)
    assert where, err
    assert str(repo) in where.group(0) and "arena" in where.group(0), where.group(0)


@pytest.mark.parametrize("case,expect", [("landed", 0), ("unknown-nn", 0)])
def test_the_first_hint_of_a_read_only_refusal_runs_as_printed(repo, capsys, case, expect):
    import shlex
    name, argv, prepare = next(c for c in refusal_cases(repo) if c[0] == case)
    prepare()
    rc, out, err = run(capsys, *argv)
    assert rc not in (0, None)
    hint = re.search(r"^\s*→ [^\n]*?:\s+(arena [^\n]+)$", err, re.M)
    assert hint, err
    words = shlex.split(hint.group(1))[1:]
    rc2, out2, err2 = run(capsys, *words)
    assert rc2 == expect, (hint.group(1), out2, err2)


# ═══ layer 3 — the hint table of §7, row by row (the commands each refusal must offer) ═══════

def prep_uncommitted_edit(repo, tmp_path):
    p = repo / "epic-tasks" / tname(7)
    p.write_text(p.read_text(encoding="utf-8") + "\nunsaved thought\n", encoding="utf-8")
    return verb_args("queue")


def prep_origin_only(repo, tmp_path):
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "branch", "fix")
    git(repo, "push", "-q", "origin", "fix")
    git(repo, "branch", "-D", "fix")
    git(repo, "fetch", "-q", "origin")
    return verb_args("queue", "fix")


def prep_draft_only(repo, tmp_path):
    put(repo, ".arena/drafts/" + tname(8), ticket_text(8, "open"))
    return ["-y", "issue", "queue", "8", "--branch", "arena"]


def prep_not_committed(repo, tmp_path):
    put(repo, "epic-tasks/" + tname(8), ticket_text(8, "open"))
    return ["-y", "issue", "queue", "8", "--branch", "arena"]


def prep_two_files(repo, tmp_path):
    put(repo, "epic-tasks/07-other-ticket.md", ticket_text(7, "open"))
    commit_all(repo, "a second 7")
    return verb_args("queue")


def prep_no_status(repo, tmp_path):
    set_ticket(repo, 7, None)
    return verb_args("queue")


def prep_unknown_word(repo, tmp_path):
    set_ticket(repo, 7, "fixed")
    return verb_args("queue")


def prep_landed(repo, tmp_path):
    set_ticket(repo, 7, "landed")
    return verb_args("queue")


def prep_no_reason(repo, tmp_path):
    return ["-y", "issue", "close", "7", "--branch", "arena"]


def prep_reopen_queued(repo, tmp_path):
    set_ticket(repo, 7, "queued")
    return verb_args("reopen")


def prep_missing_branch(repo, tmp_path):
    return verb_args("queue", "fxi")


def prep_running(repo, tmp_path):
    round_state(repo)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                             "tools.contest", "run", "--ticket", "7"], cwd=repo)
    PROCS.append(proc)
    return verb_args("queue")


PROCS: list = []

#: row → (how to get the refusal, [the substrings each of which some hint line must carry])
HINT_ROWS = {
    "uncommitted-edit": (prep_uncommitted_edit, [r"\bdiff\b", r"commit --only", r"\brestore\b",
                                                 r"issue queue 0?7"]),
    "origin-only": (prep_origin_only, [r"git branch fix origin/fix", r"issue queue 0?7"]),
    "draft-only": (prep_draft_only, [r"issue create --file \.arena/drafts/"]),
    "not-committed": (prep_not_committed, [r"commit --only", r"epic-tasks/08-bench-ticket\.md",
                                           r"issue queue 0?8"]),
    "two-files": (prep_two_files, [r"git (-C \S+ )?log -1 .*07-bench-ticket\.md",
                                   r"git (-C \S+ )?log -1 .*07-other-ticket\.md",
                                   r"git (-C \S+ )?rm .*07-bench-ticket\.md",
                                   r"git (-C \S+ )?rm .*07-other-ticket\.md"]),
    "no-status": (prep_no_status, [r"issue edit 0?7"]),
    "unknown-word": (prep_unknown_word, [r"issue edit 0?7"]),
    "landed": (prep_landed, [r"issue view 0?7", r"issue land 0?7"]),
    "no-reason": (prep_no_reason, [r"issue close 0?7 .*--reason"]),
    "reopen-of-queued": (prep_reopen_queued, [r"issue open 0?7"]),
    "missing-branch": (prep_missing_branch, [r"git branch --list '\*fxi\*'"]),
    "running": (prep_running, [r"status 0?7", r"run stop 0?7"]),
}


@pytest.mark.parametrize("row", list(HINT_ROWS))
def test_the_hint_table_row_offers_its_commands(repo, capsys, tmp_path, row):
    prep, wanted = HINT_ROWS[row]
    try:
        argv = prep(repo, tmp_path)
        rc, out, err = run(capsys, *argv)
    finally:
        for proc in PROCS:
            proc.kill()
            proc.wait()
        PROCS.clear()
    assert rc not in (0, None), (out, err)
    hints = re.findall(r"^\s*→ .*$", err, re.M)
    assert hints, err
    for pattern in wanted:
        assert any(re.search(pattern, h) for h in hints), (pattern, hints)


def test_an_unknown_status_word_lists_the_four_allowed_words(repo, capsys):
    set_ticket(repo, 7, "fixed")
    out, err = refused(repo, capsys, *verb_args("queue"))
    for word in ("open", "queued", "closed", "landed"):
        assert word in err, err


def test_a_close_hint_keeps_the_real_branch_and_number_and_leaves_only_the_reason_to_write(
        repo, capsys):
    rc, out, err = run(capsys, "-y", "issue", "close", "7", "--branch", "arena")
    hint = next(h for h in re.findall(r"^\s*→ .*$", err, re.M) if "--reason" in h)
    assert re.search(r"issue close 0?7 --branch arena", hint), hint
    assert len(re.findall(r"<[^>]+>", hint)) == 0


# ═══ layer 4 — what §6 sends the operator to `edit` for ═══════════════════════════════════════

def test_edit_may_fix_an_unknown_status_word(repo, capsys, monkeypatch, tmp_path):
    """§6: an unknown word refuses every verb *but edit* — edit is the way out."""
    set_ticket(repo, 7, "fixed")
    editor(tmp_path, monkeypatch, 'sed -i "s/^\\*\\*Status:\\*\\* fixed/**Status:** open/" "$1"')
    n = count(repo)
    rc, out, err = run(capsys, "-y", "issue", "edit", "7", "--branch", "arena")
    assert rc == 0, (out, err)
    assert count(repo) == n + 1 and status_of(read_ticket(repo)) == "open"


def test_edit_may_add_the_missing_status_line(repo, capsys, monkeypatch, tmp_path):
    """§6: no `**Status:**` line — the operator adds it with `issue edit`."""
    set_ticket(repo, 7, None)
    editor(tmp_path, monkeypatch,
           'sed -i "s/^\\*\\*Severity:\\*\\*/**Status:** open\\n**Severity:**/" "$1"')
    n = count(repo)
    rc, out, err = run(capsys, "-y", "issue", "edit", "7", "--branch", "arena")
    assert rc == 0, (out, err)
    assert count(repo) == n + 1 and status_of(read_ticket(repo)) == "open"


def test_edit_that_leaves_the_ticket_as_it_was_commits_nothing(repo, capsys, monkeypatch, tmp_path):
    editor(tmp_path, monkeypatch, "true")
    n = count(repo)
    rc, out, err = run(capsys, "-y", "issue", "edit", "7", "--branch", "arena")
    assert rc == 0, (out, err)
    assert count(repo) == n
