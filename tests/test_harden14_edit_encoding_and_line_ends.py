"""tests/test_harden14_edit_encoding_and_line_ends.py — HARDEN-14 C: `/edit` shows the file
the same way the Coder reads it, keeps a mixed file's own line endings, and refuses a wide
encoding by name instead of writing one back as latin-1 NULs.

CC-16 Part C decided the *write* codec once, from the file's own bytes, and left the *read*
on `read_file` — UTF-8, else latin-1. So a cp1251 file was shown to the model as `Ïðèâåò
ìèð` and then written back as cp1251: an instruction in Russian about a Russian file could
not be done. `/edit` now reads in the codec the write walks first, from the same helper the
Coder uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tools.actions as A


def _editor(revision):
    """An `OrchestratorActions` whose two LLM calls are stubbed, as the CC-16 tests do."""
    act = A.OrchestratorActions.__new__(A.OrchestratorActions)
    act.max_iterations, act.timeout_seconds, act.stream_agents = 3, 60, False

    def _edit(instr, fp, original, **kw):
        return revision(original) if callable(revision) else revision

    act._edit_file_content = _edit
    act._validate_edit = lambda *a, **kw: {"status": "approved"}
    return act


def _baks(path: Path):
    return list(path.parent.glob(path.name + ".bak.*"))


# ── C1 — a cp1251 file is shown as cp1251, not latin-1 ────────────────────────

def test_a_cp1251_file_is_shown_in_cp1251(tmp_path, capsys):
    """The reproduction: `Привет мир` saved as cp1251. The model is shown the real text,
    and the revision it writes back lands in cp1251."""
    f = tmp_path / "notes.txt"
    f.write_bytes("Привет мир\nвторая строка\n".encode("cp1251"))
    shown = []

    def _see(original):
        shown.append(original)
        return "Привет мир\nКонец\n"

    _editor(_see).run_edit("/edit notes.txt :: add the end", str(tmp_path))

    assert shown == ["Привет мир\nвторая строка\n"], "the model was shown mojibake"
    assert f.read_bytes().decode("cp1251") == "Привет мир\nКонец\n"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp1252"])
def test_the_other_encodings_are_unchanged(tmp_path, encoding):
    """A UTF-8, a BOM and a cp1252 file still round-trip through their own codec."""
    f = tmp_path / "notes.txt"
    text = "hello there\nsecond line\n" if encoding != "cp1252" else "caf\u00e9\nsecond\n"
    f.write_bytes(text.encode(encoding))
    _editor(text.replace("hello", "HELLO") if encoding != "cp1252"
            else "caf\u00e9\nchanged\n").run_edit("/edit notes.txt :: tweak", str(tmp_path))
    assert f.read_bytes().decode(encoding), "the file was not written back in " + encoding


# ── C2 — a mixed file keeps its own line endings ──────────────────────────────

def test_a_mixed_file_keeps_the_endings_of_the_lines_it_did_not_touch(tmp_path):
    """`a\r\nb\r\nc\nd\ne\n` edited on `c`: the untouched `a` and `b` keep their `\r`."""
    f = tmp_path / "mixed.txt"
    f.write_bytes(b"a\r\nb\r\nc\nd\ne\n")
    _editor(lambda original: original.replace("c", "C")).run_edit(
        "/edit mixed.txt :: change c", str(tmp_path))
    assert f.read_bytes() == b"a\r\nb\r\nC\nd\ne\n"


def test_an_all_crlf_and_an_all_lf_file_are_unchanged(tmp_path):
    crlf = tmp_path / "crlf.txt"
    crlf.write_bytes(b"a\r\nb\r\nc\r\n")
    _editor(lambda original: original.replace("c", "C")).run_edit(
        "/edit crlf.txt :: change c", str(tmp_path))
    assert crlf.read_bytes() == b"a\r\nb\r\nC\r\n"

    lf = tmp_path / "lf.txt"
    lf.write_bytes(b"a\nb\nc\n")
    _editor(lambda original: original.replace("c", "C")).run_edit(
        "/edit lf.txt :: change c", str(tmp_path))
    assert lf.read_bytes() == b"a\nb\nC\n"


# ── C3 — a UTF-16 / UTF-32 file is refused and left byte-identical ────────────

@pytest.mark.parametrize("raw, name", [
    (b"\xff\xfe" + "hello".encode("utf-16-le") + b"\r\n\x00", "UTF-16 LE"),
    (b"\xfe\xff" + "hello".encode("utf-16-be"), "UTF-16 BE"),
    (b"\xff\xfe\x00\x00" + "hi".encode("utf-32-le"), "UTF-32 LE"),
    (b"\x00\x00\xfe\xff" + "hi".encode("utf-32-be"), "UTF-32 BE"),
])
def test_a_wide_encoding_is_refused_and_left_alone(tmp_path, capsys, raw, name):
    f = tmp_path / "wide.txt"
    f.write_bytes(raw)
    _editor("new content\n").run_edit("/edit wide.txt :: tweak", str(tmp_path))

    assert f.read_bytes() == raw, "a refused edit touched the file"
    assert _baks(f) == [], "a refused edit left a backup behind"
    out = capsys.readouterr().out
    assert name in out
    assert "convert it to UTF-8 first" in out
    assert "FILE EDITED" not in out


# ── C4 — a cp1252 file whose only non-ASCII character is `€` ──────────────────

def test_a_lone_euro_is_read_as_cp1252_not_cp1251(tmp_path):
    """Byte `0x80` is cp1251's `Ђ` and cp1252's `€`; the tie is broken by the symbol, so the
    `€` is written back as `0x80` and reads back as `€` rather than as `ˆ`."""
    from tools.actions import _edit_write_encodings

    f = tmp_path / "euro.txt"
    f.write_bytes(b"total:\n\x80\n")
    assert _edit_write_encodings(str(f))[0] == "cp1252"
    _editor("total:\n\u20ac 5\n").run_edit("/edit euro.txt :: add a price", str(tmp_path))
    assert f.read_bytes() == b"total:\n\x80 5\n"
