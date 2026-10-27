"""tests/test_harden13_restore_and_mask_forms.py — HARDEN-13 (311).

Four defects, one request path. `mask_api_key` hides a key from the model and
`restore_masked_secrets` puts it back where the model returned the placeholder;
`Coder._write_files` and `/edit` store the result.

**Part A — an ordinary edit above a key loses the key.** HARDEN-11 A filed every
masked line under its *place*, the nearest line above it the mask left alone —
and every key masks to the same text, so the place was the only thing that could
tell them apart. One line added, edited or re-indented between a header and its
key moved the place, so "change the base_url of provider two" wrote
`here_your_key` over a working key and reported success. The restore now aligns
the reply against the masked original line for line: a masked line in an unchanged
stretch gets its text back, and an inserted, edited or re-indented line changes
only its own stretch — while HARDEN-11 A's guarantee (no key under another header)
stays, because a pairing is accepted only where the line the key sits under is the
line it sat under in the original.

**Part A2 — a failed restore is written anyway.** The only trace of a placeholder
that no original line came back for was a warning in the log. Both writers now
refuse it: `Coder._write_files` reports it as a safety refusal, so the loop retries
with it as feedback, and `/edit` names the line and leaves the file unchanged.

**Part A3 — a line ending that is not `\\n` or `\\r`.** `splitlines()` also ends a
line at U+2028, U+2029, `\\v`, `\\f`, `\\x1c`-`\\x1e` and `\\x85`; the restore cut only
`\\n` and `\\r` off the reply's line, so a key followed by U+2028 lost 8 characters
of its own value and kept the placeholder.

**Part B — a retry eats the only backup.** `_write_files` copied the file to
`.coder.bak` before *every* write, so the second attempt of a task copied what the
first attempt wrote over the copy of the original. In a plain (non-git) folder
that left nothing holding the real key. The backup is now made by the first write
of a task run and kept by the later ones.

**Part C — the forms the mask still lets through.** An XML element, an XML
attribute pair, a key name followed by its token by a space, a bare JWT and a
digit-free token after a colon all reached the provider. HARDEN-12 A's answer to
"CamelCase or credential?" — a digit or a provider prefix — threw out every
token written without one.
"""

from __future__ import annotations

import configparser
import os
import stat
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.llm_stream import (  # noqa: E402
    mask_api_key,
    restore_masked_secrets,
    unrestored_masked_lines,
)

# The two providers of the ticket's report, in the layout every ini of this
# project has: a section header, and the key under it.
_KEY_A = "sk-live-AAAAAAAAAAAAAAAA"
_KEY_B = "sk-live-BBBBBBBBBBBBBBBB"
_INI = (
    "[openai]\n"
    f"api_key = {_KEY_A}\n"
    "[groq]\n"
    f"api_key = {_KEY_B}\n"
)
_KEYS_BY_HEADER = {"openai": _KEY_A, "groq": _KEY_B}
# The same ini with the base_url above the key — the commoner layout still.
_INI_URL = (
    "[openai]\n"
    "base_url = https://api.openai.com/v1\n"
    f"api_key = {_KEY_A}\n"
    "[groq]\n"
    f"api_key = {_KEY_B}\n"
)
# Two key lines inside calls, so an edit can sit above one of them.
_CALLS = (
    'client_a = OpenAI(api_key="%s")\n'
    'client_b = Groq(api_key="%s")\n' % (_KEY_A, _KEY_B)
)


def _coder():
    from tools.auto.coder import Coder

    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "true"},
        "api_local": {
            "base_url": "http://localhost:9999", "model": "x", "api_key": "",
        },
        "coder": {"temperature": "0.2", "max_tokens": "1024"},
        "loop": {"timeout_seconds": "60"},
    })
    return Coder(config=cfg, base_url="http://localhost:9999", api_key="",
                 model="x", api_format="openai", verify_ssl=True)


def _editor(revision):
    """An `OrchestratorActions` whose two LLM calls are stubbed, as the other
    `/edit` tests do: the model hands back *revision* and approves it."""
    import tools.actions as A

    act = A.OrchestratorActions.__new__(A.OrchestratorActions)
    act.max_iterations, act.timeout_seconds, act.stream_agents = 2, 60, False
    act._edit_file_content = lambda instr, fp, original, **kw: (
        revision(original) if callable(revision) else revision)
    act._validate_edit = lambda *a, **kw: {"status": "approved"}
    return act


# ── Part A: an edit above a key no longer costs the key ─────────────────────


class TestAnEditAboveAKeyKeepsTheKey:
    """The ticket's table: every row comes back with both keys real."""

    def test_no_edit_at_all(self):
        assert restore_masked_secrets(_INI, mask_api_key(_INI)) == _INI

    def test_a_line_added_under_the_header(self):
        reply = mask_api_key(_INI).replace(
            "[openai]\napi_key", "[openai]\nmax_tokens = 4000\napi_key")
        out = restore_masked_secrets(_INI, reply)
        assert out == _INI.replace(
            "[openai]\napi_key", "[openai]\nmax_tokens = 4000\napi_key")

    def test_a_comment_added_under_the_header(self):
        reply = mask_api_key(_INI).replace(
            "[openai]\napi_key", "[openai]\n# primary\napi_key")
        out = restore_masked_secrets(_INI, reply)
        assert _KEY_A in out and _KEY_B in out
        assert out == _INI.replace(
            "[openai]\napi_key", "[openai]\n# primary\napi_key")

    def test_the_base_url_above_the_key_is_edited(self):
        reply = mask_api_key(_INI_URL).replace(
            "https://api.openai.com/v1", "https://api.openai.com/v2")
        out = restore_masked_secrets(_INI_URL, reply)
        assert out == _INI_URL.replace(
            "https://api.openai.com/v1", "https://api.openai.com/v2")
        assert _KEY_A in out and _KEY_B in out

    def test_a_line_added_above_one_of_two_key_calls(self):
        reply = mask_api_key(_CALLS).replace(
            "client_a = OpenAI", "timeout=30,\nclient_a = OpenAI")
        out = restore_masked_secrets(_CALLS, reply)
        assert out == _CALLS.replace(
            "client_a = OpenAI(api_key", "timeout=30,\nclient_a = OpenAI(api_key")
        assert _KEY_A in out and _KEY_B in out

    def test_a_comment_added_above_each_of_two_key_calls(self):
        reply = (mask_api_key(_CALLS)
                 .replace("client_a = OpenAI", "# a\nclient_a = OpenAI")
                 .replace("client_b = Groq", "# b\nclient_b = Groq"))
        out = restore_masked_secrets(_CALLS, reply)
        assert out == _CALLS.replace(
            "client_a = OpenAI(api_key", "# a\nclient_a = OpenAI(api_key").replace(
            "client_b = Groq(api_key", "# b\nclient_b = Groq(api_key")

    def test_the_whole_file_re_indented_keeps_its_keys_and_its_own_indent(self):
        reply = "\n".join(f"    {line}" if line else line
                          for line in mask_api_key(_INI).split("\n"))
        out = restore_masked_secrets(_INI, reply)
        assert _KEY_A not in reply
        assert out == "\n".join(f"    {line}" if line else line
                                for line in _INI.split("\n"))

    def test_a_renamed_section_keeps_its_key(self):
        """A section the model renamed is the same header: the key under it is
        restored, and it is restored as the key of *that* section."""
        reply = mask_api_key(_INI).replace("[groq]", "[groq_fast]")
        out = restore_masked_secrets(_INI, reply)
        assert out == _INI.replace("[groq]", "[groq_fast]")

    def test_an_edited_header_line_keeps_its_key(self):
        reply = mask_api_key(_INI).replace("[openai]", "[openai] ; main")
        out = restore_masked_secrets(_INI, reply)
        assert out == _INI.replace("[openai]", "[openai] ; main")

    def test_two_keys_in_one_section(self):
        original = ("[openai]\n"
                    f"api_key = {_KEY_A}\n"
                    f"backup_key = {_KEY_B}\n")
        out = restore_masked_secrets(original, mask_api_key(original))
        assert out == original

    def test_a_key_line_the_task_edited_is_written_as_written(self):
        """A line the model rewrote is not a masked line of the file: there is
        nothing to put back, so the expression it wrote stays."""
        reply = mask_api_key(_INI).replace(
            f"api_key = here_your_key\n[groq]",
            "api_key = os.environ['K']\n[groq]")
        out = restore_masked_secrets(_INI, reply)
        assert "api_key = os.environ['K']\n" in out
        assert _KEY_A not in out and _KEY_B in out

    def test_no_key_is_restored_under_another_header(self):
        """HARDEN-11 A's guarantee, on the alignment: a masked line outside any
        unchanged stretch keeps the placeholder."""
        replies = [
            # a single masked line, with no header at all
            "api_key = here_your_key\n",
            # a section renamed *and* moved to the top
            "[openrouter-v2]\napi_key = here_your_key\n[groq]\napi_key = here_your_key\n",
            # the first of three sections deleted
            "[openrouter]\napi_key = here_your_key\n[groq]\napi_key = here_your_key\n",
        ]
        for reply in replies:
            out = restore_masked_secrets(_INI, reply)
            header = None
            for line in out.splitlines():
                if line.startswith("[") and line.endswith("]"):
                    header = line[1:-1]
                elif "here_your_key" in line:
                    continue          # the placeholder: no key was written
                else:
                    assert header in {"openai", "groq"}, header
                    assert line == f"api_key = {_KEYS_BY_HEADER[header]}"

    def test_a_file_that_holds_the_placeholder_on_purpose_is_left_alone(self):
        """An `.env.example`: the original line IS the placeholder, so the mask
        never changed it and there is nothing to restore."""
        original = "# documented example\napi_key = here_your_key\nmodel = gpt-4o\n"
        reply = original.replace("model = gpt-4o", "model = gpt-4o-mini")
        assert restore_masked_secrets(original, reply) == reply
        assert unrestored_masked_lines(original, reply) == []


# ── Part A2: a failed restore refuses the write ─────────────────────────────


class TestAFailedRestoreRefusesTheWrite:
    _UNRESTORABLE = (
        "[anthropic]\napi_key = here_your_key\n"
        "[openai]\napi_key = here_your_key\n"
        "[groq]\napi_key = here_your_key\n"
    )

    def test_the_failed_lines_are_named(self):
        self_failures = unrestored_masked_lines(_INI, self._UNRESTORABLE)
        assert self_failures == [(2, "api_key = here_your_key")]
        # the three real ones were restored, and are not failures
        assert _KEY_A in restore_masked_secrets(_INI, self._UNRESTORABLE)
        assert _KEY_B in restore_masked_secrets(_INI, self._UNRESTORABLE)

    def test_the_coder_refuses_it_and_leaves_the_file_byte_identical(self, tmp_path):
        rel = "agents.ini"
        (tmp_path / rel).write_text(_INI, encoding="utf-8")
        written, err = _coder()._write_files(
            [{"path": rel, "content": self._UNRESTORABLE}], tmp_path,
            task_id="H13", allowed_paths={rel})
        assert written == []
        assert "[SAFETY]" in err and "'agents.ini'" in err and "line 2" in err
        assert (tmp_path / rel).read_text(encoding="utf-8") == _INI

    def test_edit_refuses_it_and_leaves_the_file_unchanged(self, tmp_path, capsys):
        rel = "agents.ini"
        (tmp_path / rel).write_text(_INI, encoding="utf-8")
        _editor(lambda original: self._UNRESTORABLE).run_edit(
            "/edit agents.ini :: add a provider", str(tmp_path))
        assert (tmp_path / rel).read_text(encoding="utf-8") == _INI
        out = capsys.readouterr().out
        assert "agents.ini" in out and "line 2" in out and "unchanged" in out
        assert not list(tmp_path.glob("agents.ini.bak.*"))

    def test_a_reply_that_deletes_the_key_line_is_written(self):
        """Removing a key is a change the task asked for, not a failed restore."""
        reply = mask_api_key(_INI).replace(
            f"[groq]\napi_key = here_your_key\n", "[groq]\nmodel = llama3\n")
        assert unrestored_masked_lines(_INI, reply) == []
        out = restore_masked_secrets(_INI, reply)
        assert out == _INI.replace(
            f"[groq]\napi_key = {_KEY_B}\n", "[groq]\nmodel = llama3\n")

    def test_a_reply_that_replaces_the_key_line_is_written(self):
        reply = mask_api_key(_INI).replace(
            "api_key = here_your_key\n[groq]",
            "api_key = os.environ['OPENAI_API_KEY']\n[groq]")
        assert unrestored_masked_lines(_INI, reply) == []
        out = restore_masked_secrets(_INI, reply)
        assert "os.environ['OPENAI_API_KEY']" in out
        assert _KEY_A not in out and _KEY_B in out


# ── Part A3: a line ending that is not \n or \r ─────────────────────────────


class TestAKeyLineKeepsItsOwnEnding:
    def _round_trip(self, ending: str) -> None:
        value = f"api_key = {_KEY_A}{ending}"
        out = restore_masked_secrets(value, mask_api_key(value))
        assert out == value, (repr(ending), repr(out))

    def test_every_boundary_splitlines_ends_a_line_at_round_trips(self):
        for ending in ("\u2028", "\u2029", "\x0b", "\x0c", "\x85",
                       "\r", "\r\n", "\n", "\x1c", "\x1d", "\x1e"):
            self._round_trip(ending)

    def test_a_key_line_between_other_lines_keeps_its_ending(self):
        for ending in ("\u2028", "\x0b", "\x0c", "\x85"):
            original = (f"[openai]\napi_key = {_KEY_A}{ending}"
                        f"model = gpt-4o\n")
            assert restore_masked_secrets(original, mask_api_key(original)) == original

    def test_a_masked_line_with_an_exotic_ending_is_still_restored(self):
        """The line is cut by the same rule it was split with, so its ending is
        not lost and the line is still the masked text of the original."""
        original = f"api_key = {_KEY_A}\u2028model = gpt-4o\n"
        masked = mask_api_key(original)
        assert masked == "api_key = here_your_key\u2028model = gpt-4o\n"
        assert restore_masked_secrets(original, masked) == original

    def test_tabs_bom_and_crlf_are_byte_identical(self):
        for original in (f"\tapi_key = {_KEY_A}\r\n",
                         f"\ufeffapi_key = {_KEY_A}\n",
                         f"api_key = {_KEY_A}\r\n",
                         f'api_key = "{_KEY_A}"  # prod\r\n'):
            assert restore_masked_secrets(original, mask_api_key(original)) == original


# ── Part B: the first write of a task makes the backup ──────────────────────


class TestTheBackupIsTheFirstWrites:
    def test_two_writes_of_one_task_keep_the_original_bytes(self, tmp_path):
        """The retry of a task must not copy what its first attempt wrote over
        the only copy of the original: in a plain (non-git) folder that is where
        the real key survived."""
        rel = "agents.ini"
        (tmp_path / rel).write_text(_INI, encoding="utf-8")
        coder = _coder()
        first = _INI.replace("[openai]", "[openai]\nmax_tokens = 4000")
        second = first.replace("[openai]", "[openai]\ntimeout = 30")
        for content in (first, second):
            written, err = coder._write_files(
                [{"path": rel, "content": mask_api_key(content)}], tmp_path,
                task_id="H13-B", allowed_paths={rel})
            assert written == [rel], err
            assert (tmp_path / rel).read_text(encoding="utf-8") == content
        assert (tmp_path / "agents.ini.coder.bak").read_text(encoding="utf-8") == _INI

    def test_a_second_task_replaces_the_backup(self, tmp_path):
        rel = "agents.ini"
        coder = _coder()
        coder._write_files([{"path": rel, "content": _INI}], tmp_path,
                           task_id="H13-B1", allowed_paths={rel})
        first = _INI
        second = first.replace("[groq]", "[groq_fast]")
        coder._write_files([{"path": rel, "content": second}], tmp_path,
                           task_id="H13-B2", allowed_paths={rel})
        assert (tmp_path / "agents.ini.coder.bak").read_text(encoding="utf-8") == first

    def test_the_delete_branch_keeps_the_first_backup(self, tmp_path):
        rel = "agents.ini"
        (tmp_path / rel).write_text(_INI, encoding="utf-8")
        coder = _coder()
        written, err = coder._write_files(
            [{"path": rel, "content": _INI.replace("[groq]", "[groq_fast]")}],
            tmp_path, task_id="H13-B", allowed_paths={rel})
        assert written == [rel] and err == ""
        written, err = coder._write_files(
            [{"path": rel, "delete": True}], tmp_path,
            task_id="H13-B", allowed_paths={rel})
        assert written == [rel] and err == ""
        assert not (tmp_path / rel).exists()
        assert (tmp_path / "agents.ini.coder.bak").read_text(encoding="utf-8") == _INI

    @pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                        reason="POSIX modes, not root")
    def test_the_backup_keeps_the_originals_mode(self, tmp_path):
        rel = "agents.ini"
        target = tmp_path / rel
        target.write_text(_INI, encoding="utf-8")
        os.chmod(target, 0o640)
        _coder()._write_files([{"path": rel, "content": _INI}], tmp_path,
                              task_id="H13-B", allowed_paths={rel})
        assert stat.S_IMODE((tmp_path / "agents.ini.coder.bak").stat().st_mode) == 0o640
