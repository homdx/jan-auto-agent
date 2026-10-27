"""Review p: what the mask sends and what /edit writes back."""

from __future__ import annotations

import pytest

import tools.actions as A
from tools.llm_stream import _mask_payload_secrets, mask_api_key, restore_masked_secrets

_SECRETS = (
    'HEADERS = {"Authorization": "Bearer sk-or-v1-0123456789abcdef0123456789abcdef"}\n'
    'client = OpenAI(api_key="sk-proj-AbCdEf0123456789xyz")\n'
    'API_KEY = "4f9c2b7e1d3a5c6b"  # prod\n'
)


def _editor(revise):
    """`/edit` with the two model calls stubbed; the edit stub is shown what the wire carries."""
    act = A.OrchestratorActions.__new__(A.OrchestratorActions)
    act.max_iterations, act.timeout_seconds, act.stream_agents = 1, 60, False

    def _edit(instr, label, source, **kw):
        sent = _mask_payload_secrets({"messages": [{"role": "user", "content": source}]})
        return revise(sent["messages"][0]["content"])

    act._edit_file_content = _edit
    act._validate_edit = lambda *a, **k: {"status": "approved"}
    return act


def test_edit_writes_the_real_secret_back_where_the_model_kept_the_masked_line(tmp_path):
    f = tmp_path / "client.py"
    f.write_text(_SECRETS, encoding="utf-8")
    _editor(lambda shown: shown + "# reviewed\n").run_edit("/edit client.py :: add a comment", str(tmp_path))
    assert f.read_text(encoding="utf-8") == _SECRETS + "# reviewed\n"


def test_edit_keeps_a_secret_line_the_model_changed(tmp_path):
    f = tmp_path / "client.py"
    f.write_text(_SECRETS, encoding="utf-8")
    revise = lambda shown: shown.replace('client = OpenAI(api_key="here_your_key")',
                                         'client = OpenAI(api_key=os.environ["KEY"])')
    _editor(revise).run_edit("/edit client.py :: read the key from the environment", str(tmp_path))
    text = f.read_text(encoding="utf-8")
    assert 'client = OpenAI(api_key=os.environ["KEY"])' in text
    assert "sk-or-v1-0123456789abcdef0123456789abcdef" in text and "4f9c2b7e1d3a5c6b" in text
    assert "here_your_key" not in text


@pytest.mark.parametrize("line", [
    '    api_key: str = "0123456789abcdef0123456789abcdef"',
    "  apiKey: string = 'AbCdEf0123456789xyz';",
    'val apiKey: String = "AbCdEf0123456789xyz"',
    '    self.api_key: str = "AbCdEf0123456789xyz"',
    'def connect(host, api_key: str = "0123456789abcdef"):',
])
def test_an_annotated_literal_key_is_masked_and_restored(line):
    masked = mask_api_key(line)
    assert "here_your_key" in masked and masked != line
    assert restore_masked_secrets(line, masked) == line


@pytest.mark.parametrize("line", [
    "api_key: Optional[str] = None",
    'api_key: str = ""',
    '        api_key:    str  = "jan",',
    'def connect(api_key: str = "test-key"):',
    'api_key: str = "<your key>"',
    'api_key: str | None = os.environ.get("OPENAI_API_KEY")',
    'if cfg.api_key: label = "set"',
    "api_key: str",
])
def test_an_annotation_without_a_literal_key_is_code(line):
    assert mask_api_key(line) == line
