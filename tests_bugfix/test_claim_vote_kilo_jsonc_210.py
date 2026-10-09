"""tests_bugfix/test_claim_vote_kilo_jsonc_210.py — ticket 210, bug 48.

``scripts/claim_vote.py``'s ``_kilo_settings`` stripped comments with
``re.sub(r"^\\s*//.*$", "", text, flags=re.M)`` — a whole-line match only. A
trailing ``// note`` after a real value, or a ``/* ... */`` block, made
``json.loads`` raise and ``--add-profiles`` / ``--models`` die with a
traceback; a ``//`` that happens to sit inside a string (a URL) was never
supposed to be touched at all. Fixed with a small JSONC-comment stripper that
walks the text tracking whether it is inside a string literal, and a parse
failure that now ends in one refusal line, not a traceback: a ``ValueError``
naming the file and the place, which ``ask`` records as the voter's error and
``--add-profiles`` prints as one line (the Sonnet 5 entry raised ``SystemExit``
here; the landed fix does not raise it from a voter's worker thread).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote  # noqa: E402


def test_a_trailing_line_comment_after_a_value_is_stripped():
    text = '{\n  "a": 1, // note\n  "b": 2\n}\n'
    stripped = claim_vote.strip_jsonc_comments(text)
    assert json.loads(stripped) == {"a": 1, "b": 2}


def test_a_block_comment_is_stripped():
    text = '{\n  /* block\n     comment */\n  "a": 1\n}\n'
    stripped = claim_vote.strip_jsonc_comments(text)
    assert json.loads(stripped) == {"a": 1}


def test_a_double_slash_inside_a_url_string_survives():
    text = '{"baseURL": "https://example.com/v1", "n": 1}'
    stripped = claim_vote.strip_jsonc_comments(text)
    assert json.loads(stripped) == {"baseURL": "https://example.com/v1", "n": 1}


def test_kilo_settings_parses_a_realistic_jsonc_config(tmp_path, monkeypatch):
    kilo_config = tmp_path / "kilo.jsonc"
    kilo_config.write_text(
        '{\n'
        '  // top-level comment\n'
        '  "provider": {\n'
        '    "acme": {\n'
        '      "options": {\n'
        '        "baseURL": "https://api.acme.test/v1" // trailing note, has // too\n'
        '      }\n'
        '    }\n'
        '  }\n'
        '  /* trailing block comment */\n'
        '}\n',
        encoding="utf-8",
    )
    kilo_auth = tmp_path / "auth.json"
    kilo_auth.write_text(json.dumps({"acme": {"key": "secret"}}), encoding="utf-8")
    monkeypatch.setattr(claim_vote, "KILO_CONFIG", kilo_config)
    monkeypatch.setattr(claim_vote, "KILO_AUTH", kilo_auth)
    settings = claim_vote._kilo_settings("acme/some-model")
    assert settings.base_url == "https://api.acme.test/v1"
    assert settings.model == "some-model"


def test_garbage_config_is_one_refusal_line_not_a_traceback(tmp_path, monkeypatch):
    kilo_config = tmp_path / "kilo.jsonc"
    kilo_config.write_text("not { valid json at all", encoding="utf-8")
    kilo_auth = tmp_path / "auth.json"
    kilo_auth.write_text(json.dumps({"acme": {"key": "secret"}}), encoding="utf-8")
    monkeypatch.setattr(claim_vote, "KILO_CONFIG", kilo_config)
    monkeypatch.setattr(claim_vote, "KILO_AUTH", kilo_auth)
    with pytest.raises(ValueError) as excinfo:
        claim_vote._kilo_settings("acme/some-model")
    # One line, naming the file — not a bare traceback-triggering exception type.
    assert str(excinfo.value).count("\n") == 0
    assert "kilo.jsonc" in str(excinfo.value) or str(kilo_config) in str(excinfo.value)
