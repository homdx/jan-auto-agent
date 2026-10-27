"""tests/test_auto_writes_m_review.py — what the pipeline writes, after review batch m (№95–102): a CRLF file stays CRLF
and keeps its BOM, a .coder.bak is the original's bytes, a replaced file keeps its mode, a task leaves
IMPROVEMENTS.md whole and alone, an English word makes no dependency, and a non-ASCII path is a path.
"""

from __future__ import annotations

import configparser
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import judge_epic_round  # noqa: E402
from tools.auto import plan_validator as pv  # noqa: E402
from tools.auto.architect import CandidateTask, CitedLocation  # noqa: E402
from tools.auto.backlog_prioritiser import BacklogPrioritiser, ReadyTask  # noqa: E402
from tools.auto.coder import Coder  # noqa: E402
from tools.auto.git_manager import GitManager  # noqa: E402
from tools.auto.utils import atomic_write_text  # noqa: E402


def _coder() -> Coder:
    cfg = configparser.ConfigParser()
    cfg.read_dict({"api": {"active": "local", "verify_ssl": "false"},
                   "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "test", "model": "m",
                                 "api_format": "openai"},
                   "loop": {"timeout_seconds": "300"}, "coder": {"temperature": "0.2", "max_tokens": "4096"}})
    return Coder(config=cfg, base_url="http://localhost:1337/v1", api_key="test", model="m")


def _write(tmp_path, rel, content):
    written, err = _coder()._write_files([{"path": rel, "content": content}], tmp_path, "AUTO-T1",
                                         allowed_paths=frozenset({rel}))
    assert written == [rel], err


# ── №95: line ends and BOM of the file the coder rewrites ────────────────────────────────────

def test_a_crlf_file_stays_crlf_and_keeps_its_bom(tmp_path):
    old = b"\xef\xbb\xbfa = 1\r\nb = 2\r\nc = 3\r\nd = 4\r\ne = 5\r\nf = 6\r\n"
    (tmp_path / "m.py").write_bytes(old)
    _write(tmp_path, "m.py", "a = 1\nb = 2\nc = 30\nd = 4\ne = 5\nf = 6\n")      # the model writes LF, no BOM
    assert (tmp_path / "m.py").read_bytes() == old.replace(b"c = 3\r\n", b"c = 30\r\n")


def test_an_lf_file_and_a_new_file_are_lf(tmp_path):
    (tmp_path / "m.py").write_bytes(b"a = 1\nb = 2\n")
    _write(tmp_path, "m.py", "a = 1\r\nb = 3\r\n")
    _write(tmp_path, "n.py", "x = 1\n")
    assert (tmp_path / "m.py").read_bytes() == b"a = 1\nb = 3\n" and (tmp_path / "n.py").read_bytes() == b"x = 1\n"


# ── №96: the backup is the original ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("old", [b"name = '\xe9t\xe9'\n", b"\xef\xbb\xbfx = 1\r\ny = 2\r\n"], ids=["latin-1", "bom-crlf"])
def test_the_backup_is_the_originals_bytes(tmp_path, old):
    (tmp_path / "m.py").write_bytes(old)
    os.chmod(tmp_path / "m.py", 0o640)
    _write(tmp_path, "m.py", "x = 2\n")
    bak = tmp_path / "m.py.coder.bak"
    assert bak.read_bytes() == old and stat.S_IMODE(bak.stat().st_mode) == 0o640


def test_a_deleted_files_backup_is_its_bytes(tmp_path):
    (tmp_path / "m.py").write_bytes(b"name = '\xe9'\n")
    written, err = _coder()._write_files([{"path": "m.py", "delete": True}], tmp_path, "AUTO-T1",
                                         allowed_paths=frozenset({"m.py"}))
    assert not (tmp_path / "m.py").exists() and (tmp_path / "m.py.coder.bak").read_bytes() == b"name = '\xe9'\n", err


# ── №97: the mode of a replaced file ─────────────────────────────────────────────────────────

@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
@pytest.mark.parametrize("mode", [0o644, 0o755, 0o600])
def test_a_replaced_file_keeps_its_mode(tmp_path, mode):
    p = tmp_path / "IMPROVEMENTS.md"
    p.write_text("old\n", encoding="utf-8")
    os.chmod(p, mode)
    atomic_write_text(p, "new\n")
    assert stat.S_IMODE(p.stat().st_mode) == mode and p.read_text(encoding="utf-8") == "new\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_new_file_gets_what_open_gives(tmp_path):
    mask = os.umask(0o022)
    try:
        atomic_write_text(tmp_path / "plan.json", "{}")
    finally:
        os.umask(mask)
    assert stat.S_IMODE((tmp_path / "plan.json").stat().st_mode) == 0o644


# ── №98–100: a task leaves IMPROVEMENTS.md whole and alone ───────────────────────────────────

def _entry(task_id, instruction):
    return (f"### {task_id}: title\n\n**Cluster:** c  \n**Location:** `a.py`  \n**Target files:** `a.py`  \n"
            f"**Dependencies:** none  \n**Acceptance check:**\n```\ntrue\n```\n\n**Instruction:**\n\n{instruction}\n\n---\n\n")


def _remove(tmp_path, text, ids):
    (tmp_path / pv.IMPROVEMENTS_FILENAME).write_text(text, encoding="utf-8")
    pv._remove_from_improvements_md(tmp_path, ids)
    return (tmp_path / pv.IMPROVEMENTS_FILENAME).read_text(encoding="utf-8")


def _doc(*entries):
    return "# IMPROVEMENTS.md\n## Autonomous Tasks\n\n" + "".join(entries) + "## Manual Suggestions\n\nnone\n"


def test_an_unclosed_fence_does_not_take_the_next_task(tmp_path):
    t2 = _entry("AUTO-T2", "Wrap the call:\n```python\nx = guard(y)")
    t3 = _entry("AUTO-T3", "Then:\n```python\nz = 1\n```")
    t4 = _entry("AUTO-T4", "four")
    assert _remove(tmp_path, _doc(t2, t3, t4), ["AUTO-T2"]) == _doc(t3, t4)


def test_a_rule_and_a_heading_inside_a_task_go_with_it(tmp_path):
    t1 = _entry("AUTO-T1", "intro\n\n---\n\n### Notes inside T1\n\nmore")
    t2 = _entry("AUTO-T2", "x")
    assert _remove(tmp_path, _doc(t1, t2), ["AUTO-T1"]) == _doc(t2)


def test_the_other_tasks_code_keeps_its_blank_lines(tmp_path):
    t1 = _entry("AUTO-T1", "x")
    t2 = _entry("AUTO-T2", "```python\nimport os\n\n\ndef f():\n    pass\n```")
    assert _remove(tmp_path, _doc(t1, t2), ["AUTO-T1"]) == _doc(t2)


# ── №101: an English word is no dependency ───────────────────────────────────────────────────

def _ready(task_id, file, symbol, instruction):
    return ReadyTask(task_id, CandidateTask(title="t", instruction=instruction, target_files=[file],
                                            acceptance_check="true",
                                            cited_location=CitedLocation(file=file, symbol=symbol), cluster="c"))


@pytest.mark.parametrize("symbol, instruction, depends", [
    ("run", "Make sure the loop will run only when enabled.", False),
    ("parse", "Fail clearly when the file cannot parse its input.", False),
    ("run", "Call `run` once the queue is ready.", True),
    ("run", "Call run() once the queue is ready.", True),
    ("run", "Use worker.run instead of the inline loop.", True),
    ("load_rows", "Reuse load_rows for the import.", True),
    ("Store", "The Store must stay small.", False),
    ("Store", "Subclass Store.compact for it.", True),
])
def test_a_symbol_named_like_a_word_must_be_written_as_code(symbol, instruction, depends):
    a = _ready("AUTO-T1", "a.py", symbol, "x")
    b = _ready("AUTO-T2", "b.py", "other", instruction)
    assert BacklogPrioritiser._cross_file_symbol_ref(a, b) is depends


# ── №102: a non-ASCII path ───────────────────────────────────────────────────────────────────

def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_a_non_ascii_path_comes_back_as_it_is(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "tests").mkdir()
    for name in ("café.py", "日本語.py", "tests/test_é.py"):
        (tmp_path / name).write_text("y = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "names")
    tip = _git(tmp_path, "rev-parse", "HEAD")
    gm = GitManager(tmp_path)
    assert sorted(gm.paths_changed_in(tip)) == ["café.py", "tests/test_é.py", "日本語.py"]
    assert sorted(gm.paths_changed_between(base, tip)) == ["café.py", "tests/test_é.py", "日本語.py"]
    assert judge_epic_round.changed_tests(str(tmp_path), base) == ["tests/test_é.py"]
