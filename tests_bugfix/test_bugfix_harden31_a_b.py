"""HARDEN-31 (round 330): the two findings the 2026-11-02 re-review of c5bece0 marked only partly fixed.

A: the workspace ownership registry lived beside workspace_root and held directory names
only, so sibling roots a/ and b/ shared it, and after executor A created a/OLD, executor B
deleted an unrelated b/OLD with user data in it. B: the test cache left `PYTHONPATH=.` out
of its key, so a bare `pytest tests` that fails on an ImportError and `PYTHONPATH=. pytest
tests` that passes had one key.

Each test fails on the round's base (e427cc5) except the guards.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from tools.auto.executor import Executor
from tools.contest import testcache, testcache_glue


def _src(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("x = 1\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"]):
        subprocess.run(["git", *args], cwd=src, check=True, capture_output=True)
    return src


def _run(src, root, tid):
    Executor(src, timeout_sec=30, workspace_root=root, max_retained_workspaces=1).run(
        {"id": tid, "target_files": ["a.py"], "acceptance_check": "true"})


def _user_dir(path):
    path.mkdir(parents=True)
    (path / "user-data.txt").write_text("keep me\n")
    old = time.time() - 3600
    os.utime(path, (old, old))


def test_a_a_name_another_root_recorded_does_not_delete_a_sibling_roots_directory(tmp_path):
    src, shared = _src(tmp_path), tmp_path / "shared"
    _run(src, shared / "a", "OLD")
    _user_dir(shared / "b" / "OLD")
    _run(src, shared / "b", "NEW")
    assert (shared / "b" / "OLD" / "user-data.txt").exists()


def test_a_a_registry_of_plain_names_deletes_nothing(tmp_path):
    src, root = _src(tmp_path), tmp_path / "shared" / "b"
    root.mkdir(parents=True)
    (tmp_path / "shared" / ".agent-workspaces.json").write_text(json.dumps(["OLD"]))
    _user_dir(root / "OLD")
    _run(src, root, "NEW")
    assert (root / "OLD" / "user-data.txt").exists()


def test_a_guard_each_root_still_trims_its_own_old_workspaces(tmp_path):
    src, shared = _src(tmp_path), tmp_path / "shared"
    for root, first, second in ((shared / "a", "A1", "A2"), (shared / "b", "B1", "B2")):
        _run(src, root, first)
        old = time.time() - 3600
        os.utime(root / first, (old, old))
    _run(src, shared / "a", "A2")
    _run(src, shared / "b", "B2")
    assert sorted(p.name for p in (shared / "a").iterdir() if p.is_dir()) == ["A2"]
    assert sorted(p.name for p in (shared / "b").iterdir() if p.is_dir()) == ["B2"]


def _key(command):
    return [testcache_glue.run_key(run) for run in testcache.parse_pytest(command)]


@pytest.mark.parametrize("command", [
    "PYTHONPATH=. pytest tests -q",
    "env PYTHONPATH=. pytest tests -q",
    "PYTHONPATH=./ pytest tests -q",
    "export PYTHONPATH=. && pytest tests -q",
])
def test_b_pythonpath_dot_is_its_own_key(command):
    assert _key(command) and _key(command) != _key("pytest tests -q")


def test_b_the_two_runs_really_differ(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "mod.py").write_text("X = 1\n")
    (tmp_path / "tests" / "test_x.py").write_text("import mod\ndef test_x():\n    assert mod.X == 1\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    plain = subprocess.run(["bash", "-c", "pytest tests -q -p no:cacheprovider"], cwd=tmp_path, env=env,
                           capture_output=True)
    dotted = subprocess.run(["bash", "-c", "PYTHONPATH=. pytest tests -q -p no:cacheprovider"], cwd=tmp_path,
                            env=env, capture_output=True)
    assert plain.returncode != dotted.returncode
    assert _key("pytest tests -q") != _key("PYTHONPATH=. pytest tests -q")


def test_b_guard_pythondontwritebytecode_still_answers_a_plain_run():
    assert _key("PYTHONDONTWRITEBYTECODE=1 pytest tests -q") == _key("pytest tests -q")
