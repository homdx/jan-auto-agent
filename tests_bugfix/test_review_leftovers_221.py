r"""Review of 206-220's fixes: a blank verdict named the agreement, a work-tree rename's old name read as a path, `\;` warned.

1. merge_validations' screen took the agreed verdict from every reviewer's, blanks too: a
   finding Bob CONFIRMED and Alice left blank was printed under AGREED with an empty
   verdict, and a reviewer with a blank and a CONFIRMED row read `|CONFIRMED`.
2. gitref.status_paths knew a rename only by its first status letter; `git add -N` makes a
   work-tree rename ` R new\\0old`, so the old name was read as a record of its own —
   `ab c.py` became the path `c.py`.
3. contest-bench/198/acceptance_198.py wrote `\;` in plain strings: a SyntaxWarning on
   every collection, an error in a later Python.
"""

from __future__ import annotations

import subprocess
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_validations as mv  # noqa: E402
from test_merge_validations_review import _csv, _merge, _row, _where  # noqa: E402

from tools.arena.gitref import status_paths  # noqa: E402


# ── 1 ────────────────────────────────────────────────────────────────────────

def test_a_blank_verdict_does_not_name_the_agreement(tmp_path):
    alice = _csv(tmp_path / "validation-v1-alice.csv", [_row("T1", "a.py", "f", "", "HIGH")])
    bob = _csv(tmp_path / "validation-v1-bob.csv", [_row("T1", "a.py", "f", "CONFIRMED", "HIGH")])
    proc, sections, pivot = _merge(tmp_path, alice, bob)
    assert proc.returncode == 0, proc.stderr
    assert _where(sections, "a.py") == "AGREED"
    line = next(l for l in sections["AGREED"] if "a.py" in l)
    assert line.split()[0] == "CONFIRMED", line
    assert pivot["a.py::f"]["agreement"] == "UNANIMOUS"


def test_a_reviewer_with_a_blank_and_a_verdict_said_the_verdict():
    rows = [{"_reviewer": "alice", "verdict": ""}, {"_reviewer": "alice", "verdict": "confirmed"},
            {"_reviewer": "bob", "verdict": ""}]
    assert mv.verdicts_by_reviewer(rows) == {"alice": "CONFIRMED", "bob": ""}


def test_all_blank_stays_blank_and_two_verdicts_stay_two():
    rows = [{"_reviewer": "a", "verdict": "FALSE_POSITIVE"}, {"_reviewer": "a", "verdict": "CONFIRMED"}]
    assert mv.verdicts_by_reviewer(rows) == {"a": "CONFIRMED|FALSE_POSITIVE"}


# ── 2 ────────────────────────────────────────────────────────────────────────

def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=repo, check=True, capture_output=True)


@pytest.mark.parametrize("old", ["ab c.py", "old.py"])
def test_a_work_tree_rename_is_its_new_name_only(tmp_path, old):
    _git(tmp_path, "init", "-q")
    (tmp_path / old).write_text("".join(f"line {i}\n" for i in range(20)))
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    (tmp_path / old).rename(tmp_path / "new.py")
    _git(tmp_path, "add", "-N", "new.py")
    raw = subprocess.run(["git", "status", "--porcelain", "-z"], cwd=tmp_path,
                         capture_output=True, text=True).stdout
    if not raw.startswith(" R "):
        pytest.skip(f"this git does not report work-tree renames: {raw!r}")
    assert status_paths(tmp_path) == ["new.py"]
    assert status_paths(tmp_path, tracked_only=True) == ["new.py"]


# ── 3 ────────────────────────────────────────────────────────────────────────

def test_the_198_bench_compiles_without_a_warning():
    path = ROOT / "contest-bench" / "198" / "acceptance_198.py"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
