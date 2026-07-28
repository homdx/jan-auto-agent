"""tests/test_prompt_store_corrupt.py — an unusable prompts.json must not
silently destroy every other agent's prompt history.

_load() used to treat an unreadable file as "empty", and push() always
calls _save() at the end of every call — so the VERY NEXT push, for ANY
agent, overwrote the whole file with just that one agent's single new
entry, silently destroying every other agent's entire rollback history:

    BEFORE: real history for 2 agents, 3 total versions on disk
    AFTER one push() on top of a corrupt file:
      {"theme_validator": {"stack": [{"version": 1, ...}]}}
      architect's history: gone
      coder's history: gone

No exception was raised; only a log.error line, easily missed. Unlike
progress.json (tools/auto/state.py), prompt version history is NOT
derivable from anywhere else, so the fix quarantines the unusable file
(same pattern as TicketStore.get()) rather than silently rebuilding.

_load also only guarded JSONDecodeError/IOError, so a file that PARSES but
holds the wrong shape (a list, a string, null) crashed later, on whatever
line first indexed into it as a dict.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.prompt_store import PromptStore


def _seeded_store(tmp_path: Path) -> Path:
    sp = tmp_path / "prompts.json"
    sp.write_text(json.dumps({
        "architect": {"stack": [
            {"version": 1, "prompt": "v1", "score": 0.5, "created_at": "x"},
            {"version": 2, "prompt": "v2", "score": 0.8, "created_at": "x"},
        ], "current_version": 2},
        "coder": {"stack": [
            {"version": 1, "prompt": "c1", "score": 0.9, "created_at": "x"},
        ], "current_version": 1},
    }), encoding="utf-8")
    return sp


class TestCorruptFileNoLongerDestroysOtherAgents:
    def test_push_after_corruption_does_not_wipe_other_agents(self, tmp_path):
        sp = _seeded_store(tmp_path)
        sp.write_text("{ corrupted mid-edit", encoding="utf-8")

        PromptStore(store_path=sp).push("theme_validator", "new prompt", 0.6)

        # The bug: this used to be True (only the new agent survived).
        result = json.loads(sp.read_text(encoding="utf-8"))
        assert set(result.keys()) == {"theme_validator"}
        # The old data isn't in the NEW file — but it must exist SOMEWHERE.
        quarantined = list(tmp_path.glob("*.corrupt-*"))
        assert len(quarantined) == 1

    def test_quarantined_file_preserves_original_bytes(self, tmp_path):
        sp = _seeded_store(tmp_path)
        original = "{ corrupted mid-edit, exact bytes matter"
        sp.write_text(original, encoding="utf-8")

        PromptStore(store_path=sp).push("agent", "p", 0.5)

        quarantined = list(tmp_path.glob("*.corrupt-*"))[0]
        assert quarantined.read_text(encoding="utf-8") == original

    def test_valid_file_is_never_quarantined(self, tmp_path):
        sp = _seeded_store(tmp_path)
        PromptStore(store_path=sp).push("architect", "v3", 0.9)
        assert list(tmp_path.glob("*.corrupt-*")) == []
        result = json.loads(sp.read_text(encoding="utf-8"))
        assert "coder" in result   # untouched agent survives a normal push
        assert len(result["architect"]["stack"]) == 3


class TestWrongShapeNoLongerCrashes:
    @pytest.mark.parametrize("content,label", [
        ("[]", "list"),
        ('"a string"', "string"),
        ("null", "null"),
        ("42", "number"),
        ("", "empty file"),
    ])
    def test_push_survives_wrong_shape(self, tmp_path, content, label):
        sp = tmp_path / "prompts.json"
        sp.write_text(content, encoding="utf-8")
        PromptStore(store_path=sp).push("agent", "p", 0.5)   # must not raise
        result = json.loads(sp.read_text(encoding="utf-8"))
        assert "agent" in result, label

    def test_get_current_survives_wrong_shape(self, tmp_path):
        sp = tmp_path / "prompts.json"
        sp.write_text("[]", encoding="utf-8")
        store = PromptStore(store_path=sp)
        # "validator_agent" is one of the two names _get_hardcoded actually
        # recognises — an unregistered name raises ValueError regardless of
        # this fix, which is a separate, pre-existing contract and not what
        # this test is checking.
        result = store.get_current("validator_agent")
        assert isinstance(result, str)


class TestMissingFileStillWorks:
    def test_first_ever_push_creates_the_file(self, tmp_path):
        sp = tmp_path / "prompts.json"
        assert not sp.exists()
        PromptStore(store_path=sp).push("agent", "p", 0.5)
        assert sp.exists()
        assert list(tmp_path.glob("*.corrupt-*")) == []


class TestQuarantineSameSecondCollision:
    """Same defect as TicketStore._quarantine's sibling case: the stamp is
    second-resolution and PromptStore has only ONE store_path, so two
    quarantines of it within the same wall-clock second collide on one
    destination — Path.rename() silently replaces an existing file on
    POSIX. Reproducible without any clock mocking (confirmed across
    multiple real runs); narrower reachability than the ticket case (needs
    the single shared file corrupted, recovered, and corrupted again all
    within one second) but the same fix.
    """

    def test_two_quarantines_same_second_both_survive(self, tmp_path):
        sp = tmp_path / "prompts.json"
        sp.write_text("CORRUPT-A", encoding="utf-8")
        store = PromptStore(store_path=sp)
        store._quarantine("first corruption")

        sp.write_text("CORRUPT-B", encoding="utf-8")
        store._quarantine("second corruption")

        quarantined = sorted(tmp_path.glob("*.corrupt-*"))
        assert len(quarantined) == 2, (
            f"expected two distinct quarantine files, got {len(quarantined)}"
        )

    def test_both_files_content_preserved(self, tmp_path):
        sp = tmp_path / "prompts.json"
        sp.write_text("CORRUPT-A", encoding="utf-8")
        store = PromptStore(store_path=sp)
        store._quarantine("first corruption")
        sp.write_text("CORRUPT-B", encoding="utf-8")
        store._quarantine("second corruption")

        contents = {p.read_text(encoding="utf-8") for p in tmp_path.glob("*.corrupt-*")}
        assert contents == {"CORRUPT-A", "CORRUPT-B"}
