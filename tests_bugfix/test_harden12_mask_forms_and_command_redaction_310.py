"""tests_bugfix/test_harden12_mask_forms_and_command_redaction_310.py — HARDEN-12: four
leaks of the same two helpers.

`mask_api_key` runs on the wire, on every request, so every line it touches and every
form it misses matters. Four defects, reproduced on 5dfd4d5:

* A: the colon-form mask read a CamelCase class name as a credential, so an indented
  dataclass field went out masked and a faithful edit wrote the placeholder back where an
  annotation named a class — the module then failed to import and nothing reported it.
* B: a key name in single quotes (`{'api_key': 'gsk_…'}`), `os.environ["GROQ_API_KEY"] =
  "gsk_…"`, a Ruby hash (`api_key => 'gsk_…'`) and an XML element (`<api_key>gsk_…`)
  were sent to the provider as they stood.
* C: a bare Groq, GitHub, Hugging Face, xAI, GitLab or AWS key in prose, a comment or a
  log line was not masked at all.
* D: the executor's command log kept a key passed as `NAME=value`, as an `x-api-key:`
  header, as `user:pass@` in a URL, or as `-u user:pass` / `-p value`.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.llm_stream import (  # noqa: E402
    _mask_payload_secrets,
    _value_is_secret_literal,
    mask_api_key,
    restore_masked_secrets,
)
from tools.auto.executor import _redact_command  # noqa: E402


_MASKED = "here_your_key"
_GROQ = "gsk_abcdefghijklmnopqrstuvwxyz012345"
_HF = "hf_abcdefghijklmnopqrstuvwxyz012345"
_XAI = "xai-abcdefghijklmnopqrstuvwxyz012345"
_HEX = "0123456789abcdef0123456789abcdef"
_ARROW = "=>"  # the Ruby and PHP hash arrow


# ── Part A: a bare identifier after a colon is a type, not a credential ──────────


class TestAColonNameIsNotACredential:
    """The ticket's table: the type-annotation lines stay, the credentials go."""

    @pytest.mark.parametrize("text", [
        "    api_key: AsyncOpenAIClientFactory,",
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
    ])
    def test_a_type_a_variable_and_a_signature_are_code(self, text):
        assert mask_api_key(text) == text

    @pytest.mark.parametrize("text, secret", [
        ("api_key: sk-live-abcdef1234567890", "sk-live-abcdef1234567890"),
        ("api_key: AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("api_key: ghp_abcdefghijklmnopqrstuvwxyz0123456789",
         "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
        (f"api_key: {_HEX}", _HEX),
    ])
    def test_a_credential_still_masks(self, text, secret):
        out = mask_api_key(text)
        assert secret not in out, text
        assert _MASKED in out, text
        assert mask_api_key(out) == out, "a masked value never re-masks"

    def test_the_annotation_survives_a_masked_file(self):
        """The shape that broke: an indented field the mask rewrote into a NameError."""
        source = (
            "@dataclass\n"
            "class Settings:\n"
            "    api_key: ProviderCredentialsHolder\n"
            f"    live_key = {_GROQ}\n"
        )
        out = mask_api_key(source)
        assert "    api_key: ProviderCredentialsHolder\n" in out
        assert _GROQ not in out
        compile(out, "masked.py", "exec")   # the masked file still imports

    def test_a_dataclass_field_per_camelcase_name_in_tools_is_untouched(self):
        """Every CamelCase name this repository has, used as the annotation of an api_key
        field, is left as written."""
        names = set()
        for path in sorted(Path("tools").rglob("*.py")):
            for match in re.finditer(r"\b([A-Z][A-Za-z]{3,})\b", path.read_text(encoding="utf-8")):
                names.add(match.group(1))
        assert names, "no CamelCase name found in tools/"
        for name in sorted(names):
            line = f"    api_key: {name}"
            assert mask_api_key(line) == line, name

    @pytest.mark.parametrize("value, expected", [
        ("AsyncOpenAIClientFactory", False),
        ("ProviderCredentialsHolder", False),
        ("SecretStr", False),
        ("SomeVeryLongTypeNameThatIsStillOnlyLetters", False),
        ("client_factory", False),
        ("str", False),
        ("api_key", False),
        ("None", False),
        ("AKIAIOSFODNN7EXAMPLE", True),
        ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", True),
        ("0123456789abcdef0123456789abcdef", True),
    ])
    def test_the_colon_literal_test_is_the_shape_of_a_credential(self, value, expected):
        assert _value_is_secret_literal(value, "api_key", colon=True) is expected


# ── Part B: more forms of a key line reach the provider ──────────────────────────


class TestMoreFormsofAKeyLine:
    @pytest.mark.parametrize("text, secret", [
        (f"{{'api_key': '{_XAI}'}}", _XAI),
        (f"{{'api_key': 'xai-abcdefghijklmnopqrstuvwxyz012345'}}", _XAI),
        (f'os.environ["GROQ_API_KEY"] = "{_GROQ}"', _GROQ),
        (f"os.environ['HF_TOKEN'] = '{_HF}'", _HF),
        (f"api_key {_ARROW} '{_GROQ}'", _GROQ),
        (f"<api_key>{_GROQ}</api_key>", _GROQ),
        (f"<openai_api_key>{_GROQ}</openai_api_key>", _GROQ),
        (f"<api_key>'{_GROQ}'</api_key>", _GROQ),
        (f'  {{"api_key": "{_GROQ}"}}', _GROQ),
        (f'os.environ["X_API_KEY"] = "{_HEX}"', _HEX),
    ])
    def test_the_new_forms_are_masked(self, text, secret):
        out = mask_api_key(text)
        assert secret not in out, text
        assert _MASKED in out, text
        assert mask_api_key(out) == out, "a masked value never re-masks"

    def test_the_quotes_are_kept(self):
        assert mask_api_key(f"{{'api_key': '{_XAI}'}}") == f"{{'api_key': '{_MASKED}'}}"
        assert (mask_api_key(f'os.environ["GROQ_API_KEY"] = "{_GROQ}"')
                == f'os.environ["GROQ_API_KEY"] = "{_MASKED}"')
        assert (mask_api_key(f"os.environ['HF_TOKEN'] = '{_HF}'")
                == f"os.environ['HF_TOKEN'] = '{_MASKED}'")

    @pytest.mark.parametrize("text", [
        'os.environ["X_API_KEY"] = os.getenv("Y")',
        "os.environ['X_API_KEY'] = None",
        "os.environ['X_API_KEY'] = value",
        "os.environ['X_TOKEN'] = value or None",
        "os.environ['HOME'] = '/root'",
        "os.environ['USER'] = 'nobody'",
        "{'api_key': None}",
        "{'api_key': value}",
        "{'api_key': api_key}",
        f"<api_key>ProviderCredentialsHolder</api_key>",
        f"<api_key>{_MASKED}</api_key>",
        "api_key >= value",
        "api_key >= 'a_short_name'",
        "assert client.api_key == key",
        "api_key: AsyncOpenAIClientFactory",
    ])
    def test_the_controls_stay_as_written(self, text):
        assert mask_api_key(text) == text

    def test_the_hash_arrow_keeps_its_shape(self):
        """A bare `sk-` key is masked wherever it sits; the arrow is not rewritten.

        The arrow is two characters, and either order of them must come back as it went
        out: `=>` is the Ruby hash, `>=` is a Python comparison, and neither is an
        assignment.
        """
        for separator in (_ARROW, ">="):
            line = f"api_key {separator} 'sk-abcdefghijklmnopqrstuvwxyz0123'"
            out = mask_api_key(line)
            assert out == f"api_key {separator} '{_MASKED}'", out
            assert out.count(separator) == 1

    def test_a_file_of_the_new_forms_masks_and_restores(self):
        """The four forms in one file, and the same file back again."""
        source = (
            "class Client:\n"
            f"    api_key = '{_MASKED}'\n"
            "    creds = {'api_key': '" + _XAI + "'}\n"
            f'    other = os.environ["GROQ_API_KEY"]\n'
            f"    api_key {_ARROW} '{_GROQ}'\n"
            f"    tag = '<api_key>{_HF}</api_key>'\n"
            "    timeout = 30\n"
        )
        masked = mask_api_key(source)
        for secret in (_GROQ, _HF, _XAI):
            assert secret not in masked, secret
        assert masked.count(_MASKED) == 4
        # the model hands the masked file back: it is the real one again
        assert restore_masked_secrets(source, masked) == source

    def test_the_provider_never_sees_a_new_form(self):
        payload = {
            "model": "m",
            "messages": [
                {"role": "user", "content":
                 f"{{'api_key': '{_XAI}'}}\n"
                 f'os.environ["GROQ_API_KEY"] = "{_GROQ}"\n'
                 f"<api_key>{_HF}</api_key>\n"},
            ],
        }
        out = _mask_payload_secrets(payload)
        body = json.dumps(out)
        for secret in (_GROQ, _HF, _XAI):
            assert secret not in body, secret
        assert _MASKED in body


# ── Part C: a bare key of a common provider, with no label at all ────────────────


class TestABareProviderKeyAnywhere:
    @pytest.mark.parametrize("text, secret", [
        (f"use {_GROQ} to log in", _GROQ),
        (f"token ghp_abcdefghijklmnopqrstuvwxyz012345", "ghp_abcdefghijklmnopqrstuvwxyz012345"),
        (f"gho_abcdefghijklmnopqrstuvwxyz012345", "gho_abcdefghijklmnopqrstuvwxyz012345"),
        ("github_pat_11ABCDabcd1234567890abcdefghijklmnopqrstuvwxyz0123456789012345678",
         "github_pat_11ABCDabcd1234567890abcdefghijklmnopqrstuvwxyz0123456789012345678"),
        (f"{_HF} here", _HF),
        (f"{_XAI}", _XAI),
        ("glpat-abcdefghijklmnopqrstuvwxyz1234", "glpat-abcdefghijklmnopqrstuvwxyz1234"),
        ("key AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("ASIAIOSFODNN7EXAMPLE", "ASIAIOSFODNN7EXAMPLE"),
        # the shapes already known stay
        ("sk-or-v1-ABCDEF1234567890abcdef", "sk-or-v1-ABCDEF1234567890abcdef"),
        ("AIzaSyABCDEF1234567890abcdef", "AIzaSyABCDEF1234567890abcdef"),
        ("xoxb-ABCDEF1234567890", "xoxb-ABCDEF1234567890"),
    ])
    def test_a_bare_key_is_masked_wherever_it_sits(self, text, secret):
        out = mask_api_key(text)
        assert secret not in out, text
        assert _MASKED in out, text
        assert mask_api_key(out) == out, "a masked value never re-masks"

    @pytest.mark.parametrize("text", [
        "hf_model = 3",
        "gsk_ alone",
        "AKIA in a sentence",
        "ASIA in a sentence",
        "ghp_ and hf_",
        "hf_module_name_is_here",
        "xai-config",
        "glpat-short",
        'self.fallback = "sk-real-1"',
        'self.token = "sk-real-1"',
        "Bearer short",
        "bearer of the torch",
    ])
    def test_a_short_word_that_merely_starts_the_same_way_is_prose(self, text):
        assert mask_api_key(text) == text

    def test_no_line_of_code_of_this_repository_is_rewritten(self):
        """The mask runs over these files on the wire; a code line it rewrote would come
        back through the model and land on disk."""
        paths = sorted(Path("tools").rglob("*.py")) + [Path("main.py")]
        assert paths, "nothing to check"
        for path in paths:
            source = path.read_text(encoding="utf-8")
            before = source.splitlines(keepends=True)
            after = mask_api_key(source).splitlines(keepends=True)
            for number in _code_line_numbers(source):
                assert before[number - 1] == after[number - 1], (path, number)


def _code_line_numbers(source: str) -> set:
    """The lines a module actually executes: every statement, minus docstrings and minus
    comments and blanks, which sit inside a statement's range and run nothing."""
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


# ── Part D: the executor's command log keeps inline secrets ──────────────────────


class TestTheCommandLogRedacts:
    @pytest.mark.parametrize("command, expected", [
        ("OPENAI_API_KEY=sk123abc4567890 pytest",
         "OPENAI_API_KEY=[REDACTED] pytest"),
        ("API_KEY=abcd1234efgh python run.py",
         "API_KEY=[REDACTED] python run.py"),
        (f"HF_TOKEN={_HF} python x.py", "HF_TOKEN=[REDACTED] python x.py"),
        ("API_KEY=\"a secret value\" pytest -q", "API_KEY=[REDACTED] pytest -q"),
        ("SECRET_PASSWD=abcd1234efgh python x.py", "SECRET_PASSWD=[REDACTED] python x.py"),
        ("export API_KEY=abcd1234efgh pytest -q", "export API_KEY=[REDACTED] pytest -q"),
        ("API_KEY=abcd1234efgh mysql -psecret1234 && pytest -q",
         "API_KEY=[REDACTED] mysql -p[REDACTED] && pytest -q"),
        # not the first assignment, but a credential name anyway: redacted too
        ("FOO=bar OPENAI_API_KEY=sk123 pytest -q",
         "FOO=bar OPENAI_API_KEY=[REDACTED] pytest -q"),
        ("curl -H 'x-api-key: abcd1234efgh5678' http://x",
         "curl -H 'x-api-key: [REDACTED]' http://x"),
        ('curl -H "X-API-KEY: abcd1234efgh5678" http://x',
         'curl -H "X-API-KEY: [REDACTED]" http://x'),
        ("curl -H 'x-token: abcd1234efgh5678' http://x",
         "curl -H 'x-token: [REDACTED]' http://x"),
        ("Authorization: Bearer sk-or-v1-ABCDEF1234567890abcdef",
         "Authorization: [REDACTED]"),
        ("curl -u user:secretpass1234 http://x",
         "curl -u user:[REDACTED] http://x"),
        ("docker login -p secretpass1234", "docker login -p [REDACTED]"),
        ("mysql -psecretpass1234", "mysql -p[REDACTED]"),
        ("mysql -p secretpass1234 db", "mysql -p [REDACTED] db"),
        ("git clone https://user:tokenabc12345@host/r.git",
         "git clone https://user:[REDACTED]@host/r.git"),
        ("pytest --api-key=abc12345", "pytest --api-key=[REDACTED]"),
    ])
    def test_the_secret_is_out_of_the_log(self, command, expected):
        assert _redact_command(command) == expected

    @pytest.mark.parametrize("command", [
        "ls -p",
        "grep -p pat file.txt",
        "pytest -p no:cacheprovider",
        "docker run -p 8080:80 nginx",
        "curl -u someflag http://x",
        "mysql -p db",
        "mysql -p5432",
        "git clone https://host/r.git",
        "git clone https://host/path?query=value",
        "FOO=bar pytest -q",
        "grep foo=bar file.txt",
        "curl -H 'Content-Type: application/json' http://x",
        "curl -H 'Accept: application/json' http://x",
        "ls -la /tmp",
        "pytest -q tests/",
        "python -c 'x = 1'",
        "git@github.com:org/repo.git",
        "env PATH=/usr/bin pytest -q",
    ])
    def test_a_command_without_a_secret_is_byte_for_byte(self, command):
        assert _redact_command(command) == command

    def test_no_secret_survives_any_form(self):
        for command in (
            "OPENAI_API_KEY=sk123abc4567890 pytest",
            "curl -H 'x-api-key: abcd1234efgh5678' http://x",
            "curl -u user:secretpass1234 http://x",
            "docker login -p secretpass1234",
            "mysql -psecretpass1234",
            "git clone https://user:tokenabc12345@host/r.git",
        ):
            out = _redact_command(command)
            assert "[REDACTED]" in out, command
            for word in ("sk123abc4567890", "abcd1234efgh5678", "secretpass1234",
                         "tokenabc12345"):
                assert word not in out, command

    def test_redaction_never_raises_and_never_changes_the_run(self):
        assert _redact_command("") == ""
        for command in (";&&|", "|", "&&", "A=B", "python -c", "-p", "-u"):
            assert isinstance(_redact_command(command), str)
