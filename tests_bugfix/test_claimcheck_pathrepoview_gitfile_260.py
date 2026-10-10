"""tests_bugfix/test_claimcheck_pathrepoview_gitfile_260.py — found while building CC-2 (ticket 260).

``PathRepoView.files()`` pruned a ``.git`` *directory* only. In a worktree (what CC-2's Target
makes) and in a submodule ``.git`` is a *file*, so it was listed as a file of the repository,
and a path anchor ``.git`` resolved as ``found``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from tools.claimcheck.anchors import PathRepoView


def test_gitlink_file_is_not_a_repository_file(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    (root / "a.py").write_text("A = 1\n")
    (root / "sub").mkdir()
    (root / "sub" / ".git").write_text("gitdir: ../.git/modules/sub\n")
    (root / "sub" / "b.py").write_text("B = 1\n")
    assert PathRepoView(root).files() == ["a.py", "sub/b.py"]
