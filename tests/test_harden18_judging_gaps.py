"""tests/test_harden18_judging_gaps.py — HARDEN-18 (316): two gaps the judge's fuzz found.

**A reorder of one-line sections.** In a JSON file whose sections sit one per line
(`"work": {"api_key": "…", "model": "a"},`) the reorder moves the comma, so no
reply line equals a masked original line; the alignment paired the lines by
position and the work key came back under `"openai"` and the openai key under
`"work"`. The text before the placeholder names the section, and it decides.

**The parse error names the field.** `{"content": 0}` answered "no valid file
entries after parsing 1 item(s)", which tells the retry nothing about the field.
"""
import configparser
import json
import tempfile
from pathlib import Path

from tools.auto.coder import Coder
from tools.llm_stream import mask_api_key, restore_masked_secrets

_K_OPENAI = "sk-proj-AAAA1111BBBB2222CCCC3333DDDD4444"
_K_WORK = "sk-proj-ZZZZ9999YYYY8888XXXX7777WWWW6666"
_JSON = (
    '{\n  "openai": {"api_key": "%s", "model": "a"},\n'
    '  "work": {"api_key": "%s", "model": "a"}\n}\n' % (_K_OPENAI, _K_WORK))
_SWAPPED = (
    '{\n  "work": {"api_key": "here_your_key", "model": "a"},\n'
    '  "openai": {"api_key": "here_your_key", "model": "a"}\n}\n')


def _coder():
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "true"},
        "api_local": {"base_url": "http://localhost:9999", "model": "x", "api_key": ""},
        "coder": {"temperature": "0.2", "max_tokens": "1024"},
        "loop": {"timeout_seconds": "60"},
    })
    return Coder(config=cfg, base_url="http://localhost:9999", api_key="",
                 model="x", api_format="openai", verify_ssl=True)


def _key_of(text: str, section: str) -> str:
    return json.loads(text)[section]["api_key"]


def test_a_reordered_one_line_section_keeps_its_own_key():
    out = restore_masked_secrets(_JSON, _SWAPPED)
    assert _key_of(out, "work") == _K_WORK
    assert _key_of(out, "openai") == _K_OPENAI


def test_the_coder_writes_each_key_under_its_own_section(tmp_path):
    (tmp_path / "c.json").write_text(_JSON, encoding="utf-8")
    written, err = _coder()._write_files(
        [{"path": "c.json", "content": _SWAPPED}], tmp_path,
        task_id="H18", allowed_paths={"c.json"})
    text = (tmp_path / "c.json").read_text(encoding="utf-8")
    if written:
        assert _key_of(text, "work") == _K_WORK
        assert _key_of(text, "openai") == _K_OPENAI
    else:
        assert text == _JSON and err


def test_a_faithful_json_reply_restores_byte_for_byte():
    assert restore_masked_secrets(_JSON, mask_api_key(_JSON)) == _JSON


def test_a_non_text_content_error_names_the_item_and_the_type():
    reply = json.dumps({"files": [{"path": "a.py", "content": 0}]})
    files, err = _coder()._parse_response(reply, "H18", ["a.py"])
    assert files == []
    assert "content" in err and "a.py" in err and "int" in err


def test_a_name_with_its_own_quote_inside_is_not_unwrapped():
    from tools.actions import OrchestratorActions as A
    assert A._unquote_path('"my notes.txt"') == "my notes.txt"
    assert A._unquote_path("'my notes.txt'") == "my notes.txt"
    assert A._unquote_path('"a"b"') == '"a"b"'
    assert A._unquote_path("it's.txt") == "it's.txt"
    assert A._unquote_path('"it\'s.txt"') == "it's.txt"
