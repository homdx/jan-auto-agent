"""HARDEN-8 E/F (306): a prompt record with no version, and an IMPROVEMENTS.md entry whose id is
letters alone.

* **E** HARDEN-1 A3 made `get_store_summary` read the top of the stack with `.get("version")` and
  show a version-less record as `v?`. `get_version_label` still read `stack[-1]["version"]` as the
  default of `entry.get("current_version", …)`, and a default is evaluated eagerly: an entry whose
  top record has no `version` — hand-edited, or written by an older version — raised
  `KeyError: 'version'` where `get_store_summary` lists it. It now reads the version the way the
  summary does: the label of the last record that still carries one, or `hardcoded`.
* **F** `_NEXT_ENTRY` accepted a next heading only when its id held a digit, `.`, `_` or `-`.
  Entries are separated by `---`, so an id of letters alone was not a boundary and removing the
  first of three such entries removed the rest of the file with it. Any id is a boundary now; what
  decides it is where the heading stands, not what it is called.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.auto.plan_validator import _remove_from_improvements_md, _task_section_pattern  # noqa: E402
from tools.prompt_store import PromptStore  # noqa: E402


# ── E: a prompt entry whose top record has no version ─────────────────────────────────────────

def _store(tmp_path: Path, data: dict) -> PromptStore:
    (tmp_path / "prompts.json").write_text(json.dumps(data), encoding="utf-8")
    return PromptStore(store_path=tmp_path / "prompts.json")


def _record(version=None):
    rec = {"prompt": "x"}
    if version is not None:
        rec["version"] = version
    return rec


def test_a_version_less_top_record_answers_the_last_version_it_has(tmp_path):
    """The reproduction: `stack[-1]["version"]` raised `KeyError` where the summary lists the entry."""
    store = _store(tmp_path, {"validator_agent": {"stack": [_record(1), _record()]}})
    assert store.get_version_label("validator_agent") == "v1"
    assert "v?" in store.get_store_summary(["validator_agent"])


def test_a_version_less_only_record_is_hardcoded(tmp_path):
    store = _store(tmp_path, {"validator_agent": {"stack": [_record()]}})
    assert store.get_version_label("validator_agent") == "hardcoded"
    assert "v?" in store.get_store_summary(["validator_agent"])


def test_a_version_less_middle_record_answers_the_top(tmp_path):
    store = _store(tmp_path, {"validator_agent": {"stack": [_record(1), _record(), _record(3)]}})
    assert store.get_version_label("validator_agent") == "v3"


def test_a_null_version_is_a_missing_version(tmp_path):
    stack = [_record(1), {"prompt": "y", "version": None}]
    store = _store(tmp_path, {"validator_agent": {"stack": stack}})
    assert store.get_version_label("validator_agent") == "v1"


def test_an_entry_with_a_current_version_is_unchanged(tmp_path):
    """`current_version` is read first, as before, even when the top record has no version."""
    store = _store(tmp_path, {"validator_agent": {
        "stack": [_record(1), _record(2), _record()], "current_version": 2}})
    assert store.get_version_label("validator_agent") == "v2"


@pytest.mark.parametrize("entry", [[1], "text", None, {"stack": "text"}, {"stack": ["not a record"]}])
def test_an_entry_or_stack_of_the_wrong_shape_is_hardcoded(entry, tmp_path):
    """A hand-edited prompts.json whose entry is not a mapping, or whose stack is not a list of records,
    answers `hardcoded` instead of raising (the label is fail-open like the table)."""
    assert _store(tmp_path, {"coder": entry}).get_version_label("coder") == "hardcoded"


def test_an_empty_stack_and_an_unknown_agent_are_still_hardcoded(tmp_path):
    store = _store(tmp_path, {"validator_agent": {"stack": []}})
    assert store.get_version_label("validator_agent") == "hardcoded"
    assert store.get_version_label("nobody") == "hardcoded"


# ── F: removing an IMPROVEMENTS.md entry whose id is letters alone ─────────────────────────────

def _doc(ids) -> str:
    return ("## Autonomous Tasks\n\n"
            + "".join(f"### {i}: task {i}\n\nInstruction: {i}\n\n---\n\n" for i in ids)
            + "## Manual Suggestions\n\n- a manual note\n")


@pytest.mark.parametrize("ids", [
    ("AUTO-T1", "AUTO-T2", "AUTO-T3"),
    ("t1", "t2", "t3"),
    ("alpha", "beta", "gamma"),
    ("FIX", "BAR", "BAZ"),
    ("task_b", "task_c"),
])
def test_removing_the_first_entry_keeps_the_rest(ids, tmp_path):
    """The reproduction: `alpha` / `FIX` took the entries after them with them."""
    (tmp_path / "IMPROVEMENTS.md").write_text(_doc(ids), encoding="utf-8")
    _remove_from_improvements_md(tmp_path, [ids[0]])
    out = (tmp_path / "IMPROVEMENTS.md").read_text(encoding="utf-8")
    assert f"### {ids[0]}:" not in out
    assert all(f"### {i}:" in out for i in ids[1:]), ids
    assert "## Manual Suggestions" in out and "- a manual note" in out


def test_removing_the_last_entry_keeps_the_first(tmp_path):
    (tmp_path / "IMPROVEMENTS.md").write_text(_doc(("alpha", "beta", "gamma")), encoding="utf-8")
    _remove_from_improvements_md(tmp_path, ["gamma"])
    out = (tmp_path / "IMPROVEMENTS.md").read_text(encoding="utf-8")
    assert "### gamma:" not in out
    assert "### alpha:" in out and "### beta:" in out


def test_a_note_inside_an_entrys_text_does_not_end_it(tmp_path):
    """A `### Note: …` line that is not after a separator is the entry's own text."""
    doc = ("## Autonomous Tasks\n\n"
           "### alpha: first task\n\nInstruction: keep this.\n\n"
           "### Note: not a next entry\n\nmore text\n\n---\n\n"
           "### beta: second task\n\nInstruction: and this.\n\n---\n\n"
           "## Manual Suggestions\n\n- a manual note\n")
    (tmp_path / "IMPROVEMENTS.md").write_text(doc, encoding="utf-8")
    _remove_from_improvements_md(tmp_path, ["alpha"])
    out = (tmp_path / "IMPROVEMENTS.md").read_text(encoding="utf-8")
    assert "### alpha:" not in out and "### Note:" not in out and "more text" not in out
    assert "### beta:" in out and "## Manual Suggestions" in out


def test_a_fenced_block_inside_an_entry_still_does_not_end_it():
    """FIX-2 #8's own shape, with letters-only ids: the fence is body, the separator is not."""
    doc = ("## Autonomous Tasks\n\n### alpha: first task\n\nInstruction:\n\n"
           "```markdown\n---\n### Example heading\nbody\n```\n\nAcceptance: done.\n\n---\n\n"
           "### beta: second task\n\nInstruction: nothing special.\n\n---\n\n"
           "## Manual Suggestions\n\n- a manual note\n")
    out = _task_section_pattern("alpha").sub("", doc)
    assert "alpha" not in out and "### Example heading" not in out and "Acceptance: done." not in out
    assert "### beta:" in out and "## Manual Suggestions" in out


def test_the_last_entry_before_manual_suggestions_is_removed(tmp_path):
    """The no-separator fallback, letters-only ids and all."""
    doc = ("## Autonomous Tasks\n\n"
           "### alpha: first task\n\nInstruction: this.\n\n---\n\n"
           "### beta: only the last one\n\nInstruction:\n\n```md\n---\n### Example\n```\n"
           "\n## Manual Suggestions\n\n- a manual note\n")
    (tmp_path / "IMPROVEMENTS.md").write_text(doc, encoding="utf-8")
    _remove_from_improvements_md(tmp_path, ["beta"])
    out = (tmp_path / "IMPROVEMENTS.md").read_text(encoding="utf-8")
    assert "### beta:" not in out and "### Example" not in out
    assert "### alpha:" in out and "## Manual Suggestions" in out


def test_an_id_the_file_does_not_hold_changes_nothing(tmp_path):
    doc = _doc(("alpha", "beta"))
    (tmp_path / "IMPROVEMENTS.md").write_text(doc, encoding="utf-8")
    _remove_from_improvements_md(tmp_path, ["nobody"])
    assert (tmp_path / "IMPROVEMENTS.md").read_text(encoding="utf-8") == doc


def test_a_missing_file_is_a_no_op(tmp_path):
    _remove_from_improvements_md(tmp_path, ["alpha"])
