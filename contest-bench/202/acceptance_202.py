"""Judge's acceptance suite for round 202 (the cross matrix and `tests_bugfix/`), from the ticket alone.

Each case builds throwaway git repos (a base and entries that branch from it), then runs
`scripts/judge_epic_round.py --round 202 --cross` on them as a subprocess and reads stdout, the exit
code, `cross.md` and `cross.json` — the ticket's own observable surface. A case whose answer the
ticket leaves open (the scratch file name, `--tests`) is not here.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/202/acceptance_202.py -n 0 -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JUDGE = ROOT / "scripts" / "judge_epic_round.py"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]

IMPL = "def f():\n    return 1\n"
GOOD = "from impl import f\n\ndef test_ok():\n    assert f() == 1\n"
BAD_IMPL = "def f():\n    return 2\n"


def _git(cwd, *a):
    subprocess.run([*GIT, *a], cwd=cwd, check=True, capture_output=True)


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _write(path, {"impl.py": IMPL, "tests/test_base.py": "def test_base():\n    assert True\n",
                  "tests_bugfix/.keep": ""})
    _git(path, "add", "-A"); _git(path, "commit", "-qm", "base"); _git(path, "tag", "base")
    return path


def _write(path, files):
    for rel, text in files.items():
        p = Path(path) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def _entry(tmp: Path, name: str, files: dict[str, str]) -> Path:
    dst = tmp / name
    subprocess.run(["git", "clone", "-q", str(tmp / "base"), str(dst)], check=True)
    _write(dst, files)
    _git(dst, "add", "-A")
    _git(dst, "commit", "-qm", name, "--allow-empty")
    return dst


def _nleads(js):
    return sum(len(v) for v in js["leads"].values())


def _judge(tmp: Path, entries: dict[str, Path]):
    tasks = tmp / "tasks"
    tasks.mkdir(exist_ok=True)
    (tasks / "202-t.md").write_text("# 202 — t\n\n**Status:** queued\n**Round:** 202\n")
    out = tmp / "out"
    cmd = [sys.executable, str(JUDGE), "--round", "202", "--tasks", str(tasks), "--base", "base",
           "--cross", "--cross-out", str(out), "--cell-timeout", "60"]
    for n, p in entries.items():
        cmd += ["--worktree", f"{n}={p}"]
    r = subprocess.run(cmd, cwd=tmp / "base", capture_output=True, text=True, timeout=300)
    js = out / "cross.json"
    return r, (json.loads(js.read_text()) if js.exists() else None), \
        ((out / "cross.md").read_text() if (out / "cross.md").exists() else "")


@pytest.fixture
def tmp(tmp_path):
    _repo(tmp_path / "base", {})
    return tmp_path


def test_tests_bugfix_only_gets_cells(tmp):
    a = _entry(tmp, "a", {"tests_bugfix/test_a.py": GOOD})
    b = _entry(tmp, "b", {"tests_bugfix/test_b.py": GOOD})
    r, js, md = _judge(tmp, {"a": a, "b": b})
    assert js is not None, r.stdout + r.stderr
    assert len(js["cells"]) > 0 and len(js["tests"]) > 0
    assert "nothing was crossed" not in r.stdout


def test_behaviour_lead_from_tests_bugfix(tmp):
    a = _entry(tmp, "a", {"tests_bugfix/test_a.py": GOOD})
    b = _entry(tmp, "b", {"impl.py": BAD_IMPL, "tests_bugfix/test_b.py": "def test_b():\n    assert True\n"})
    r, js, md = _judge(tmp, {"a": a, "b": b})
    assert js and len(js["cells"]) > 0
    assert _nleads(js) >= 1, r.stdout


def test_same_basename_in_both_roots_no_collision(tmp):
    a = _entry(tmp, "a", {"tests/test_x.py": GOOD, "tests_bugfix/test_x.py": GOOD})
    b = _entry(tmp, "b", {"tests_bugfix/test_y.py": GOOD})
    r, js, md = _judge(tmp, {"a": a, "b": b})
    cell = {(c["tests"], c["code"]): c for c in js["cells"]}
    assert cell[("a", "a")]["total"] == 2, js        # tests/test_x.py and tests_bugfix/test_x.py both ran


def test_entry_without_test_is_a_column_not_a_row(tmp):
    a = _entry(tmp, "a", {"tests_bugfix/test_a.py": GOOD})
    b = _entry(tmp, "b", {"impl.py": IMPL + "\n"})
    r, js, md = _judge(tmp, {"a": a, "b": b})
    assert js and len(js["cells"]) >= 2


def test_nobody_changed_a_test_exits_2(tmp):
    a = _entry(tmp, "a", {"impl.py": IMPL + "\n"})
    b = _entry(tmp, "b", {"impl.py": IMPL + "\n\n"})
    r, js, md = _judge(tmp, {"a": a, "b": b})
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "nothing was crossed" in r.stdout
    assert "nothing was crossed" in md
    assert "no cell failed" not in r.stdout
    assert js is not None and _nleads(js) == 0


def test_leads_count_equals_printed_lists(tmp):
    a = _entry(tmp, "a", {"tests_bugfix/test_a.py": GOOD})
    b = _entry(tmp, "b", {"impl.py": BAD_IMPL, "tests_bugfix/test_b.py": "def test_b():\n    assert True\n"})
    r, js, md = _judge(tmp, {"a": a, "b": b})
    assert js is not None
    assert _nleads(js) >= 1
    printed = [l for l in md.splitlines() if " on " in l and l.startswith("- ") and ":" in l
               and not l.startswith("- **")]
    assert len(printed) == _nleads(js), (printed, js["leads"])


def test_tests_root_still_crossed(tmp):
    a = _entry(tmp, "a", {"tests/test_a.py": GOOD})
    b = _entry(tmp, "b", {"tests/test_b.py": GOOD})
    r, js, md = _judge(tmp, {"a": a, "b": b})
    assert js and len(js["cells"]) > 0
