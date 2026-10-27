"""tests/test_auto_coder_d_encoding.py — HARDEN-5 D: the Coder reads and writes a
file in its own codec, not UTF-8 with ``errors="replace"``.

Before this change the Coder's whole-file path did what CC-16 had already fixed
for ``/edit``: it read every target file as UTF-8 with ``errors="replace"``, so
a legacy byte came back as U+FFFD — and the model copies what it is shown — then
wrote the reply back as UTF-8, so a legacy file came out as UTF-8 mojibake. The
fix decides the codec once, from the file's own bytes, through the same helper
``/edit`` uses, applies it strictly on the read, writes the reply back in it,
and refuses — instead of writing ``?`` or U+FFFD — a reply the file's codec
cannot hold.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.auto import coder as coder_mod  # noqa: E402
from tools.auto.coder import Coder, _file_encodings, _file_read_encoding  # noqa: E402
from tools.auto.coder import _pick_file_encoding, _refuse_inencodable  # noqa: E402


# Helpers

def _coder() -> Coder:
    """A minimal Coder: ``__init__`` needs a ConfigParser, nothing else matters here."""
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "true"},
        "api_local": {"base_url": "http://localhost:9999", "model": "x", "api_key": ""},
        "coder":     {"temperature": "0.2", "max_tokens": "1024"},
        "loop":      {"timeout_seconds": "60"},
    })
    return Coder(config=cfg, base_url="http://localhost:9999", api_key="",
                 model="x", api_format="openai", verify_ssl=True)


def _write(parsed_files, base_dir, allowed=None):
    return _coder()._write_files(parsed_files, base_dir=base_dir, task_id="T1",
                                 allowed_paths=allowed)


# The legacy fixture: the two codecs this repository actually holds, in the byte
# order that makes the probe order `cp1251`, `cp1252`, `latin-1`.
CYR = "Привет, мир! «клавы» — …\n"
CYR_BYTES = CYR.encode("cp1251")


# Unit: the codec probe is /edit's, and it fails open

class TestEncodingProbe:
    def test_a_utf8_file_is_utf8_only(self, tmp_path):
        p = tmp_path / "a.py"
        p.write_bytes("def f() -> int:\n    return 1  # café ☀\n".encode("utf-8"))
        assert _file_encodings(p) == ("utf-8",)
        assert _file_read_encoding(p) == "utf-8"

    def test_a_legacy_file_is_its_own_codec_first(self, tmp_path):
        p = tmp_path / "legacy.py"
        p.write_bytes(CYR_BYTES)
        encs = _file_encodings(p)
        assert encs == ("cp1251", "cp1252", "latin-1")
        assert _file_read_encoding(p) == "cp1251"

    def test_a_missing_file_fails_open_to_utf8(self, tmp_path):
        p = tmp_path / "absent.py"
        assert _file_encodings(p) == ("utf-8",)
        assert _file_read_encoding(p) == "utf-8"

    def test_a_read_encoding_uses_the_first_candidate(self, tmp_path, monkeypatch):
        p = tmp_path / "x.py"
        p.write_bytes(b"x = 1\n")
        monkeypatch.setattr(coder_mod, "_file_encodings", lambda _path: ("cp1251",))
        assert _file_read_encoding(p) == "cp1251"

    def test_a_probe_that_breaks_fails_open_to_utf8(self, tmp_path, monkeypatch):
        # /edit's helper is unreachable — a dead import or a broken helper — and
        # the Coder still gets a codec: the old one, UTF-8.
        import tools.actions as actions

        p = tmp_path / "y.py"
        p.write_bytes(b"x = 1\n")
        monkeypatch.setattr(
            actions, "_edit_write_encodings",
            lambda _target: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert _file_encodings(p) == ("utf-8",)
        assert _file_read_encoding(p) == "utf-8"

    def test_a_picker_that_breaks_fails_open_to_utf8(self, monkeypatch):
        import tools.actions as actions

        monkeypatch.setattr(
            actions, "_pick_edit_encoding",
            lambda encs, content: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert _pick_file_encoding(("cp1251",), "x") == ("utf-8", "")

    def test_pick_walks_the_list_in_order_and_names_what_is_missing(self):
        encs = ("cp1251", "cp1252", "latin-1")
        assert _pick_file_encoding(encs, "ascii only\n")[0] == "cp1251"
        picked, missing = _pick_file_encoding(encs, "bad \ufffd here\n")
        assert picked is None
        assert "\ufffd" in missing  # the reportable character is named, not a codec traceback

    def test_refusal_names_the_file_and_the_missing_characters(self):
        assert _refuse_inencodable("utf-8", "", "a.py") is None
        msg = _refuse_inencodable(None, "“ ”", "legacy.py")
        assert msg is not None
        assert msg.startswith("[ENCODING]")
        assert "legacy.py" in msg and "“" in msg and "”" in msg

    def test_refusal_without_printable_missing_characters_still_reports(self):
        msg = _refuse_inencodable(None, "", "legacy.py")
        assert msg is not None and "some characters" in msg


# Integration: the context the model is shown

class TestReadUsesTheFilesOwnCodec:
    def test_a_legacy_file_is_shown_as_itself_not_as_replacement(self, tmp_path):
        rel = "legacy.py"
        (tmp_path / rel).write_bytes(CYR_BYTES)
        ctx = _coder()._read_file_contents([rel], tmp_path)
        assert "\ufffd" not in ctx
        assert CYR.strip() in ctx and "«клавы»" in ctx

    def test_a_utf8_file_is_unchanged(self, tmp_path):
        body = "def f() -> int:\n    return 1  # café ☀\n"
        rel = "a.py"
        (tmp_path / rel).write_bytes(body.encode("utf-8"))
        ctx = _coder()._read_file_contents([rel], tmp_path)
        assert "\ufffd" not in ctx and body in ctx

    def test_the_probe_always_yields_a_codec_that_round_trips(self, tmp_path):
        # Every byte run has a candidate — cp1251/cp1252 only when they
        # reproduce the bytes, latin-1 always — so a strict decode in the probe's
        # first candidate cannot fail, and the lossy fallback is only for a file
        # that changed between the probe and the read.
        rel = "odd.py"
        for raw in (b"x = 1\n", b"\x81\x8d\x8f\x90\x9d", bytes(range(64)), CYR_BYTES):
            (tmp_path / rel).write_bytes(raw)
            encs = _file_encodings(tmp_path / rel)
            assert encs
            assert raw.decode(encs[0]).encode(encs[0]) == raw  # a strict decode cannot fail
            assert encs[0] == _file_read_encoding(tmp_path / rel)
            if encs != ("utf-8",):
                assert encs[-1] == "latin-1"  # the last resort is always present

    def test_a_decode_that_still_fails_falls_back_to_the_lossy_read(self, tmp_path, monkeypatch):
        # The file changed between the codec probe and the read: the task still
        # gets its context instead of losing the file.
        rel = "legacy.py"
        (tmp_path / rel).write_bytes(CYR_BYTES)
        monkeypatch.setattr(coder_mod, "_file_read_encoding", lambda p: "ascii")
        ctx = _coder()._read_file_contents([rel], tmp_path)
        assert "\ufffd" in ctx and "def" not in ctx

    def test_an_unreadable_file_is_reported_not_fatal(self, tmp_path, monkeypatch):
        rel = "gone.py"
        (tmp_path / rel).write_bytes(b"x\n")
        monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(OSError("gone")))
        ctx = _coder()._read_file_contents([rel], tmp_path)
        assert "[unreadable" in ctx


# Integration: the write keeps the file's own codec

class TestWriteKeepsTheFilesOwnCodec:
    def test_a_legacy_file_comes_back_in_its_own_codec(self, tmp_path):
        rel = "legacy.py"
        (tmp_path / rel).write_bytes(CYR_BYTES)
        written, err = _write([{"path": rel, "content": CYR}], tmp_path, allowed={rel})
        assert written == [rel] and err == ""
        out = (tmp_path / rel).read_bytes()
        assert out == CYR_BYTES  # byte for byte: the legacy encoding survived
        assert b"\xef\xbf\xbd" not in out  # no U+FFFD was stored
        assert (tmp_path / (rel + ".coder.bak")).exists()

    def test_unchanged_bytes_are_the_original_bytes(self, tmp_path):
        rel = "legacy.py"
        first = "def load():\n"
        second = "    return 'неизмененная строка'  # «скалы»\n"
        (tmp_path / rel).write_bytes((first + second).encode("cp1251"))
        revised = "def load_rows():\n" + second  # only the first line was revised
        written, err = _write([{"path": rel, "content": revised}], tmp_path, allowed={rel})
        assert written == [rel] and err == ""
        out = (tmp_path / rel).read_bytes()
        # the untouched line came back as its own bytes, in the file's own codec
        assert second.encode("cp1251") in out
        assert out.decode("cp1251") == revised
        assert b"\xef\xbf\xbd" not in out
        assert out.decode("utf-8", "ignore") != revised  # it is not a UTF-8 file any more

    def test_a_reply_the_codec_cannot_hold_is_refused_and_the_file_is_untouched(self, tmp_path):
        rel = "legacy.py"
        (tmp_path / rel).write_bytes(CYR_BYTES)
        bad = "Привет, мир! ☀️ done\n"
        written, err = _write([{"path": rel, "content": bad}], tmp_path, allowed={rel})
        assert written == []
        assert err.startswith("[ENCODING]")
        assert rel in err
        assert "\ufffd" not in err and "?" not in err
        assert (tmp_path / rel).read_bytes() == CYR_BYTES  # the file is exactly as it was
        assert not (tmp_path / (rel + ".coder.bak")).exists()  # nothing was touched

    def test_a_utf8_file_is_written_as_utf8(self, tmp_path):
        rel = "a.py"
        body = "def f() -> int:\n    return 1  # café ☀\n"
        (tmp_path / rel).write_bytes(body.encode("utf-8"))
        written, err = _write([{"path": rel, "content": body}], tmp_path, allowed={rel})
        assert written == [rel] and err == ""
        assert (tmp_path / rel).read_bytes() == body.encode("utf-8")

    def test_a_new_file_is_written_as_utf8(self, tmp_path):
        rel = "new.py"
        written, err = _write([{"path": rel, "content": "x = '☀'\n"}], tmp_path, allowed={rel})
        assert written == [rel] and err == ""
        assert (tmp_path / rel).read_bytes() == "x = '☀'\n".encode("utf-8")


# Integration: what a faithful model was shown, one line changed

class TestFaithfulRevision:
    def test_the_ticket_case_latin1_one_line_changed(self, tmp_path):
        # The ticket's case: a latin-1 file the model edits by hand, changing
        # LIMIT from 10 to 20. Nothing outside that line moves, and the bytes
        # outside it are the original bytes.
        rel = "data.py"
        original_text = "LIMIT = 10\nname = '¡ñé'\n"
        (tmp_path / rel).write_bytes(original_text.encode("latin-1"))
        # what the model actually sees, then one line changed
        shown = (tmp_path / rel).read_bytes().decode(
            _file_read_encoding(tmp_path / rel))
        assert "\ufffd" not in shown
        written, err = _write(
            [{"path": rel, "content": shown.replace("LIMIT = 10", "LIMIT = 20")}],
            tmp_path, allowed={rel})
        assert written == [rel] and err == ""
        out = (tmp_path / rel).read_bytes()
        assert out.replace(b"LIMIT = 20", b"LIMIT = 10") == original_text.encode("latin-1")
        assert b"\xef\xbf\xbd" not in out


class TestWhatStayedIsStillThere:
    def test_a_crlf_file_stays_crlf(self, tmp_path):
        rel = "windows.py"
        body = "a = 1\r\nb = 2\r\nc = 3\r\n"
        (tmp_path / rel).write_bytes(body.encode("utf-8"))
        written, err = _write(
            [{"path": rel, "content": "a = 1\nb = 2\nc = 3\n"}],
            tmp_path, allowed={rel})
        assert written == [rel] and err == ""
        assert (tmp_path / rel).read_bytes() == body.encode("utf-8")

    def test_a_bom_file_keeps_its_bom(self, tmp_path):
        rel = "bom.py"
        body = "a = 1\nb = 2\n"
        (tmp_path / rel).write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        written, err = _write(
            [{"path": rel, "content": "a = 1\nb = 2\n"}],
            tmp_path, allowed={rel})
        assert written == [rel] and err == ""
        assert (tmp_path / rel).read_bytes() == b"\xef\xbb\xbf" + body.encode("utf-8")

    def test_a_0755_file_keeps_its_mode(self, tmp_path):
        import os
        rel = "tool.py"
        p = tmp_path / rel
        p.write_bytes("#!/usr/bin/env python3\nprint(1)\n".encode("utf-8"))
        os.chmod(p, 0o755)
        written, err = _write(
            [{"path": rel, "content": "#!/usr/bin/env python3\nprint(2)\n"}],
            tmp_path, allowed={rel})
        assert written == [rel] and err == ""
        assert p.stat().st_mode & 0o777 == 0o755
        assert p.read_bytes() == "#!/usr/bin/env python3\nprint(2)\n".encode("utf-8")
