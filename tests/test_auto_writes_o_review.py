"""tests/test_auto_writes_o_review.py — what the tools write and where, after review batch o."""

from __future__ import annotations

import configparser
import glob
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import actions as A  # noqa: E402
from tools.arena import merge as arena_merge  # noqa: E402
from tools.auto import git_manager as gm  # noqa: E402
from tools.auto import plan_validator as pv  # noqa: E402
from tools.auto.coder import Coder  # noqa: E402
from tools.auto.controller import AutoController  # noqa: E402
from tools.auto.utils import atomic_write_text  # noqa: E402
from tools.claimcheck.truth import locked  # noqa: E402
from tools.collect import manifest as mf  # noqa: E402
from tools.llm_stream import mask_api_key  # noqa: E402


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout


def _controller(base: Path) -> AutoController:
    ctrl = AutoController.__new__(AutoController)
    ctrl.base_dir = base
    ctrl.workspace_dir = base / ".agent" / "workspace"
    ctrl.config = configparser.ConfigParser()
    ctrl.state = SimpleNamespace(log=lambda msg: None)
    (base / ".agent").mkdir(parents=True, exist_ok=True)
    return ctrl


# ── --auto and the user's own files ─────────────────────────────────────────────────────────

def test_a_failed_task_on_a_plain_folder_keeps_the_users_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('mine')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("notes\n", encoding="utf-8")
    (tmp_path / ".collect").mkdir()
    (tmp_path / ".collect" / "artifact.json").write_text("{}\n", encoding="utf-8")
    ctrl = _controller(tmp_path)
    ctrl._setup_git()
    assert sorted(_git(tmp_path, "ls-files").split()) == [".gitignore", "README.md", "src/app.py"]
    (tmp_path / "src" / "app.py").write_text("print('a task')\n", encoding="utf-8")
    (tmp_path / "scratch.py").write_text("x = 1\n", encoding="utf-8")
    ctrl.git.discard_working_changes()
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "print('mine')\n"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "notes\n"
    assert not (tmp_path / "scratch.py").exists()
    assert (tmp_path / ".agent").is_dir() and (tmp_path / ".collect" / "artifact.json").is_file()


def test_auto_refuses_a_repository_with_uncommitted_changes_and_leaves_it_alone(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "mine")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("y = 1\n", encoding="utf-8")
    ctrl = _controller(tmp_path)
    with pytest.raises(RuntimeError) as err:
        ctrl._setup_git()
    assert "a.py" in str(err.value) and "new.py" in str(err.value)
    assert _git(tmp_path, "rev-list", "--count", "HEAD").strip() == "1"
    assert not (tmp_path / ".gitignore").exists()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 2\n"


def test_the_agents_own_outputs_are_not_uncommitted_changes(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "mine")
    (tmp_path / "IMPROVEMENTS.md").write_text("# plan\n", encoding="utf-8")
    (tmp_path / "a.py.coder.bak").write_text("x = 0\n", encoding="utf-8")
    ctrl = _controller(tmp_path)
    ctrl._setup_git()
    assert ctrl.git is not None


def test_an_untracked_collect_artifact_is_neither_a_change_nor_residue(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "mine")
    (tmp_path / ".collect").mkdir()
    (tmp_path / ".collect" / "artifact.json").write_text("{}\n", encoding="utf-8")
    ctrl = _controller(tmp_path)
    ctrl._setup_git()
    (tmp_path / "scratch.py").write_text("x = 2\n", encoding="utf-8")
    ctrl.git.discard_working_changes()
    assert (tmp_path / ".collect" / "artifact.json").is_file()
    assert not (tmp_path / "scratch.py").exists()


def test_the_gitignore_commit_takes_only_the_gitignore(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "mine")
    (tmp_path / "staged.py").write_text("y = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "staged.py")
    g = gm.GitManager(tmp_path)
    g.configure_identity()
    g.ensure_gitignore_committed()
    assert _git(tmp_path, "show", "--name-only", "--format=", "HEAD").split() == [".gitignore"]
    assert "staged.py" in _git(tmp_path, "diff", "--cached", "--name-only")


# ── /edit and /search ───────────────────────────────────────────────────────────────────────

def _editor():
    act = A.OrchestratorActions.__new__(A.OrchestratorActions)
    act.max_iterations, act.timeout_seconds, act.stream_agents = 1, 60, False
    act._edit_file_content = lambda instr, fp, original, **kw: original.replace("old", "new")
    act._validate_edit = lambda *a, **kw: {"status": "approved"}
    return act


def test_edit_keeps_the_files_bytes_mode_and_link(tmp_path, capsys):
    f = tmp_path / "run.sh"
    before = b"#!/bin/sh\r\necho old\r\necho caf\xe9\r\n"
    f.write_bytes(before)
    os.chmod(f, 0o755)
    _editor().run_edit("/edit run.sh :: replace old", str(tmp_path))
    assert f.read_bytes() == before.replace(b"old", b"new")
    assert stat.S_IMODE(f.stat().st_mode) == 0o755
    backup = glob.glob(str(f) + ".bak.*")
    assert len(backup) == 1 and Path(backup[0]).read_bytes() == before
    (tmp_path / "real.txt").write_text("old line\n", encoding="utf-8")
    os.symlink("real.txt", tmp_path / "link.txt")
    _editor().run_edit("/edit link.txt :: replace old", str(tmp_path))
    assert (tmp_path / "link.txt").is_symlink()
    assert (tmp_path / "real.txt").read_text(encoding="utf-8") == "new line\n"
    assert not [p for p in os.listdir(tmp_path) if "tmp" in p]


def test_a_symlink_out_of_the_project_is_outside_it(tmp_path, capsys):
    proj, outside = tmp_path / "proj", tmp_path / "outside"
    proj.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP SECRET old\n", encoding="utf-8")
    os.symlink(outside, proj / "vendor")
    assert A._resolve_within_base(str(proj), "vendor/secret.txt") is None
    _editor().run_edit("/edit vendor/secret.txt :: replace old", str(proj))
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "TOP SECRET old\n"
    assert not list(outside.glob("*.bak.*"))
    assert A._resolve_within_base(str(proj), "sub/x.py") == os.path.join(os.path.realpath(proj), "sub", "x.py")


# ── secrets in a prompt ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, kept", [
    ("api_key = sk-secret\r\n", "api_key = here_your_key\r\n"),
    ("x\napi_key = sk-secret\r\ny\n", "x\napi_key = here_your_key\r\ny\n"),
    ('{"api_key": "sk-secret"}', '{"api_key": "here_your_key"}'),
    ("export OPENAI_API_KEY=sk-secret\n", "export OPENAI_API_KEY=here_your_key\n"),
    ("the api_key is read here", "the api_key is read here"),
])
def test_mask_api_key_masks_crlf_json_and_env_forms(text, kept):
    assert mask_api_key(text) == kept


# ── the Coder's writes ──────────────────────────────────────────────────────────────────────

def _coder() -> Coder:
    cfg = configparser.ConfigParser()
    cfg.read_dict({"api": {"active": "local", "verify_ssl": "false"},
                   "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "test", "model": "m",
                                 "api_format": "openai"},
                   "loop": {"timeout_seconds": "300"}, "coder": {"temperature": "0.2", "max_tokens": "4096"}})
    return Coder(config=cfg, base_url="http://localhost:1337/v1", api_key="test", model="m")


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX modes, not root")
def test_a_read_only_file_is_edited_twice(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("x = 0\n", encoding="utf-8")
    os.chmod(f, 0o444)
    for n in (1, 2):
        written, err = _coder()._write_files([{"path": "m.py", "content": f"x = {n}\n"}], tmp_path, "AUTO-T1",
                                             allowed_paths=frozenset({"m.py"}))
        assert written == ["m.py"], err
    assert (tmp_path / "m.py.coder.bak").read_text(encoding="utf-8") == "x = 1\n"


def test_the_coder_never_writes_into_git(tmp_path):
    (tmp_path / ".git").mkdir()
    written, err = _coder()._write_files([{"path": ".git/config", "content": "[core]\nhooksPath = x\n"}],
                                         tmp_path, "AUTO-T1")
    assert written == [] and ".git" in err
    assert not (tmp_path / ".git" / "config").exists()


# ── create-only, atomic writes ──────────────────────────────────────────────────────────────

def test_exclusive_without_hard_links_does_not_replace_a_file_another_writer_created(tmp_path, monkeypatch):
    target = tmp_path / "t.json"

    def no_link(src, dst):
        Path(dst).write_text("winner", encoding="utf-8")       # another writer, between our check and our write
        raise PermissionError(1, "no hard links")

    monkeypatch.setattr(os, "link", no_link)
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: False if self == target else real_exists(self))
    with pytest.raises(FileExistsError):
        atomic_write_text(target, "loser", exclusive=True)
    assert target.read_text(encoding="utf-8") == "winner"
    assert [p.name for p in tmp_path.iterdir()] == ["t.json"]


def test_exclusive_without_hard_links_creates_a_new_file(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "link", lambda s, d: (_ for _ in ()).throw(PermissionError(1, "no hard links")))
    atomic_write_text(tmp_path / "n.json", "{}", exclusive=True)
    assert (tmp_path / "n.json").read_text(encoding="utf-8") == "{}"
    assert [p.name for p in tmp_path.iterdir()] == ["n.json"]


def test_the_collect_manifest_is_replaced_not_rewritten_in_place(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    path = tmp_path / "collect_manifest.json"
    mf.write_manifest(mf.build_manifest(tmp_path, files=["a.py"]), path)
    old = path.read_text(encoding="utf-8")
    os.link(path, tmp_path / "old.json")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    mf.write_manifest(mf.build_manifest(tmp_path, files=["a.py"]), path)
    assert (tmp_path / "old.json").read_text(encoding="utf-8") == old
    assert path.read_text(encoding="utf-8") != old


def test_improvements_md_keeps_its_line_ends(tmp_path):
    md = tmp_path / "IMPROVEMENTS.md"
    text = ("# Improvements\n\n## Autonomous Tasks\n\n### AUTO-T1: one\n\nbody\n\n---\n\n"
            "### AUTO-T2: two\n\nbody\n\n---\n\n## Manual Suggestions\n")
    md.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    pv._remove_from_improvements_md(tmp_path, ["AUTO-T1"])
    raw = md.read_bytes()
    assert b"AUTO-T1" not in raw and b"AUTO-T2" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_a_merge_record_write_waits_for_the_lock(tmp_path):
    target = tmp_path / arena_merge.MERGED_RECORD
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"1": {"agent": "a", "sha": "s", "subject": "x"}}), encoding="utf-8")
    done = threading.Event()
    with locked(target):
        t = threading.Thread(target=lambda: (arena_merge.write_merged_record(tmp_path, 2, "b", "t", "y"), done.set()))
        t.start()
        assert not done.wait(0.5)
    t.join(10)
    assert done.is_set()
    assert set(json.loads(target.read_text(encoding="utf-8"))) == {"1", "2"}
    assert [p.name for p in target.parent.iterdir()] == [target.name]
