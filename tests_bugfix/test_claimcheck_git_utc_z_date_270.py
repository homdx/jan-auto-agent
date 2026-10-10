"""CC-4 (270) fix: a git that writes a zero UTC offset as `Z` (strict ISO) gives the same commit header as one that writes `+00:00`."""
from __future__ import annotations

import os
import subprocess

from tools.claimcheck.anchors import PathRepoView, extract_anchors, resolve_anchors
from tools.claimcheck.evidence_git import git_chunks

_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@a", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00"}


def _g(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], env=_ENV, capture_output=True,
                          check=True).stdout.decode().strip()


class _ZView(PathRepoView):
    """The view of a git that prints a UTC `%aI` as `...Z`."""

    def git(self, *args):
        out = super().git(*args)
        return out.replace("T00:00:00+00:00", "T00:00:00Z") if any("%aI" in a for a in args) else out


def _header(view_cls, root, sha):
    view = view_cls(root)
    claim = f"commit {sha[:7]} changed the file"
    chunks = git_chunks(resolve_anchors(extract_anchors(claim), view), view, claim=claim)
    return next(c for c in chunks if c.id.endswith(":header")).text


def test_a_z_suffixed_utc_date_is_written_as_plus_zero(tmp_path):
    """Golden fixtures hold `Date:   ...+00:00`; a newer git's `Z` changed every header text."""
    _g(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "k.py").write_text("a = 1\n")
    _g(tmp_path, "add", "-A")
    _g(tmp_path, "commit", "-qm", "root")
    sha = _g(tmp_path, "rev-parse", "HEAD")
    plain = _header(PathRepoView, tmp_path, sha)
    z = _header(_ZView, tmp_path, sha)
    assert "Date:   2026-01-01T00:00:00+00:00" in plain
    assert z == plain
