"""tests_bugfix/test_harden1_a3_prompt_store_missing_version.py — HARDEN-1 A3:
a stored prompt version without ``version`` shows as ``v?`` and does not take
``/prompts`` down.

``get_store_summary`` read ``stack[-1]["version"]``, ``item["version"]`` and
``current["version"]`` directly, so one hand-edited or older-format
``prompts.json`` whose record has no ``version`` raised ``KeyError: 'version'``.
The REPL's ``/prompts`` command has no handler, so a single such record ended
the shell. A missing or ``null`` ``score`` was already shown as ``(no score)``
(fix 36); ``version`` gets the same treatment now, and the other records are
still listed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.prompt_store import PromptStore  # noqa: E402


def _store(tmp_path: Path, data: dict) -> PromptStore:
    (tmp_path / "prompts.json").write_text(json.dumps(data), encoding="utf-8")
    return PromptStore(store_path=tmp_path / "prompts.json")


def _record(version=None, score=None):
    rec = {"prompt": "x"}
    if version is not None:
        rec["version"] = version
    if score is not None:
        rec["score"] = score
    return rec


class TestARecordWithoutAVersion:
    @pytest.mark.parametrize("position", ["first", "middle", "last"])
    def test_the_table_still_prints_with_v_placeholder(self, tmp_path, position):
        stack = [_record(1), _record(2), _record(3)]
        stack[{"first": 0, "middle": 1, "last": 2}[position]].pop("version")
        # no current_version: the top of the stack is the active record
        store = _store(tmp_path, {"validator_agent": {"stack": stack}})
        out = store.get_store_summary(["validator_agent"])
        assert "v?" in out
        assert "validator_agent" in out
        # the two records that still carry a version are listed, not dropped with the third
        others = [v for i, v in enumerate(("v1", "v2", "v3"))
                  if i != {"first": 0, "middle": 1, "last": 2}[position]]
        assert all(v in out for v in others)

    def test_the_only_record_without_a_version_shows_v_placeholder(self, tmp_path):
        store = _store(tmp_path, {"validator_agent": {"stack": [_record(score=0.5)]}})
        out = store.get_store_summary(["validator_agent"])
        assert out.split()[1] == "v?"
        assert "(score 0.50)" in out

    def test_the_active_record_without_a_version_is_labelled_v_placeholder(self, tmp_path):
        store = _store(tmp_path, {"validator_agent": {"stack": [_record(1), _record()]}})
        out = store.get_store_summary(["validator_agent"])
        assert out.split()[1] == "v?"
        assert "rollback: v1, hardcoded" in out

    def test_a_record_with_no_version_is_not_an_error_in_the_rollback_chain(self, tmp_path):
        store = _store(tmp_path, {"improvement_agent": {"stack": [_record(1), _record()]}})
        assert "v?" in store.get_store_summary(["improvement_agent"])

    def test_a_null_version_is_a_missing_version(self, tmp_path):
        stack = [_record(1), {"prompt": "y", "version": None, "score": None}]
        store = _store(tmp_path, {"validator_agent": {"stack": stack}})
        assert "v?" in store.get_store_summary(["validator_agent"])


class TestANormalStoreIsUnchanged:
    def test_the_usual_table_is_printed_as_before(self, tmp_path):
        store = _store(tmp_path, {"validator_agent": {
            "stack": [_record(1, 0.72), _record(2, 0.87)], "current_version": 2},
            "improvement_agent": {"stack": []}})
        out = store.get_store_summary(["validator_agent", "improvement_agent"])
        lines = out.splitlines()
        assert lines[0].startswith("validator_agent")
        assert "v2" in lines[0] and "(score 0.87)" in lines[0]
        assert "rollback: v1, hardcoded" in lines[0]
        assert lines[1].startswith("improvement_agent") and "hardcoded" in lines[1]
        assert "v?" not in out
