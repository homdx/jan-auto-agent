"""tests/test_mask_api_key_forms.py — HARDEN-5 B: the secret mask finds the key
where it sits and does not rewrite code.

Two failures, both reproduced on 1fa8599. The mask's line regex ended in ``$``,
so the value had to be the last thing on the line and the common shapes leaked:
a trailing comment (``api_key = sk-live-…  # production key``), a value inside
prose (``Set api_key = 'sk-live-…' in agents.ini``), and a credential that is
not an ``api_key`` assignment at all (``Authorization: Bearer sk-or-v1-…``).
And the mask still rewrote code: HARDEN-1 A8 kept only the key's exact own name,
so ``self.api_key = None``, ``self.api_key = key`` and
``API_KEY = DEFAULT_API_KEY`` went out as ``here_your_key``, and a quoted
default (``api_key = "x"``) came back unquoted — a faithful model asked to
change ``timeout = 30`` to ``60`` changed three lines, and importing the file
raised ``NameError: name 'here_your_key' is not defined``.
"""

from __future__ import annotations

import configparser
import json
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
)
from tools.auto.coder import Coder  # noqa: E402


_MASKED = "here_your_key"
_LIVE = "sk-live-abcdef1234567890"
# HARDEN-9 A: a token that contains a dot. `.` stood in `_EXPRESSION_CHARS` on the
# strength of `config.api_key`, so these went out to the provider unchanged.
_JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
_GOOGLE = "ya29.a0AfH6SMBx9abcdefghij1234567890"
_HEX = "0123456789abcdef0123456789abcdef"


# ── The ticket's table, row by row ───────────────────────────────────────────


@pytest.mark.parametrize("text, expected", [
    # the leaks the line anchor caused
    (f"api_key = {_LIVE}", f"api_key = {_MASKED}"),
    (f"api_key = {_LIVE}  # production key",
     f"api_key = {_MASKED}  # production key"),
    (f"api_key = {_LIVE} ; production key",
     f"api_key = {_MASKED} ; production key"),
    (f"Set api_key = '{_LIVE}' in agents.ini",
     f"Set api_key = '{_MASKED}' in agents.ini"),
    # a credential that is not an api_key assignment at all
    ("Authorization: Bearer sk-or-v1-ABCDEF1234567890abcdef",
     f"Authorization: Bearer {_MASKED}"),
    # HARDEN-9 A: a token that contains a dot is a literal, not an attribute path
    (f"API_KEY={_JWT}", f"API_KEY={_MASKED}"),
    (f"OPENAI_API_KEY={_GOOGLE}", f"OPENAI_API_KEY={_MASKED}"),
    (f"export API_KEY={_JWT}", f"export API_KEY={_MASKED}"),
    (f";api_key={_JWT}", f";api_key={_MASKED}"),
    (f"api_key = {_JWT}  # production token", f"api_key = {_MASKED}  # production token"),
    # HARDEN-9 B: the unquoted colon form, which the `=` and the JSON rules both miss
    (f"api_key: {_HEX}", f"api_key: {_MASKED}"),
    (f"api_key: {_JWT}", f"api_key: {_MASKED}"),
    (f"  - api_key: {_HEX}", f"  - api_key: {_MASKED}"),
    # code on the right-hand side: sent as it is
    ("self.api_key = api_key", "self.api_key = api_key"),
    ("    token = fetch(api_key=token)", "    token = fetch(api_key=token)"),
    ("self.api_key = None", "self.api_key = None"),
    ("self.api_key = key", "self.api_key = key"),
    ("API_KEY = DEFAULT_API_KEY", "API_KEY = DEFAULT_API_KEY"),
    # a quoted default stays quoted: the masked text is what a faithful model
    # writes back, and an unquoted placeholder is a NameError waiting to happen
    ('api_key = "x"', f'api_key = "{_MASKED}"'),
    ("api_key = 'x'", f"api_key = '{_MASKED}'"),
])
def test_the_ticket_table_row_by_row(text, expected):
    assert mask_api_key(text) == expected


@pytest.mark.parametrize("text, expected", [
    # HARDEN-1 A8's own rows, unchanged
    ("self.api_key = api_key", False),
    ("self.api_key = _API_KEY", False),
    ("api_key = os.environ.get('API_KEY', '')", False),
    ("api_key = config.api_key", False),
    ("api_key = _resolve_key()", False),
    ("api_key = settings['api_key']", False),
    # an unset template value is not a secret and is sent as it is (HARDEN-5 B)
    ("api_key = your_key", False),
    ("api_key = changeme", False),
    ("api_key = <your key>", False),
    ('api_key = "your_key"', False),
    ("api_key = xxx", False),
    # the A8 regression HARDEN-5 B closed: a name that names the key
    ("self.api_key", False),
    ("key", False),
    ("DEFAULT_API_KEY", False),
    ("_API_KEY", False),
    ("apikey", False),
    # language constants and bare numbers are code, not literals
    ("None", False),
    ("True", False),
    ("False", False),
    ("nil", False),
    ("30", False),
    ("3.5", False),
    # a placeholder that does not name the key is still a literal
    ("test_or_key", True),
    ("something", True),
    ("sk-abc123", True),
    ('"k1"', True),
    ("'k1'", True),
    # HARDEN-9 A: a dotted token is a literal, a path of names is code
    (_JWT, True),
    (_GOOGLE, True),
    ("0123456789abcdef.0123456789abcdef", True),
    ("client.v2Endpoint", False),
    # an empty quoted value is a default in code, not a secret
    ('""', False),
    ("''", False),
])
def test_a_literal_is_told_from_code(text, expected):
    assert _value_is_secret_literal(text, "api_key") is expected


class TestTheUnquotedColonForm:
    """HARDEN-9 B: `api_key: <value>` is neither the `=` form nor the JSON form, so it was
    masked only when the value carried a known provider prefix. Now the same rule decides it."""

    @pytest.mark.parametrize("value, expected", [
        ("0123456789abcdef0123456789abcdef", True),
        ("123e4567-e89b-12d3-a456-426614174000", True),
        ("dGhpc2lzYXNlY3JldGtleXZhbHVlMTIzNDU2", True),
        ("AKIAIOSFODNN7EXAMPLE", True),
        ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", True),
        ("str", False),                 # a type annotation
        ("api_key", False),             # a name for the key
        ("some_variable", False),       # a short name
        ("<your key>", False),          # a template
        ("changeme", False),
        ("None", False),
        ("Optional[str]", False),       # an expression
    ])
    def test_the_colon_form_is_a_literal_only_for_a_token(self, value, expected):
        assert _value_is_secret_literal(value, "api_key", colon=True) is expected

    @pytest.mark.parametrize("text", [
        "api_key: <your key>",
        "api_key: changeme",
        "api_key: api_key",
        "api_key: str",
        "api_key: Optional[str] = None",
        "def f(api_key: str):",
        "    api_key: str,",
        "if self._api_key:",
    ])
    def test_a_name_a_type_and_a_signature_after_a_colon_are_code(self, text):
        assert mask_api_key(text) == text

    def test_a_quoted_colon_value_keeps_its_quotes(self):
        assert mask_api_key('api_key: "sk-abcdefghijklmnopqrstuvwxyz0123"') == \
            f'api_key: "{_MASKED}"'
        assert mask_api_key("api_key: 'sk-abcdefghijklmnopqrstuvwxyz0123'") == \
            f"api_key: '{_MASKED}'"

    @pytest.mark.parametrize("text, secret", [
        (f"api_key: {_HEX}", _HEX),
        (f"api_key: {_JWT}", _JWT),
        (f"openai_api_key: {_HEX}", _HEX),
        ("api_key: ghp_abcdefghijklmnopqrstuvwxyz0123456789",
         "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
    ])
    def test_the_colon_form_is_masked_and_never_remasked(self, text, secret):
        out = mask_api_key(text)
        assert secret not in out and _MASKED in out
        assert mask_api_key(out) == out, "a masked value never re-masks"


@pytest.mark.parametrize("text, secret", [
    ("api_key = sk-live-abcdef1234567890  # production key", _LIVE),
    ("api_key = sk-live-abcdef1234567890 ; production key", _LIVE),
    (f"Set api_key = '{_LIVE}' in agents.ini", _LIVE),
    ("Authorization: Bearer sk-or-v1-ABCDEF1234567890abcdef",
     "sk-or-v1-ABCDEF1234567890abcdef"),
    ('api_key = "sk-live-abcdef1234567890"', _LIVE),
    ("api_key = sk-or-v1-ABCDEF1234567890", "sk-or-v1-ABCDEF1234567890"),
    ("api_key = sk-proj-ABCDEF1234567890", "sk-proj-ABCDEF1234567890"),
    ("api_key = AIzaSyABCDEF1234567890abcdef", "AIzaSyABCDEF1234567890abcdef"),
    ("api_key = xoxb-ABCDEF1234567890", "xoxb-ABCDEF1234567890"),
])
def test_every_shape_of_the_table_masks_the_secret(text, secret):
    out = mask_api_key(text)
    assert secret not in out, text
    assert _MASKED in out, text


@pytest.mark.parametrize("text, expected", [
    # the Bearer phrase is English too: a short word is prose, not a credential
    ("bearer of the torch", "bearer of the torch"),
    ("Bearer short", "Bearer short"),
    # a short sk- token is a placeholder in this repo's own sources: it is
    # another name's value, so the api_key assignment regex never reaches it
    ('self.fallback = "sk-real-1"', 'self.fallback = "sk-real-1"'),
    ('self.token = "sk-real-1"', 'self.token = "sk-real-1"'),
])
def test_a_short_token_is_prose_not_a_credential(text, expected):
    assert mask_api_key(text) == expected


class TestTheJsonFormIsUnchanged:
    def test_a_json_key_value_is_masked(self):
        assert mask_api_key('{"api_key": "sk-secret"}') == \
            '{"api_key": "here_your_key"}'

    def test_a_dotted_json_key_is_masked(self):
        assert mask_api_key('"openai_api_key": "k1"') == \
            '"openai_api_key": "here_your_key"'

    def test_a_crlf_ini_line_is_masked(self):
        assert mask_api_key("api_key = sk-secret\r\n") == \
            "api_key = here_your_key\r\n"


class TestAMaskedValueKeepsItsShape:
    def test_a_masked_value_is_idempotent(self):
        for text in [
            "api_key = sk-live-abcdef1234567890  # production key",
            'api_key = "sk-live-abcdef1234567890"',
            f"Set api_key = '{_LIVE}' in agents.ini",
        ]:
            once = mask_api_key(text)
            assert _MASKED in once
            assert mask_api_key(once) == once, "a masked value never re-masks"

    def test_a_masked_quoted_value_is_still_valid_python(self):
        # The whole point: the masked text is what a faithful model hands back,
        # so it must be parseable code, not a bare placeholder.
        masked = mask_api_key("self.api_key = \"sk-live-abcdef1234567890\"")
        assert masked == f'self.api_key = "{_MASKED}"'
        compile("x = 1\n" + masked + "\n", "masked.py", "exec")  # no NameError


# ── The request the provider sees ────────────────────────────────────────────


def _payload_for(text: str) -> dict:
    return {"model": "m", "messages": [
        {"role": "system", "content": "You edit files."},
        {"role": "user", "content": text},
    ]}


class TestTheOutgoingRequest:
    def test_a_real_key_is_not_in_the_request(self):
        source = (
            "class Client:\n"
            "    def __init__(self, api_key):\n"
            "        self.api_key = api_key\n"
            "        self.timeout = 30\n"
            f"        self.fallback = '{_LIVE}'  # production key\n"
        )
        out = _mask_payload_secrets(_payload_for(source))
        body = json.dumps(out)
        assert _LIVE not in body
        assert _MASKED in body

    def test_an_unmasked_payload_is_returned_unchanged(self):
        payload = _payload_for("def f():\n    return 1\n")
        assert _mask_payload_secrets(payload) is payload


# ── End to end: a faithful model on a file that carries a real key ───────────


_CLIENT = (
    "class Client:\n"
    "    def __init__(self, api_key):\n"
    "        self.api_key = api_key\n"
    "        self.timeout = 30\n"
    "        self.fallback = \"sk-real-1\"\n"
    '        self.other = os.environ["OPENAI_API_KEY"]\n'
    '        self.api_key = "{key}"\n'
)


def _coder() -> Coder:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "true"},
        "api_local": {
            "base_url": "http://localhost:9999", "model": "x", "api_key": "",
        },
        "coder": {"temperature": "0.2", "max_tokens": "1024"},
        "loop": {"timeout_seconds": "60"},
    })
    return Coder(config=cfg, base_url="http://localhost:9999", api_key="",
                 model="x", api_format="openai", verify_ssl=True)


class TestFaithfulRewriteOfAMaskedFile:
    """A faithful model changes the one line it was asked for, and nothing else.

    Before HARDEN-5 B the mask turned two lines of this file into
    ``here_your_key`` and stripped the quotes from the third, so a faithful
    edit changed four lines and the file stopped importing.
    """

    def test_nothing_but_the_asked_line_is_masked(self):
        source = _CLIENT.format(key=_LIVE)
        masked = mask_api_key(source)
        assert _LIVE not in masked
        # every other line is byte-identical: the mask touches only the literal
        assert masked.splitlines() == [
            "class Client:",
            "    def __init__(self, api_key):",
            "        self.api_key = api_key",
            "        self.timeout = 30",
            "        self.fallback = \"sk-real-1\"",
            '        self.other = os.environ["OPENAI_API_KEY"]',
            f'        self.api_key = "{_MASKED}"',
        ]

    def test_the_file_comes_back_byte_identical_except_the_asked_line(
            self, tmp_path):
        rel = "client.py"
        source = _CLIENT.format(key=_LIVE)
        (tmp_path / rel).write_bytes(source.encode("utf-8"))

        # what the model is shown, then asked to change timeout 30 -> 60
        shown = mask_api_key(source)
        reply = shown.replace("self.timeout = 30", "self.timeout = 60")
        written, err = _coder()._write_files(
            [{"path": rel, "content": reply}], tmp_path, task_id="T1",
            allowed_paths={rel})
        assert written == [rel] and err == ""

        out = (tmp_path / rel).read_bytes()
        # the only difference from the original is the line the model was asked
        # for: the key was masked on the way out, and the reply that gives the
        # masked line back gets the real one again (HARDEN-5 B)
        assert out == source.encode("utf-8").replace(b"self.timeout = 30", b"self.timeout = 60")
        # and the result still imports: no placeholder left a bare name behind
        compile(out.decode("utf-8"), rel, "exec")

    def test_a_pass_through_file_is_touched_only_where_asked(self, tmp_path):
        # the code rows of the ticket: no secret at all, so the mask is a no-op
        # and a faithful edit changes exactly one line.
        rel = "plain.py"
        source = (
            "class Client:\n"
            "    def __init__(self, api_key=None):\n"
            "        self.api_key = api_key\n"
            "        self.key = key_value\n"
            "        self.timeout = 30\n"
        )
        (tmp_path / rel).write_bytes(source.encode("utf-8"))
        assert mask_api_key(source) == source, "no secret, nothing masked"
        reply = source.replace("self.timeout = 30", "self.timeout = 60")
        written, err = _coder()._write_files(
            [{"path": rel, "content": reply}], tmp_path, task_id="T1",
            allowed_paths={rel})
        assert written == [rel] and err == ""
        assert (tmp_path / rel).read_bytes() == reply.encode("utf-8")
        assert _MASKED not in reply


# ── HARDEN-5 B: a masked line the model gives back is the original line again ─
from tools.llm_stream import restore_masked_secrets  # noqa: E402

_KEYED = 'class C:\n    api_key = "sk-live-real-key-123456"  # deploy\n    timeout = 30\n'


def test_a_line_given_back_masked_is_restored_and_an_edited_line_is_kept():
    masked = mask_api_key(_KEYED)
    assert "sk-live-real-key-123456" not in masked
    # faithful reply: the real key line comes back, the asked line is changed
    assert restore_masked_secrets(_KEYED, masked.replace("= 30", "= 60")) == _KEYED.replace("= 30", "= 60")
    # a line the model rewrote is not a masked line of the file: it stays as written
    edited = masked.replace('"here_your_key"', '"mine"')
    assert restore_masked_secrets(_KEYED, edited) == edited


def test_two_secret_lines_are_restored_in_order_and_crlf_survives():
    original = 'a_key = "sk-aaaaaaaaaaaaaaaa"\r\napi_key = "sk-bbbbbbbbbbbbbbbb"\r\napi_key = "sk-cccccccccccccccc"\r\n'
    assert restore_masked_secrets(original, mask_api_key(original)) == original


def test_nothing_to_restore_returns_the_reply_as_it_is():
    assert restore_masked_secrets("x = 1\n", "x = 2\n") == "x = 2\n"
    assert restore_masked_secrets(None, "x") == "x"
    assert restore_masked_secrets("api_key = None\n", "api_key = None\n") == "api_key = None\n"


def test_the_coder_keeps_the_real_key_on_disk_and_off_the_wire(tmp_path):
    """The whole path: the request is masked as the wire masks it, a faithful reply is written back."""
    import configparser
    import json
    from unittest.mock import patch
    import tools.llm_stream as L
    from tools.auto.coder import Coder

    cfg = configparser.ConfigParser()
    cfg.read_dict({"api": {"active": "local", "verify_ssl": "false"},
                   "api_local": {"base_url": "http://localhost:1/v1", "api_key": "t", "model": "m", "api_format": "openai"},
                   "loop": {"timeout_seconds": "300"}, "coder": {"temperature": "0.2", "max_tokens": "4096"}})
    (tmp_path / "client.py").write_text(_KEYED, encoding="utf-8")
    sent = []

    def model(url, headers, payload, *a, **k):
        prompt = L._mask_payload_secrets(payload)["messages"][1]["content"]
        sent.append(prompt)
        body = prompt.split("### client.py\n", 1)[1].split("\n\n", 1)[0].rstrip("\n") + "\n"
        return json.dumps({"files": [{"path": "client.py", "content": body.replace("= 30", "= 60")}]})

    task = {"id": "M", "title": "t", "instruction": "Make the one change.", "target_files": ["client.py"],
            "acceptance_check": "pytest -q", "status": "in_progress", "round": 0, "attempt": 0,
            "dependencies": [], "cited_locations": []}
    coder = Coder(config=cfg, base_url="http://localhost:1/v1", api_key="t", model="m", api_format="openai", verify_ssl=False)
    with patch("tools.llm_stream.request_completion", side_effect=model):
        coder.generate(task, tmp_path)
    assert "sk-live-real-key-123456" not in "".join(sent)
    assert (tmp_path / "client.py").read_text(encoding="utf-8") == _KEYED.replace("= 30", "= 60")


def test_a_colon_form_reference_to_an_environment_variable_is_not_a_secret():
    """`api_key: $OPENAI_API_KEY` names the variable that holds the key; it is sent as written."""
    from tools.llm_stream import mask_api_key
    for line in ("api_key: $OPENAI_API_KEY", "- api_key: $KEY_FROM_CI", "    openai_api_key: $OPENAI_API_KEY"):
        assert mask_api_key(line) == line


# ── HARDEN-12: a CamelCase type after a colon is code, and more key-line forms ──


class TestAColonFormNameIsNotACredential:
    """Part A: the colon form read a CamelCase class name as a credential, so an indented
    dataclass field went out masked and a faithful edit wrote the placeholder back where an
    annotation named a class."""

    @pytest.mark.parametrize("text", [
        "    api_key: AsyncOpenAIClientFactory,",
        "api_key: ProviderCredentialsHolder",
        "api_key: SecretStr",
        "api_key: Optional[ProviderCredentialsHolder] = None",
        "def __init__(self, api_key: APIKeyProviderFactory, timeout: int = 30):",
    ])
    def test_the_ticket_rows_are_untouched(self, text):
        assert mask_api_key(text) == text

    @pytest.mark.parametrize("text, secret", [
        ("api_key: sk-live-abcdef1234567890", "sk-live-abcdef1234567890"),
        ("api_key: AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("api_key: 0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
    ])
    def test_a_credential_after_a_colon_is_still_masked(self, text, secret):
        out = mask_api_key(text)
        assert secret not in out
        assert _MASKED in out
        assert mask_api_key(out) == out

    def test_the_annotated_file_still_imports(self):
        source = (
            "@dataclass\n"
            "class Settings:\n"
            "    api_key: ProviderCredentialsHolder\n"
            "    live = gsk_abcdefghijklmnopqrstuvwxyz012345\n"
        )
        out = mask_api_key(source)
        assert out.splitlines()[2] == "    api_key: ProviderCredentialsHolder"
        compile(out, "masked.py", "exec")


class TestTheOtherKeyLineForms:
    """Part B: a key line with a quoted name, a subscript on the left, a hash arrow, or an
    XML tag. The same literal test decides what a value is, so a reference stays as written."""

    @pytest.mark.parametrize("text, secret", [
        ("{'api_key': 'xai-abcdefghijklmnopqrstuvwxyz012345'}",
         "xai-abcdefghijklmnopqrstuvwxyz012345"),
        ('os.environ["GROQ_API_KEY"] = "gsk_abcdefghijklmnopqrstuvwxyz012345"',
         "gsk_abcdefghijklmnopqrstuvwxyz012345"),
        ("api_key => 'gsk_abcdefghijklmnopqrstuvwxyz012345'",
         "gsk_abcdefghijklmnopqrstuvwxyz012345"),
        ("<api_key>gsk_abcdefghijklmnopqrstuvwxyz012345</api_key>",
         "gsk_abcdefghijklmnopqrstuvwxyz012345"),
    ])
    def test_the_secret_does_not_leave(self, text, secret):
        out = mask_api_key(text)
        assert secret not in out, text
        assert _MASKED in out, text
        assert mask_api_key(out) == out

    @pytest.mark.parametrize("text", [
        'os.environ["X_API_KEY"] = os.getenv("Y")',
        "os.environ['X_API_KEY'] = None",
        "{'api_key': None}",
        "{'api_key': value}",
        "<api_key>Value</api_key>",
        "api_key: AsyncOpenAIClientFactory",
    ])
    def test_a_reference_or_a_type_is_code(self, text):
        assert mask_api_key(text) == text

    def test_the_quotes_are_kept(self):
        assert mask_api_key("{'api_key': 'xai-abcdefghijklmnopqrstuvwxyz012345'}") == \
            "{'api_key': 'here_your_key'}"
        assert mask_api_key('os.environ["GROQ_API_KEY"] = "gsk_abcdefghijklmnopqrstuvwxyz012345"') == \
            'os.environ["GROQ_API_KEY"] = "here_your_key"'


class TestABareProviderKeyInProse:
    """Part C: a key with no label at all, wherever it sits."""

    @pytest.mark.parametrize("text, secret", [
        ("use gsk_abcdefghijklmnopqrstuvwxyz012345 to log in",
         "gsk_abcdefghijklmnopqrstuvwxyz012345"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz012345", "ghp_abcdefghijklmnopqrstuvwxyz012345"),
        ("hf_abcdefghijklmnopqrstuvwxyz012345 here", "hf_abcdefghijklmnopqrstuvwxyz012345"),
        ("xai-abcdefghijklmnopqrstuvwxyz012345", "xai-abcdefghijklmnopqrstuvwxyz012345"),
        ("glpat-abcdefghijklmnopqrstuvwxyz1234", "glpat-abcdefghijklmnopqrstuvwxyz1234"),
        ("key AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ])
    def test_a_bare_key_is_masked(self, text, secret):
        out = mask_api_key(text)
        assert secret not in out
        assert _MASKED in out
        assert mask_api_key(out) == out

    @pytest.mark.parametrize("text", [
        "hf_model = 3",
        "gsk_ alone",
        "AKIA in a sentence",
        'self.fallback = "sk-real-1"',
    ])
    def test_a_short_word_that_starts_the_same_way_is_prose(self, text):
        assert mask_api_key(text) == text


def test_a_name_that_is_a_count_or_a_path_holds_no_credential():
    """`os.environ["TOKEN_COUNT"] = "5"` and `KEY_DIR=/tmp/keys` mention a credential word but name a count and a path."""
    from tools.llm_stream import _name_is_not_a_credential, mask_api_key
    for name in ("TOKEN_COUNT", "KEY_DIR", "API_KEY_NAME", "SECRET_FILE", "PASSWORD_LENGTH"):
        assert _name_is_not_a_credential(name)
    for name in ("API_KEY", "HF_TOKEN", "DB_PASSWORD", "OPENAI_API_KEY"):
        assert not _name_is_not_a_credential(name)
    assert mask_api_key("os.environ['TOKEN_COUNT'] = '5'") == "os.environ['TOKEN_COUNT'] = '5'"
    assert "here_your_key" in mask_api_key("os.environ['HF_TOKEN'] = 'hf_abcdefghijklmnopqrstuvwxyz0123'")


def test_a_backtick_value_on_the_colon_form_is_markdown_not_a_key():
    """A comment quoting a header, `x-api-key: v`, is prose and is sent as written."""
    from tools.llm_stream import mask_api_key
    for line in ("# the header `x-api-key: v`, a token", "(``x-api-key:``), the", "`X-API-KEY: v`, `x-token: v`."):
        assert mask_api_key(line) == line


def test_an_elision_is_not_a_key():
    """`api_key=…,` in a docstring is shorthand for an argument, not a credential."""
    from tools.llm_stream import mask_api_key
    for line in ("make(config, base_url=…, api_key=…, model=…,", "api_key=…", "api_key: …", "api_key: ...", "api_key = ..."):
        assert mask_api_key(line) == line
