"""tests/test_harden11_restore_by_place.py — HARDEN-11 A: a masked line is
restored by its *place*, not by position.

Story
-----
`restore_masked_secrets` filed every original line the mask changed under its
masked text alone and handed the real lines back **in order**. Every provider
section of an ini masks to the same `api_key = here_your_key`, so the three keys
of an ini shared one queue and a reply whose *structure* differed from the file
— by one section — moved a real key onto another provider: delete the first of
three sections and its two survivors each got the key above them, add a
placeholder `[anthropic]` section and the first key was copied into it, reorder
the sections and all three keys were swapped. Nothing refused it.

The restore now files every changed line under the pair (the nearest line above
it that the mask left *alone* — a section header, a key that is not itself
masked — and its masked text), and gives a real line back only where the reply
has the same pair. A masked line with no original line in its place is left as
the placeholder and warned about, so a structural edit can never move a key.

What this file pins
-------------------
* a deleted, an added and a reordered provider section: each surviving key stays
  under its own section and an added section gets the placeholder, never a real
  key;
* the order-preserving cases of HARDEN-5 B are unchanged: a faithful reply, a
  reply that only changes other lines, two identical masked lines under one
  header, and CRLF line endings;
* the whole path through `Coder._write_files`, which is where the wrong-provider
  key actually reached a disk.
"""

from __future__ import annotations

import configparser
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.llm_stream import mask_api_key, restore_masked_secrets  # noqa: E402

# Three provider sections of one ini, each with its own key, and the one masked
# text every one of them shares.
_INI = (
    "[openai]\n"
    "api_key = sk-openai-aaaaaaaaaaaaaaaa\n"
    "[openrouter]\n"
    "api_key = sk-or-v1-bbbbbbbbbbbbbbbb\n"
    "[groq]\n"
    "api_key = gsk-cccccccccccccccccccc\n"
)
_OPENAI = "sk-openai-aaaaaaaaaaaaaaaa"
_OPENROUTER = "sk-or-v1-bbbbbbbbbbbbbbbb"
_GROQ = "gsk-cccccccccccccccccccc"
_KEYS = {"openai": _OPENAI, "openrouter": _OPENROUTER, "groq": _GROQ}

# The same ini with a line of its own next to each key, so a reply can change
# something that is not a key and not a header.
_PROFILE = (
    "[openai]\n"
    "api_key = sk-openai-aaaaaaaaaaaaaaaa\n"
    "model = gpt-4o-mini\n"
    "[openrouter]\n"
    "api_key = sk-or-v1-bbbbbbbbbbbbbbbb\n"
    "model = anthropic/claude\n"
)


def _coder():
    """A Coder wired the way the other mask tests wire one: `_write_files`
    never makes a request, so the base_url is never dialled."""
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


class TestAStructuralEditNeverMovesAKey:
    """The three shapes the outside review reproduced on the code."""

    def test_the_masked_ini_looks_like_one_key(self):
        """The precondition: the mask cannot tell the three keys apart, so the
        place is the only thing that can."""
        masked = mask_api_key(_INI)
        assert masked == (
            "[openai]\napi_key = here_your_key\n"
            "[openrouter]\napi_key = here_your_key\n"
            "[groq]\napi_key = here_your_key\n"
        )
        for key in (_OPENAI, _OPENROUTER, _GROQ):
            assert key not in masked

    def test_a_deleted_section_leaves_the_surviving_keys_where_they_are(self):
        reply = ("[openrouter]\napi_key = here_your_key\n"
                 "[groq]\napi_key = here_your_key\n")
        out = restore_masked_secrets(_INI, reply)
        assert out == (f"[openrouter]\napi_key = {_OPENROUTER}\n"
                       f"[groq]\napi_key = {_GROQ}\n")
        # the OpenAI key is gone with its section, not redistributed
        assert _OPENAI not in out

    def test_an_added_section_gets_the_placeholder_not_a_real_key(self, caplog):
        reply = ("[anthropic]\napi_key = here_your_key\n"
                 "[openai]\napi_key = here_your_key\n"
                 "[openrouter]\napi_key = here_your_key\n"
                 "[groq]\napi_key = here_your_key\n")
        with caplog.at_level(logging.WARNING, logger="tools.llm_stream"):
            out = restore_masked_secrets(_INI, reply)
        # the section the original does not have keeps the placeholder …
        assert out.startswith("[anthropic]\napi_key = here_your_key\n")
        # … and the three real ones stay under their own sections
        assert out == ("[anthropic]\napi_key = here_your_key\n"
                       f"[openai]\napi_key = {_OPENAI}\n"
                       f"[openrouter]\napi_key = {_OPENROUTER}\n"
                       f"[groq]\napi_key = {_GROQ}\n")
        assert any("api_key = here_your_key" in r.message for r in caplog.records)

    def test_reordered_sections_keep_their_own_keys(self):
        reply = ("[groq]\napi_key = here_your_key\n"
                 "[openrouter]\napi_key = here_your_key\n"
                 "[openai]\napi_key = here_your_key\n")
        assert restore_masked_secrets(_INI, reply) == (
            f"[groq]\napi_key = {_GROQ}\n"
            f"[openrouter]\napi_key = {_OPENROUTER}\n"
            f"[openai]\napi_key = {_OPENAI}\n"
        )

    def test_a_renamed_section_gets_no_key(self, caplog):
        """The header is the place: a reply that renames the section above a
        masked line has no original line in that place, so nothing is restored
        and nothing is silently moved either."""
        reply = ("[openrouter-v2]\napi_key = here_your_key\n"
                 "[groq]\napi_key = here_your_key\n[openai]\napi_key = here_your_key\n")
        with caplog.at_level(logging.WARNING, logger="tools.llm_stream"):
            out = restore_masked_secrets(_INI, reply)
        assert out == ("[openrouter-v2]\napi_key = here_your_key\n"
                       f"[groq]\napi_key = {_GROQ}\n"
                       f"[openai]\napi_key = {_OPENAI}\n")
        assert any("here_your_key" in r.message for r in caplog.records)

    def test_the_real_key_never_reaches_a_section_it_was_not_in(self):
        """Whatever the reply's structure, no real key ends up under a header it
        was not under in the original."""
        replies = [
            "[openrouter]\napi_key = here_your_key\n[groq]\napi_key = here_your_key\n",
            "[anthropic]\napi_key = here_your_key\n[openai]\napi_key = here_your_key\n",
            "[groq]\napi_key = here_your_key\n[openai]\napi_key = here_your_key\n",
            "[groq]\napi_key = here_your_key\n[openrouter]\napi_key = here_your_key\n",
            "api_key = here_your_key\n",
        ]
        for reply in replies:
            header = None
            for line in restore_masked_secrets(_INI, reply).splitlines():
                if line.startswith("[") and line.endswith("]"):
                    header = line[1:-1]
                elif "here_your_key" in line:
                    continue          # the placeholder: no key was written
                else:
                    assert header in {"openai", "openrouter", "groq"}, header
                    assert line == f"api_key = {_KEYS[header]}"


class TestOrderPreservingRestoresAreUnchanged:
    """HARDEN-5 B: the cases the place rule must keep exactly as they were."""

    def test_a_faithful_reply_is_byte_identical(self):
        assert restore_masked_secrets(_INI, mask_api_key(_INI)) == _INI

    def test_a_reply_that_changes_other_lines_restores_the_keys(self):
        """A reply that only rewrites lines of its own keeps every key: the
        headers the keys sit under are exactly where the original has them."""
        reply = mask_api_key(_PROFILE).replace("model = gpt-4o-mini",
                                               "model = gpt-4o")
        assert restore_masked_secrets(_PROFILE, reply) == _PROFILE.replace(
            "model = gpt-4o-mini", "model = gpt-4o")

    def test_two_identical_lines_in_one_place_are_restored_in_order(self):
        original = ('a_key = "sk-aaaaaaaaaaaaaaaa"\r\n'
                    'api_key = "sk-bbbbbbbbbbbbbbbb"\r\n'
                    'api_key = "sk-cccccccccccccccc"\r\n')
        # the a_key line is itself masked, so the two api_key lines share one
        # place and are interchangeable only with each other
        assert restore_masked_secrets(original, mask_api_key(original)) == original

    def test_a_rewritten_masked_line_is_kept_as_written(self):
        """A line the model edited does not equal a masked line of the file, so
        there is nothing to put back."""
        reply = mask_api_key(_INI).replace(
            f"[groq]\napi_key = here_your_key",
            f"[groq]\napi_key = os.environ['GROQ_KEY']")
        out = restore_masked_secrets(_INI, reply)
        assert "os.environ['GROQ_KEY']" in out
        assert _GROQ not in out
        # the other two are still theirs
        assert f"[openai]\napi_key = {_OPENAI}\n" in out
        assert f"[openrouter]\napi_key = {_OPENROUTER}\n" in out

    def test_no_masked_line_returns_the_reply_untouched(self):
        assert restore_masked_secrets("x = 1\n", "x = 2\n") == "x = 2\n"
        assert restore_masked_secrets(None, "x") == "x"
        assert restore_masked_secrets("api_key = None\n",
                                      "api_key = None\n") == "api_key = None\n"

    def test_a_trailing_newline_is_not_required(self):
        reply = mask_api_key(_INI).rstrip("\n")
        assert restore_masked_secrets(_INI, reply) == _INI.rstrip("\n")

    def test_an_undecodable_original_line_is_never_a_place(self, caplog):
        original = ("[openai]\napi_key = \ufffd\n"
                    "[groq]\napi_key = gsk-dd\n")
        reply = ("[openai]\napi_key = \ufffd\n"
                 "api_key = here_your_key\n"
                 "[groq]\napi_key = here_your_key\n")
        with caplog.at_level(logging.WARNING, logger="tools.llm_stream"):
            out = restore_masked_secrets(original, reply)
        assert out == ("[openai]\napi_key = \ufffd\n"
                       "api_key = here_your_key\n"
                       f"[groq]\napi_key = gsk-dd\n")
        assert any("api_key = here_your_key" in r.message for r in caplog.records)


class TestAFileOfNothingButKeyLines:
    """The one case the place rule cannot reach on its own, and its limit.

    A file all of whose lines are masked has no place to give a key line: the
    nearest unmasked line above it is nothing. When the model rewrites one key
    line into an unmasked one, the lines below it shift too — so a key line whose
    masked text the whole file holds exactly once, and which no other line of
    that text was restored by place, still comes back. Two candidates do not:
    the limit is what keeps HARDEN-11 A's guarantee.
    """

    _SECRETS = ('HEADERS = {"Authorization": "Bearer sk-or-v1-0123456789abcdef0123456789abcdef"}\n'
                'client = OpenAI(api_key="sk-proj-AbCdEf0123456789xyz")\n'
                'API_KEY = "4f9c2b7e1d3a5c6b"  # prod\n')

    def test_the_key_below_a_rewritten_key_line_comes_back(self):
        reply = mask_api_key(self._SECRETS).replace(
            'client = OpenAI(api_key="here_your_key")',
            'client = OpenAI(api_key=os.environ["KEY"])')
        assert restore_masked_secrets(self._SECRETS, reply) == self._SECRETS.replace(
            'client = OpenAI(api_key="sk-proj-AbCdEf0123456789xyz")',
            'client = OpenAI(api_key=os.environ["KEY"])')

    def test_an_added_key_line_gets_no_key(self):
        """The same file with a *fourth* masked line of its own shape: the text
        is new, so there is no candidate for it and the placeholder stays."""
        reply = (mask_api_key(self._SECRETS)
                 + 'OTHER_KEY = "here_your_key"  # new\n')
        out = restore_masked_secrets(self._SECRETS, reply)
        assert out == self._SECRETS + 'OTHER_KEY = "here_your_key"  # new\n'


class TestTheCoderWritesEachKeyUnderItsOwnSection:
    """Layer 2 of the acceptance: `/edit` of an ini, asked to delete the first
    section, leaves the other two keys under their own sections."""

    def test_the_deleted_section_is_the_only_change_on_disk(self, tmp_path):
        rel = "agents.ini"
        (tmp_path / rel).write_text(_INI, encoding="utf-8")

        # what the model is shown (the file masked), and the reply it gives back
        reply = ("[openrouter]\napi_key = here_your_key\n"
                 "[groq]\napi_key = here_your_key\n")
        written, err = _coder()._write_files(
            [{"path": rel, "content": reply}], tmp_path, task_id="T1",
            allowed_paths={rel})
        assert written == [rel] and err == ""

        out = (tmp_path / rel).read_text(encoding="utf-8")
        assert out == (f"[openrouter]\napi_key = {_OPENROUTER}\n"
                       f"[groq]\napi_key = {_GROQ}\n")
        assert _OPENAI not in out

    def test_a_faithful_edit_keeps_the_three_keys(self, tmp_path):
        rel = "agents.ini"
        (tmp_path / rel).write_text(_PROFILE, encoding="utf-8")
        reply = mask_api_key(_PROFILE).replace("model = gpt-4o-mini",
                                               "model = gpt-4o")
        written, err = _coder()._write_files(
            [{"path": rel, "content": reply}], tmp_path, task_id="T1",
            allowed_paths={rel})
        assert written == [rel] and err == ""
        assert (tmp_path / rel).read_text(encoding="utf-8") == _PROFILE.replace(
            "model = gpt-4o-mini", "model = gpt-4o")

    def test_a_placeholder_that_reaches_a_real_key_is_refused(self, tmp_path, caplog):
        """A reply that moved a section gets the placeholder and a warning
        naming the line, and HARDEN-11 A wrote the file anyway — a placeholder on
        disk is a working key destroyed, so HARDEN-13 A2 refuses the write: the
        file stays byte-identical, and the refusal names the file and the line so
        the attempt's feedback can ask for the line back."""
        rel = "agents.ini"
        (tmp_path / rel).write_text(_INI, encoding="utf-8")
        reply = ("[anthropic]\napi_key = here_your_key\n"
                 "[openai]\napi_key = here_your_key\n"
                 "[openrouter]\napi_key = here_your_key\n"
                 "[groq]\napi_key = here_your_key\n")
        with caplog.at_level(logging.WARNING, logger="tools.llm_stream"):
            written, err = _coder()._write_files(
                [{"path": rel, "content": reply}], tmp_path, task_id="T1",
                allowed_paths={rel})
        assert written == [] and err
        assert "[SAFETY]" in err and "'agents.ini'" in err and "line 2" in err
        assert (tmp_path / rel).read_text(encoding="utf-8") == _INI
        assert any("here_your_key" in r.message for r in caplog.records)


def test_a_blank_line_between_a_header_and_its_key_is_not_a_place():
    """`[a]` + blank + `api_key = …` is the layout of most hand-written inis: with the blank counted as the
    place, every key shared one place and deleting the first section handed its key to the second."""
    from tools.llm_stream import mask_api_key, restore_masked_secrets
    original = "[a]\n\napi_key = sk-proj-aaa111bbb222\n\n[b]\n\napi_key = sk-proj-ccc333ddd444\n"
    masked = mask_api_key(original)
    assert restore_masked_secrets(original, masked) == original
    reply = masked.replace("[a]\n\napi_key = here_your_key\n", "").lstrip("\n")
    out = restore_masked_secrets(original, reply)
    assert out == "[b]\n\napi_key = sk-proj-ccc333ddd444\n"
    assert "sk-proj-aaa111bbb222" not in out
