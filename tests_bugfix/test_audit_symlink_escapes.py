"""tests_bugfix/test_audit_symlink_escapes.py — AUDIT-1, -2, -3, -10, -21.

Paths that were checked by their spelling rather than by where they lead:

* AUDIT-1  ``resolve_collect_dir`` accepted a ``.collect`` symlink out of the project.
* AUDIT-2  the repo walk yielded file symlinks out of the repo; their targets were read.
* AUDIT-3  ``tmp_roots`` matched an original spelling such as ``/tmp/kilo/../../etc/passwd``.
* AUDIT-10 a ``workspace_root`` inside ``base_dir`` was mirrored into itself.
* AUDIT-21 the mirror followed symlinks out of ``base_dir`` (and a link to an
  ancestor looped) — a new case found while fixing AUDIT-21.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

import pytest

from tools.auto.executor import Executor
from tools.auto.repo_ingest import RepoIngestor
from tools.collect.cli import CollectCliError, resolve_collect_dir
from tools.collect.scanner import scan_repo_ex
from tools.contest import policy


def _cfg(**collect) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["collect"] = {k: str(v) for k, v in collect.items()}
    return cfg


# ── AUDIT-1 ─────────────────────────────────────────────────────────────────

def test_collect_dir_symlink_out_of_the_project_is_refused(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".collect").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CollectCliError):
        resolve_collect_dir(root, _cfg())


def test_collect_dir_that_does_not_exist_yet_is_fine(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    assert resolve_collect_dir(root, _cfg()) == root / ".collect"


def test_collect_dir_link_inside_the_project_is_fine(tmp_path):
    root = tmp_path / "proj"
    (root / "build" / "c").mkdir(parents=True)
    (root / ".collect").symlink_to(root / "build" / "c", target_is_directory=True)
    assert resolve_collect_dir(root, _cfg()) == root / ".collect"


# ── AUDIT-2 ─────────────────────────────────────────────────────────────────

def _repo_with_links(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    secret = tmp_path / "secret.py"
    secret.write_text("TOKEN = 'do-not-read'\n")
    (root / "real.py").write_text("x = 1\n")
    (root / "leak.py").symlink_to(secret)
    (root / "alias.py").symlink_to(root / "real.py")
    return root


def test_repo_walk_skips_file_links_out_of_the_repo(tmp_path):
    root = _repo_with_links(tmp_path)
    walker = RepoIngestor(root, collect_skips=True)
    files = list(walker.walk())
    assert "leak.py" not in files
    assert {"real.py", "alias.py"} <= set(files)
    assert ("leak.py", "outside repo") in walker.skipped_files


def test_collect_scan_never_reads_a_link_target_outside(tmp_path):
    root = _repo_with_links(tmp_path)
    modules, skipped = scan_repo_ex(root, config=None)
    assert "leak.py" not in {m.path for m in modules}
    assert ("leak.py", "outside repo") in skipped


# ── AUDIT-3 ─────────────────────────────────────────────────────────────────

def test_dotdot_spelling_under_a_scratch_glob_is_not_inside(tmp_path):
    pairs = [(Path("/etc/passwd"), ("/tmp/kilo/../../etc/passwd",))]
    assert policy._inside_worktree_or_tmp(pairs, None, ["/tmp/kilo/*"]) is False


def test_symlink_inside_scratch_pointing_out_is_not_inside(tmp_path):
    scratch = tmp_path / "kilo"
    scratch.mkdir()
    (scratch / "link").symlink_to("/etc", target_is_directory=True)
    original = str(scratch / "link" / "passwd")
    pairs = policy._extract_paths({"patterns": [original],
                                   "metadata": {"directories": []}}, None)
    assert pairs, "the path must be extracted"
    assert policy._inside_worktree_or_tmp(pairs, None, [str(scratch) + "/*"]) is False


def test_real_scratch_paths_still_match(tmp_path):
    scratch = (tmp_path / "kilo").resolve()
    scratch.mkdir()
    for original in (str(scratch / "a.txt"), str(scratch / "sub") + "/*", str(scratch) + "/*"):
        pairs = policy._extract_paths({"patterns": [original],
                                       "metadata": {"directories": []}}, None)
        assert policy._inside_worktree_or_tmp(pairs, None, [str(scratch) + "/*"]), original


def test_glob_through_a_linked_directory_matches_the_resolved_path(tmp_path):
    real = tmp_path / "private_tmp"
    real.mkdir()
    linked = tmp_path / "tmp"
    linked.symlink_to(real, target_is_directory=True)
    pairs = policy._extract_paths({"patterns": [str(linked / "kilo" / "x")],
                                   "metadata": {"directories": []}}, None)
    assert policy._inside_worktree_or_tmp(pairs, None, [str(linked) + "/kilo/*"])


# ── AUDIT-10 / AUDIT-21 ─────────────────────────────────────────────────────

def test_workspace_root_inside_base_dir_is_not_mirrored_into_itself(tmp_path):
    base = tmp_path / "repo"
    base.mkdir()
    (base / "mod.py").write_text("x = 1\n")
    ex = Executor(base, workspace_root=base / "ws")
    ws = ex._prepare_workspace("T1", ["mod.py"])
    assert (ws / "mod.py").exists()
    assert not (ws / "ws").exists(), "the workspace root was copied into the workspace"


def test_mirror_skips_links_out_of_base_and_link_cycles(tmp_path):
    base = tmp_path / "repo"
    base.mkdir()
    (base / "mod.py").write_text("x = 1\n")
    outside = tmp_path / "home"
    outside.mkdir()
    (outside / "id_rsa").write_text("PRIVATE\n")
    (base / "home_link").symlink_to(outside, target_is_directory=True)
    (base / "key").symlink_to(outside / "id_rsa")
    (base / "inner").mkdir()
    (base / "inner" / "loop").symlink_to(base, target_is_directory=True)
    (base / "alias.py").symlink_to(base / "mod.py")

    ex = Executor(base, workspace_root=tmp_path / "wsroot")
    ws = ex._prepare_workspace("T1", [])
    assert (ws / "mod.py").read_text() == "x = 1\n"
    assert (ws / "alias.py").read_text() == "x = 1\n", "an in-repo link is still copied"
    assert not (ws / "home_link").exists()
    assert not (ws / "key").exists()
    assert not (ws / "inner" / "loop").exists()
    assert not any("PRIVATE" in p.read_text(errors="replace")
                   for p in ws.rglob("*") if p.is_file())
