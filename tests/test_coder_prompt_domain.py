"""tests/test_coder_prompt_domain.py

Locks in the coder's per-mode system prompt selection (DM): the coder now
switches writing persona by task_mode the same way the architect
(_SYSTEM_PROMPTS) and Gate-2 validator (_GATE2_SYSTEMS) already do, while
keeping the identical JSON output contract across modes.
"""

import configparser
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.auto.coder as C


def _coder(mode, coder_section=None):
    cfg = configparser.ConfigParser()
    data = {
        "api": {"active": "local", "verify_ssl": "true"},
        "api_local": {"base_url": "http://x/v1", "api_key": "k", "model": "m",
                      "api_format": "openai"},
        "coder": {"temperature": "0.2", "max_tokens": "4096"},
        "loop": {"timeout_seconds": "300"},
    }
    if coder_section:
        data["coder"].update(coder_section)
    cfg.read_dict(data)
    return C.make_coder(cfg, task_mode=mode)


def test_code_mode_uses_engineer_persona():
    assert "senior software engineer" in _coder("code")._system


def test_docs_mode_uses_writer_persona():
    s = _coder("docs")._system
    assert "senior technical writer" in s
    assert "not code" in s


def test_creative_mode_uses_editor_persona():
    # AUTO-CR-1: creative prompt is now prose-native ("author" not "editor").
    assert "creative writing author" in _coder("creative")._system
    assert "Return ONLY the chapter prose" in _coder("creative")._system


def test_json_contract_preserved_across_modes():
    # The JSON contract must survive for code and docs modes.
    # Creative mode deliberately uses a prose-native protocol (AUTO-CR-1) and
    # does NOT carry the JSON contract.
    for mode in ("code", "docs"):
        s = _coder(mode)._system
        assert '"files"' in s, f"JSON contract missing for mode={mode!r}"
    # Creative: prose-native, no JSON schema.
    creative_s = _coder("creative")._system
    assert '"files"' not in creative_s
    assert "Return ONLY the chapter prose" in creative_s


def test_mode_specific_ini_override_wins():
    s = _coder("docs", {"system_docs": "CUSTOM DOCS PROMPT"})._system
    assert s == "CUSTOM DOCS PROMPT"


def test_legacy_system_key_is_fallback_when_no_mode_key():
    # No system_creative key -> legacy [coder] system applies.
    s = _coder("creative", {"system": "LEGACY"})._system
    assert s == "LEGACY"


def test_unknown_mode_falls_back_to_code():
    assert "senior software engineer" in _coder("nonsense")._system


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
