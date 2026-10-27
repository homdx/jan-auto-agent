"""CC-4 (270) fixes found judging the three entries: a header's --stat is bounded, a long-line hunk keeps its keyword line, an unnamed binary file is a note."""
from __future__ import annotations

import os
import subprocess

from tools.claimcheck.anchors import PathRepoView, extract_anchors, resolve_anchors
from tools.claimcheck.evidence_git import git_chunks

MAX_STAT_FILES = 30

_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a", "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@a", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00"}


def _g(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], env=_ENV, capture_output=True,
                          check=True).stdout.decode().strip()


def _chunks(root, claim, **kw):
    view = PathRepoView(root)
    return git_chunks(resolve_anchors(extract_anchors(claim), view), view, claim=claim, **kw)


def test_a_commit_of_many_files_has_a_bounded_header_and_still_gives_the_named_file(tmp_path):
    """200 new files: the header kept every --stat line (10 KB, 80 KB at 1500); now 30 lines and a count."""
    _g(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "k.py").write_text("a = 1\n")
    _g(tmp_path, "add", "-A")
    _g(tmp_path, "commit", "-qm", "root")
    (tmp_path / "many").mkdir()
    for i in range(200):
        (tmp_path / "many" / f"f{i}.py").write_text(f"x = {i}\n")
    (tmp_path / "many" / "f7.py").write_text("x = 7\nTARGET = 1\n")
    _g(tmp_path, "add", "-A")
    _g(tmp_path, "commit", "-qm", "many")
    sha = _g(tmp_path, "rev-parse", "HEAD")
    chunks = _chunks(tmp_path, f"commit {sha[:7]} changed `many/f7.py`")
    header = next(c for c in chunks if c.id.endswith(":header"))
    assert len(header.text) < 4000
    assert header.text.count(".py |") <= MAX_STAT_FILES
    assert "more files" in header.text and "200 files changed" in header.text
    assert any("TARGET = 1" in c.text for c in chunks)


def test_a_long_line_hunk_keeps_the_line_the_claim_is_about(tmp_path):
    """1000 changed 300-char lines: the window of ±3 lines never fitted, so the `+load_timeout = 9` line was dropped."""
    _g(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "data.py").write_text("".join(f"row_{i} = " + "v" * 290 + "\n" for i in range(1000)))
    _g(tmp_path, "add", "-A")
    _g(tmp_path, "commit", "-qm", "base")
    base = _g(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "data.py").write_text("load_timeout = 9\n" + "".join(f"row_{i} = " + "w" * 290 + "\n" for i in range(1, 1000)))
    _g(tmp_path, "add", "-A")
    _g(tmp_path, "commit", "-qm", "big")
    head = _g(tmp_path, "rev-parse", "HEAD")
    claim = "`load_timeout` is 9 in `data.py`"
    for chunks in (_chunks(tmp_path, claim, base=base, head=head),
                   _chunks(tmp_path, f"commit {head[:7]} changed `data.py` load_timeout")):
        hunks = [c for c in chunks if c.path == "data.py" and c.kind == "git" and c.start]
        assert hunks and any("+load_timeout = 9" in c.text for c in hunks)
        assert all(len(c.text) <= 2400 for c in hunks)


def test_a_binary_file_of_a_commit_naming_no_file_is_a_note(tmp_path):
    """A binary file has no changed lines to rank by, so the "largest files" never held it and no note said so."""
    _g(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "logo.png").write_bytes(b"\0\1" * 40)
    _g(tmp_path, "add", "-A")
    _g(tmp_path, "commit", "-qm", "root")
    (tmp_path / "logo.png").write_bytes(b"\0\2" * 40)
    for name in "abc":    # three text files that out-rank the binary one by changed lines
        (tmp_path / f"{name}.py").write_text("x = 1\ny = 2\n")
    _g(tmp_path, "add", "-A")
    _g(tmp_path, "commit", "-qm", "logo")
    sha = _g(tmp_path, "rev-parse", "HEAD")
    chunks = _chunks(tmp_path, f"commit {sha[:7]} changed the logo")
    assert any(c.kind == "note" and c.text == "binary file: logo.png" for c in chunks)
    assert not any(c.path == "logo.png" and c.kind == "git" for c in chunks)
