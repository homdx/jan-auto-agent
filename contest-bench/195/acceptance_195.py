"""Judge's acceptance suite for round 195 (AR-8), written from the ticket alone.

`arena entry merge NN AGENT [--uncommitted] [--squash]` (commit 1: the winner as-is) and
`arena issue land NN [-m SUBJECT] [--score TEXT] [--note TEXT] [-- PATH...]` (commit 2: the
ticket, the INDEX row, the bench). Real git repositories in `tmp_path`, everything through
`tools.arena.cli.main` with `REPO_ROOT` patched in every `tools.arena` module. The assertions
name behaviour (exit codes, commits, files, the trailer and status line the ticket spells out),
not the wording of a refusal.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/195/acceptance_195.py -n 0 -q
"""

from __future__ import annotations

import json
import os
import re
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
NN = 7
TNAME = "07-bench-ticket.md"
TICKET = ("# AR-8 — a bench ticket\n\n**Status:** queued\n**Severity:** LOW\n**File:** pkg/a.py\n"
          "**Round:** 7\n**Size:** S\n\n## Why\n\nBecause.\n\n    indented  line  \n\nend\n")
INDEX = ("# Index\n\n| 3 | `AR-1` | open — old | LOW | S | [t](x) | `a.py` |\n"
         "| 7 | `AR-8` | queued | LOW | S | [t](x) | `pkg/a.py` |\n")


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
    put(r, ".gitignore", "__pycache__/\n.arena/\nout/\n")
    put(r, "pkg/a.py", "def a():\n    return 1\n")
    put(r, "pkg/old.py", "OLD = 1\n")
    put(r, "pkg/b.py", "B = 1\n")
    put(r, f"epic-tasks/{TNAME}", TICKET)
    put(r, "epic-tasks/INDEX.md", INDEX)
    put(r, ".arena/rounds/7.json", json.dumps({"branch": "arena"}))
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


def make_patch(repo: Path, agent: str, edits: dict, *, message=("agent subject", ""),
               author="Far Away <far@away.example>", commits=1, state="", ext="patch",
               model="kenary/glm-4.7:free", entry: dict | None = None):
    """Make the round's entry for *agent*: `edits` committed in a clone and exported, the roster
    written. Returns the base sha."""
    base = git(repo, "rev-parse", "HEAD")
    clone = repo.parent / f"clone-{agent}-{len(list(repo.parent.iterdir()))}"
    git(repo, "clone", "-q", str(repo), str(clone))
    git(clone, "config", "user.name", "Far")
    git(clone, "config", "user.email", "far@away.example")
    out = repo / "out" / f"{NN:02d}"
    out.mkdir(parents=True, exist_ok=True)
    if entry is not None:
        text = ""
    elif ext == "diff":
        for rel, text in edits.items():
            put(clone, rel, text)
        text = git(clone, "diff", base) + "\n"
    else:
        for i in range(commits):
            for rel, text in edits.items():
                if text is None:
                    git(clone, "rm", "-q", rel)
                elif isinstance(text, tuple):  # rename: (new path, new text)
                    git(clone, "mv", rel, text[0])
                    put(clone, text[0], text[1])
                else:
                    put(clone, rel, text + (f"# {i}\n" if commits > 1 else ""))
            git(clone, "add", "-A")
            subj, body = message
            args = ["commit", "-q", "--author", author, "-m",
                    subj + (f" ({i + 1})" if commits > 1 else "")]
            if body:
                args += ["-m", body]
            git(clone, *args)
        text = git(clone, "format-patch", f"-{commits}", "--stdout") + "\n"
    name = f"{agent}.{state}.{ext}" if state else f"{agent}.{ext}"
    if entry is None:
        (out / name).write_text(text, encoding="utf-8")
    roster = json.loads((out / "entrants.json").read_text())["entrants"] \
        if (out / "entrants.json").exists() else {}
    agents = json.loads((out / "state.json").read_text())["agents"] \
        if (out / "state.json").exists() else []
    item = {"source": f"out/{NN:02d}/{name}"}
    if state:
        item["state"] = state
    roster[agent] = entry if entry is not None else item
    agents.append({"agent": {"name": agent, "model_id": model, "provider_id": "kenary"},
                   "workspace": {"path": "/x"}, "state": state or "READY"})
    (out / "entrants.json").write_text(json.dumps({"base": base, "entrants": roster}))
    (out / "state.json").write_text(json.dumps(
        {"round_no": NN, "ticket": f"epic-tasks/{TNAME}", "base_sha": base, "agents": agents}))
    return base


def merge(capsys, agent, *flags, yes=False):
    return run(capsys, *(["-y"] if yes else []), "entry", "merge", str(NN), agent, *flags)


def land(capsys, *flags, paths=()):
    return run(capsys, "issue", "land", str(NN), *flags, *(["--", *paths] if paths else []))


def files_of(repo: Path, rev="HEAD") -> list[str]:
    return sorted(git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r",
                      "--no-renames", rev).splitlines())


# ── 1. entry merge: one commit, the checkout's identity, the trailer, the subject ─────────────

def test_a_foreign_patch_is_one_commit_authored_by_the_checkout(repo, capsys):
    base = make_patch(repo, "agent", {"pkg/a.py": "def a():\n    return 2\n"},
                      message=("make a return 2", "the body\nof it"))
    n = count(repo)
    rc, out, err = merge(capsys, "agent")
    assert rc == 0, (out, err)
    assert count(repo) == n + 1
    assert git(repo, "log", "-1", "--format=%ae") == OPERATOR
    assert git(repo, "log", "-1", "--format=%an") == "Bench Operator"
    assert (repo / "pkg/a.py").read_text() == "def a():\n    return 2\n"
    msg = git(repo, "log", "-1", "--format=%B")
    assert msg.splitlines()[0] == "7: make a return 2"
    assert "the body\nof it" in msg
    assert msg.rstrip().splitlines()[-1] == "Co-Authored-By: glm-4-7 <glm-4-7@round-7.contest>"
    assert files_of(repo) == ["pkg/a.py"]
    assert git(repo, "status", "--porcelain") == ""
    assert base in git(repo, "rev-list", "HEAD")


@pytest.mark.parametrize("subject,want", [
    ("plain", "7: plain"), ("7: numbered", "7: numbered"), ("AR-8: ticketed", "AR-8: ticketed")])
def test_the_subject_gets_the_round_prefix_unless_it_has_one(repo, capsys, subject, want):
    make_patch(repo, "agent", {"pkg/a.py": "A = 2\n"}, message=(subject, ""))
    assert merge(capsys, "agent")[0] == 0
    assert git(repo, "log", "-1", "--format=%s") == want


def test_a_long_and_non_ascii_subject_comes_out_whole(repo, capsys):
    subject = "dostaviť пользователя — " + "very long subject words " * 5 + "end"
    make_patch(repo, "agent", {"pkg/a.py": "A = 2\n"}, message=(subject, ""))
    assert merge(capsys, "agent")[0] == 0
    assert git(repo, "log", "-1", "--format=%s") == f"7: {subject}"


def test_the_agents_epic_tasks_and_runs_files_are_dropped_and_named(repo, capsys):
    make_patch(repo, "agent", {"pkg/a.py": "A = 2\n", f"epic-tasks/{TNAME}": TICKET + "agent edit\n",
                               "epic-tasks/99-new.md": "new\n", "runs/a/PROGRESS.csv": "x\n"})
    rc, out, err = merge(capsys, "agent")
    assert rc == 0, (out, err)
    assert files_of(repo) == ["pkg/a.py"]
    assert "epic-tasks/99-new.md" in out + err and "runs/a/PROGRESS.csv" in out + err
    assert not (repo / "epic-tasks/99-new.md").exists()
    assert (repo / "epic-tasks" / TNAME).read_text() == TICKET


def test_a_rename_lands_the_deletion_and_the_destination(repo, capsys):
    make_patch(repo, "agent", {"pkg/old.py": ("pkg/new.py", "OLD = 2\n")})
    assert merge(capsys, "agent")[0] == 0
    assert files_of(repo) == ["pkg/new.py", "pkg/old.py"]
    assert not (repo / "pkg/old.py").exists() and (repo / "pkg/new.py").exists()


# ── 2. a patch that does not apply, the refusals ─────────────────────────────────────────────

def test_a_patch_that_does_not_apply_exits_1_and_changes_nothing(repo, capsys):
    make_patch(repo, "agent", {"pkg/a.py": "def a():\n    return 2\n"})
    put(repo, "pkg/a.py", "def a():\n    return 'moved on'\n")
    commit_all(repo, "the branch moved")
    before, n = git(repo, "status", "--porcelain"), count(repo)
    rc, out, err = merge(capsys, "agent")
    assert rc == 1
    assert git(repo, "status", "--porcelain") == before and count(repo) == n
    assert "<<<<<<<" not in (repo / "pkg/a.py").read_text()
    assert "git am -3" in out + err
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_no_such_agent_a_dirty_tree_and_a_wrong_branch_are_exit_2_and_touch_nothing(repo, capsys):
    make_patch(repo, "agent", {"pkg/a.py": "A = 2\n"})
    n = count(repo)
    assert merge(capsys, "nobody")[0] == 2
    put(repo, "pkg/b.py", "B = 5\n")
    assert merge(capsys, "agent")[0] == 2
    assert git(repo, "diff", "--cached", "--name-only") == ""
    git(repo, "checkout", "-q", "--", "pkg/b.py")
    git(repo, "checkout", "-q", "-b", "elsewhere")
    assert merge(capsys, "agent")[0] == 2
    assert git(repo, "branch", "--show-current") == "elsewhere" and count(repo) == n


def test_untracked_files_do_not_make_the_tree_dirty(repo, capsys):
    make_patch(repo, "agent", {"pkg/a.py": "A = 2\n"})
    put(repo, "scratch.txt", "mine\n")
    assert merge(capsys, "agent")[0] == 0
    assert (repo / "scratch.txt").exists()


def test_a_round_with_no_record_still_merges_on_the_profile_branch_or_unchecked(repo, capsys):
    (repo / ".arena/rounds/7.json").unlink()
    make_patch(repo, "agent", {"pkg/a.py": "A = 2\n"})
    assert merge(capsys, "agent")[0] == 0


# ── 3. the entry kinds ───────────────────────────────────────────────────────────────────────

def test_a_stalled_diff_needs_uncommitted_and_names_the_untracked_files(repo, capsys):
    make_patch(repo, "agent", {"pkg/a.py": "A = 2\n"}, state="STALLED", ext="diff")
    out_dir = repo / "out/07"
    p = out_dir / "agent.STALLED.diff"
    p.write_text(p.read_text() + "# untracked: pkg/fresh.py\n")
    n = count(repo)
    assert merge(capsys, "agent")[0] == 2 and count(repo) == n
    rc, out, err = merge(capsys, "agent", "--uncommitted")
    assert rc == 0 and count(repo) == n + 1
    assert "agent" in git(repo, "log", "-1", "--format=%s") and "uncommitted" in git(
        repo, "log", "-1", "--format=%s")
    assert "Co-Authored-By: glm-4-7 <glm-4-7@round-7.contest>" in git(repo, "log", "-1", "--format=%B")


def test_a_stalled_patch_needs_yes_and_names_the_state(repo, capsys):
    make_patch(repo, "agent", {"pkg/a.py": "A = 2\n"}, state="STALLED")
    n = count(repo)
    rc, out, err = merge(capsys, "agent")
    assert rc == 2 and "STALLED" in out + err and count(repo) == n
    assert merge(capsys, "agent", yes=True)[0] == 0 and count(repo) == n + 1


def test_a_two_message_patch_needs_squash(repo, capsys):
    make_patch(repo, "agent", {"pkg/a.py": "A = 2\n"}, commits=2, message=("two parts", "b"))
    n = count(repo)
    rc, out, err = merge(capsys, "agent")
    assert rc == 2 and "2" in out + err and count(repo) == n
    rc, out, err = merge(capsys, "agent", "--squash")
    assert rc == 0, (out, err)
    assert count(repo) == n + 1
    msg = git(repo, "log", "-1", "--format=%B")
    assert msg.splitlines()[0] == "7: two parts (1)" and "(squashed from 2 commits)" in msg


def test_a_duplicate_resolves_to_the_named_patch_with_its_own_trailer(repo, capsys):
    make_patch(repo, "first", {"pkg/a.py": "A = 2\n"}, model="x/alpha-1.0:free")
    make_patch(repo, "second", {}, model="kenary/GLM-4.7:free", entry={"duplicate_of": "first"})
    assert merge(capsys, "second")[0] == 0
    assert (repo / "pkg/a.py").read_text() == "A = 2\n"
    assert git(repo, "log", "-1", "--format=%B").rstrip().endswith(
        "Co-Authored-By: glm-4-7 <glm-4-7@round-7.contest>")


@pytest.mark.parametrize("model,want", [
    ("kenary/GLM-4.7", "glm-4-7"), ("x/y-6.8-z:free", "y-6-8-z"), ("plain", "plain"),
    ("a/b/Some Model_1:x", "some-model_1")])
def test_the_trailers_model_part_follows_the_naming_rule(repo, capsys, model, want):
    make_patch(repo, "agent-var2", {"pkg/a.py": "A = 2\n"}, model=model)
    assert merge(capsys, "agent-var2")[0] == 0
    assert git(repo, "log", "-1", "--format=%B").rstrip().endswith(
        f"Co-Authored-By: {want} <{want}@round-7.contest>")


def test_merged_json_records_this_round_and_keeps_the_others(repo, capsys):
    put(repo, ".arena/merged.json", json.dumps({"3": {"agent": "old", "sha": "a" * 40, "subject": "s"}}))
    make_patch(repo, "agent", {"pkg/a.py": "A = 2\n"})
    assert merge(capsys, "agent")[0] == 0
    rec = json.loads((repo / ".arena/merged.json").read_text())
    assert rec["3"]["agent"] == "old"
    assert rec["7"]["agent"] == "agent" and rec["7"]["sha"].startswith(git(repo, "rev-parse", "--short", "HEAD"))
    assert "subject" in rec["7"]


def test_merge_never_pushes_never_adds_a_branch_and_never_amends_a_ticket_commit(repo, capsys):
    git(repo, "branch", "arena-round/7")
    make_patch(repo, "agent", {"pkg/a.py": "A = 2\n"})
    before = git(repo, "branch", "--list").split()
    assert merge(capsys, "agent")[0] == 0
    assert sorted(git(repo, "branch", "--list").split()) == sorted(before)
    assert git(repo, "branch", "--list", "contest-legs") == ""


# ── 4. issue land ────────────────────────────────────────────────────────────────────────────

def merged(repo, capsys, agent="agent"):
    make_patch(repo, agent, {"pkg/a.py": "A = 2\n"})
    assert merge(capsys, agent)[0] == 0
    return git(repo, "rev-parse", "HEAD")


def bench(repo):
    put(repo, "contest-bench/7/acceptance_7.py", "def test_x():\n    pass\n")
    put(repo, "contest-bench/7/__pycache__/x.pyc", "junk")


def test_land_is_one_commit_with_the_ticket_the_index_row_and_the_bench(repo, capsys):
    sha = merged(repo, capsys)
    bench(repo)
    n = count(repo)
    rc, out, err = land(capsys, "--score", "19/19 on contest-bench/7")
    assert rc == 0, (out, err)
    assert count(repo) == n + 1
    assert files_of(repo) == ["contest-bench/7/acceptance_7.py", f"epic-tasks/{TNAME}",
                              "epic-tasks/INDEX.md"]
    new = (repo / "epic-tasks" / TNAME).read_text().splitlines(keepends=True)
    old = TICKET.splitlines(keepends=True)
    diff = [(a, b) for a, b in zip(old, new) if a != b]
    assert len(new) == len(old) and len(diff) == 1
    assert diff[0][1] == (f"**Status:** landed — round 7, winner agent "
                          f"(19/19 on contest-bench/7), {sha[:7]} as-is\n")
    index = (repo / "epic-tasks/INDEX.md").read_text().splitlines()
    assert "landed" in index[-1] and index[-1].startswith("| 7 |") and index[-2] == INDEX.splitlines()[-2]
    assert git(repo, "log", "-1", "--format=%B").count(f"agent taken as-is in {sha[:7]}") == 1
    assert git(repo, "status", "--porcelain") == ""


def test_without_a_score_there_is_no_parenthesis(repo, capsys):
    sha = merged(repo, capsys)
    assert land(capsys)[0] == 0
    line = [x for x in (repo / "epic-tasks" / TNAME).read_text().splitlines() if x.startswith("**Status:")]
    assert line == [f"**Status:** landed — round 7, winner agent, {sha[:7]} as-is"]


def test_the_default_subject_names_the_bench_only_when_there_is_one(repo, capsys):
    merged(repo, capsys)
    assert land(capsys)[0] == 0
    assert git(repo, "log", "-1", "--format=%s") == "7: ticket landed"


def test_the_default_subject_with_a_bench_and_a_custom_subject_and_note(repo, capsys):
    merged(repo, capsys)
    bench(repo)
    assert land(capsys, "--note", "bench 19/19; tests 100 passed")[0] == 0
    assert git(repo, "log", "-1", "--format=%s") == (
        "7: ticket landed, and contest-bench/7 holds the round's acceptance bench")
    assert "bench 19/19; tests 100 passed" in git(repo, "log", "-1", "--format=%b")


def test_a_custom_subject_replaces_the_default(repo, capsys):
    merged(repo, capsys)
    assert land(capsys, "-m", "7: my words")[0] == 0
    assert git(repo, "log", "-1", "--format=%s") == "7: my words"


def test_land_without_a_merge_record_says_only_the_round(repo, capsys):
    assert land(capsys)[0] == 0
    line = [x for x in (repo / "epic-tasks" / TNAME).read_text().splitlines() if x.startswith("**Status:")]
    assert line == ["**Status:** landed — round 7"]
    assert "winner" not in git(repo, "log", "-1", "--format=%b")


def test_follow_up_paths_ride_in_the_same_commit_and_the_status_says_so(repo, capsys):
    sha = merged(repo, capsys)
    put(repo, "pkg/b.py", "B = 2\n")
    put(repo, "pkg/fresh.py", "F = 1\n")
    n = count(repo)
    rc, out, err = land(capsys, paths=["pkg/b.py", "pkg/fresh.py"])
    assert rc == 0, (out, err)
    assert count(repo) == n + 1
    assert "pkg/b.py" in files_of(repo) and "pkg/fresh.py" in files_of(repo)
    assert f"{sha[:7]} + follow-up" in (repo / "epic-tasks" / TNAME).read_text()


def test_a_tracked_change_not_on_the_list_a_dead_path_and_an_unchanged_path_are_exit_2(repo, capsys):
    merged(repo, capsys)
    n = count(repo)
    put(repo, "pkg/b.py", "B = 9\n")
    assert land(capsys)[0] == 2
    assert land(capsys, paths=["pkg/a.py"])[0] == 2
    assert land(capsys, paths=["pkg/missing.py"])[0] == 2
    assert count(repo) == n and (repo / "epic-tasks" / TNAME).read_text() == TICKET
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_a_refused_land_does_not_stage_the_bench(repo, capsys):
    merged(repo, capsys)
    bench(repo)
    put(repo, "pkg/b.py", "B = 9\n")
    assert land(capsys)[0] == 2
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_no_ticket_and_a_wrong_branch_are_exit_2(repo, capsys):
    merged(repo, capsys)
    git(repo, "checkout", "-q", "-b", "elsewhere")
    assert land(capsys)[0] == 2
    git(repo, "checkout", "-q", "arena")
    git(repo, "rm", "-q", f"epic-tasks/{TNAME}")
    git(repo, "commit", "-q", "-m", "drop")
    assert land(capsys)[0] == 2


def test_the_ticket_text_is_read_from_the_rounds_ref_and_that_commit_stays_out(repo, capsys):
    git(repo, "checkout", "-q", "-b", "arena-round/7")
    put(repo, f"epic-tasks/{TNAME}", TICKET + "a line only the round's branch has\n")
    commit_all(repo, "7: ticket for the round")
    git(repo, "checkout", "-q", "arena")
    merged(repo, capsys)
    assert land(capsys)[0] == 0
    assert "a line only the round's branch has" in (repo / "epic-tasks" / TNAME).read_text()
    assert "ticket for the round" not in git(repo, "log", "--format=%s", "arena")
    assert git(repo, "branch", "--list", "contest-legs") == ""


def test_land_prints_the_next_steps_and_never_pushes(repo, capsys):
    merged(repo, capsys)
    rc, out, err = land(capsys)
    assert rc == 0
    text = out + err
    assert "pytest tests" in text and "tests_bugfix" in text and "git push" in text
    assert git(repo, "remote") == ""
