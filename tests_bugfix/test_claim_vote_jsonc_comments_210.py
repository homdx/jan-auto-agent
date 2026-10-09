"""tests_bugfix/test_claim_vote_jsonc_comments_210.py — bug 48: kilo.jsonc comments after a value or in a block parse, a // in a string survives.

`_kilo_settings` stripped only whole-line `//` comments, so a comment after a
value or a `/* … */` block made `json.loads` raise and `--add-profiles` /
`--models` die with a traceback. Ticket 210: a JSONC reader that removes
comments outside strings, and one refusal line on a file that still does not
parse.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402

CONFIG = """{
  /* Kilo's providers
     — a block comment */
  "provider": {
    "acme": {
      "options": {
        "baseURL": "https://api.acme.invalid/v1/", // a note after a value
        "other": "a // inside a string" /* inline block */
      }
    }
  }
}
"""


@pytest.fixture
def kilo(tmp_path, monkeypatch):
    config, auth = tmp_path / "kilo.jsonc", tmp_path / "auth.json"
    auth.write_text(json.dumps({"acme": {"key": "k-secret"}}), encoding="utf-8")
    monkeypatch.setattr(cv, "KILO_CONFIG", config)
    monkeypatch.setattr(cv, "KILO_AUTH", auth)
    return config


def test_trailing_and_block_comments_parse_and_a_url_keeps_its_slashes(kilo):
    kilo.write_text(CONFIG, encoding="utf-8")
    st = cv._kilo_settings("acme/m-1")
    assert st.base_url == "https://api.acme.invalid/v1" and st.model == "m-1"
    assert st.api_key == "k-secret"


def test_a_slash_pair_inside_a_string_and_an_escaped_quote_survive(kilo):
    kilo.write_text('{"provider": {"acme": {"options": '
                    '{"baseURL": "https://h//x", "n": "q\\" // r"}}}} // tail\n',
                    encoding="utf-8")
    assert cv._kilo_settings("acme/m").base_url == "https://h//x"


def test_garbage_is_one_refusal_line_not_a_traceback(kilo, tmp_path, capsys):
    kilo.write_text("{ this is not json // at all", encoding="utf-8")
    code = cv.main(["--add-profiles", "acme/m-1", "--repo-root", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 2
    assert len(err.strip().splitlines()) == 1 and "Traceback" not in err
    assert "not valid JSONC" in err and "k-secret" not in err
    assert not (tmp_path / "contest.local.ini").exists()


def test_add_profiles_works_through_a_commented_config(kilo, tmp_path, capsys):
    kilo.write_text(CONFIG, encoding="utf-8")
    assert cv.main(["--add-profiles", "acme/m-1", "--repo-root", str(tmp_path)]) == 0
    text = (tmp_path / "contest.local.ini").read_text(encoding="utf-8")
    assert "https://api.acme.invalid/v1" in text


# From the Sonnet 5.5 entry of round 210, on this solution's contract: an unreadable
# config is one `ValueError` line (``ask`` records it as the voter's error), never a
# `SystemExit` raised inside a voter's worker thread.
SONNET_CONFIG = """{
  // a whole-line comment
  "provider": {
    "p": { "options": { "baseURL": "https://x.example/v1/", // after a value
      /* a block
         comment */ "other": "a \\" // not a comment" } }
  }
}
"""


def test_strip_leaves_strings_alone():
    out = json.loads(cv.strip_jsonc_comments(SONNET_CONFIG))
    assert out["provider"]["p"]["options"]["other"] == 'a " // not a comment'
    assert out["provider"]["p"]["options"]["baseURL"] == "https://x.example/v1/"


def test_an_unterminated_block_comment_is_dropped_not_raised():
    assert cv.strip_jsonc_comments('{"a": 1} /* open').strip() == '{"a": 1}'


@pytest.mark.parametrize("text", ["{ not json", "[]", '{"provider": {}}',
                                  '{"provider": {"acme": {"options": {"baseURL": 7}}}}'])
def test_a_config_without_the_provider_is_one_value_error_line(kilo, text):
    kilo.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError) as err:
        cv._kilo_settings("acme/m")
    assert "\n" not in str(err.value) and str(err.value).startswith(str(kilo))


@pytest.mark.parametrize("text", ["[]", '{"provider": {}}'])
def test_add_profiles_refuses_a_config_without_the_provider_in_one_line(kilo, tmp_path, capsys, text):
    kilo.write_text(text, encoding="utf-8")
    assert cv.main(["--add-profiles", "acme/m-1", "--repo-root", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "Traceback" not in err


def test_a_missing_config_is_one_refusal_line(kilo, tmp_path, capsys):
    assert cv.main(["--add-profiles", "acme/m-1", "--repo-root", str(tmp_path)]) == 2
    assert len(capsys.readouterr().err.strip().splitlines()) == 1
