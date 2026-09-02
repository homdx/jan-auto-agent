"""tests/test_collect_line_comments_token_error.py — regression.

`tools.collect.registries._line_comments` tokenizes source to find `#`
comments (for fail-open rationale extraction, COLLECT-9). Its docstring
promises graceful degradation — "a source that fails to tokenize ...
yields an empty map rather than raising" — via
``except (tokenize.TokenError, SyntaxError, IndentationError)``.

Previously that tuple named ``tokenize.TokenizeError``, which does not
exist on the stdlib ``tokenize`` module (the real name is
``tokenize.TokenError`` — see ``tests/test_theme_validator.py`` for the
same bug class already caught once in ``tools/auto/coder.py``'s ASCII
guard). Because Python only evaluates an `except` tuple's expressions when
an exception is actually raised, this stayed invisible until source that
genuinely fails to tokenize (e.g. an unterminated string) was fed in: at
that point the handler itself raised ``AttributeError`` instead of
catching anything, breaking the "never raises" contract.
"""

from __future__ import annotations

from tools.collect.registries import _line_comments


def test_line_comments_survives_unterminated_string():
    # Valid enough for callers to pass through, but tokenize.generate_tokens
    # raises TokenError on the unterminated string literal at EOF.
    source = 'x = "unterminated\n'
    # Must not raise (previously: AttributeError from the bad except tuple).
    assert _line_comments(source) == {}


def test_line_comments_normal_case_still_works():
    source = "x = 1  # a real comment\ny = 2\n"
    assert _line_comments(source) == {1: "a real comment"}
