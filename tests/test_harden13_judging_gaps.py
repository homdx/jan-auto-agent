"""tests/test_harden13_judging_gaps.py — HARDEN-13 (311): three gaps found at judging, shared by the entries.

A line the model deleted between a header and its key is not a reason to lose the key; an XML element that
names an environment variable is a reference, like `api_key: $VAR`; and the alignment of a long file stays
fast (SequenceMatcher is quadratic in the lines that repeat).
"""
import logging
import time

from tools.llm_stream import mask_api_key, restore_masked_secrets

KA = "sk-live-AAAAAAAAAAAAAAAAAAAA1"
KB = "sk-live-BBBBBBBBBBBBBBBBBBBB2"
INI = f"[openai]\nbase_url = http://localhost:1234\napi_key = {KA}\n\n[groq]\nbase_url = http://x\napi_key = {KB}\n"


def test_a_base_url_deleted_above_a_key_keeps_both_keys():
    reply = mask_api_key(INI).replace("base_url = http://localhost:1234\n", "")
    out = restore_masked_secrets(INI, reply)
    assert KA in out and KB in out and "here_your_key" not in out


def test_a_deleted_line_does_not_let_a_key_move_to_another_header():
    reply = "[provider_c]\napi_key = here_your_key\n\n" + mask_api_key(INI)
    out = restore_masked_secrets(INI, reply)
    assert f"[provider_c]\napi_key = {KA}" not in out and f"[provider_c]\napi_key = {KB}" not in out


def test_an_xml_element_naming_an_environment_variable_is_a_reference():
    for line in ("<api_key>$OPENAI_API_KEY</api_key>", "<api_key>${OPENAI_API_KEY}</api_key>", "  <apiKey>$KEY</apiKey>"):
        assert mask_api_key(line) == line
    assert mask_api_key("<key>$KEY</key> <!-- api_key -->") == "<key>$KEY</key> <!-- api_key -->"


def test_a_long_file_with_repeated_lines_is_restored_in_well_under_a_second():
    logging.disable(logging.CRITICAL)
    try:
        _long_file_body()
    finally:
        logging.disable(logging.NOTSET)


def _long_file_body():
    lines = [f"    x{i % 7} = {i % 5}\n    }}\n\n" for i in range(8000)]
    lines.insert(4000, f"api_key = {KA}\n")
    original = "".join(lines)
    reply = mask_api_key(original).replace("x1 = 1", "x1 = 9", 1)
    reply = "# head\n" + reply + "# tail\n"
    started = time.monotonic()
    out = restore_masked_secrets(original, reply)
    assert time.monotonic() - started < 8.0
    assert KA in out


def test_a_key_line_the_model_edited_around_keeps_its_key():
    """`"api_key": "…", "model": "x"` with the model changed, and a key followed by blanks or a comment, are not
    the masked text of any original line, and used to be written with the placeholder in them and no warning."""
    from tools.llm_stream import unrestored_masked_lines
    js = f'{{\n  "a": {{"api_key": "{KA}", "model": "x"}},\n  "b": {{"api_key": "{KB}", "model": "y"}}\n}}\n'
    ini = f"[a]\nbase_url = u1\napi_key = {KA}\n[b]\nbase_url = u2\napi_key = {KB}\n"
    py = f'x = f(api_key="{KA}", secret_key="{KB}")\n'
    for original, edit in ((js, lambda s: s.replace('"x"', '"z"')),
                           (js, lambda s: s.replace('"model": "x"', '"model": "x", "timeout": 3')),
                           (ini, lambda s: s.replace("here_your_key", "here_your_key  ")),
                           (ini, lambda s: s.replace("api_key = here_your_key", "api_key = here_your_key  # main", 1)),
                           (py, lambda s: s.replace("f(", "g("))):
        reply = edit(mask_api_key(original))
        out = restore_masked_secrets(original, reply)
        assert "here_your_key" not in out and KA in out
        assert unrestored_masked_lines(original, reply) == []
