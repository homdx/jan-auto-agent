"""tests/test_manifest_is_dirty_unicode.py — is_dirty false-positive on a
non-ASCII collect output directory.

git octal-escapes non-ASCII path bytes in porcelain/status output by
default (core.quotepath=true). is_dirty's exclusion filtering compares
these strings directly against a plain Python path string built from
exclude_dir — so when the EXCLUDED directory's own name contains non-ASCII
characters (a Cyrillic [collect] dir config value, plausible for a
non-English-language project), the escaped porcelain path never matches
the plain prefix, and is_dirty reported True for output entirely inside
the excluded directory:

    exclude_dir = root / "выход"
    is_dirty(root, exclude_dir) == True   (WRONG)

This is exactly the "dirty on every refresh" bug this module's docstrings
already describe fixing once (for a PREVIOUS run's leftover .collect/
output), reopened here for the one case that fix's ASCII assumption
missed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.collect.manifest import is_dirty


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", ".", cwd=root)
    _git("config", "user.email", "a@b.c", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "committed.py").write_text("x\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


class TestNonAsciiExcludeDir:
    def test_cyrillic_exclude_dir_name_is_not_dirty(self, tmp_path):
        root = _repo(tmp_path)
        outdir = root / "выход"
        outdir.mkdir()
        (outdir / "old.json").write_text("old\n", encoding="utf-8")
        _git("add", "-A", cwd=root)
        _git("commit", "-qm", "track output dir", cwd=root)

        (outdir / "новый_файл.json").write_text("new\n", encoding="utf-8")

        assert is_dirty(root, exclude_dir=outdir) is False

    def test_cyrillic_filename_inside_ascii_exclude_dir(self, tmp_path):
        """The narrower case: dir name is plain ASCII, but a file WITHIN
        it (e.g. a rendered report named after a Cyrillic chapter) is not."""
        root = _repo(tmp_path)
        outdir = root / ".collect"
        outdir.mkdir()
        (outdir / "old.json").write_text("old\n", encoding="utf-8")
        _git("add", "-A", cwd=root)
        _git("commit", "-qm", "track output dir", cwd=root)

        (outdir / "главa_report.json").write_text("new\n", encoding="utf-8")

        assert is_dirty(root, exclude_dir=outdir) is False

    def test_real_change_outside_exclusion_still_detected(self, tmp_path):
        """The fix must not make is_dirty blind to genuine changes."""
        root = _repo(tmp_path)
        outdir = root / "выход"
        outdir.mkdir()
        (outdir / "old.json").write_text("old\n", encoding="utf-8")
        _git("add", "-A", cwd=root)
        _git("commit", "-qm", "track output dir", cwd=root)

        (outdir / "новый_файл.json").write_text("new\n", encoding="utf-8")
        (root / "committed.py").write_text("real source change\n", encoding="utf-8")

        assert is_dirty(root, exclude_dir=outdir) is True

    def test_ascii_case_still_works(self, tmp_path):
        """Sanity: the original, already-tested ASCII-only case must be
        unaffected by this fix."""
        root = _repo(tmp_path)
        outdir = root / ".collect"
        outdir.mkdir()
        (outdir / "old.json").write_text("old\n", encoding="utf-8")
        _git("add", "-A", cwd=root)
        _git("commit", "-qm", "track output dir", cwd=root)

        (outdir / "new.json").write_text("new\n", encoding="utf-8")
        assert is_dirty(root, exclude_dir=outdir) is False
