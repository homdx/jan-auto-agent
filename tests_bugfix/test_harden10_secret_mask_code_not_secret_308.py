r"""tests_bugfix/test_harden10_secret_mask_code_not_secret_308.py — HARDEN-10 B: the secret
mask leaves code alone when the line holds no literal secret.

The `^…$` anchors HARDEN-5 B removed let the value class `[^\s'";]+` match a lone `=`, and
the Bearer class accepted `$`, `{` and `.`. So a line that holds no secret at all was
rewritten: `assert client.api_key == key` went out as `assert client.api_key =here_your_key
key` — code that does not parse — and a template kept its braces stripped,
`f'Bearer {self.settings.api_key}'` became `f'Bearer here_your_key}'`. A string prefix
(`r`, `b`, `f`, `u`, `rb`, …) was left behind its own quotes, so `api_key = r"abc"` went out
as `api_key = here_your_key"abc"`: parseable only by luck, and `restore_masked_secrets` can
never put a real key back in a line whose shape no longer matches its original.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.llm_stream import mask_api_key, restore_masked_secrets  # noqa: E402


_MASKED = "here_your_key"
_LIVE = "sk-live-abcdef1234567890"
_JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
_GOOGLE = "ya29.a0AfH6SMBx9abcdefghij1234567890"
_HEX = "0123456789abcdef0123456789abcdef"


class TestAComparisonIsNotAnAssignment:
    """HARDEN-10 B: `==`, `!=`, `~=`, `<=`, `>=` are comparisons, not assignments.

    The old value class matched the lone `=` of a `==`, and the line left the machine with
    the second one still there: `api_key =here_your_key key`."""

    @pytest.mark.parametrize("line", [
        "assert client.api_key == key",
        "assert client.api_key != key",
        "assert cfg.api_key == None",
        "if client.api_key == expected:",
        "if client.api_key != None:",
        "while client.api_key <= MAX:",
        "if client.api_key >= MIN:",
        "if api_key == here_your_key:",
    ])
    def test_the_comparison_is_sent_verbatim(self, line):
        assert mask_api_key(line) == line

    def test_the_operator_survives_when_the_value_is_a_key(self):
        # `=~` still reads as `=~`, never as an assignment's `=`.
        out = mask_api_key("api_key =~ /sk-live-abcdef1234567890/")
        assert out == "api_key =~ /here_your_key/"
        assert out.count("=") == 1

    def test_the_ticket_row_is_untouched(self):
        assert mask_api_key("assert client.api_key == key") == "assert client.api_key == key"


class TestABearerTokenStopsAtAnInterpolation:
    """HARDEN-10 B: a template hole is a `$` or a `{`, never part of a credential."""

    @pytest.mark.parametrize("line", [
        "`Bearer ${process.env.OPENAI_API_KEY}`",
        "f'Bearer {self.settings.api_key}'",
        'f"Bearer {token}"',
        "f\"Bearer {self.api_key} done\"",
        "api_key=f\"key-{ref}\"",
        "api_key = f\"Bearer {token}\"",
    ])
    def test_the_template_is_sent_verbatim(self, line):
        assert mask_api_key(line) == line

    @pytest.mark.parametrize("line, prefix", [
        ("Bearer ${process.env.OPENAI_API_KEY}", "Bearer ${process.env.OPENAI_API_KEY}"),
        ("f'Bearer {self.settings.api_key}'", "Bearer {self.settings.api_key}"),
    ])
    def test_the_interpolation_is_not_eaten(self, line, prefix):
        assert mask_api_key(line) == line
        assert prefix in mask_api_key(line)


class TestABearerWordInProseIsNotAToken:
    """HARDEN-10 B: `the bearer authentication_scheme is used` is English."""

    @pytest.mark.parametrize("line", [
        "the bearer authentication_scheme is used",
        "the bearer secret_identifier is passed on",
        "bearer authentication_flow configured here",
    ])
    def test_a_word_of_letters_is_prose(self, line):
        assert mask_api_key(line) == line

    def test_a_short_token_is_not_one_either(self):
        # The 16-character rule of HARDEN-5 B stays: a short run is a word.
        assert mask_api_key("Bearer abcdefghijklmnop") == "Bearer abcdefghijklmnop"


class TestAStringPrefixBelongsToTheQuotedValue:
    """HARDEN-10 B: `api_key = r"abc"` masks to `r"here_your_key"`, quotes and all."""

    @pytest.mark.parametrize("line", [
        'api_key = r"abc123abc"',
        "api_key = b'abc123abc'",
        "api_key = f'abc123abc'",
        "api_key = u'abc123abc'",
        "api_key = rb'abc123abc'",
        'api_key = br"abc123abc"',
        'api_key = rf"abc123abc"',
        "api_key = fr'abc123abc'",
    ])
    def test_the_prefix_and_both_quotes_are_kept(self, line):
        out = mask_api_key(line)
        _, _, quoted = line.partition(" = ")
        out_prefix, _, out_quoted = out.partition(" = ")
        assert out_prefix == line.split(" = ")[0]
        # the masked value keeps the original's prefix and quote character on the outside
        assert out_quoted.startswith(quoted[:2] if quoted[:2] in {"rb", "br", "rf", "fr"}
                                     else quoted[:1])
        assert out_quoted.endswith(quoted[-1])
        assert out_quoted.count(quoted[-1]) == 2
        # and it still parses: a bare `here_your_key` on the right-hand side would not
        ast.parse(out)

    @pytest.mark.parametrize("quoted, masked", [
        ('r"abc123abc"', 'r"here_your_key"'),
        ("b'abc123abc'", "b'here_your_key'"),
        ('f"abc123abc"', 'f"here_your_key"'),
        ("rb'abc123abc'", "rb'here_your_key'"),
        ('fr"abc123abc"', 'fr"here_your_key"'),
    ])
    def test_the_shape_of_the_masked_value(self, quoted, masked):
        assert mask_api_key(f"api_key = {quoted}") == f"api_key = {masked}"

    def test_a_two_letter_prefix_does_not_lose_the_quotes(self):
        # `rb"..."` used to come out as the bare name `here_your_key`: a NameError.
        out = mask_api_key("api_key = rb'abc123abc'")
        assert out == "api_key = rb'here_your_key'"
        ast.parse(out)


class TestATemplateQuotedValueIsCode:
    """A `{` inside the quotes makes the string a template, not a literal."""

    @pytest.mark.parametrize("line", [
        'api_key=f"key-{ref}"',
        'api_key = f"Bearer {token}"',
        "api_key = 'Bearer {self.token}'",
    ])
    def test_it_is_sent_verbatim(self, line):
        assert mask_api_key(line) == line


class TestTheSecretsOfHARDEN5BStayMasked:
    """The rows HARDEN-5 B closed are not reopened."""

    @pytest.mark.parametrize("line, expected", [
        (f"api_key = {_LIVE}", f"api_key = {_MASKED}"),
        (f"api_key = {_LIVE}  # production key", f"api_key = {_MASKED}  # production key"),
        (f"api_key = {_LIVE} ; production key", f"api_key = {_MASKED} ; production key"),
        (f"Set api_key = '{_LIVE}' in agents.ini",
         f"Set api_key = '{_MASKED}' in agents.ini"),
        ("Authorization: Bearer sk-or-v1-ABCDEF1234567890abcdef",
         f"Authorization: Bearer {_MASKED}"),
        (f"API_KEY={_JWT}", f"API_KEY={_MASKED}"),
        (f"OPENAI_API_KEY={_GOOGLE}", f"OPENAI_API_KEY={_MASKED}"),
        (f"api_key = {_JWT}  # production token", f"api_key = {_MASKED}  # production token"),
        (f"api_key: {_HEX}", f"api_key: {_MASKED}"),
        (f"api_key: {_JWT}", f"api_key: {_MASKED}"),
        (f"  - api_key: {_HEX}", f"  - api_key: {_MASKED}"),
        (f"export API_KEY={_JWT}", f"export API_KEY={_MASKED}"),
    ])
    def test_still_masked(self, line, expected):
        assert mask_api_key(line) == expected


class TestTheCodeOfHARDEN1A8StaysUntouched:
    """HARDEN-1 A8: an expression, a constant or a name for the key is sent as it is."""

    @pytest.mark.parametrize("line", [
        "self.api_key = api_key",
        "self.api_key = key",
        "key = api_key",
        "API_KEY = DEFAULT_API_KEY",
        "self.api_key = None",
        "api_key = None",
        "api_key = config.api_key",
        "api_key = get_key()",
        'api_key = os.environ["OPENAI_API_KEY"]',
        "api_key = 30",
        "api_key = ''",
        "api_key = \"\"",
    ])
    def test_unchanged(self, line):
        assert mask_api_key(line) == line


class TestTheMaskedLineRestores:
    """HARDEN-10 B: a masked literal comes back byte-identical."""

    @pytest.mark.parametrize("line", [
        'api_key = r"abc123abc"',
        "api_key = b'abc123abc'",
        "api_key = rb'abc123abc'",
        f"api_key = {_LIVE}",
        f"api_key: {_HEX}",
    ])
    def test_the_round_trip_is_byte_identical(self, line):
        masked = mask_api_key(line)
        assert masked != line
        assert restore_masked_secrets(line, masked) == line
