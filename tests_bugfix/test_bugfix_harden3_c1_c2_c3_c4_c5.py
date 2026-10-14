"""tests_bugfix/test_bugfix_harden3_c1_c2_c3_c4_c5.py

Pinned regression guards for HARDEN-3's five guards on empty or null input. Each
class names the one behaviour that must never come back; the feature-level
coverage with the real `git mv` repo and every spelling of a rename lives in
tests/test_guards_empty_input.py.

C1 — an empty `NN-*.md` was an `IndexError` on `splitlines()[0]`. Pinned at
     `gates.ticket_for_round`: the title is `""` and the declared paths `[]`.
C2 — `git diff --numstat` prints a rename in the brace form by default, so
     `a/{b => c}/d.txt` was read as the path `c}/d.txt`. Pinned at
     `runner._split_numstat`: the field names `a/c/d.txt`.
C3 — an empty `TARGET` file was an `IndexError` on `.split()[0]`. Pinned at the
     script's own exit code: 1, naming the file.
C4 — an event with `"ts": null` was a `TypeError` on `None[:19]`. Pinned at
     `view_trace.render_summary`: the row still renders.
C5 — an exception whose message is empty was an `IndexError` on
     `splitlines()[0]`. Pinned at `profile._one_line`: the type name.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.arena import profile
from tools.auto import view_trace as vt
from tools.contest import gates
from tools.contest import runner as _runner


# ── C1 ────────────────────────────────────────────────────────────────────────

def test_c1_an_empty_ticket_has_an_empty_title(tmp_path):
    """An empty ticket file reads as no title and no declared paths."""
    tasks = tmp_path / "epic-tasks"
    tasks.mkdir()
    (tasks / "07-empty.md").write_text("", encoding="utf-8")
    assert gates.ticket_for_round(tasks, 7) == ("07-empty.md", "", [])


# ── C2 ────────────────────────────────────────────────────────────────────────

def test_c2_a_brace_rename_names_the_new_path():
    """`a/{b => c}/d.txt` is the path `a/c/d.txt`, not `c}/d.txt`."""
    assert _runner._split_numstat("1\t0\ta/{b => c}/d.txt") == (1, 0, "a/c/d.txt")
    assert _runner._split_numstat("1\t0\t{a => b}/x") == (1, 0, "b/x")
    assert _runner._split_numstat("1\t0\told => new") == (1, 0, "new")


# ── C3 ────────────────────────────────────────────────────────────────────────

def test_c3_an_empty_target_file_is_exit_one_naming_the_file(tmp_path):
    """An empty `TARGET` is exit 1 naming the file, not an `IndexError`."""
    known = tmp_path / "known"
    known.mkdir()
    (known / "plan.json").write_text(json.dumps(
        {"goal": "g", "base_dir": "b", "created_at": "now",
         "tasks": [{"id": "AUTO-T1", "title": "t", "instruction": "i"}]}),
        encoding="utf-8")
    (known / "truth.csv").write_text("id,truth\nAUTO-T1,REAL\n", encoding="utf-8")
    (known / "TARGET").write_text("", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "gate1_vote_layer2.py"),
         "--known", str(known), "--sets", "3", "--dry-run"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "TARGET" in proc.stderr and "empty" in proc.stderr, proc.stderr


# ── C4 ────────────────────────────────────────────────────────────────────────

def test_c4_a_null_timestamp_still_renders_a_row():
    """An event with `"ts": null` renders one summary row."""
    out = vt.render_summary(
        [{"seq": 2, "ts": None, "source": "coder", "target": "gate2",
          "kind": "result"}], use_color=False)
    assert "#   2" in out and "coder" in out, out


# ── C5 ────────────────────────────────────────────────────────────────────────

def test_c5_an_exception_with_no_message_is_its_type_name():
    """An exception with an empty message prints its type name."""
    assert profile._one_line(ValueError()) == "ValueError"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_a_file_lifted_out_of_a_directory_has_no_doubled_slash():
    """C2: git prints `a/{b => }/c.txt` when a file moves up one directory, and `{b => }/c.txt` out of the first one."""
    from tools.contest.runner import _split_numstat
    assert _split_numstat("1\t0\ta/{b => }/c.txt") == (1, 0, "a/c.txt")
    assert _split_numstat("1\t0\t{b => }/c.txt") == (1, 0, "c.txt")
    assert _split_numstat("1\t0\ta/{ => b}/c.txt") == (1, 0, "a/b/c.txt")
    assert _split_numstat("2\t3\t{a => b}/x.py") == (2, 3, "b/x.py")
