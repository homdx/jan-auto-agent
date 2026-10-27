"""tests_bugfix/test_harden9_secret_mask_dotted_and_colon_307.py — HARDEN-9 A and B:
the secret mask no longer reads a dotted token as an attribute access, and it looks at an
unquoted `api_key: <value>` line at all.

`mask_api_key` runs on the wire, on every request, so whatever it lets through is sent to
the provider. HARDEN-1 A8 and HARDEN-5 B taught it to leave code alone — `self.api_key =
token`, `os.environ["X"]` — and the price was that a *literal* which merely contains a dot
was read as an attribute path (a JWT, `ya29.a0AfH6…`), and a literal in the YAML form was
not looked at at all: `_API_KEY_LINE_RE` wants an `=`, `_API_KEY_JSON_RE` wants a quoted
value, and `api_key: <hex>` is neither.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.llm_stream import _value_is_secret_literal, mask_api_key  # noqa: E402

_MASKED = "here_your_key"
JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
       "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
GOOGLE = "ya29.a0AfH6SMBx9abcdefghij1234567890"


class TestADottedTokenIsMasked:
    """Part A: the ticket's three rows, in `.env`, INI, `export` and commented forms."""

    @pytest.mark.parametrize("text", [
        f"API_KEY={JWT}",
        f"OPENAI_API_KEY={GOOGLE}",
        f"api_key = {JWT}",
        f"export API_KEY={JWT}",
        f";api_key={GOOGLE}",
        f"# api_key = {JWT}",
        f"api_key = {JWT}  # production token",
        f"api_key = {JWT}\r\n",
        f'api_key = "{JWT}"',
        "api_key = sk-live-abcdefghijklmnopqrstuvwxyz0123",   # the old gap, unchanged
    ])
    def test_the_dotted_token_does_not_leave(self, text):
        out = mask_api_key(text)
        assert JWT not in out
        assert GOOGLE not in out
        assert _MASKED in out
        assert mask_api_key(out) == out, "a masked value never re-masks"

    @pytest.mark.parametrize("value", [
        JWT,
        GOOGLE,
        "0123456789abcdef.0123456789abcdef",
    ])
    def test_the_dotted_token_is_a_literal(self, value):
        assert _value_is_secret_literal(value, "api_key") is True


class TestAnAttributePathIsStillCode:
    """The HARDEN-1 A8 and HARDEN-5 B pins: a path of names is a reference, not a token."""

    @pytest.mark.parametrize("text", [
        "api_key = config.api_key",
        "api_key = self.settings.api_key",
        "api_key = os.environ.get",
        "api_key = settings.OPENAI_KEY",
        "api_key = config.openai.api_key",
        "api_key = client.v2Endpoint",
        "api_key = 3.5",
        "api_key = 1.5.5",
        "api_key = a.b[0]",
        "api_key = get_key()",
        "api_key = 30",
        "api_key = None",
    ])
    def test_the_expression_is_sent_as_it_is(self, text):
        assert mask_api_key(text) == text

    @pytest.mark.parametrize("value", [
        "config.api_key",
        "self.settings.api_key",
        "settings.OPENAI_KEY",
        "config.openai.api_key",
        "os.environ.get",
        "3.5",
    ])
    def test_the_path_is_not_a_literal(self, value):
        assert _value_is_secret_literal(value, "api_key") is False


class TestTheUnquotedColonForm:
    """Part B: the ticket's table, row by row."""

    @pytest.mark.parametrize("text, secret", [
        ("api_key: 0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
        ("api_key: 123e4567-e89b-12d3-a456-426614174000", "123e4567-e89b-12d3-a456-426614174000"),
        ("api_key: dGhpc2lzYXNlY3JldGtleXZhbHVlMTIzNDU2", "dGhpc2lzYXNlY3JldGtleXZhbHVlMTIzNDU2"),
        ("api_key: AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("api_key: ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
        ("api_key: sk-abcdefghijklmnopqrstuvwxyz0123", "sk-abcdefghijklmnopqrstuvwxyz0123"),
        (f"api_key: {JWT}", JWT),
        (f"  api_key: {JWT}", JWT),
        (f"  - api_key: {JWT}", JWT),
        ("  api_key: 0123456789abcdef0123456789abcdef  # production key",
         "0123456789abcdef0123456789abcdef"),
        ("openai_api_key: 0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
        ("# api_key: 0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
    ])
    def test_the_colon_form_is_masked(self, text, secret):
        out = mask_api_key(text)
        assert secret not in out, text
        assert _MASKED in out, text
        assert mask_api_key(out) == out, "a masked value never re-masks"

    @pytest.mark.parametrize("text", [
        "api_key: <your key>",
        "api_key: changeme",
        "api_key: api_key",
        "api_key: str",
        "api_key: None",
        "api_key: Optional[str] = None",
        "def f(api_key: str):",
        "    api_key: str,",
        "    api_key:",
        "if self._api_key:",
        "api_key: 123",
        "api_key: some_variable",
    ])
    def test_a_placeholder_a_name_and_a_type_after_a_colon_are_not_touched(self, text):
        assert mask_api_key(text) == text

    def test_a_quoted_colon_value_keeps_its_quotes(self):
        assert mask_api_key('api_key: "sk-abcdefghijklmnopqrstuvwxyz0123"') == \
            f'api_key: "{_MASKED}"'
        assert mask_api_key("api_key: 'sk-abcdefghijklmnopqrstuvwxyz0123'") == \
            f"api_key: '{_MASKED}'"

    def test_a_json_line_is_still_the_json_rule(self):
        assert mask_api_key('{"api_key": "sk-secret"}') == '{"api_key": "here_your_key"}'

    def test_a_yaml_mapping_value_that_is_not_a_literal_is_kept(self):
        assert mask_api_key("api_key: value") == "api_key: value"


def _code_line_numbers(source: str) -> set:
    """The lines a module actually executes: every statement, minus docstrings (string
    literals) and minus comments and blanks, which sit inside a statement's range and run
    nothing."""
    tree = ast.parse(source)
    executed: set = set()
    docstrings: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        span = range(node.lineno, getattr(node, "end_lineno", node.lineno) + 1)
        value = getattr(node, "value", None)
        if isinstance(node, ast.Expr) and isinstance(value, ast.Constant) \
                and isinstance(value.value, str):
            docstrings.update(span)
        else:
            executed.update(span)
    noise: set = set()
    for number, line in enumerate(source.splitlines(), start=1):
        # A comment and a blank line sit inside a statement's range but execute nothing.
        if not line.strip() or line.lstrip().startswith("#"):
            noise.add(number)
    return executed - docstrings - noise


def test_no_line_of_code_of_this_repository_is_rewritten():
    """The mask runs over these files on the wire; a code line that it rewrote would come
    back through the model and land on disk. A comment and a docstring may carry the
    placeholder, since neither of them is executed."""
    paths = sorted(Path("tools").rglob("*.py")) + [Path("main.py")]
    assert paths, "nothing to check"
    for path in paths:
        source = path.read_text(encoding="utf-8")
        before = source.splitlines(keepends=True)
        after = mask_api_key(source).splitlines(keepends=True)
        for number in _code_line_numbers(source):
            assert before[number - 1] == after[number - 1], (path, number)
