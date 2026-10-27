"""tests/test_claimcheck_writes_review.py — what the claim check writes and cleans up, after the review of
2026-10-12: a world fact under `--accept committed` is no defect ticket, `--append` keeps every cell
of a hand-edited truth.csv, a symlinked truth.csv is written through, a stray file in a worktree's
users/ does not stop `Target.close`, and a lock taken between a waiter's look and its break survives.
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.claimcheck import target as tg
from tools.claimcheck import truth as tr

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import claim_vote as cv  # noqa: E402
import claimcheck_truth as ct  # noqa: E402
import make_jira_tasks as mj  # noqa: E402

SHA = "c" * 40
FIVE = {"v1": "kenary/agnes-3-0-flash:free", "v2": "openai/gpt-5-mini", "v3": "meta/llama-4-scout",
        "v4": "zai/glm-5", "v5": "qwen/qwen3-coder"}
WORLD = "Python's sorted() is stable: equal keys keep their input order in the output list, as documented."


# ── №20: a world fact under --accept committed is no defect ──────────────────────────────────

def _committed_world_votes() -> dict:
    """Five voters of five families, three TRUE and two UNSURE, tallied under `committed`."""
    rows = [{"claim": WORLD, "truth": None}]
    verdicts = {"v1": "TRUE", "v2": "TRUE", "v3": "TRUE", "v4": "UNSURE", "v5": "UNSURE"}
    results = [{"model": ref, "run": 0, "votes": {0: v}, "accepted": {}, "rejected": []} for ref, v in verdicts.items()]
    table = cv.tally(rows, results, meta=[{"kind": "world", "dangling": False, "sha": SHA}], accept="committed")
    return json.loads(json.dumps({"target": {"repo": "/nonexistent", "ref": SHA, "sha": SHA, "base": None,
                                             "warnings": []}, "accept": "committed", "models": FIVE,
                                  "results": results, "claims": table}))


def test_a_committed_world_fact_is_not_filed_as_a_ticket(tmp_path):
    votes = tmp_path / "votes.json"
    votes.write_text(json.dumps(_committed_world_votes()), encoding="utf-8")
    truth = tmp_path / "truth.csv"
    assert ct.main([str(votes), "--out", str(truth), "--no-rebuild", "--no-lenz-hint", "--repo-root", str(tmp_path)]) == 0
    [row] = list(csv.DictReader(io.StringIO(truth.read_text(encoding="utf-8"))))
    assert row["truth"] == "REAL" and row["how"] == "3 of 5 voters committed; world fact"
    # and one real defect next to it, so the script has something to file
    with truth.open("a", encoding="utf-8", newline="") as fh:
        csv.writer(fh, lineterminator="\n").writerow(
            [{"finding": "tools/x.py::run", "truth": "REAL", "how": "3 of 5 voters committed; src:tools/x.py:1-4 \"def run\""}
             .get(k, "") for k in tr.TRUTH_HEADER])
    out = tmp_path / "tasks"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "make_jira_tasks.py"), "--truth", str(truth),
                        "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "skipped 1 world-fact row(s)" in r.stderr
    tickets = sorted(p.name for p in out.glob("[0-9]*-*.md"))      # INDEX.md aside
    assert len(tickets) == 1 and "run" in tickets[0]


@pytest.mark.parametrize("how, world", [
    ("world fact", True),
    ("3 of 5 voters committed; world fact", True),
    ("  4 of 4 voters committed;world fact ", True),
    ('src:a.py:1-2 "a\\; world fact"', False),          # a quote that ends with the words
    ("3 of 5 voters committed; src:x.py:1-2", False),
    ("", False),
    (None, False),
])
def test_which_how_texts_are_a_world_fact(how, world):
    assert mj.is_world_fact(how) is world


# ── №21: --append keeps every cell of a hand-edited file ─────────────────────────────────────

def _row(finding, truth="REAL"):
    return {**{k: "" for k in tr.TRUTH_HEADER}, "finding": finding, "truth": truth, "how": "world fact"}


def test_append_keeps_the_cells_past_the_header_and_the_bytes_before(tmp_path):
    path = tmp_path / "truth.csv"
    head = ",".join(tr.TRUTH_HEADER)
    hand = "a.py::f,REAL" + "," * (len(tr.TRUTH_HEADER) - 2) + ",hand-checked,see issue #4,and the PR\r\n"
    path.write_text("\ufeff" + head + "\r\n" + hand, encoding="utf-8")
    before = path.read_bytes()
    written, conflicts = tr.write_truth(path, [_row("b.py::g")], append=True)
    assert [r["finding"] for r in written] == ["b.py::g"] and not conflicts
    after = path.read_bytes()
    # nothing of the old file rewritten (its CRLF too), only the BOM dropped, as fixes 13 write the file
    assert before.startswith(b"\xef\xbb\xbf") and after.startswith(before[3:])
    assert after[len(before):].decode("utf-8").endswith("\r\n")
    assert list(csv.reader(io.StringIO(after.decode("utf-8-sig"))))[1][-2:] == ["see issue #4", "and the PR"]


def test_an_older_header_grows_and_every_row_keeps_its_own_cells(tmp_path):
    path = tmp_path / "truth.csv"
    path.write_text("finding,truth,note\na.py::f,REAL,mine,past the header\n", encoding="utf-8")
    tr.write_truth(path, [_row("b.py::g")], append=True)
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
    header = rows[0]
    assert header[:3] == ["finding", "truth", "note"] and set(tr.TRUTH_HEADER) <= set(header)
    assert rows[1][:3] == ["a.py::f", "REAL", "mine"] and rows[1][-1] == "past the header"
    assert rows[2][header.index("finding")] == "b.py::g"


def test_a_header_only_file_keeps_the_operators_own_column(tmp_path):
    """Found next to №21: a truth.csv with only its header lost the operator's extra column."""
    path = tmp_path / "truth.csv"
    path.write_text(",".join(tr.TRUTH_HEADER) + ",reviewer\n", encoding="utf-8")
    tr.write_truth(path, [_row("b.py::g")], append=True)
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
    assert rows[0][-1] == "reviewer" and rows[1][0] == "b.py::g"


# ── №22: a symlinked truth.csv is written through ────────────────────────────────────────────

@pytest.mark.parametrize("append", [False, True])
def test_a_symlinked_truth_csv_stays_a_link_and_the_target_gets_the_rows(tmp_path, append):
    shared = tmp_path / "shared" / "truth.csv"
    shared.parent.mkdir()
    shared.write_text(",".join(tr.TRUTH_HEADER) + "\n", encoding="utf-8")
    link = tmp_path / "run" / "truth.csv"
    link.parent.mkdir()
    link.symlink_to(shared)
    tr.write_truth(link, [_row("b.py::g")], append=append)
    assert link.is_symlink() and "b.py::g" in shared.read_text(encoding="utf-8")


# ── №23: a stray file in users/ ──────────────────────────────────────────────────────────────

def test_token_pid_reads_only_our_tokens():
    assert tg._token_pid(f"{os.getpid()}-abc123") == os.getpid()
    for name in (".DS_Store", "notes.txt~", "-1-x", "", "12a-b"):
        assert tg._token_pid(name) is None


def _git(root, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True,
                   capture_output=True)


def test_close_removes_the_worktree_with_a_stray_file_in_users(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "one")
    t = tg.Target.open(str(repo), "HEAD", scratch=tmp_path / "scratch")
    (t.tree.parent / "users" / ".DS_Store").write_text("", encoding="utf-8")
    t.close()
    assert not t.tree.exists()


# ── №24: the lock a second waiter would have broken ──────────────────────────────────────────

def test_a_lock_taken_between_the_look_and_the_break_is_put_back(tmp_path):
    path = tmp_path / ".lock"
    path.mkdir()
    live = f"{os.getpid()}-the-new-owner"
    (path / "owner").write_text(live, encoding="utf-8")
    waiter = tg._Lock(path)
    waiter._break(seen="999999999-the-dead-owner")       # it judged the dead lock, a live one is there now
    assert (path / "owner").read_text(encoding="utf-8") == live
    assert not list(tmp_path.glob(".lock.stale-*"))


def test_the_stale_lock_that_was_judged_is_broken(tmp_path):
    path = tmp_path / ".lock"
    path.mkdir()
    dead = "999999999-the-dead-owner"
    (path / "owner").write_text(dead, encoding="utf-8")
    waiter = tg._Lock(path, wait=5)
    assert waiter._stale() == (True, dead)
    with waiter:
        assert (path / "owner").read_text(encoding="utf-8") == waiter.token
    assert not path.exists()


def test_a_lock_with_a_garbled_owner_falls_back_to_its_age(tmp_path):
    path = tmp_path / ".lock"
    path.mkdir()
    (path / "owner").write_text(".DS_Store", encoding="utf-8")
    assert tg._Lock(path)._stale() == (False, ".DS_Store")      # fresh: not stale
