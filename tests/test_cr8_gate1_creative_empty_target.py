"""tests/test_cr8_gate1_creative_empty_target.py — AUTO-CR-8.

Reproduces the live failure: in creative mode, llama3.1:8b emitted
``line_start=0`` (instead of null) and the target chapter file was empty
(0 lines), so Gate1's existence check rejected every candidate and nothing
was ever written.

Fix under test:
  * existence check ignores symbol/line anchors in creative/docs (file alone
    is sufficient grounding);
  * Stage B presence check (an improvement-detector) is skipped in creative
    mode, since generating a new chapter has no pre-existing "issue".
"""

from __future__ import annotations

import configparser

import pytest

from tools.auto.gate1_filter import Gate1Filter
from tools.auto.architect import CandidateTask, CitedLocation


def _cfg():
    c = configparser.ConfigParser()
    c.add_section("gate1")
    c.add_section("api")
    c.add_section("loop")
    return c


def _candidate(file_name, line_start):
    return CandidateTask(
        title="Add chapter 2 content",
        instruction="Write chapter 2 continuing from chapter 1",
        target_files=[file_name],
        acceptance_check="true",
        cited_location=CitedLocation(
            file=file_name, symbol=None, line_start=line_start, line_end=line_start + 5
        ),
        cluster="support",
    )


def _filter(task_mode):
    return Gate1Filter(
        _cfg(), base_url="http://localhost:11434", api_key="x",
        model="llama3.1:8b", api_format="ollama", task_mode=task_mode,
    )


def test_creative_empty_target_with_hallucinated_line_anchor_passes(tmp_path):
    # chapter1.txt has content; chapter2.txt is the empty target.
    (tmp_path / "chapter1.txt").write_text("Captain Reyes stood on the bridge.\n", encoding="utf-8")
    (tmp_path / "chapter2.txt").write_text("", encoding="utf-8")  # empty (0 lines)

    cand = _candidate("chapter2.txt", line_start=0)  # hallucinated anchor
    cluster_files = {"support": {"chapter1.txt", "chapter2.txt"}}

    gate = _filter("creative")
    accepted, rejected = gate.filter([cand], tmp_path, cluster_files=cluster_files)

    assert len(accepted) == 1, f"expected 1 accepted, got rejected={[r.reason for r in rejected]}"
    assert rejected == []


def test_creative_missing_file_still_rejected(tmp_path):
    # File grounding is still required: a non-existent cited file must reject.
    cand = _candidate("chapter9.txt", line_start=0)
    gate = _filter("creative")
    accepted, rejected = gate.filter([cand], tmp_path,
                                     cluster_files={"support": {"chapter9.txt"}})
    assert accepted == []
    assert any("not found" in r.reason for r in rejected)


def test_code_mode_still_validates_line_range(tmp_path):
    # Regression: code mode must keep strict line-range existence checks.
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    cand = CandidateTask(
        title="fix", instruction="fix it", target_files=["mod.py"],
        acceptance_check="pytest -q",
        cited_location=CitedLocation(file="mod.py", symbol=None, line_start=999, line_end=1000),
        cluster="support",
    )
    gate = _filter("code")
    gate._skip_llm = True  # isolate Stage A
    accepted, rejected = gate.filter([cand], tmp_path,
                                     cluster_files={"support": {"mod.py"}})
    assert accepted == []
    assert any("out of range" in r.reason for r in rejected)
