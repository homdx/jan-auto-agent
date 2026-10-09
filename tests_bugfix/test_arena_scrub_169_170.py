"""tests_bugfix/test_arena_scrub_169_170.py — pins tickets 169 (prefixed secret names) and 170 (linear scrub)."""

from __future__ import annotations

import time

import pytest

from tools.arena import output


# ── 169: prefixed secret names are scrubbed ─────────────────────────────────
@pytest.mark.parametrize("text,want", [
    ("access_token=abc", "access_token=***"),
    ("x?gate_token=abc&y=1", "x?gate_token=***&y=1"),
    ("CLIENT_SECRET=s3", "CLIENT_SECRET=***"),
    ("OPENAI_API_KEY=sk-1 next", "OPENAI_API_KEY=*** next"),
    ("db.password=p", "db.password=***"),
    ("api-key=abc", "api-key=***"),
    # camelCase names: the same words `output.mask` finds in a dict key
    ("accessToken=abc", "accessToken=***"),
    ("openaiApiKey=sk-1&x=2", "openaiApiKey=***&x=2"),
])
def test_169_prefixed_secret_names_are_masked(text, want):
    assert output.scrub(text) == want


def test_169_non_secret_names_survive():
    text = "monkey=banana tokens=12 max_tokens=5 keyboard=us"
    assert output.scrub(text) == text


def test_169_refuse_line_hides_the_env_key(capsys):
    output.refuse("cannot start: OPENAI_API_KEY=sk-live-123")
    assert "sk-live-123" not in capsys.readouterr().err


# ── 170: one long word must not stall the scrub (O(n²) before) ──────────────
@pytest.mark.parametrize("text", ["a-" * 20000, "a." * 20000, "x" * 100000,
                                  "k=v " * 30000, "a-" * 50000, "a." * 50000,
                                  "http://" * 20000])
def test_170_scrub_stays_linear_on_a_long_line(text):
    started = time.monotonic()
    output.scrub(text)
    assert time.monotonic() - started < 2.0


@pytest.mark.parametrize("text,want", [
    ("see https://u:p@h/x", "see https://***@h/x"),
    ("git+ssh://me:pw@host/r", "git+ssh://***@host/r"),
    ("(http://a:b@c)", "(http://***@c)"),
])
def test_170_url_credentials_still_masked(text, want):
    assert output.scrub(text) == want
