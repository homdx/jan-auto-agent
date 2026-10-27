"""tests_bugfix/test_harden1_a8_mask_only_literals.py — HARDEN-1 A8: the secret
mask rewrites only a literal value.

Fix 34 widened ``mask_api_key`` so that ANY assignment whose name contains
``api_key`` was rewritten to ``api_key = here_your_key`` — and the text it masks
is the file the model is asked to rewrite **in full** (``/edit``, and the Coder,
which read and writes whole files through the same request path). The model
gives the placeholder back, the file is written, and working code becomes a
``NameError`` nothing catches: a faithful edit asked to change one line changed
three.

Only a *literal* is masked now — a quoted string (its quotes are kept,
HARDEN-5 B: an unquoted placeholder is a ``NameError`` waiting to happen), or a
bare token with no whitespace and no call/index/attribute/punctuation characters
that does not name the key it is assigned to. An expression on the right-hand
side is sent as it is, which still keeps the real secret out of the request: a
literal key is the literal case.
"""

from __future__ import annotations

import configparser
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.llm_stream import mask_api_key  # noqa: E402


class TestTheFiveRowsOfTheTicket:
    """Only the literal rows are masked; the expression rows are sent as they are."""

    def test_a_pass_through_assignment_is_sent_as_it_is(self):
        assert mask_api_key("self.api_key = api_key") == "self.api_key = api_key"

    def test_an_env_lookup_is_sent_as_it_is(self):
        text = 'OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]'
        assert mask_api_key(text) == text

    def test_a_json_literal_is_masked(self):
        assert mask_api_key('"api_key": "k1"') == '"api_key": "here_your_key"'

    def test_a_bare_key_token_is_masked(self):
        assert mask_api_key("api_key = sk-abc123") == "api_key = here_your_key"

    def test_unrelated_code_is_untouched(self):
        assert mask_api_key("x = 1") == "x = 1"


class TestALiteralIsMaskedWhateverItsShape:
    @pytest.mark.parametrize("text", [
        "api_key = test_or_key",
        "API_KEY = something",
        "    api_key = indented_secret",
        "### api_key = mykey1",
        "# api_key = secret",
        "; api_key = otherkey",
        'api_key = "k1"',
        "api_key = 'k1'",
        'export OPENAI_API_KEY=sk-secret\n',
        "api_key = sk-real-secret-999",
    ])
    def test_a_literal_is_masked(self, text):
        out = mask_api_key(text)
        assert "here_your_key" in out
        assert mask_api_key(out) == out, "masking is idempotent"

    def test_a_masked_value_never_survives_a_second_pass(self):
        once = mask_api_key("api_key = sk-live-AbC123XyZ9")
        assert "sk-live-AbC123XyZ9" not in once

    def test_a_crlf_ini_line_is_masked(self):
        assert mask_api_key("api_key = sk-secret\r\n") == "api_key = here_your_key\r\n"

    def test_a_json_block_line_is_masked(self):
        assert mask_api_key('{"api_key": "sk-secret"}') == '{"api_key": "here_your_key"}'


class TestCodeOnTheRightHandSideIsSentAsItIs:
    @pytest.mark.parametrize("text", [
        "self.api_key = api_key",
        "self.api_key = _API_KEY",
        "self.api_key = self.api_key or ''",
        'OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]',
        "api_key = os.environ.get('API_KEY', '')",
        "api_key = config.api_key",
        "api_key = _resolve_key()",
        "api_key = settings['api_key']",
        "not_api_key_at_all = foo",
        "the api_key is read here",
    ])
    def test_an_expression_is_not_masked(self, text):
        assert mask_api_key(text) == text

    def test_a_code_file_holding_both_is_masked_only_on_the_literal(self):
        source = (
            "class Client:\n"
            "    def __init__(self, api_key):\n"
            "        self.api_key = api_key\n"
            "        assert self.api_key\n"
        )
        assert mask_api_key(source) == source


class TestAMixedFileKeepsItsShape:
    def test_only_the_literal_line_changes(self):
        source = (
            "class Client:\n"
            "    def __init__(self, api_key):\n"
            "        self.api_key = api_key\n"
            "        self._timeout = 30\n"
            "        self.fallback = \"sk-real-1\"\n"
            '        self.api_key = "sk-real-2"\n'
            '        self.other = os.environ["OPENAI_API_KEY"]\n'
        )
        out = mask_api_key(source).splitlines()
        assert out[2] == "        self.api_key = api_key"          # sent verbatim
        assert out[3] == "        self._timeout = 30"
        assert out[4] == '        self.fallback = "sk-real-1"'     # a different name
        # HARDEN-5 B: a masked quoted value keeps its quotes. Stripping them
        # sent `self.api_key = here_your_key` to a faithful model, which wrote
        # the placeholder back and turned the file into a NameError.
        assert out[5] == '        self.api_key = "here_your_key"'   # a literal key
        assert out[6] == '        self.other = os.environ["OPENAI_API_KEY"]'


class TestTheContractIsUnchanged:
    @pytest.mark.parametrize("text", ["", None])
    def test_empty_and_none_safe(self, text):
        assert mask_api_key(text) == text

    def test_text_without_such_a_line_is_returned_unchanged(self):
        text = "base_url = http://x\nmodel = m\n"
        assert mask_api_key(text) == text


class TestAFaithfulEditRoundTrip:
    """The end-to-end shape the ticket reproduced: a file the model is asked to
    rewrite in full.

    The old mask sent ``self.api_key = here_your_key``, a faithful model gave
    the placeholder back, and the file on disk came out with three lines
    changed instead of one — working code turned into a ``NameError`` nothing
    catches. A pass-through assignment is sent verbatim now, so the round trip
    changes exactly the line asked for, while a literal key on the same line
    still never reaches the request.
    """

    def _faithful_edit(self, tmp_path: Path, source: str, change: str, changed_line: int):
        """Drive one Coder call with a model that echoes the file back verbatim
        except for *change*; return ``(after, sent)``."""
        from unittest.mock import patch

        from tools.auto.coder import Coder

        base_dir = tmp_path / "repo"
        base_dir.mkdir()
        (base_dir / "client.py").write_text(source, encoding="utf-8")

        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "api": {"active": "local", "verify_ssl": "false"},
            "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "test",
                          "model": "test-model", "api_format": "openai"},
            "loop": {"timeout_seconds": "300"},
            "coder": {"temperature": "0.2", "max_tokens": "4096"},
        })
        coder = Coder(config=cfg, base_url="http://localhost:1337/v1",
                      api_key="test", model="test-model")
        task = {"id": "MASK-T1", "title": "one line", "instruction": "Make the one change.",
                "target_files": ["client.py"], "acceptance_check": "pytest -q",
                "status": "in_progress", "round": 0, "attempt": 0, "dependencies": [],
                "cited_locations": []}

        sent = []

        def _model(url, headers, payload, *args, **kwargs):
            prompt = payload["messages"][1]["content"]
            sent.append(prompt)
            body = prompt.split("### client.py\n", 1)[1]
            body = body.split("\n### ", 1)[0].rstrip("\n") + "\n"
            return json.dumps({"files": [{"path": "client.py",
                                          "content": body.replace(change[0], change[1])}]})

        with patch("tools.llm_stream.request_completion", side_effect=_model):
            coder.generate(task, base_dir)
        after = (base_dir / "client.py").read_text(encoding="utf-8")
        before_lines, after_lines = source.splitlines(), after.splitlines()
        changed = [i for i, (b, a) in enumerate(zip(before_lines, after_lines)) if b != a]
        return after, "".join(sent), changed

    PASS_THROUGH = (
        "class Client:\n"
        "    def __init__(self, api_key):\n"
        "        self.api_key = api_key\n"
        "        self.timeout = 30\n"
        '        self.fallback = os.environ["OPENAI_API_KEY"]\n'
    )
    LITERAL_KEY = (
        "class Client:\n"
        "    def __init__(self, api_key):\n"
        "        self.api_key = api_key\n"
        '        self.api_key = "sk-live-real-key"\n'
        "        self.timeout = 30\n"
    )

    def test_only_the_line_asked_for_changes(self, tmp_path):
        after, sent, changed = self._faithful_edit(
            tmp_path, self.PASS_THROUGH, ("self.timeout = 30", "self.timeout = 60"), 3)
        assert changed == [3], f"lines changed: {changed}: {after!r}"
        assert "self.api_key = api_key" in after
        assert "here_your_key" not in after
        assert 'os.environ["OPENAI_API_KEY"]' in after

    def test_the_pass_through_line_is_sent_verbatim(self, tmp_path):
        _after, sent, _changed = self._faithful_edit(
            tmp_path, self.PASS_THROUGH, ("self.timeout = 30", "self.timeout = 60"), 3)
        assert "self.api_key = api_key" in sent
        assert "here_your_key" not in sent

    def test_a_real_key_in_the_same_file_is_not_in_the_request(self, tmp_path):
        after, sent, changed = self._faithful_edit(
            tmp_path, self.LITERAL_KEY, ("self.timeout = 30", "self.timeout = 60"), 4)
        assert "sk-live-real-key" not in sent          # the literal key never reaches the model
        assert "here_your_key" in sent
        assert "self.api_key = api_key" in sent        # the pass-through line does
        # only the instructed line differs on disk: the key line the model gave back
        # masked is the real line again (HARDEN-5 B), not the placeholder
        assert changed == [4], f"lines changed: {changed}: {after!r}"
        assert "sk-live-real-key" in after
