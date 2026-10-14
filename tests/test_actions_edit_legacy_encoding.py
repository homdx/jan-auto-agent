"""tests/test_actions_edit_legacy_encoding.py — CC-16 (Part C).

Fix 34 made `/edit` write back the file's own bytes: back up with `shutil.copy2`,
choose the encoding by testing whether the bytes decode as UTF-8 (else `latin-1`,
`read_file`'s fallback), and write the revision with that encoding. That is right
while the model hands back what it was shown — the file read as Latin-1, mojibake
and all — and wrong the moment it *fixes* the text, which is what it is asked to
do for a legacy file: a revision with real Cyrillic, or `“ ” — … → ✓`, cannot be
encoded in Latin-1. The write then failed *after every LLM iteration had run*, so
the whole edit was lost, the user was told a codec could not encode rather than
that the file is not UTF-8, and the timestamped `.bak` made just before the write
stayed behind.

These tests pin the fix: the encoding is decided once from the file's bytes
before the model is asked, a revision that fits none of the file's write
encodings is refused with a message that says the file is not UTF-8 and names
the characters, and a write that fails removes the backup it made.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import tools.actions as A


def _editor(revision, validate_calls=None, edit_calls=None):
    """An `OrchestratorActions` whose two LLM calls are stubbed, as the existing
    `/edit` tests do. `edit_calls`/`validate_calls`, when given, are lists the
    stubs append to, so a test can count what actually ran."""
    act = A.OrchestratorActions.__new__(A.OrchestratorActions)
    act.max_iterations, act.timeout_seconds, act.stream_agents = 3, 60, False

    def _edit(instr, fp, original, **kw):
        if edit_calls is not None:
            edit_calls.append(instr)
        return revision(original) if callable(revision) else revision

    def _validate(*a, **kw):
        if validate_calls is not None:
            validate_calls.append(a)
        return {"status": "approved"}

    act._edit_file_content = _edit
    act._validate_edit = _validate
    return act


def _baks(path: Path):
    return list(path.parent.glob(path.name + ".bak.*"))


# ── the defect: a legacy file the model has fixed ──────────────────────────────

def test_a_cp1251_file_with_a_real_cyrillic_and_punctuation_revision_is_written_as_cp1251(tmp_path, capsys):
    """The reproduction: a cp1251 file and a revision that carries real Cyrillic and
    curly quotes, an em dash and an ellipsis — none of it encodable in Latin-1, all of
    it in cp1251. The file stays cp1251, decoded with cp1251 it equals the revision,
    and the lines the edit did not touch are byte-identical."""
    f = tmp_path / "note.txt"
    original = "Привет, мир\nвторая строка\n"
    f.write_bytes(original.encode("cp1251"))
    revision = 'Привет, мир\nHe said “ok” — done…\n'
    _editor(revision).run_edit("/edit note.txt :: fix the punctuation", str(tmp_path))

    assert f.read_bytes().decode("cp1251") == revision
    # The line the edit did not touch is byte-identical, so only the changed line moved.
    assert f.read_bytes().splitlines()[0] == original.splitlines()[0].encode("cp1251")
    assert len(_baks(f)) == 1
    out = capsys.readouterr().out
    assert "FILE EDITED" in out
    assert "Failed to write" not in out


def test_a_revision_no_legacy_encoding_holds_is_refused_before_the_work_is_spent(tmp_path, capsys):
    """The same file, a revision with a character no legacy encoding here holds (an
    emoji): the edit is refused as soon as the revision exists — the validator never
    runs and no further iteration is spent — the file is untouched, no `.bak` is left,
    and the message says the file is not UTF-8 and names the character."""
    f = tmp_path / "note.txt"
    before = "Привет, мир\nвторая строка\n".encode("cp1251")
    f.write_bytes(before)
    edit_calls: list = []
    validate_calls: list = []
    _editor('Привет, мир\nHe said “ok” — done 🎉\n', validate_calls, edit_calls).run_edit(
        "/edit note.txt :: add a mark", str(tmp_path),
    )

    assert f.read_bytes() == before, "the file was touched by a refused edit"
    assert _baks(f) == [], "a refused edit left a backup behind"
    # One model call, and nothing after it: fix 34 spent all of max_iterations
    # before discovering the revision could not be written.
    assert len(edit_calls) == 1
    assert validate_calls == []
    out = capsys.readouterr().out
    assert "not UTF-8" in out
    assert "🎉" in out
    assert "FILE EDITED" not in out


def test_a_latin1_revision_of_a_latin1_file_is_still_written_back_byte_for_byte(tmp_path, capsys):
    """The case fix 34 got right, kept: a file already read as Latin-1 and edited with
    Latin-1 text is written back byte for byte."""
    f = tmp_path / "note.txt"
    before = b"echo caf\xe9\n"
    f.write_bytes(before)
    _editor(b"echo caf\xe9 again\n".decode("latin-1")).run_edit(
        "/edit note.txt :: tweak", str(tmp_path),
    )
    assert f.read_bytes() == b"echo caf\xe9 again\n"


# ── a failed write leaves no backup ────────────────────────────────────────────

def test_a_failing_write_removes_the_backup_it_made(tmp_path, capsys, monkeypatch):
    """A write that fails (here: the directory made read-only after the backup was
    taken) removes the `.bak` it had just created, so no backup of an edit that did
    not happen is left behind."""
    import shutil

    f = tmp_path / "note.txt"
    f.write_text("line one\n", encoding="utf-8")

    real_copy2 = shutil.copy2

    def _copy_then_lock(src, dst, **kw):
        real_copy2(src, dst, **kw)
        os.chmod(tmp_path, 0o500)  # the write that follows cannot land

    monkeypatch.setattr(shutil, "copy2", _copy_then_lock)
    try:
        _editor("line two\n").run_edit("/edit note.txt :: tweak", str(tmp_path))
    finally:
        os.chmod(tmp_path, 0o700)

    assert f.read_text(encoding="utf-8") == "line one\n"
    assert _baks(f) == [], "a failed write left a backup behind"
    assert "Failed to write" in capsys.readouterr().out


def test_a_successful_write_keeps_its_backup(tmp_path):
    """The other half of the rule: a backup of a write that succeeded stays, as fix 34
    left it — it is the operator's restore point."""
    f = tmp_path / "note.txt"
    f.write_text("line one\n", encoding="utf-8")
    _editor("line two\n").run_edit("/edit note.txt :: tweak", str(tmp_path))
    backups = _baks(f)
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "line one\n"


# ── what fix 34 made true stays true ───────────────────────────────────────────

def test_a_utf8_file_is_unchanged_in_every_respect(tmp_path, capsys):
    """A UTF-8 file keeps everything fix 34 gave it: the revision is written as UTF-8,
    whatever it contains (an emoji included, since UTF-8 can hold it), and the backup
    is the original's bytes."""
    f = tmp_path / "note.txt"
    before = "line one\n".encode("utf-8")
    f.write_bytes(before)
    _editor("line one, done 🎉\n").run_edit("/edit note.txt :: mark it", str(tmp_path))
    assert f.read_bytes() == "line one, done 🎉\n".encode("utf-8")
    assert len(_baks(f)) == 1


def test_a_crlf_and_executable_utf8_file_keeps_both(tmp_path, capsys):
    """CRLF line ends and the file's mode survive a UTF-8 edit, as fix 34 required."""
    f = tmp_path / "run.sh"
    before = b"#!/bin/sh\r\necho old\r\n"
    f.write_bytes(before)
    os.chmod(f, 0o755)
    _editor(lambda original: original.replace("old", "new")).run_edit(
        "/edit run.sh :: replace old", str(tmp_path),
    )
    assert f.read_bytes() == b"#!/bin/sh\r\necho new\r\n"
    assert stat.S_IMODE(f.stat().st_mode) == 0o755
    assert len(_baks(f)) == 1


def test_a_utf8_bom_file_stays_a_utf8_bom_file(tmp_path, capsys):
    """A UTF-8 file with a BOM is a UTF-8 file: the BOM is not a legacy encoding, and
    the bytes the edit does not touch keep it."""
    f = tmp_path / "note.txt"
    before = "﻿line one\n".encode("utf-8")
    f.write_bytes(before)
    _editor(lambda original: original.replace("old", "new") if "old" in original else original + "line two\n").run_edit(
        "/edit note.txt :: add a line", str(tmp_path),
    )
    assert f.read_bytes().startswith(b"\xef\xbb\xbf")
    assert f.read_bytes().decode("utf-8-sig") == "line one\nline two\n"
