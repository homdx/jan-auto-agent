"""tests/test_auto_procs_o_review.py — processes, git and config reads, after review batch o."""

from __future__ import annotations

import configparser
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import judge_epic_round  # noqa: E402
from tools.arena import profile as arena_profile  # noqa: E402
from tools.auto import git_manager as gm  # noqa: E402
from tools.auto.executor import Executor, pytest_config_requests_pool  # noqa: E402
from tools.collect import manifest as mf  # noqa: E402
from tools.contest import harvest  # noqa: E402
from tools.contest import kilo_client  # noqa: E402
from tools.skills.loader import apply_skill  # noqa: E402

posix = pytest.mark.skipif(os.name == "nt", reason="POSIX")


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd,
                          capture_output=True, check=True).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    return path


# ── skills ──────────────────────────────────────────────────────────────────────────────────

def test_a_percent_in_a_skill_body_reaches_the_prompt(tmp_path):
    skills = tmp_path / "skills"
    (skills / "pct").mkdir(parents=True)
    src = (ROOT / "skills" / "hello-code.skill.ini").read_text(encoding="utf-8")
    src = src.replace("hello-code", "pct").replace("min_num_ctx = 16384", "min_num_ctx = 1024")
    (skills / "pct.skill.ini").write_text(src, encoding="utf-8")
    body = (ROOT / "skills" / "hello-code" / "SKILL.md").read_text(encoding="utf-8")
    (skills / "pct" / "SKILL.md").write_text(body + "\nCoverage must reach 100% before you stop.\n", encoding="utf-8")
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))     # what main.py builds
    cfg.read(ROOT / "agents.ini", encoding="utf-8")
    overlay = apply_skill(cfg, "pct", tmp_path, skills_dir="skills")
    section, key, _ = next(e for e in overlay.entries if "100%" in e[2])
    assert "Coverage must reach 100% before you stop." in cfg.get(section, key)


# ── the acceptance check's process ──────────────────────────────────────────────────────────

@posix
def test_a_timed_out_check_takes_its_children_with_it(tmp_path):
    pid_file = tmp_path / "pid"
    child = f"{sys.executable} -c \"import os,time; open('{pid_file}','w').write(str(os.getpid())); time.sleep(60)\""
    ex = Executor(tmp_path, timeout_sec=1.0)
    started = time.monotonic()
    result = ex._execute(f"{child}; echo done", cwd=tmp_path, task_id="T1")
    assert result.timed_out and time.monotonic() - started < 15
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)
        pytest.fail("the check's child outlived its timeout")


def test_the_interpreter_path_is_quoted(tmp_path):
    ex = Executor(tmp_path, python_bin="/Users/Jane Doe/.venv/bin/python")
    rewritten = ex._rewrite_python("python -m pytest -q")
    assert rewritten.endswith(" -m pytest -q")
    assert rewritten[:-len(" -m pytest -q")] in ("'/Users/Jane Doe/.venv/bin/python'",
                                                "\"/Users/Jane Doe/.venv/bin/python\"")


@pytest.mark.parametrize("files, pool", [
    ({"pytest.ini": "[pytest]\naddopts =\n    -q\n    -n auto\n"}, True),
    ({"pyproject.toml": "[tool.black]\nline-length = 100\n", "tox.ini": "[pytest]\naddopts = -n 4\n"}, True),
    ({"pyproject.toml": '[tool.pytest.ini_options]\naddopts = ["-q", "-n", "auto"]\n'}, True),
    ({"pyproject.toml": '[tool.pytest.ini_options]\naddopts = "-q"\n', "tox.ini": "[pytest]\naddopts = -n 4\n"}, False),
    ({"pytest.ini": "", "tox.ini": "[pytest]\naddopts = -n 4\n"}, False),
])
def test_addopts_are_read_from_the_file_pytest_uses(tmp_path, monkeypatch, files, pool):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    assert pytest_config_requests_pool(tmp_path) is pool


def test_addopts_of_a_pyproject_without_tomllib(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.setitem(sys.modules, "tomllib", None)
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = [\n  "-q",\n  "-n", "auto",\n]\n',
                                             encoding="utf-8")
    assert pytest_config_requests_pool(tmp_path) is True


# ── git output ──────────────────────────────────────────────────────────────────────────────

@posix
def test_a_name_that_is_not_utf8_is_dirty_not_a_crash(tmp_path):
    root = _repo(tmp_path / "r")
    _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    (root / os.fsdecode(b"caf\xe9.py")).write_text("x = 1\n", encoding="utf-8")
    assert mf.is_dirty(root) is True
    assert mf.capture_provenance(root)[1] is True


def test_the_first_porcelain_line_keeps_its_path(tmp_path):
    root = _repo(tmp_path / "r")
    (root / ".collect").mkdir()
    (root / ".collect" / "artifact.json").write_text("{}\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    (root / ".collect" / "artifact.json").write_text('{"a": 1}\n', encoding="utf-8")   # " M .collect/artifact.json"
    assert mf.is_dirty(root, exclude_dir=root / ".collect") is False


@posix
def test_one_test_name_that_is_not_utf8_does_not_hide_the_others(tmp_path):
    root = _repo(tmp_path / "r")
    _git(root, "commit", "-q", "--allow-empty", "-m", "base")
    base = _git(root, "rev-parse", "HEAD").decode().strip()
    (root / "tests").mkdir()
    (root / "tests" / "test_ok.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (root / "tests" / os.fsdecode(b"test_caf\xe9.py")).write_text("def test_y(): pass\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "tests")
    assert "tests/test_ok.py" in judge_epic_round.changed_tests(str(root), base)


def test_a_git_failure_is_no_silent_skip_of_the_reset(tmp_path, monkeypatch):
    root = _repo(tmp_path / "r")
    _git(root, "commit", "-q", "--allow-empty", "-m", "init")
    real = gm.run_git

    def broken(cmd, *a, **kw):
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: something else")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(gm, "run_git", broken)
    with pytest.raises(gm.GitError):
        gm.GitManager(root).discard_working_changes()


def test_a_repository_without_a_commit_has_nothing_to_reset(tmp_path):
    root = _repo(tmp_path / "r")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    gm.GitManager(root).discard_working_changes()
    assert (root / "a.py").exists()


def test_the_paths_of_a_root_commit(tmp_path):
    root = _repo(tmp_path / "r")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "a.py")
    _git(root, "commit", "-q", "-m", "root")
    g = gm.GitManager(root)
    assert g.paths_changed_in(g.get_current_hash()) == ["a.py"]


def test_harvest_without_git_says_no(tmp_path, monkeypatch):
    def missing(*a, **kw):
        raise FileNotFoundError("git")
    monkeypatch.setattr(harvest.subprocess, "run", missing)
    assert harvest._is_ancestor(tmp_path, "abc") is False


# ── config reads ────────────────────────────────────────────────────────────────────────────

def test_a_path_in_skip_dirs_is_named_and_ignored(caplog):
    cfg = configparser.ConfigParser()
    cfg.read_string("[collect]\nskip_dirs = rounds/sandbox, sandbox2\n")
    with caplog.at_level("WARNING"):
        dirs = mf.skip_dirs_from_config(cfg)
    assert "sandbox2" in dirs and "rounds/sandbox" not in dirs
    assert "rounds/sandbox" in caplog.text


def test_arena_reads_a_contest_ini_with_a_bom(tmp_path):
    (tmp_path / "contest.ini").write_bytes(b"\xef\xbb\xbf[arena.profile.p1]\nmodels = a/b\n")
    profiles, _ = arena_profile.load_profiles(tmp_path)
    assert "p1" in profiles


# ── kilo serve ──────────────────────────────────────────────────────────────────────────────

@posix
def test_a_200_from_another_server_is_not_this_childs_health(tmp_path, monkeypatch):
    monkeypatch.setattr(kilo_client, "_http_status", lambda url, timeout=3.0: 200)
    exits = tmp_path / "kilo"
    exits.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    os.chmod(exits, 0o755)
    monkeypatch.setattr(kilo_client.time, "sleep", lambda s: None)
    real_poll = subprocess.Popen.poll

    def settled_poll(self):
        self.wait()                       # the child has exited by the time the 200 comes back
        return real_poll(self)

    monkeypatch.setattr(subprocess.Popen, "poll", settled_poll)
    with pytest.raises(kilo_client.KiloServerError, match="exited"):
        kilo_client.KiloServer.spawn(str(exits), log_path=str(tmp_path / "serve.log"), health_timeout=5)


def test_judge_round_refuses_a_path_for_a_round(tmp_path):
    proc = subprocess.run(["bash", str(ROOT / "scripts" / "judge_round.sh"), "../x"], capture_output=True, text=True,
                          env={**os.environ, "OUT": str(tmp_path / "out"), "BASE": "HEAD", "QUICK": "1"}, timeout=60)
    assert proc.returncode == 2 and "not a path" in proc.stderr
    assert not (tmp_path / "out").exists()
