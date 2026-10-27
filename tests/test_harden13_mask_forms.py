"""tests/test_harden13_mask_forms.py — HARDEN-13 C: the secret forms the mask
still let through.

MASK-KEY-1 hides every credential the request carries. Four shapes reached the
provider untouched: an XML element and an XML attribute pair holding a key
(`<api_key>V</api_key>`, `<add key="api_key" value="V"/>`), a key name followed
by its token by a space instead of `=` or `:` (`apikey V`), a bare JWT with no
key name anywhere near it (`use eyJ….eyJ….SflK… to log in`), and a token written
without a single digit after a colon (`api_key: qLKjHGfdSaZXcVbNmPoiUyTzAbCdEfGh`).

The last one is the gap HARDEN-12 A opened. It taught the colon form to read a
CamelCase class name as code, on the rule that a credential "carries a digit or a
provider prefix" — and every digit-free token that is not one of those providers
went out with it. The fix is not a longer pattern: the run of letters is asked
whether it *reads as a name*, by its humps (a name is words of three or more
letters with at most one short acronym; a token has several one- and two-letter
humps) and, for a word with no case structure at all, by its length.

Prose keeps its keys' *names*: `set the api_key first`, `apikey of the provider`
and `api_key is required` are English and stay as written, and so do all twenty
type names of the HARDEN-12 A table.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.llm_stream import mask_api_key, restore_masked_secrets  # noqa: E402

#: A 32-character token with letters and no digit at all — the shape the
#: HARDEN-12 A rule threw away.
_TOKEN = "qLKjHGfdSaZXcVbNmPoiUyTzAbCdEfGh"
#: A JWT, split over three lines the way every `.env` holds one.
_JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
_MASKED = "here_your_key"


# ── the ticket's table, row by row ──────────────────────────────────────────

class TestTheFormsThatReachedTheProvider:
    @pytest.mark.parametrize("text, secret", [
        (f"<api_key>{_TOKEN}</api_key>", _TOKEN),
        (f"<apiKey>{_TOKEN}</apiKey>", _TOKEN),
        (f"<ApiKey>{_TOKEN}</ApiKey>", _TOKEN),
        (f"<apikey>{_TOKEN}</apikey>", _TOKEN),
        (f"<secret_key>{_TOKEN}</secret_key>", _TOKEN),
        (f"<openai_api_key>{_TOKEN}</openai_api_key>", _TOKEN),
        ('<add key="api_key" value="%s"/>' % _TOKEN, _TOKEN),
        ('<add name="api_key" value="%s"/>' % _TOKEN, _TOKEN),
        ('  <add key="apiKey" value="%s"/>,' % _TOKEN, _TOKEN),
        (f"<api_keys>\n  <key>{_TOKEN}</key>\n</api_keys>", _TOKEN),
        (f"apikey {_TOKEN}", _TOKEN),
        (f"api_key {_TOKEN}", _TOKEN),
        (f"x-api-key {_TOKEN}", _TOKEN),
        (f"API key: {_TOKEN}", _TOKEN),
        (f"use {_JWT} to log in", _JWT),
        (f"token: {_JWT}", _JWT),
        (f"api_key: {_TOKEN}", _TOKEN),
        (f"Authorization: Bearer {_TOKEN}", _TOKEN),
        # the controls: the forms HARDEN-9 and HARDEN-12 already reached
        (f"apikey={_TOKEN}", _TOKEN),
        (f"apikey: {_TOKEN}", _TOKEN),
        (f"X-Api-Key: {_TOKEN}", _TOKEN),
    ])
    def test_the_secret_never_leaves(self, text, secret):
        out = mask_api_key(text)
        assert secret not in out, text
        assert _MASKED in out, text
        assert mask_api_key(out) == out, "a masked value never re-masks"

    def test_a_bare_key_element_counts_only_under_a_key_name(self):
        """`<key>` is a dictionary entry as often as it is a credential, so it is
        masked when the element's line or its parent names an API key."""
        assert mask_api_key(f"<key>{_TOKEN}</key>") == f"<key>{_TOKEN}</key>"
        assert mask_api_key(f"<config>\n  <settings>\n    <key>{_TOKEN}</key>\n"
                            f"  </settings>\n</config>").count(_TOKEN) == 1
        parented = ("<api_keys>\n"
                    f"  <key>{_TOKEN}</key>\n"
                    "</api_keys>")
        assert _TOKEN not in mask_api_key(parented)

    def test_a_quoted_attribute_pair_keeps_its_quotes(self):
        out = mask_api_key('<add key="api_key" value="%s"/>' % _TOKEN)
        assert out == '<add key="api_key" value="here_your_key"/>'

    def test_the_spaced_form_keeps_the_key_names_own_text(self):
        out = mask_api_key(f"X-Api-Key: {_TOKEN}")
        assert out.startswith("X-Api-Key: ") and _MASKED in out

    def test_a_restore_of_every_new_form(self):
        """Each new form is a masked line like any other: a faithful reply gets
        the original text back."""
        for text in (f"<api_key>{_TOKEN}</api_key>",
                     '<add key="api_key" value="%s"/>' % _TOKEN,
                     f"apikey {_TOKEN}",
                     f"api_key: {_TOKEN}",
                     f"use {_JWT} to log in",
                     f"Authorization: Bearer {_TOKEN}"):
            assert restore_masked_secrets(text, mask_api_key(text)) == text, text


# ── what must stay prose, and what must stay code ──────────────────────────

class TestWhatStaysAsWritten:
    @pytest.mark.parametrize("text", [
        "set the api_key first",
        "apikey of the provider",
        "api_key is required",
        "the api key is read here",
        "x-api-key is required",
        # the 20 type names of the HARDEN-12 A table
        "api_key: AsyncOpenAIClientFactory",
        "api_key: ProviderCredentialsHolder",
        "api_key: SecretStr",
        "api_key: Optional[ProviderCredentialsHolder] = None",
        "def __init__(self, api_key: APIKeyProviderFactory, timeout: int = 30):",
        "    api_key: SomeName,",
        "    api_key: str,",
        "api_key: Optional[str] = None",
        "def f(api_key: str):",
        "if self._api_key:",
        "api_key: some_variable",
        "api_key: 123",
        "api_key: Settings",
        "api_key: OpenAIClient",
        "api_key: ClientFactory",
        "api_key: KeyHolder",
        "api_key: ApiKeyType",
        "api_key: TokenStore",
        "api_key: CredentialProvider",
        "api_key: ApiCredentials",
        "api_key: None",
    ])
    def test_prose_and_annotations_stay(self, text):
        assert mask_api_key(text) == text, text

    def test_an_xml_element_holding_a_name_is_not_a_credential(self):
        assert mask_api_key(f"<api_key>ProviderCredentialsHolder</api_key>") == (
            f"<api_key>ProviderCredentialsHolder</api_key>")

    def test_a_key_element_with_no_key_name_around_it_is_not_a_credential(self):
        assert mask_api_key(f"<key>{_TOKEN}</key>") == f"<key>{_TOKEN}</key>"

    def test_a_dotted_path_is_not_a_jwt(self):
        """A bare JWT starts with `eyJ`; a path of words does not, whatever its dots."""
        for text in ("import os.path.here",
                     "self.settings.api_key",
                     "a.b.c"):
            assert mask_api_key(text) == text, text


# ── the mask over this repository's own sources ────────────────────────────

def _code_line_numbers(source: str) -> set:
    """The lines a module actually executes: every statement, minus docstrings and
    minus comments and blanks, which sit inside a statement's range and run nothing."""
    import ast

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
        if not line.strip() or line.lstrip().startswith("#"):
            noise.add(number)
    return executed - docstrings - noise


class TestNoLineOfThisRepositoryIsRewritten:
    """The mask runs over these files on the wire; a code line it rewrote would
    come back through the model and land on disk. Every new form lives or dies
    here: a pattern that rewrote a line of this repository's own code would be a
    `here_your_key` in the next file a task edits."""

    @pytest.mark.parametrize("pattern", ["tools/**/*.py", "scripts/*.py"])
    def test_no_line_of_code_of_this_repository_is_rewritten(self, pattern):
        paths = sorted(Path(PROJECT_ROOT).glob(pattern))
        assert paths, f"nothing to check under {pattern}"
        for path in paths:
            source = path.read_text(encoding="utf-8")
            before = source.splitlines(keepends=True)
            after = mask_api_key(source).splitlines(keepends=True)
            for number in _code_line_numbers(source):
                assert before[number - 1] == after[number - 1], (path, number)

    def test_the_main_entry_point_is_not_rewritten(self):
        path = PROJECT_ROOT / "main.py"
        source = path.read_text(encoding="utf-8")
        before = source.splitlines(keepends=True)
        after = mask_api_key(source).splitlines(keepends=True)
        for number in _code_line_numbers(source):
            assert before[number - 1] == after[number - 1], (path, number)

    def test_an_ini_or_a_doc_line_that_changes_names_a_key(self):
        """An ini and a doc file ARE masked — that is the point — but only on the
        lines that carry a key: a configuration line or a sentence the mask
        rewrote would come back through the model changed."""
        key_name = re.compile(r"(?i)api[ _\-]?key|apikey|secret|token|bearer|eyJ")
        paths = sorted(PROJECT_ROOT.glob("*.ini")) + sorted(PROJECT_ROOT.glob("docs/**/*.md"))
        assert paths, "nothing to check"
        for path in paths:
            text = path.read_text(encoding="utf-8")
            before = text.splitlines()
            after = mask_api_key(text).splitlines()
            for number, (old, new) in enumerate(zip(before, after), start=1):
                assert old == new or key_name.search(new), (path, number)
