"""Bug 22 (round 203): --add-profiles copied API keys into a world-readable file.

claim_vote.add_profiles copies a provider's real key out of Kilo's auth.json (which
Kilo itself keeps private) into contest.local.ini with ``Path.write_text`` -- which
gives the file the umask's mode, ``0644`` under a ``022`` umask: every other user of
the machine could read the key. The arena's own write_profile_keys (mkstemp, 0600)
never did this; the runbook says the key is "copied, never printed".

Fix: a file that does not exist yet is created with ``os.open(..., 0o600)``, and a
file that does exist keeps the mode it already has (an operator may have set 0400,
or may have deliberately widened it, and the script must not silently narrow or
widen it).

Ticket 203; probed on arena @ 00355fd.
"""
from __future__ import annotations

import contextlib
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402
from tools.auto.llm_profile import LlmSettings  # noqa: E402


REFS = ["sensenova123/sensenova-6.8-flash-lite", "kenary/agnes-3-0-flash:free"]
INI = "contest.local.ini"


@contextlib.contextmanager
def umask(mask: int):
    """Make the mode arithmetic of the test independent of the caller's umask."""
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


@pytest.fixture
def stub_kilo(monkeypatch):
    def fake(ref: str) -> LlmSettings:
        return LlmSettings(base_url=f"https://example.invalid/{ref.split('/')[0]}/v1",
                           api_key=f"key-{ref}", model=ref.split("/")[-1],
                           api_format="openai", temperature=0.3, max_tokens=4000)
    monkeypatch.setattr(cv, "_kilo_settings", fake)
    return fake


def test_a_new_local_ini_carrying_keys_is_not_world_readable(stub_kilo, tmp_path):
    with umask(0o022):      # the mode Path.write_text would have given the file
        names = cv.add_profiles(REFS, tmp_path)
    path = tmp_path / INI
    assert path.is_file()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"{path} is {oct(mode)}: the keys inside are readable by other users"
    assert mode & 0o077 == 0
    assert names == ["claim_vote_llm.sensenova123_sensenova_6_8_flash_lite",
                     "claim_vote_llm.kenary_agnes_3_0_flash_free"]
    text = path.read_text(encoding="utf-8")
    assert "key-sensenova123/sensenova-6.8-flash-lite" in text
    assert "api_key" in text and "[claim_vote_llm" in text


def test_the_private_mode_holds_under_a_stricter_umask_too(stub_kilo, tmp_path):
    with umask(0o077):
        cv.add_profiles(REFS, tmp_path)
    assert stat.S_IMODE((tmp_path / INI).stat().st_mode) == 0o600


def test_an_existing_file_keeps_its_own_mode(stub_kilo, tmp_path):
    path = tmp_path / INI
    path.write_text("[contest]\nkeep = me\n", encoding="utf-8")
    os.chmod(path, 0o640)
    with umask(0o022):
        cv.add_profiles(REFS, tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    text = path.read_text(encoding="utf-8")
    assert "[contest]\nkeep = me" in text       # the old content is appended to, not replaced
    assert "key-kenary/agnes-3-0-flash:free" in text


def test_a_section_that_is_already_there_is_not_written_twice(stub_kilo, tmp_path):
    path = tmp_path / INI
    path.write_text("[claim_vote_llm.kenary_agnes_3_0_flash_free]\nbase_url = https://old\n",
                    encoding="utf-8")
    os.chmod(path, 0o600)
    with umask(0o022):
        names = cv.add_profiles(REFS, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert names == ["claim_vote_llm.sensenova123_sensenova_6_8_flash_lite",
                     "claim_vote_llm.kenary_agnes_3_0_flash_free"]
    assert text.count("[claim_vote_llm.kenary_agnes_3_0_flash_free]") == 1
    assert "https://old" in text                # the operator's own values are kept


def test_no_refs_to_add_writes_no_file_at_all(stub_kilo, tmp_path):
    path = tmp_path / INI
    path.write_text("[contest]\nkeep = me\n", encoding="utf-8")
    os.chmod(path, 0o644)
    with umask(0o022):
        cv.add_profiles([], tmp_path)
    assert path.read_text(encoding="utf-8") == "[contest]\nkeep = me\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
