"""contest-bench/158/acceptance_158.py — round 158: the judge's cross-test matrix.

Behaviour only, through the judge's own CLI (`scripts/judge_epic_round.py`), on a tiny synthetic repo:

    base  lib.py: add(a, b) = a + b, tests/test_base.py
    p     adds lib.only_p(); tests/test_p_features.py with four tests
            test_imports  passes on every code
            test_only_p   uses a name only p's code has           -> `api` on a code without it
            test_wrong    asserts something no code does         -> `base` (fails on the base too)
            test_add      add(2, 3) == 5, true on the base       -> `behaviour` on a code that breaks it
    q     breaks add (a + b + 1); tests/test_q_features.py with one test that passes everywhere
    r     lib.py as the base; tests/test_r_import.py imports `only_p` at module level
            -> a collection error (ImportError) on every code without it: `api`
    h     (own run) tests/test_h_hang.py sleeps for ten minutes: a cell must end at its timeout
    ideal a branch with the correct add and only_p

The ticket fixes no flag names, so the bench reads `--help` and takes the cross flag
(`--cross` | `--cross-tests`), the output-dir flag (`--cross-out` | `--out`), the cell-timeout flag
(`--cell-timeout` | `--cross-timeout`) and `--jobs` from it. An output-dir flag may name the folder
itself or its parent (`<out>/99/`): both are read.

Run it inside the entry's checkout:  python3 -m pytest contest-bench/158/acceptance_158.py -n 0 -q
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "judge_epic_round.py"
ROUND = 99

BASE_LIB = "def add(a, b):\n    return a + b\n"
P_LIB = BASE_LIB + "\n\ndef only_p():\n    return 1\n"
Q_LIB = "def add(a, b):\n    return a + b + 1\n"
BASE_TEST = "import lib\n\n\ndef test_base_add():\n    assert lib.add(1, 1) == 2\n"
P_TEST = '''"""p's tests."""
import lib


def test_imports():
    assert lib.add is not None


def test_only_p():
    assert lib.only_p() == 1


def test_wrong():
    assert lib.add(2, 3) == 7


def test_add():
    assert lib.add(2, 3) == 5
'''
Q_TEST = '''"""q's test."""
import lib


def test_q_ok():
    assert lib.add(0, 0) in (0, 1)
'''
R_TEST = '''"""r's test: a module-level import of a name only p's code has."""
from lib import only_p


def test_r_import():
    assert only_p() == 1
'''
H_TEST = '''"""h's test: never ends by itself."""
import time


def test_h_hang():
    time.sleep(600)
'''
TICKET = "# 99 — fake ticket for the cross-test bench\n\n**Status:** open\n**File:** lib.py\n"


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          cwd=cwd, capture_output=True, text=True, check=check)


def _commit_all(cwd, msg):
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", msg)
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    top = tmp_path_factory.mktemp("cross158")
    repo = top / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "epic-tasks").mkdir()
    (repo / "lib.py").write_text(BASE_LIB, encoding="utf-8")
    (repo / "conftest.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_base.py").write_text(BASE_TEST, encoding="utf-8")
    (repo / "epic-tasks" / "99-fake-cross.md").write_text(TICKET, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    base = _commit_all(repo, "base")

    trees = {}
    for name, lib, tests in (("p", P_LIB, {"tests/test_p_features.py": P_TEST}),
                             ("q", Q_LIB, {"tests/test_q_features.py": Q_TEST}),
                             ("r", BASE_LIB, {"tests/test_r_import.py": R_TEST}),
                             ("h", BASE_LIB, {"tests/test_h_hang.py": H_TEST}),
                             ("ideal", P_LIB, {})):
        wt = top / f"wt-{name}"
        _git(repo, "worktree", "add", "-q", "-b", f"entry-{name}", str(wt), base)
        (wt / "lib.py").write_text(lib, encoding="utf-8")
        for rel, body in tests.items():
            (wt / rel).write_text(body, encoding="utf-8")
        _commit_all(wt, f"{name}: the entry")
        trees[name] = wt
    return {"top": top, "repo": repo, "base": base, "trees": trees}


def _help() -> str:
    proc = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60)
    return proc.stdout + proc.stderr


def _flag(options: tuple[str, ...]) -> str | None:
    text = _help()
    for opt in options:
        if re.search(r"(?<![\w-])" + re.escape(opt) + r"(?![\w-])", text):
            return opt
    return None


def _run_judge(world, *, names=("p", "q", "r"), ideal=False, out=None, tag="run", extra=(), timeout=600):
    cross = _flag(("--cross", "--cross-tests"))
    assert cross, "the judge has no flag that starts the cross phase (--cross / --cross-tests)"
    tmpdir = world["top"] / f"tmp-{tag}"
    tmpdir.mkdir(exist_ok=True)
    cmd = [sys.executable, str(SCRIPT), "--round", str(ROUND),
           "--tasks", str(world["repo"] / "epic-tasks"), "--base", world["base"], cross]
    for name in names:
        cmd += ["--worktree", f"{name}={world['trees'][name]}"]
    if ideal:
        cmd += ["--ideal", "entry-ideal"]
    if out is not None:
        out_flag = _flag(("--cross-out", "--out"))
        assert out_flag, "no output-dir flag (--cross-out / --out)"
        cmd += [out_flag, str(out)]
    cmd += list(extra)
    env = {**os.environ, "TMPDIR": str(tmpdir), "PYTHONDONTWRITEBYTECODE": "1"}
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=world["repo"], capture_output=True, text=True, timeout=timeout, env=env)
    return proc, tmpdir, time.monotonic() - started


def _find_md(out_dir: Path) -> Path | None:
    found = sorted(Path(out_dir).rglob("cross.md")) if Path(out_dir).is_dir() else []
    return found[0] if found else None


@pytest.fixture(scope="module")
def default_run(world):
    """One run with --ideal and no output-dir flag: the files land where the ticket says."""
    proc, tmpdir, _took = _run_judge(world, ideal=True, tag="default")
    out_dir = world["repo"] / "contest-out" / str(ROUND)
    return {"proc": proc, "tmpdir": tmpdir, "out_dir": out_dir,
            "md": (out_dir / "cross.md").read_text(encoding="utf-8") if (out_dir / "cross.md").is_file() else "",
            "json": (out_dir / "cross.json") if (out_dir / "cross.json").is_file() else None}


def _text(run) -> str:
    return run["proc"].stdout + "\n" + run["md"]


CLASSES = ("api", "base", "behaviour")
# a path or a node id in a message (`.../impl-base/lib.py`, `tests/_xcross_p_x.py::t`) is no class word
_PATHS = re.compile(r"\S*[/\\]\S*")


def _labels(text: str, test: str) -> set[str]:
    """The classes printed for *test*: the words on every line that names it, plus the line after
    it when that line names no other test and is no heading (`api` on its own below the name).
    A test that fails in several cells must carry the same one class in all of them."""
    lines = text.splitlines()
    found: set[str] = set()
    for i, line in enumerate(lines):
        if test not in line:
            continue
        chunk = [line]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt.strip() and "::" not in nxt and "test_" not in nxt and not nxt.lstrip().startswith(("#", "|", "-")):
            chunk.append(nxt)
        words = _PATHS.sub(" ", " ".join(chunk).replace(test, " "))
        for word in CLASSES:
            if re.search(r"\b" + word + r"\b", words, re.I):
                found.add(word)
    return found


def _near(text: str, test: str, word: str) -> bool:
    """Kept for the passing-test check: *word* is among the labels of *test*."""
    return word in _labels(text, test)


def _header_cells(md: str) -> list[str]:
    """Every cell of every markdown table row, lower-cased: a column named `ideal` is among them."""
    cells = []
    for line in md.splitlines():
        if line.lstrip().startswith("|"):
            cells.extend(c.strip().strip("`*").lower() for c in line.strip().strip("|").split("|"))
    return cells


# ── the run ─────────────────────────────────────────────────────────────────
def test_the_judge_exits_zero_with_the_cross_phase(default_run):
    assert default_run["proc"].returncode == 0, default_run["proc"].stderr[-800:]


def test_the_score_table_comes_before_the_matrix(default_run):
    out = default_run["proc"].stdout
    score = re.search(r"Round 99|\bgate\b|\bcommits\b", out)
    matrix = re.search(r"cross|matrix", out, re.I)
    assert score and matrix and score.start() < matrix.start()


def test_cross_json_and_cross_md_are_written_to_contest_out_nn(default_run):
    assert default_run["md"].strip(), "contest-out/99/cross.md missing or empty"
    assert default_run["json"] is not None, "contest-out/99/cross.json missing"
    json.loads(default_run["json"].read_text(encoding="utf-8"))


# ── the matrix ───────────────────────────────────────────────────────────────
def test_the_cells_count_passed_over_total(default_run):
    text = _text(default_run)
    # p's four tests: 3/4 on its own code, 1/4 on q's, 2/4 on the base, 3/4 on the ideal; q's one test 1/1
    for cell in ("3/4", "1/4", "2/4", "1/1"):
        assert cell in text, f"cell {cell} not in the matrix"
    assert text.count("3/4") >= 2          # p on p, p on ideal


def test_the_base_and_the_ideal_are_columns(default_run):
    cells = _header_cells(default_run["md"])
    assert "base" in cells and "ideal" in cells, cells[:12]


# ── classification ──────────────────────────────────────────────────────────
def test_a_name_only_the_other_code_has_is_api(default_run):
    """Every cell it fails in says `api` and nothing else — on the base too (AttributeError comes first)."""
    assert _labels(_text(default_run), "test_only_p") == {"api"}


def test_a_module_that_cannot_import_on_the_other_code_is_api(default_run):
    """A collection error (ImportError at module level) is the commonest `api` there is."""
    assert _labels(_text(default_run), "test_r_import") == {"api"}


def test_a_test_that_fails_on_the_base_too_is_base(default_run):
    """`base` in every cell it fails in — never `api` borrowed from another failure's error line."""
    assert _labels(_text(default_run), "test_wrong") == {"base"}


def test_a_failure_that_only_the_other_code_has_is_behaviour_with_its_e_line(default_run):
    text = _text(default_run)
    assert _labels(text, "test_add") == {"behaviour"}
    assert re.search(r"6\s*==\s*5", default_run["md"]), "the `E ` line of the failing assert is not in cross.md"


def test_a_passing_test_is_not_listed_as_a_failure(default_run):
    assert _labels(default_run["md"], "test_imports") == set()


# ── the rules ────────────────────────────────────────────────────────────────
def test_no_entry_worktree_is_edited_in_place(world, default_run):
    for name, wt in world["trees"].items():
        assert _git(wt, "status", "--porcelain").stdout.strip() == "", f"{name}: worktree is dirty"
        assert not list(Path(wt).rglob("_xcross_*")), f"{name}: an _xcross_ file was left in the worktree"


def test_no_scratch_copy_or_basetemp_is_left_in_tmpdir(default_run):
    left = sorted(p.name for p in default_run["tmpdir"].iterdir())
    assert left == [], f"left in TMPDIR: {left[:5]}"


def test_an_explicit_output_dir_gets_the_files_and_no_ideal_means_no_ideal_column(world, tmp_path):
    out = tmp_path / "xout"
    proc, tmpdir, _took = _run_judge(world, ideal=False, out=out, tag="explicit")
    assert proc.returncode == 0, proc.stderr[-800:]
    md = _find_md(out)
    assert md is not None, f"no cross.md under {out}: {sorted(str(p) for p in out.rglob('*'))[:5]}"
    assert any(out.rglob("cross.json"))
    assert "ideal" not in _header_cells(md.read_text(encoding="utf-8"))
    assert sorted(p.name for p in tmpdir.iterdir()) == []


def test_a_second_run_overwrites_cleanly(world, default_run):
    proc, _tmpdir, _took = _run_judge(world, ideal=True, tag="again")
    assert proc.returncode == 0, proc.stderr[-800:]
    again = (world["repo"] / "contest-out" / str(ROUND) / "cross.md").read_text(encoding="utf-8")
    assert "3/4" in again and "1/4" in again


def test_two_jobs_give_the_same_matrix_and_leave_nothing(world, tmp_path):
    jobs = _flag(("--jobs",))
    assert jobs, "no --jobs flag (the ticket: `--jobs N` opt-in)"
    out = tmp_path / "jout"
    proc, tmpdir, _took = _run_judge(world, ideal=True, out=out, tag="jobs", extra=(jobs, "2"))
    assert proc.returncode == 0, proc.stderr[-800:]
    md = _find_md(out)
    assert md is not None
    text = md.read_text(encoding="utf-8")
    for cell in ("3/4", "1/4", "2/4", "1/1"):
        assert cell in text, f"cell {cell} not in the matrix of a --jobs 2 run"
    assert sorted(p.name for p in tmpdir.iterdir()) == []


def test_a_test_that_never_ends_is_ended_at_the_cell_timeout_and_leaves_no_process(world, tmp_path):
    flag = _flag(("--cell-timeout", "--cross-timeout"))
    if flag is None:
        pytest.fail("no cell-timeout flag: a hanging test would hold the judge for the default budget")
    out = tmp_path / "hout"
    proc, tmpdir, took = _run_judge(world, names=("h",), out=out, tag="hang", extra=(flag, "6"), timeout=240)
    assert proc.returncode == 0, proc.stderr[-800:]
    assert took < 150, f"the judge took {took:.0f}s: the cell timeout did not end the hanging test"
    assert _find_md(out) is not None
    time.sleep(1.0)
    alive = subprocess.run(["pgrep", "-f", "test_h_hang"], capture_output=True, text=True).stdout.split()
    assert alive == [], f"the hanging test's pytest is still running: {alive}"
    assert sorted(p.name for p in tmpdir.iterdir()) == []
