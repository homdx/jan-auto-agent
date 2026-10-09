"""tests_bugfix/test_claim_vote_profile_file_mode_217.py — bug 217: `--add-profiles` copies API keys into a world-readable file.

Bug: `add_profiles` copies a provider's REAL key out of Kilo's `auth.json` (a file
Kilo keeps private, mode 0600) into `contest.local.ini` with `Path.write_text`: a new
file gets the mode the umask leaves — 0644 — so every user of the machine could read
the key. `write_profile_keys` of the arena (`mkstemp`, 0600) never did this. A file
this writes is created 0600; a file that already exists keeps its own mode.
"""

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402


@pytest.fixture
def kilo(tmp_path, monkeypatch):
    (tmp_path / "kilo.jsonc").write_text(
        '{"provider": {"p": {"options": {"baseURL": "https://api.invalid/v1"}}}}', encoding="utf-8")
    (tmp_path / "auth.json").write_text('{"p": {"key": "sk-secret-123"}}', encoding="utf-8")
    monkeypatch.setattr(cv, "KILO_CONFIG", tmp_path / "kilo.jsonc")
    monkeypatch.setattr(cv, "KILO_AUTH", tmp_path / "auth.json")
    old = os.umask(0o022)
    yield tmp_path
    os.umask(old)


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_a_new_file_holding_a_key_is_private(kilo, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    assert cv.add_profiles(["p/some-model"], root) == ["claim_vote_llm.p_some_model"]
    ini = root / "contest.local.ini"
    assert "sk-secret-123" in ini.read_text(encoding="utf-8")
    assert _mode(ini) & 0o077 == 0, f"mode is {oct(_mode(ini))}: group and others can read the key"


def test_an_existing_file_keeps_its_mode_and_its_text(kilo, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    ini = root / "contest.local.ini"
    ini.write_text("[arena]\nprofile = p\n", encoding="utf-8")
    os.chmod(ini, 0o640)
    cv.add_profiles(["p/some-model"], root)
    assert _mode(ini) == 0o640
    text = ini.read_text(encoding="utf-8")
    assert text.startswith("[arena]\nprofile = p\n") and "[claim_vote_llm.p_some_model]" in text


def test_a_profile_that_is_already_there_is_left_alone(kilo, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    cv.add_profiles(["p/some-model"], root)
    before = (root / "contest.local.ini").read_text(encoding="utf-8")
    cv.add_profiles(["p/some-model"], root)
    assert (root / "contest.local.ini").read_text(encoding="utf-8") == before
