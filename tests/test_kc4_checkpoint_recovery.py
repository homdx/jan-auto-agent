"""AUDIT-8 — a damaged KC4 results checkpoint is kept aside and the batch goes on.

Runs the real ``run_all_kc4.py`` (a byte copy of it, so its ``HERE`` is the temporary
folder) against a stand-in ``run_one_kc4.py`` that always answers with a good cell, so
every recovery below can prove a scenario really ran and was recorded.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "contest-bench" / "kc4" / "run_all_kc4.py"

GOOD_CELL = '@@RESULT@@{"checks": [{"name": "standin", "ok": true}]}\n'
TRUNCATED = b'{"e": {"fresh_round": '
NOT_UTF8 = b"\xff\xfe"
TOP_LIST = b"[1]"
BAD_ENTRANT = b'{"e": 5}'

STAND_IN = f"""import sys
print({GOOD_CELL!r}, end="")
"""


@pytest.fixture
def bench(tmp_path):
    """A temporary copy of ``run_all_kc4.py`` next to a stand-in ``run_one_kc4.py``, plus a worktree."""
    (tmp_path / "run_all_kc4.py").write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "run_one_kc4.py").write_text(STAND_IN, encoding="utf-8")
    entry = tmp_path / "wt" / "e"
    entry.mkdir(parents=True)
    return tmp_path


def _run(bench, out, capsys=None):
    p = subprocess.run(
        [sys.executable, str(bench / "run_all_kc4.py"), "--wt", str(bench / "wt"), "--out", str(out)],
        capture_output=True, text=True,
    )
    return p


def _recorded(out, entrant="e"):
    """The scenario the stand-in ran is in the checkpoint as a good cell."""
    data = json.loads(Path(out).read_text(encoding="utf-8"))
    cell = data[entrant]["fresh_round"]
    assert cell["checks"][0]["ok"] is True, cell
    return data


def test_a_missing_checkpoint_is_just_an_empty_start(bench, tmp_path):
    out = tmp_path / "out.json"
    p = _run(bench, out)
    assert p.returncode == 0, p.stderr
    _recorded(out)
    assert not (tmp_path / "out.json.corrupt").exists()


def test_a_valid_checkpoint_keeps_its_results_and_is_added_to(bench, tmp_path):
    out = tmp_path / "out.json"
    out.write_text('{"pre": {"fresh_round": {"checks": [{"name": "old", "ok": false}]}}}', encoding="utf-8")
    p = _run(bench, out)
    assert p.returncode == 0, p.stderr
    data = _recorded(out)
    assert data["pre"]["fresh_round"] == {"checks": [{"name": "old", "ok": False}]}
    assert not (tmp_path / "out.json.corrupt").exists()


@pytest.mark.parametrize("damaged", [TRUNCATED, NOT_UTF8, TOP_LIST, BAD_ENTRANT], ids=[
    "truncated", "not-utf8", "top-list", "per-entrant-not-object"])
def test_a_damaged_checkpoint_is_kept_aside_and_the_batch_runs(bench, tmp_path, damaged):
    out = tmp_path / "out.json"
    out.write_bytes(damaged)
    p = _run(bench, out)
    assert p.returncode == 0, p.stderr
    copy = tmp_path / "out.json.corrupt"
    assert copy.exists()
    assert copy.read_bytes() == damaged, "the diagnostic copy is the original bytes"
    assert "out.json" in p.stderr and "out.json.corrupt" in p.stderr
    _recorded(out)


def test_two_damaged_runs_in_a_row_make_two_copies_and_the_first_is_untouched(bench, tmp_path):
    out = tmp_path / "out.json"
    out.write_bytes(TRUNCATED)
    first = _run(bench, out)
    assert first.returncode == 0, first.stderr
    corrupt = tmp_path / "out.json.corrupt"
    assert corrupt.read_bytes() == TRUNCATED

    out.write_bytes(NOT_UTF8)
    second = _run(bench, out)
    assert second.returncode == 0, second.stderr
    assert corrupt.read_bytes() == TRUNCATED, "the first copy was overwritten"
    assert (tmp_path / "out.json.corrupt.1").read_bytes() == NOT_UTF8
    _recorded(out)


def test_a_copy_that_cannot_be_made_refuses_and_leaves_the_original(bench, tmp_path):
    out = tmp_path / "out.json"
    out.write_bytes(TRUNCATED)
    before = out.read_bytes()
    # the sibling cannot be created: the folder is read-only
    (tmp_path).chmod(0o500)
    try:
        p = _run(bench, out)
    finally:
        (tmp_path).chmod(0o700)
    assert p.returncode != 0
    assert "out.json" in p.stderr and "corrupt" in p.stderr
    assert "[e]" not in p.stdout, "the scenarios ran despite the refused copy"
    assert out.read_bytes() == before, "the original was replaced"
    assert not (tmp_path / "out.json.corrupt").exists()
    assert not out.with_name("out.json.tmp").exists()


def test_a_checkpoint_that_is_a_directory_is_a_clear_error(bench, tmp_path):
    out = tmp_path / "out.json"
    out.mkdir()
    p = _run(bench, out)
    assert p.returncode != 0
    assert "out.json" in p.stderr

# ── the traps the first review of the round found in every entry ─────────────

def _copies(bench_dir, name, data):
    return [f for f in bench_dir.iterdir() if f.name not in (name, name + ".tmp") and f.is_file() and f.read_bytes() == data]


def test_a_document_nested_too_deep_for_the_parser_is_kept_aside(bench, tmp_path):
    """`json.loads` raises RecursionError, not ValueError, for 200000 open brackets."""
    out = tmp_path / "out.json"
    deep = b"[" * 200000
    out.write_bytes(deep)
    p = _run(bench, out)
    assert p.returncode == 0, p.stderr[-300:]
    assert len(_copies(tmp_path, "out.json", deep)) == 1
    _recorded(out)


def test_the_copy_keeps_crlf_nul_and_invalid_bytes_exactly(bench, tmp_path):
    out = tmp_path / "out.json"
    raw = b'{"e":\r\n  {"fresh_round": [1,\r\n\x00\xff\r'
    out.write_bytes(raw)
    p = _run(bench, out)
    assert p.returncode == 0, p.stderr[-300:]
    assert len(_copies(tmp_path, "out.json", raw)) == 1, "a text-mode copy changes the line ends"
    _recorded(out)


def test_existing_diagnostic_copies_are_never_overwritten(bench, tmp_path):
    out = tmp_path / "out.json"
    keep = b"EARLIER-DIAGNOSTIC"
    names = ["out.json.corrupt"] + [f"out.json.corrupt.{i}" for i in range(1, 6)]
    for n in names:
        (tmp_path / n).write_bytes(keep)
    out.write_bytes(TRUNCATED)
    p = _run(bench, out)
    assert p.returncode == 0, p.stderr[-300:]
    assert all((tmp_path / n).read_bytes() == keep for n in names)
    assert len(_copies(tmp_path, "out.json", TRUNCATED)) == 1


def test_a_symlink_where_the_copy_would_go_is_not_written_through(bench, tmp_path):
    out = tmp_path / "out.json"
    out.write_bytes(TRUNCATED)
    victim = tmp_path / "victim"
    victim.write_bytes(b"NOT-MINE")
    (tmp_path / "out.json.corrupt").symlink_to(tmp_path / "never_created")
    (tmp_path / "out.json.corrupt.1").symlink_to(victim)
    p = _run(bench, out)
    assert p.returncode == 0, p.stderr[-300:]
    assert not (tmp_path / "never_created").exists(), "the copy followed a dangling symlink"
    assert victim.read_bytes() == b"NOT-MINE", "the copy followed a symlink onto another file"
    assert len(_copies(tmp_path, "out.json", TRUNCATED)) == 1


def test_a_copy_that_cannot_be_made_in_a_writable_folder_stops_the_batch(bench, tmp_path):
    """The folder is writable (so the atomic save would work): only the copy's name is too long for the file system."""
    name = "r" * 246 + ".json"
    try:
        (tmp_path / (name + ".corrupt")).write_bytes(b"")
    except OSError:
        pass
    else:
        pytest.skip("this file system accepts names over 255 characters")
    out = tmp_path / name
    out.write_bytes(TRUNCATED)
    p = _run(bench, out)
    assert p.returncode != 0, "the batch went on without keeping the damage"
    assert out.read_bytes() == TRUNCATED, "the original was replaced although it could not be kept"
    assert "Traceback" not in p.stderr


def test_an_unreadable_checkpoint_is_an_error_and_is_not_replaced(bench, tmp_path):
    import os
    if os.geteuid() == 0:
        pytest.skip("root reads any file")
    out = tmp_path / "out.json"
    body = b'{"e": {"fresh_round": {"checks": [{"name": "k", "ok": true}]}}}'
    out.write_bytes(body)
    out.chmod(0)
    try:
        p = _run(bench, out)
    finally:
        out.chmod(0o644)
    assert p.returncode != 0
    assert out.read_bytes() == body
    assert not [f for f in tmp_path.iterdir() if f.name.startswith("out.json.")], "a copy was made of a file that is only unreadable"
    assert "Traceback" not in p.stderr
