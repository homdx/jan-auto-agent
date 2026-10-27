"""tests_bugfix/test_harden9_coder_encoding_script_307.py — HARDEN-9 C: a Western cp1252 or
latin-1 file is no longer read as cp1251, and a write the codec cannot hold is refused with
the characters named.

`_edit_write_encodings` tried `cp1251` first and accepted it when the decoded text encoded
back to the same bytes. cp1251 and cp1252 define almost every byte, so a Western file always
round-tripped as cp1251 — `Café` went out to the model as `Cafй`, the model copied the
garbage back, and a revision that added a genuine Western letter was refused as "missing some
characters" with nothing named.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.actions import _edit_write_encodings, _pick_edit_encoding  # noqa: E402
from tools.auto.coder import (  # noqa: E402
    Coder,
    _file_encodings,
    _file_read_encoding,
    _pick_file_encoding,
    _refuse_inencodable,
)


WEST = "Café au lait, naïve façade, déjà vu\n"
RUSSIAN = "Привет мир\n"
CYRILLIC_FULL = "Привет, мир! «клавы» — …\n"


def _coder() -> Coder:
    """A minimal Coder: ``__init__`` needs a ConfigParser, nothing else matters here."""
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "true"},
        "api_local": {"base_url": "http://localhost:9999", "model": "x", "api_key": ""},
        "coder": {"temperature": "0.2", "max_tokens": "1024"},
        "loop": {"timeout_seconds": "60"},
    })
    return Coder(config=cfg, base_url="http://localhost:9999", api_key="",
                 model="x", api_format="openai", verify_ssl=True)


def _write(parsed_files, base_dir, allowed=None):
    return _coder()._write_files(parsed_files, base_dir=base_dir, task_id="T1",
                                 allowed_paths=allowed)


class TestTheTicketTable:
    """The file's own script decides the codec, not the probe order."""

    def test_a_western_file_is_read_as_cp1252(self, tmp_path):
        p = tmp_path / "menu.py"
        p.write_bytes(WEST.encode("cp1252"))
        assert _file_encodings(p) == ("cp1252", "cp1251", "latin-1")
        assert _file_read_encoding(p) == "cp1252"
        assert p.read_bytes().decode(_file_read_encoding(p)) == WEST

    def test_a_two_byte_western_file_is_read_as_cp1252(self, tmp_path):
        p = tmp_path / "b.py"
        p.write_bytes(b"caf\xe9\n")
        assert _file_read_encoding(p) == "cp1252"
        assert p.read_bytes().decode("cp1252") == "café\n"

    def test_a_german_file_is_read_as_cp1252(self, tmp_path):
        p = tmp_path / "c.py"
        p.write_bytes(b"Gr\xfc\xdfe \xf6l\n")
        assert _file_read_encoding(p) == "cp1252"
        assert p.read_bytes().decode("cp1252") == "Grüße öl\n"

    def test_a_russian_file_is_still_read_as_cp1251(self, tmp_path):
        p = tmp_path / "ru.py"
        p.write_bytes(RUSSIAN.encode("cp1251"))
        assert _file_encodings(p) == ("cp1251", "cp1252", "latin-1")
        assert _file_read_encoding(p) == "cp1251"
        assert p.read_bytes().decode("cp1251") == RUSSIAN

    def test_a_russian_file_with_high_punctuation_is_still_cp1251(self, tmp_path):
        p = tmp_path / "ru2.py"
        p.write_bytes(CYRILLIC_FULL.encode("cp1251"))
        assert _file_read_encoding(p) == "cp1251"
        assert p.read_bytes().decode("cp1251") == CYRILLIC_FULL


class TestWhatDidNotChange:
    def test_a_utf8_file_is_utf8_only(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_bytes("def f() -> int:\n    return 1  # café ☀\n".encode("utf-8"))
        assert _file_encodings(p) == ("utf-8",)
        assert _file_read_encoding(p) == "utf-8"

    def test_a_utf8_file_with_a_bom_is_utf8_only(self, tmp_path):
        p = tmp_path / "bom.py"
        p.write_bytes(b"\xef\xbb\xbf" + "a = 1\n".encode("utf-8"))
        assert _file_encodings(p) == ("utf-8",)
        assert _file_read_encoding(p) == "utf-8"

    def test_a_pure_ascii_file_is_utf8(self, tmp_path):
        p = tmp_path / "plain.py"
        p.write_bytes(b"x = 1\n")
        assert _file_encodings(p) == ("utf-8",)
        assert _file_read_encoding(p) == "utf-8"

    def test_a_missing_file_fails_open_to_utf8(self, tmp_path):
        assert _file_read_encoding(tmp_path / "absent.py") == "utf-8"

    def test_the_probe_first_candidate_round_trips(self, tmp_path):
        for raw in (b"x = 1\n", b"\x81\x8d\x8f\x90\x9d", bytes(range(64)),
                    RUSSIAN.encode("cp1251"), WEST.encode("cp1252")):
            p = tmp_path / "odd.py"
            p.write_bytes(raw)
            encodings = _file_encodings(p)
            assert encodings
            assert raw.decode(encodings[0]).encode(encodings[0]) == raw
            assert encodings[0] == _file_read_encoding(p)
            if encodings != ("utf-8",):
                assert "latin-1" in encodings  # the last resort is always present


class TestTheCodingCookie:
    def test_a_cookie_names_the_codec_and_wins(self, tmp_path):
        p = tmp_path / "lat.py"
        p.write_bytes("# -*- coding: latin-1 -*-\nname = '¡ñé'\n".encode("latin-1"))
        encodings = _file_encodings(p)
        # The cookie leads; the guess behind it is not the test's business.
        assert encodings[0] == "latin-1" and set(encodings) == {"latin-1", "cp1252", "cp1251"}
        assert _file_read_encoding(p) == "latin-1"
        assert p.read_bytes().decode("latin-1") == "# -*- coding: latin-1 -*-\nname = '¡ñé'\n"

    def test_a_bare_cookie_assignment_wins_too(self, tmp_path):
        p = tmp_path / "c1252.py"
        p.write_bytes("# coding=cp1252\nCafé au lait\n".encode("cp1252"))
        assert _file_read_encoding(p) == "cp1252"

    def test_a_cookie_on_the_second_line_wins(self, tmp_path):
        p = tmp_path / "second.py"
        p.write_bytes("# shebang-ish\n# -*- coding: latin-1 -*-\nname = '¡ñé'\n".encode("latin-1"))
        assert _file_read_encoding(p) == "latin-1"

    def test_a_cookie_a_codec_cannot_holds_is_ignored(self, tmp_path):
        p = tmp_path / "wrong.py"
        p.write_bytes("# -*- coding: utf-8 -*-\nCafé au lait\n".encode("cp1252"))
        assert _file_read_encoding(p) == "cp1252"

    def test_a_cookie_with_an_unknown_name_is_ignored(self, tmp_path):
        p = tmp_path / "unknown.py"
        p.write_bytes("# -*- coding: xko88 -*-\nCafé au lait\n".encode("cp1252"))
        assert _file_read_encoding(p) == "cp1252"

    def test_a_coding_word_outside_a_cookie_does_not_count(self, tmp_path):
        p = tmp_path / "prose.py"
        p.write_bytes("# encoding of this module is utf-8\nCafé au lait\n".encode("cp1252"))
        assert _file_read_encoding(p) == "cp1252"

    def test_a_utf8_file_ignores_its_cookie(self, tmp_path):
        p = tmp_path / "u.py"
        p.write_bytes("# -*- coding: cp1252 -*-\ndef f():\n    return 1\n".encode("utf-8"))
        assert _file_encodings(p) == ("utf-8",)


class TestTheWriteKeepsTheWesternCodec:
    def test_an_added_western_line_is_written_back_in_cp1252(self, tmp_path):
        rel = "menu.py"
        (tmp_path / rel).write_bytes(WEST.encode("cp1252"))
        shown = (tmp_path / rel).read_bytes().decode(_file_read_encoding(tmp_path / rel))
        assert "Café" in shown and "naïve" in shown

        revised = shown + "second = 'Crème brûlée'\n"
        written, err = _write([{"path": rel, "content": revised}], tmp_path, allowed={rel})
        assert written == [rel] and err == ""

        out = (tmp_path / rel).read_bytes()
        assert out.decode("cp1252") == revised
        assert "Crème brûlée".encode("cp1252") in out   # é and û are cp1252's own bytes
        assert b"\xef\xbf\xbd" not in out
        assert out.decode("cp1251") != revised        # it is not cp1251

    def test_a_russian_file_still_comes_back_in_cp1251(self, tmp_path):
        rel = "ru.py"
        (tmp_path / rel).write_bytes(RUSSIAN.encode("cp1251"))
        written, err = _write([{"path": rel, "content": RUSSIAN}], tmp_path, allowed={rel})
        assert written == [rel] and err == ""
        assert (tmp_path / rel).read_bytes() == RUSSIAN.encode("cp1251")


class TestTheRefusalNamesTheProblem:
    def test_the_picker_names_the_characters_it_cannot_hold(self):
        picked, missing = _pick_file_encoding(
            ("cp1251", "cp1252", "latin-1"),
            "Cafй au lait, naпve faзade\nCrème brûlée\n",
        )
        assert picked is None
        assert missing, "not 'some characters'"
        assert "é" in missing
        assert "\ufffd" not in missing

    def test_the_picker_still_names_a_character_that_fits_no_codec(self):
        picked, missing = _pick_file_encoding(("cp1251", "cp1252", "latin-1"), "bad \ufffd here\n")
        assert picked is None
        assert "\ufffd" in missing

    def test_the_refusal_message_names_them(self):
        message = _refuse_inencodable(None, "è û é", "menu.py")
        assert message is not None
        assert "menu.py" in message and "é" in message and "û" in message
        assert "some characters" not in message

    def test_a_write_the_codec_cannot_hold_is_refused_with_the_characters_named(self, tmp_path):
        rel = "ru.py"
        (tmp_path / rel).write_bytes(RUSSIAN.encode("cp1251"))
        bad = RUSSIAN + "menu = 'Crème brûlée'\n"
        written, err = _write([{"path": rel, "content": bad}], tmp_path, allowed={rel})
        assert written == []
        assert err.startswith("[ENCODING]")
        assert rel in err and "é" in err and "û" in err
        assert "some characters" not in err
        assert (tmp_path / rel).read_bytes() == RUSSIAN.encode("cp1251")
        assert not (tmp_path / (rel + ".coder.bak")).exists()


class TestOneDecision:
    def test_edit_and_the_coder_share_the_probe(self, tmp_path):
        p = tmp_path / "menu.py"
        p.write_bytes(WEST.encode("cp1252"))
        assert _file_encodings(p) == _edit_write_encodings(str(p))

    def test_the_picker_edit_and_the_coder_share_the_rule(self):
        encodings = ("cp1252", "cp1251", "latin-1")
        assert _pick_edit_encoding(encodings, WEST)[0] == "cp1252"
        assert _pick_file_encoding(encodings, WEST)[0] == "cp1252"

    def test_a_prose_cookie_inside_a_string_is_not_a_cookie(self, tmp_path):
        # A `# coding:` that is not in the first two lines is prose, not a declaration.
        p = tmp_path / "late.py"
        p.write_bytes(("def f():\n    return 1\n" * 5 + "# -*- coding: latin-1 -*-\n")
                      .encode("cp1252"))
        p.write_bytes(("def f():\n    return 1\n" * 5 + "x = 'Caf\xe9'\n").encode("cp1252"))
        assert _file_read_encoding(p) == "cp1252"
