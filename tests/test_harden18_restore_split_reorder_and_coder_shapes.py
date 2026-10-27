"""tests/test_harden18_restore_split_reorder_and_coder_shapes.py — HARDEN-18 (316).

Five defects on one request path, one writer behind each.

**Part A — the model split a key call over several lines.**
`client = OpenAI(api_key="sk-proj-…", timeout=30)` became

    client = OpenAI(
        api_key="here_your_key",
        timeout=30,
    )

and `status=approved` was printed: the split line is not the masked text of any
original line, so the restore could not pair it, and the placeholder went to disk
with the real key only in the `.bak`. The masked lines of a replaced region are
now paired with its placeholder lines by order, and a count that does not match
is a failed restore, so the write is refused.

**Part B — a non-string `content` was written as its repr.**
`str(content)` turned `["import os", "print(os.getcwd())"]` into the file text
`['import os', 'print(os.getcwd())']` and `false` into `False` — for a `.py`
file that is a valid expression statement, so the code was gone and nothing
complained. A list of strings is the file's lines, joined without stripping; any
other non-string value is a parse error that names the field.

**Part C — `/edit "my notes.txt"` kept the quotes, and the hint was not shell-safe.**
A matching pair of quotes around the path is the quoting of a name with a space;
the `restore with:` hint is now `shlex.quote`d on both sides.

**Part D — two sections that share a line traded their keys on a reorder.**
`[openai_personal]` and `[openai_work]` held the same `base_url` above each of
their keys, and "put the work section first" wrote the personal key under the work
header and the work key under the personal header, `written=['agents.ini']` and no
error. A masked line is paired with its original only on a line the file holds
*once* — the section header, or the nearest unique neighbour.

**Part E — the first write of an id-less task made no backup.**
`_backup_is_new` compared against `""`, so an empty `task_id` was `"" != ""` and
the first `.coder.bak` was never made; `./app.py` and `app.py` were two keys, so
the second write of the same file copied attempt one over the original.
"""

from __future__ import annotations

import configparser
import json
import shlex
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

_KEY_A = "sk-live-AAAAAAAAAAAAAAAA"
_KEY_B = "sk-live-BBBBBBBBBBBBBBBB"
_KEY_C = "sk-live-CCCCCCCCCCCCCCCCCC"

# A key inside a call, so a model can split it over lines.
_CALL = f'client = OpenAI(api_key="{_KEY_A}", timeout=30)\n'
_SPLIT = (
    "client = OpenAI(\n"
    '    api_key="here_your_key",\n'
    "    timeout=30,\n"
    ")\n"
)
_SPLIT_RESTORED = (
    "client = OpenAI(\n"
    f'    api_key="{_KEY_A}",\n'
    "    timeout=30,\n"
    ")\n"
)
# One key, two placeholders: the restore cannot say which key a placeholder is.
_SPLIT_UNPAIRED = (
    "client = OpenAI(\n"
    '    api_key="here_your_key",\n'
    '    token="here_your_key",\n'
    "    timeout=30,\n"
    ")\n"
)
_TWO_CALLS = (
    f'client_a = OpenAI(api_key="{_KEY_A}", timeout=30)\n'
    f'client_b = Groq(api_key="{_KEY_B}")\n'
)
_TWO_CALLS_ONE_SPLIT = (
    "client_a = OpenAI(\n"
    '    api_key="here_your_key",\n'
    "    timeout=30,\n"
    ")\n"
    'client_b = Groq(api_key="here_your_key")\n'
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
    """An `OrchestratorActions` whose two LLM calls are stubbed: the model hands
    back *revision* and approves it, as the other `/edit` tests do."""
    import tools.actions as A

    act = A.OrchestratorActions.__new__(A.OrchestratorActions)
    act.max_iterations, act.timeout_seconds, act.stream_agents = 2, 60, False
    act._edit_file_content = lambda instr, fp, original, **kw: (
        revision(original) if callable(revision) else revision)
    act._validate_edit = lambda *a, **kw: {"status": "approved"}
    return act


def _parse_edit_command(user_input: str):
    from tools.actions import OrchestratorActions

    return OrchestratorActions._parse_edit_command(user_input)


def _restore_hint(stdout: str) -> str:
    """The `mv …` command after `restore with:` on the final status line."""
    marker = "restore with: "
    parts = stdout.rsplit(marker, 1)
    assert len(parts) == 2, stdout
    return parts[1].rsplit(")", 1)[0]


# ── Part A: a key call split over several lines ─────────────────────────────


class TestASplitKeyCallKeepsTheKey:
    def test_a_call_split_over_lines_restores_the_key(self):
        out = restore_masked_secrets(_CALL, _SPLIT)
        assert out == _SPLIT_RESTORED
        assert unrestored_masked_lines(_CALL, _SPLIT) == []

    def test_the_split_key_is_written_by_the_coder(self, tmp_path):
        rel = "app.py"
        (tmp_path / rel).write_text(_CALL, encoding="utf-8")
        written, err = _coder()._write_files(
            [{"path": rel, "content": _SPLIT}], tmp_path,
            task_id="H18-A", allowed_paths={rel})
        assert written == [rel], err
        assert (tmp_path / rel).read_text(encoding="utf-8") == _SPLIT_RESTORED

    def test_the_split_key_is_written_by_edit(self, tmp_path, capsys):
        rel = "app.py"
        (tmp_path / rel).write_text(_CALL, encoding="utf-8")
        _editor(lambda original: _SPLIT).run_edit(
            "/edit app.py :: split the call", str(tmp_path))
        assert (tmp_path / rel).read_text(encoding="utf-8") == _SPLIT_RESTORED
        assert _KEY_A in (tmp_path / rel).read_text(encoding="utf-8")

    def test_a_placeholder_with_no_key_to_pair_is_a_failed_restore(self):
        out = restore_masked_secrets(_CALL, _SPLIT_UNPAIRED)
        assert _KEY_A not in out
        assert unrestored_masked_lines(_CALL, _SPLIT_UNPAIRED) == [
            (2, '    api_key="here_your_key",'),
            (3, '    token="here_your_key",')]

    def test_the_coder_refuses_the_unpaired_placeholder(self, tmp_path):
        rel = "app.py"
        (tmp_path / rel).write_text(_CALL, encoding="utf-8")
        written, err = _coder()._write_files(
            [{"path": rel, "content": _SPLIT_UNPAIRED}], tmp_path,
            task_id="H18-A", allowed_paths={rel})
        assert written == []
        assert "[SAFETY]" in err and "'app.py'" in err and "line 2" in err
        assert (tmp_path / rel).read_text(encoding="utf-8") == _CALL

    def test_edit_refuses_the_unpaired_placeholder(self, tmp_path, capsys):
        rel = "app.py"
        (tmp_path / rel).write_text(_CALL, encoding="utf-8")
        _editor(lambda original: _SPLIT_UNPAIRED).run_edit(
            "/edit app.py :: split the call", str(tmp_path))
        assert (tmp_path / rel).read_text(encoding="utf-8") == _CALL
        out = capsys.readouterr().out
        assert "app.py" in out and "line 2" in out and "unchanged" in out
        assert not list(tmp_path.glob("app.py.bak.*"))

    def test_two_keys_one_split_one_untouched_keeps_both(self):
        """Neither key may be lost: both back in place, or the write refused."""
        out = restore_masked_secrets(_TWO_CALLS, _TWO_CALLS_ONE_SPLIT)
        failures = unrestored_masked_lines(_TWO_CALLS, _TWO_CALLS_ONE_SPLIT)
        assert failures == []
        assert _KEY_A in out and _KEY_B in out
        assert "here_your_key" not in out

    def test_the_one_line_forms_still_keep_the_key(self):
        """The shapes HARDEN-13 closed must not regress while the split is fixed."""
        reply = mask_api_key(_CALL).replace("timeout=30", "base_url=1")
        assert _KEY_A in restore_masked_secrets(_CALL, reply)
        for variant in (
            "try:\n    client = OpenAI(api_key=\"here_your_key\", timeout=30)\n",
            "client = OpenAI(api_key='here_your_key', timeout=30)\n",
            f"other = OpenAI(api_key=\"here_your_key\", timeout=30)\n",
        ):
            out = restore_masked_secrets(_CALL, variant)
            assert _KEY_A in out and "here_your_key" not in out, variant


# ── Part B: a non-string `content` is not written as its repr ──────────────


class TestANonStringContentIsNotWritten:
    def _parse(self, content):
        return _coder()._parse_response(
            json.dumps({"files": [{"path": "app.py", "content": content}]}), "T")

    def test_a_list_of_lines_is_the_file(self):
        parsed, err = self._parse(["import os", "print(os.getcwd())"])
        assert err == ""
        assert parsed == [{"path": "app.py",
                           "content": "import os\nprint(os.getcwd())\n"}]

    def test_an_indented_line_keeps_its_indentation(self):
        parsed, err = self._parse(["  def f():", "    return 1"])
        assert err == ""
        assert parsed[0]["content"] == "  def f():\n    return 1\n"

    def test_a_list_that_already_ends_with_a_newline_gets_one_only(self):
        parsed, _ = self._parse(["import os\n"])
        assert parsed[0]["content"] == "import os\n"

    @pytest.mark.parametrize("content", [0, 1.5, False, True, {"a": 1}, None, ["import os", 3]])
    def test_any_other_non_string_content_is_a_parse_error(self, content, caplog):
        parsed, err = self._parse(content)
        assert parsed == []
        # HARDEN-18 judge: the message names the item and the type it got.
        assert err.startswith("no valid file entries after parsing 1 item(s)")
        assert "'app.py'" in err and "'content' must be a string" in err
        # The item is named the way a missing content is, so the loop knows to ask
        # for the text instead of writing the repr of what it got.
        assert "'content'" in caplog.text and "app.py" in caplog.text

    def test_a_string_still_takes_the_code_fence_strip(self):
        parsed, _ = self._parse("```python\nimport os\n```")
        assert parsed[0]["content"] == "import os\n"

    def test_an_empty_string_stays_what_it_is_today(self):
        parsed, err = self._parse("")
        assert err == ""
        assert parsed[0]["content"] == "\n"

    def test_a_number_is_not_written_to_disk(self, tmp_path):
        rel = "app.py"
        (tmp_path / rel).write_text("import os\n", encoding="utf-8")
        written, err = _coder()._write_files(
            [{"path": rel, "content": 0}], tmp_path,
            task_id="H18-B", allowed_paths={rel})
        assert written == []
        assert (tmp_path / rel).read_text(encoding="utf-8") == "import os\n"


# ── Part C: a quoted path, and a shell-safe hint ────────────────────────────


class TestAQuotedPathIsUnquoted:
    def test_both_quote_pairs_are_stripped(self):
        for quoted, plain in (
            ('"my notes.txt"', "my notes.txt"),
            ("'my notes.txt'", "my notes.txt"),
            ("plain.txt", "plain.txt"),
        ):
            payload, path = _parse_edit_command(
                f"/edit {quoted} :: append a line")
            assert (payload, path) == ("append a line", plain), quoted

    def test_a_name_with_an_inner_quote_is_left_alone(self):
        for name in ("it's a note.txt", '"my"file.txt', "a'b.txt"):
            _, path = _parse_edit_command(f"/edit {name} :: touch it")
            assert path == name, name

    def test_a_quoted_path_with_a_space_is_edited(self, tmp_path):
        rel = "my notes.txt"
        (tmp_path / rel).write_text("alpha\n", encoding="utf-8")
        _editor(lambda original: "alpha\nbeta\n").run_edit(
            f'/edit "{rel}" :: add a line', str(tmp_path))
        assert (tmp_path / rel).read_text(encoding="utf-8") == "alpha\nbeta\n"

    def test_the_hint_is_three_words_for_a_spaced_name(self, tmp_path, capsys):
        rel = "my notes.txt"
        (tmp_path / rel).write_text("alpha\n", encoding="utf-8")
        _editor(lambda original: "alpha\nbeta\n").run_edit(
            f"/edit {rel} :: add a line", str(tmp_path))
        out = capsys.readouterr().out
        hint = _restore_hint(out)
        words = shlex.split(hint)
        assert len(words) == 3, hint
        assert words[0] == "mv" and words[-1].endswith(rel)

    def test_the_hint_is_plain_for_a_plain_name(self, tmp_path, capsys):
        rel = "plain.txt"
        (tmp_path / rel).write_text("alpha\n", encoding="utf-8")
        _editor(lambda original: "alpha\nbeta\n").run_edit(
            f"/edit {rel} :: add a line", str(tmp_path))
        words = shlex.split(_restore_hint(capsys.readouterr().out))
        assert len(words) == 3 and words[0] == "mv"
        assert f"{rel}.bak." in words[1] and words[2].endswith(rel)


# ── Part D: sections that share a line must not trade their keys ────────────


def _section(header: str, key: str, shared_above: bool = True, shared_below: bool = True) -> str:
    body = ""
    if shared_above:
        body += "base_url = https://api.openai.com/v1\n"
    body += f"api_key = {key}\n"
    if shared_below:
        body += "model = gpt-4o\n"
    return f"[{header}]\n{body}"


_SHARED_INI = (_section("openai_personal", _KEY_A) + _section("openai_work", _KEY_B))
_SHARED_INI_SWAPPED = (_section("openai_work", "here_your_key")
                       + _section("openai_personal", "here_your_key"))
_THREE = (_section("alpha", _KEY_A) + _section("beta", _KEY_B) + _section("gamma", _KEY_C))


class TestASharedLineDoesNotTradeTheKeys:
    def test_sections_that_share_both_neighbours_keep_their_keys(self):
        """A reorder of the sections restores by the header, never crossed."""
        out = restore_masked_secrets(_SHARED_INI, _SHARED_INI_SWAPPED)
        assert unrestored_masked_lines(_SHARED_INI, _SHARED_INI_SWAPPED) == []
        expected = (_section("openai_work", _KEY_B)
                    + _section("openai_personal", _KEY_A))
        assert out == expected
        for key in (_KEY_A, _KEY_B):
            assert out.count(key) == 1
        assert "here_your_key" not in out

    def test_the_keys_are_never_crossed(self):
        out = restore_masked_secrets(_SHARED_INI, _SHARED_INI_SWAPPED)
        assert f"[openai_work]\nbase_url = https://api.openai.com/v1\napi_key = {_KEY_A}" not in out
        assert f"[openai_personal]\nbase_url = https://api.openai.com/v1\napi_key = {_KEY_B}" not in out

    def test_a_shared_line_above_only(self):
        ini = (_section("p", _KEY_A, shared_below=False)
               + _section("q", _KEY_B, shared_below=False))
        reply = (_section("q", "here_your_key", shared_below=False)
                 + _section("p", "here_your_key", shared_below=False))
        out = restore_masked_secrets(ini, reply)
        assert unrestored_masked_lines(ini, reply) == []
        body = "base_url = https://api.openai.com/v1\napi_key = "
        assert f"[q]\n{body}{_KEY_B}" in out and f"[p]\n{body}{_KEY_A}" in out
        for key in (_KEY_A, _KEY_B):
            assert out.count(key) == 1
        assert "here_your_key" not in out

    def test_a_shared_line_below_only(self):
        ini = (_section("p", _KEY_A, shared_above=False)
               + _section("q", _KEY_B, shared_above=False))
        reply = (_section("q", "here_your_key", shared_above=False)
                 + _section("p", "here_your_key", shared_above=False))
        out = restore_masked_secrets(ini, reply)
        assert unrestored_masked_lines(ini, reply) == []
        assert f"[q]\napi_key = {_KEY_B}" in out and f"[p]\napi_key = {_KEY_A}" in out
        for key in (_KEY_A, _KEY_B):
            assert out.count(key) == 1
        assert "here_your_key" not in out

    def test_three_sections_rotated_keep_every_key(self):
        reply = (_section("gamma", "here_your_key")
                 + _section("alpha", "here_your_key")
                 + _section("beta", "here_your_key"))
        out = restore_masked_secrets(_THREE, reply)
        assert unrestored_masked_lines(_THREE, reply) == []
        for key in (_KEY_A, _KEY_B, _KEY_C):
            assert out.count(key) == 1
        assert out == reply.replace(_section("alpha", "here_your_key"),
                                    _section("alpha", _KEY_A)).replace(
            _section("beta", "here_your_key"),
            _section("beta", _KEY_B)).replace(
            _section("gamma", "here_your_key"),
            _section("gamma", _KEY_C))

    def test_a_faithful_reply_restores_every_key(self):
        for ini in (_SHARED_INI, _THREE):
            masked = mask_api_key(ini)
            assert "here_your_key" in masked
            assert restore_masked_secrets(ini, masked) == ini
            assert unrestored_masked_lines(ini, masked) == []

    def test_an_edit_that_shares_no_line_still_restores(self):
        ini = (f"[openai]\nbase_url = https://api.openai.com/v1\napi_key = {_KEY_A}\n"
               f"[groq]\nbase_url = https://api.groq.com/v1\napi_key = {_KEY_B}\n")
        reply = mask_api_key(ini).replace(
            "https://api.openai.com/v1", "https://api.openai.com/v2")
        out = restore_masked_secrets(ini, reply)
        assert out == ini.replace("https://api.openai.com/v1", "https://api.openai.com/v2")

    def test_the_coder_keeps_each_key_under_its_own_header(self, tmp_path):
        rel = "agents.ini"
        (tmp_path / rel).write_text(_SHARED_INI, encoding="utf-8")
        written, err = _coder()._write_files(
            [{"path": rel, "content": _SHARED_INI_SWAPPED}], tmp_path,
            task_id="H18-D", allowed_paths={rel})
        assert written == [rel], err
        out = (tmp_path / rel).read_text(encoding="utf-8")
        header = "base_url = https://api.openai.com/v1\napi_key = "
        assert f"[openai_work]\n{header}{_KEY_B}" in out
        assert f"[openai_personal]\n{header}{_KEY_A}" in out

    def test_edit_keeps_each_key_under_its_own_header(self, tmp_path, capsys):
        rel = "agents.ini"
        (tmp_path / rel).write_text(_SHARED_INI, encoding="utf-8")
        _editor(lambda original: _SHARED_INI_SWAPPED).run_edit(
            "/edit agents.ini :: put the work section first", str(tmp_path))
        out = (tmp_path / rel).read_text(encoding="utf-8")
        assert _KEY_A in out and _KEY_B in out, out
        assert "api_key = here_your_key" not in out, out
        assert "status=approved" in capsys.readouterr().out

    def test_a_key_that_cannot_be_paired_refuses_the_write(self, tmp_path):
        rel = "agents.ini"
        (tmp_path / rel).write_text(_SHARED_INI, encoding="utf-8")
        # Both sections moved, the personal header dropped: the personal key has
        # no header of its own to sit under, so the write is refused.
        reply = (_section("openai_work", "here_your_key")
                 + "base_url = https://api.openai.com/v1\n"
                 + "api_key = here_your_key\nmodel = gpt-4o\n")
        written, err = _coder()._write_files(
            [{"path": rel, "content": reply}], tmp_path,
            task_id="H18-D", allowed_paths={rel})
        assert written == []
        assert (tmp_path / rel).read_text(encoding="utf-8") == _SHARED_INI
        assert "here_your_key" not in (tmp_path / rel).read_text(encoding="utf-8")


# ── Part E: the first write of an id-less task makes the backup ─────────────


class TestAnEmptyTaskIdMakesTheBackup:
    def test_the_first_write_of_an_id_less_task_makes_the_backup(self, tmp_path):
        rel = "app.py"
        (tmp_path / rel).write_text("import os\n", encoding="utf-8")
        coder = _coder()
        for content in ("import os\nprint(1)\n", "import os\nprint(2)\n"):
            written, err = coder._write_files(
                [{"path": rel, "content": content}], tmp_path,
                task_id="", allowed_paths={rel})
            assert written == [rel], err
            assert (tmp_path / rel).read_text(encoding="utf-8") == content
        assert (tmp_path / "app.py.coder.bak").read_text(encoding="utf-8") == "import os\n"

    def test_two_writes_of_one_task_keep_the_first_backup(self, tmp_path):
        rel = "app.py"
        (tmp_path / rel).write_text("import os\n", encoding="utf-8")
        coder = _coder()
        coder._write_files([{"path": rel, "content": "import os\nprint(1)\n"}],
                           tmp_path, task_id="H18-E", allowed_paths={rel})
        coder._write_files([{"path": rel, "content": "import os\nprint(2)\n"}],
                           tmp_path, task_id="H18-E", allowed_paths={rel})
        assert (tmp_path / "app.py.coder.bak").read_text(encoding="utf-8") == "import os\n"

    def test_a_dotted_path_and_a_plain_path_are_one_backup(self, tmp_path):
        """`./app.py` then `app.py` is one file: one backup, the original's."""
        rel = "app.py"
        (tmp_path / rel).write_text("import os\n", encoding="utf-8")
        coder = _coder()
        written, err = coder._write_files(
            [{"path": "./app.py", "content": "import os\nprint(1)\n"}], tmp_path,
            task_id="H18-E", allowed_paths={"app.py"})
        assert written, err
        written, err = coder._write_files(
            [{"path": "app.py", "content": "import os\nprint(2)\n"}], tmp_path,
            task_id="H18-E", allowed_paths={"app.py"})
        assert written == ["app.py"], err
        assert (tmp_path / "app.py.coder.bak").read_text(encoding="utf-8") == "import os\n"

    def test_a_new_task_run_replaces_the_backup(self, tmp_path):
        rel = "app.py"
        (tmp_path / rel).write_text("import os\n", encoding="utf-8")
        coder = _coder()
        coder._write_files([{"path": rel, "content": "import os\none\n"}],
                           tmp_path, task_id="", allowed_paths={rel})
        coder._write_files([{"path": rel, "content": "import os\ntwo\n"}],
                           tmp_path, task_id="OTHER", allowed_paths={rel})
        assert (tmp_path / "app.py.coder.bak").read_text(encoding="utf-8") == "import os\none\n"

    def test_the_delete_branch_makes_the_backup_for_an_id_less_task(self, tmp_path):
        rel = "app.py"
        (tmp_path / rel).write_text("import os\n", encoding="utf-8")
        written, err = _coder()._write_files(
            [{"path": rel, "delete": True}], tmp_path,
            task_id="", allowed_paths={rel})
        assert written == [rel] and err == ""
        assert not (tmp_path / rel).exists()
        assert (tmp_path / "app.py.coder.bak").read_text(encoding="utf-8") == "import os\n"
