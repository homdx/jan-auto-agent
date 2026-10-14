"""tests/test_guards_empty_input.py — HARDEN-3: five guards on empty or null input.

Each entry of the ticket is one wrong answer on an input nobody tested, and each
needs the guard its sibling code already has:

  C1  `tools/contest/gates.py` `ticket_for_round` — an empty `NN-*.md` was an
      `IndexError` on `splitlines()[0]`; the title is now `""` and the round
      refuses the file, naming it.
  C2  `tools/contest/runner.py` `_split_numstat` — `git diff --numstat` prints a
      rename in the brace form by default (`a/{b => c}/d.txt`), so a moved file
      was counted under `c}/d.txt`, a path that exists in no tree.
  C3  `scripts/gate1_vote_layer2.py` `main` — an empty `TARGET` file was an
      `IndexError` on `.split()[0]`; it is now exit 1 naming the file.
  C4  `tools/auto/view_trace.py` `render_summary` — an event with `"ts": null`
      was a `TypeError` on `None[:19]`.
  C5  `tools/arena/profile.py` `_one_line` — an exception whose message is empty
      was an `IndexError` on `splitlines()[0]`; the type name is printed.

C2 is the only one that changes a result on a real input, so it carries the real
`git mv` repo as well as the three spellings git prints a rename in.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.arena import profile
from tools.auto import view_trace as vt
from tools.contest import gates
from tools.contest import runner as _runner
from tools.contest.workspace import Workspace


def _git(cwd, *args) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    return repo


# ── C1 — an empty ticket file ─────────────────────────────────────────────────

def test_c1_an_empty_ticket_has_an_empty_title_and_no_declared_paths(tmp_path):
    """`ticket_for_round` on an empty `NN-*.md`: `""` for the title, `[]` for the
    declared paths — never an `IndexError` out of the read."""
    tasks = tmp_path / "epic-tasks"
    tasks.mkdir()
    (tasks / "07-empty-ticket.md").write_text("", encoding="utf-8")

    assert gates.ticket_for_round(tasks, 7) == ("07-empty-ticket.md", "", [])


def test_c1_the_round_refuses_an_empty_ticket_naming_the_file(tmp_path):
    """The round that found an empty ticket refuses it, naming the file."""
    repo = _repo(tmp_path)
    (repo / "epic-tasks").mkdir()
    (repo / "epic-tasks" / "07-empty-ticket.md").write_text("", encoding="utf-8")
    (repo / "tools" / "auto").mkdir(parents=True)
    (repo / "tools" / "auto" / "collect_bridge.py").write_text(
        "class CollectBridge:\n    def _shrink(self, raw):\n        return raw\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "entry", str(wt))

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "judge_epic_round.py"),
         "--round", "7", "--tasks", str(repo / "epic-tasks"),
         "--base", "main", "--worktree", f"entry={wt}"],
        cwd=str(repo), capture_output=True, text=True, timeout=300)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "07-empty-ticket.md" in proc.stderr, proc.stderr
    assert "empty" in proc.stderr, proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr


# ── C2 — a rename in git's brace form ─────────────────────────────────────────

@pytest.mark.parametrize("field,expected", [
    # git's brace form: a rename that shares its head and its tail
    ("1\t0\ta/{b => c}/d.txt", "a/c/d.txt"),
    # the same rename, spelled at the root
    ("1\t0\t{a => b}/x", "b/x"),
    # a rename with nothing shared: the plain form, unchanged
    ("1\t0\told.txt => new.txt", "new.txt"),
    # two brace groups in one path
    ("1\t0\ta/{b => c}/{d => e}/f", "a/c/e/f"),
    # an ordinary path is untouched
    ("3\t2\ttools/contest/runner.py", "tools/contest/runner.py"),
    # a binary file's counts, with a path
    ("-\t-\tblob.bin", "blob.bin"),
])
def test_c2_a_rename_field_names_the_new_path(field, expected):
    """`_split_numstat` reads every spelling git prints a rename in."""
    added, deleted, path = _runner._split_numstat(field)
    assert (path, added, deleted) == (expected, *(
        (0, 0) if field.startswith("-") else
        (int(field.split("\t")[0]), int(field.split("\t")[1]))))


def test_c2_a_real_git_mv_is_counted_under_the_new_path(tmp_path):
    """The evidence: a repo with `git mv a/b/d.txt a/c/d.txt` leaves the leg
    record naming `a/c/d.txt`, not `c}/d.txt`."""
    repo = _repo(tmp_path)
    (repo / "a" / "b").mkdir(parents=True)
    (repo / "a" / "b" / "d.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "a" / "c").mkdir()
    _git(repo, "mv", "a/b/d.txt", "a/c/d.txt")
    (repo / "a" / "c" / "d.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "move")

    ws = Workspace(agent="agent-a", path=repo.resolve(), branch="main",
                   base_sha=base, kind="worktree")
    assert _runner._leg_files(ws) == [("a/c/d.txt", 1, 0, False)]


# ── C3 — an empty TARGET file ─────────────────────────────────────────────────

def test_c3_an_empty_target_file_is_exit_one_naming_the_file(tmp_path):
    """An empty `TARGET` is exit 1 naming the file, never an `IndexError`."""
    known = tmp_path / "known"
    known.mkdir()
    (known / "plan.json").write_text(json.dumps(
        {"goal": "g", "base_dir": "b", "created_at": "now",
         "tasks": [{"id": "AUTO-T1", "title": "t", "instruction": "i"}]}),
        encoding="utf-8")
    (known / "truth.csv").write_text("id,truth\nAUTO-T1,REAL\n", encoding="utf-8")
    (known / "TARGET").write_text("", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gate1_vote_layer2.py"),
         "--known", str(known), "--sets", "3", "--dry-run"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "TARGET" in proc.stderr, proc.stderr
    assert "empty" in proc.stderr, proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr


# ── C4 — a null timestamp ─────────────────────────────────────────────────────

def test_c4_a_null_timestamp_renders_a_row_not_a_typeerror():
    """An event with `"ts": null` still renders one summary row."""
    out = vt.render_summary(
        [{"seq": 1, "ts": None, "source": "architect", "target": "gate1",
          "kind": "decision"}],
        use_color=False)
    assert "#   1" in out, out
    assert "architect" in out and "decision" in out, out


# ── C5 — an exception with no message ─────────────────────────────────────────

def test_c5_an_exception_with_no_message_is_its_type_name():
    """An exception whose message is empty prints its type name."""
    assert profile._one_line(ValueError()) == "ValueError"
    assert profile._one_line(Exception("boom")) == "boom"
    assert profile._one_line(ValueError("two\nlines")) == "two"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
