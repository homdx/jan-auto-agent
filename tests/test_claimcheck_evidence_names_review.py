"""tests/test_claimcheck_evidence_names_review.py — evidence after the self-review of 2026-10-12: a venv made in the
project root does not empty the index, and a private name written without backticks makes a code claim.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.claimcheck import anchors as cc_anchors
from tools.claimcheck.anchors import PathRepoView


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


# ── №56: `python -m venv .` in the project root ──────────────────────────────────────────────

@pytest.mark.parametrize("git", [True, False], ids=["git-checkout", "plain-tree"])
def test_a_venv_made_in_the_root_keeps_the_projects_files(tmp_path, git):
    """Its pyvenv.cfg at the root gave the prefix "": every file of a checkout was left out, an empty index,
    every claim a world claim. Its packages stay out (site-packages), and so does a venv in a subdirectory."""
    if git:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write(tmp_path, "pyvenv.cfg", "home = /usr/bin\n")
    _write(tmp_path, "app.py", "def load_rows(path):\n    return open(path).read()\n")
    _write(tmp_path, "lib/python3.12/site-packages/dep/mod.py", "def load_rows(path):\n    return []\n")
    _write(tmp_path, "tools/env/pyvenv.cfg", "home = /usr/bin\n")
    _write(tmp_path, "tools/env/lib/helper.py", "def load_rows():\n    pass\n")
    files = PathRepoView(tmp_path).files()
    assert "app.py" in files
    assert not [f for f in files if "site-packages" in f or f.startswith("tools/env/")]
    claim = "`load_rows` in `app.py` never closes the file"
    resolved = cc_anchors.resolve_claim(claim, PathRepoView(tmp_path))
    assert cc_anchors.classify(claim, resolved) == "code"


# ── №60: a private name written without backticks ────────────────────────────────────────────

@pytest.fixture
def view(tmp_path):
    _write(tmp_path, "pkg/m.py", "class Reader:\n    def __init__(self):\n        self.rows = []\n\n"
                                 "    def _check_presence(self):\n        return bool(self.rows)\n\n\n"
                                 "def _flush(fh):\n    fh.write('')\n\n\n_MAX_ROWS = 10\n")
    return PathRepoView(tmp_path)


@pytest.mark.parametrize("claim, symbol", [
    ("_check_presence never looks at the rows it was given", "Reader._check_presence"),
    ("_flush drops the last line of the file", "_flush"),
    ("_MAX_ROWS is far too low for a real report", "_MAX_ROWS"),
])
def test_a_private_name_without_backticks_is_code(view, claim, symbol):
    resolved = cc_anchors.resolve_claim(claim, view)
    assert [r.qualname for r in resolved if r.found] == [symbol]
    assert cc_anchors.classify(claim, resolved) == "code"


@pytest.mark.parametrize("claim", [
    "__init__ runs twice for every object the reader makes",          # a dunder names no one definition
    "_load_everything is called on every request of the server",       # the repository does not define it
    "the _important_ part of the design is the cache in front of it",  # emphasis, and no definition
    "_flush() is called once",                                         # a call: the anchors' own rule
])
def test_what_stays_a_word(view, claim):
    resolved = cc_anchors.resolve_claim(claim, view)
    assert not [r for r in resolved if r.found and r.anchor.kind == "symbol" and r.anchor.text.startswith("_")
                and r.anchor not in cc_anchors.extract_anchors(claim)]
