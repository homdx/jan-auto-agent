"""tests/test_harden29_retention_context_splice_basecheck_env.py — HARDEN-29.

One reproduction per letter of the round's ticket:
A — workspace retention never deletes a directory it does not own (the
    source repository, a sibling project) and still trims old marked
    workspaces;
B — context retrieval never follows a symlink out of the repository, and a
    link that stays inside still resolves;
C — the project search never reads a stale ``.agent/workspace`` mirror — the
    live source wins;
D — a changed module-level statement in a partly-shown file replaces the one
    it changes (a complete reply is written as the model wrote it), and a
    reply that drops an unseen function is still refused;
E — two overlapping base checks of one commit both succeed and neither
    removes the other's live checkout, and a killed run's leftover is cleaned
    by the next check;
F — an environment assignment that reaches pytest is part of the cache key.
"""

from __future__ import annotations

import configparser
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.arena import basecheck
from tools.auto.context_broker import ContextBroker
from tools.auto.coder import Coder
from tools.auto.executor import Executor


def _git(cwd: Path, *words: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *words],
        capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


# ── A ────────────────────────────────────────────────────────────────────────

def test_a_prune_keeps_the_source_and_a_sibling_and_trims_old_workspaces(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "f.txt").write_text("x", encoding="utf-8")
    _git(source, "init", "-q")
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "one")
    unrelated = tmp_path / "unrelated-project"
    unrelated.mkdir()
    (unrelated / "g.txt").write_text("y", encoding="utf-8")

    maker = Executor(source, workspace_root=tmp_path, max_retained_workspaces=10)
    maker._prepare_workspace("OLD1", [])
    maker._prepare_workspace("OLD2", [])

    # A shared parent as workspace_root: source and the sibling are among the
    # candidates but neither is owned by the executor, so neither may go.
    pruning = Executor(source, workspace_root=tmp_path, max_retained_workspaces=1)
    pruning._prepare_workspace("NEW", [])

    assert source.is_dir(), "the source repository must never be pruned"
    assert unrelated.is_dir(), "an unrelated sibling must never be pruned"
    assert not (tmp_path / "OLD1").exists(), "the old marked workspace is trimmed"
    assert not (tmp_path / "OLD2").exists(), "the old marked workspace is trimmed"
    assert (tmp_path / "NEW").is_dir()


def test_a_marker_only_marks_workspaces_the_executor_made(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "f.txt").write_text("x", encoding="utf-8")
    _git(source, "init", "-q")
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "one")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("keep", encoding="utf-8")

    ex = Executor(source, workspace_root=tmp_path, max_retained_workspaces=1)
    ex._prepare_workspace("T1", [])
    ex._prune_old_workspaces(keep=tmp_path / "T1")

    assert foreign.is_dir(), "a directory without the executor's mark is never a candidate"


# ── B and C ──────────────────────────────────────────────────────────────────

def _repo_with_mirror(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("import helper\nprint('main')\n", encoding="utf-8")
    (repo / "helper.py").write_text("def helper():\n    return 'CURRENT'\n", encoding="utf-8")
    mirror = repo / ".agent" / "workspace" / "AUTO-T1"
    mirror.mkdir(parents=True)
    (mirror / "helper.py").write_text("def helper():\n    return 'STALE'\n", encoding="utf-8")
    return repo


def test_b_a_chapter_symlinked_out_is_never_returned(tmp_path):
    repo = _repo_with_mirror(tmp_path)
    outside = tmp_path / "private.md"
    outside.write_text("PRIVATE OUTSIDE", encoding="utf-8")
    (repo / "chapter_9.md").write_text("", encoding="utf-8")
    (repo / "chapter_9.md").unlink()
    os.symlink(outside, repo / "chapter_9.md")

    # Prose task (no code targets): the whole-file pass runs and must refuse
    # the link that leaves the repository, so the outside text never appears.
    out = ContextBroker().resolve(["chapter_9"], [], repo)
    assert out.get("chapter_9", "") == "", "the outside chapter is never read"


def test_b_a_stem_symlinked_out_is_never_returned(tmp_path):
    repo = _repo_with_mirror(tmp_path)
    outside = tmp_path / "private.md"
    outside.write_text("PRIVATE OUTSIDE", encoding="utf-8")
    os.symlink(outside, repo / "prologue.md")

    out = ContextBroker().resolve(["prologue"], [], repo)
    assert "PRIVATE" not in out.get("prologue", "")


def test_b_a_symbol_reached_through_a_symlinked_directory_is_never_returned(tmp_path):
    repo = _repo_with_mirror(tmp_path)
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "helper.py").write_text("def helper():\n    return 'OUTSIDE'\n", encoding="utf-8")
    os.symlink(outside, repo / "ext")

    out = ContextBroker().resolve(["helper"], ["main.py"], repo)
    assert "OUTSIDE" not in out.get("helper", "")


def test_b_a_link_that_stays_inside_still_resolves(tmp_path):
    repo = _repo_with_mirror(tmp_path)
    (repo / "real_prologue.md").write_text("INSIDE OK", encoding="utf-8")
    os.symlink(repo / "real_prologue.md", repo / "prologue.md")

    out = ContextBroker().resolve(["prologue"], [], repo)
    assert "INSIDE OK" in out.get("prologue", "")


def test_c_the_live_source_wins_over_a_stale_agent_mirror(tmp_path):
    repo = _repo_with_mirror(tmp_path)
    out = ContextBroker().resolve(["helper"], ["main.py"], repo)
    assert "CURRENT" in out.get("helper", "")
    assert "STALE" not in out.get("helper", "")


def test_c_nothing_under_agent_is_read(tmp_path):
    repo = _repo_with_mirror(tmp_path)
    mirror = repo / ".agent" / "workspace" / "AUTO-T1"
    (mirror / "only_in_mirror.py").write_text("def ghost():\n    return 'GHOST'\n", encoding="utf-8")
    out = ContextBroker().resolve(["ghost"], ["main.py"], repo)
    assert "ghost" not in out, "a symbol that exists only under .agent is never resolved"


# ── D ────────────────────────────────────────────────────────────────────────

def _coder(tmp_path: Path, budget: int) -> Coder:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "t",
                      "model": "m", "api_format": "openai"},
        "loop": {"timeout_seconds": "300"},
        "coder": {"temperature": "0.2", "max_tokens": "4096"},
    })
    coder = Coder(config=cfg, base_url="http://localhost:1337/v1", api_key="t", model="m")
    coder._max_file_chars = budget
    return coder


def _large_module() -> str:
    funcs = "".join(f"def f{i}(x):\n    return x + {i}\n\n\n" for i in range(8))
    return "import os\n\nLIMIT = 10\n\n" + funcs + "def helper():\n    return LIMIT\n"


def _splice_coder(tmp_path, src: str, budget: int, cited: str) -> Coder:
    (tmp_path / "m.py").write_text(src, encoding="utf-8")
    coder = _coder(tmp_path, budget)
    coder._read_file_contents(["m.py"], tmp_path, cited_symbol=cited)
    return coder


def test_d_a_changed_module_level_constant_replaces_the_original(tmp_path):
    src = _large_module()
    coder = _splice_coder(tmp_path, src, 140, "helper")
    reply = src.replace("LIMIT = 10", "LIMIT = 20")
    spliced = coder._splice_partial_reply("m.py", reply)
    assert spliced is not None
    ns: dict = {}
    exec(spliced, ns)
    assert ns["LIMIT"] == 20, "the changed constant must evaluate to the new value"
    assert spliced.count("LIMIT =") == 1, "the old and the new constant are not both kept"


def test_d_a_complete_reply_is_written_as_the_model_wrote_it(tmp_path):
    src = _large_module()
    coder = _splice_coder(tmp_path, src, 140, "helper")
    removed = "\n".join(l for l in src.splitlines() if not l.startswith("LIMIT ="))
    assert coder._splice_partial_reply("m.py", removed) == removed


def test_d_a_module_level_if_block_edit_is_applied(tmp_path):
    src = ("import os\n\nDEBUG = True\n\nif DEBUG:\n    LIMIT = 10\n\n"
           + "".join(f"def f{i}(x):\n    return x + {i}\n\n\n" for i in range(8))
           + "def helper():\n    return LIMIT\n")
    coder = _splice_coder(tmp_path, src, 120, "helper")
    reply = src.replace("    LIMIT = 10", "    LIMIT = 99")
    spliced = coder._splice_partial_reply("m.py", reply)
    assert spliced is not None
    ns: dict = {}
    exec(spliced, ns)
    assert ns["LIMIT"] == 99


def test_d_a_reply_that_drops_an_unseen_function_is_refused(tmp_path):
    funcs = "".join(f"def f{i}(x):\n    return x + {i}  # {'pad ' * 10}\n\n\n" for i in range(12))
    src = "import os\n\n\n" + funcs + "def dead_helper(a, b):\n    return a - b\n"
    coder = _splice_coder(tmp_path, src, 300, "f5")
    err = coder._partial_view_error("m.py", src.replace("def f11(", "def f11_gone("),
                                    tmp_path / "m.py")
    assert err.startswith("[SAFETY]") and "f11" in err


# ── E ────────────────────────────────────────────────────────────────────────

def _basecheck_args(**overrides) -> SimpleNamespace:
    base = dict(ref=None, force=True, keep=False, dry_run=False, output="json")
    base.update(overrides)
    return SimpleNamespace(**base)


def _good_steps() -> list[basecheck.Step]:
    return [
        basecheck.Step("tests", [sys.executable, "-c", "print('3 passed in 0.01s')"]),
        basecheck.Step("tests_bugfix", [sys.executable, "-c", "print('2 passed in 0.02s')"]),
        basecheck.Step("tiers", [sys.executable, "-c", "print('ok')"]),
        basecheck.Step("clocks", [sys.executable, "-c", "print('ok')"]),
    ]


def _basecheck_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "marker.txt").write_text("committed\n", encoding="utf-8")
    (root / "contest.ini").write_text(
        "[contest]\nout_dir = contest-out\n[arena]\nprofile = base\n"
        "[arena.profile.base]\nbranch = HEAD\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    _git(root, "branch", "base")
    (root / "marker.txt").write_text("dirty\n", encoding="utf-8")
    _git(root, "commit", "-qam", "two")
    return root


def test_e_two_overlapping_checks_of_one_commit_both_pass(tmp_path, monkeypatch):
    repo = _basecheck_repo(tmp_path)
    monkeypatch.setattr(basecheck, "STEPS",
                        tuple((s.name, list(s.command)) for s in _good_steps()))
    head = _git(repo, "rev-parse", "HEAD")
    tree = basecheck.worktree_path(repo, head)
    entered = threading.Event()
    release = threading.Event()
    seen: dict = {}
    calls = {"n": 0}
    gate = threading.Lock()

    def runner(line, **kwargs):
        with gate:
            calls["n"] += 1
            n = calls["n"]
        marker = tree / "marker.txt"
        if n == 1:                       # the first run's first step: plant a marker
            marker.write_text("run1 live", encoding="utf-8")
            entered.set()
            release.wait(timeout=15)
            seen["run1_tree_survived"] = marker.exists()
        elif n == 2:                     # the second run, once it can run its step
            seen["run2_saw"] = marker.exists()
        return subprocess.CompletedProcess(list(line), 0, stdout="3 passed", stderr="")

    monkeypatch.setattr(basecheck, "RUN", runner)
    results: list = []

    def run():
        try:
            results.append(basecheck.run_base_check(repo, _basecheck_args()))
        except Exception as exc:  # noqa: BLE001 — the test wants the return code
            results.append(exc)

    t1 = threading.Thread(target=run)
    t1.start()
    assert entered.wait(timeout=15), "the first run never reached its runner"
    t2 = threading.Thread(target=run)
    t2.start()
    time.sleep(0.5)              # let the second run reach its own runner
    release.set()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert results == [0, 0], f"both overlapping checks must exit 0, got {results}"
    assert seen.get("run1_tree_survived") is True, \
        "no overlapping check may remove the first run's live checkout"
    assert not tree.exists(), "no live checkout may be left behind"


def test_e_a_killed_runs_leftover_is_removed_by_the_next_check(tmp_path, monkeypatch):
    repo = _basecheck_repo(tmp_path)
    monkeypatch.setattr(basecheck, "STEPS",
                        tuple((s.name, list(s.command)) for s in _good_steps()))
    head = _git(repo, "rev-parse", "HEAD")
    tree = basecheck.worktree_path(repo, head)
    tree.mkdir(parents=True)
    (tree / "junk.txt").write_text("left by a crash", encoding="utf-8")
    rec = []

    def record(line, **kwargs):
        rec.append(list(line))
        return subprocess.CompletedProcess(list(line), 0, stdout="ok", stderr="")

    monkeypatch.setattr(basecheck, "RUN", record)
    code = basecheck.run_base_check(repo, _basecheck_args())
    assert code == 0
    assert not tree.exists(), "the leftover is removed by the next check"
    assert len(rec) == 4


# ── F ────────────────────────────────────────────────────────────────────────

def _run_key(command: str) -> str:
    from tools.contest.testcache import parse_pytest
    from tools.contest.testcache_glue import run_key
    runs = parse_pytest(command)
    assert runs, f"expected a pytest run in {command!r}"
    return run_key(runs[0])


def test_f_an_env_assignment_that_reaches_pytest_is_part_of_the_key():
    plain = _run_key("pytest tests -q")
    assert _run_key("FEATURE_X=1 pytest tests -q") != plain
    assert _run_key("env FEATURE_X=1 pytest tests -q") != plain
    assert _run_key("export FEATURE_X=1 && pytest tests -q") != plain
    assert _run_key("FEATURE_X=1; pytest tests -q") != plain


def test_f_a_different_value_is_a_miss_and_the_same_value_hits():
    one = _run_key("FEATURE_X=1 pytest tests -q")
    two = _run_key("FEATURE_X=2 pytest tests -q")
    assert one != two
    assert _run_key("FEATURE_X=1 pytest tests -q") == one


def test_f_an_assignment_that_cannot_change_an_outcome_is_ignored():
    assert _run_key("PYTHONDONTWRITEBYTECODE=1 pytest tests -q") \
        == _run_key("pytest tests -q")


def test_f_an_assignment_the_parser_cannot_read_is_not_cached():
    from tools.contest.testcache import parse_pytest
    assert parse_pytest("X=$(date) pytest tests") == []
    assert parse_pytest("X=`date` pytest tests") == []
    assert parse_pytest("unset X && pytest tests") == []
