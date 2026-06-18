"""tests/test_cr15_fidelity_and_ingest.py — AUTO-CR-15.

From the field synopsis.md:
  * the fidelity verifier appended "[corrected] ..." annotations (with verbose
    parentheticals and English) instead of fixing bullets → never converged
    (always hit the round cap) and polluted the synopsis;
  * synopsis.md / IMPROVEMENTS.md were ingested as if they were story files.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from tools.auto.summary_memory import _clean_bullet_list, SummaryFidelityVerifier


def _fv(llm, rounds=2):
    return SummaryFidelityVerifier(llm, max_fidelity_rounds=rounds)


# ── fidelity: replace, converge, stay clean ──────────────────────────────────

def test_no_corrected_annotation_pollution():
    """A correction replaces the list; no '[corrected]' text ever appears."""
    calls = [0]

    def stub(system, user):
        calls[0] += 1
        if calls[0] == 1:
            return "• Капитан Рейес на мостике\n• «Альбатрос» идёт на север"
        return "OK"

    out = _fv(stub, rounds=3).verify_and_fix("исходный текст главы", "• неверный факт")
    assert "[corrected]" not in out
    assert "неверный факт" not in out      # replaced, not appended
    assert "Рейес" in out
    assert calls[0] == 2                    # converged after OK


def test_converges_when_summary_already_good():
    """If the verifier says OK on round 1, only one call is made (no wasted cap)."""
    calls = [0]

    def stub(system, user):
        calls[0] += 1
        return "OK"

    out = _fv(stub, rounds=2).verify_and_fix("chapter", "• fact")
    assert calls[0] == 1
    assert out == "• fact"


def test_rambling_reply_keeps_current():
    """A non-bullet rambling reply is unusable → keep current, stop."""
    calls = [0]

    def stub(system, user):
        calls[0] += 1
        return "I think it is fine but could be better."

    out = _fv(stub, rounds=3).verify_and_fix("chapter", "• original fact")
    assert out == "• original fact"
    assert calls[0] == 1


def test_clean_bullet_list_rejects_prose():
    assert _clean_bullet_list("just some prose with no bullets") == ""
    assert _clean_bullet_list("- a\n* b\n1. c").count("•") == 3


# ── ingest excludes meta files ───────────────────────────────────────────────

def test_ingest_excludes_synopsis_and_improvements(tmp_path):
    from tools.auto.repo_ingest import RepoIngestor

    (tmp_path / "chapter_1.txt").write_text("Глава 1.", encoding="utf-8")
    (tmp_path / "synopsis.md").write_text("• fact", encoding="utf-8")
    (tmp_path / "IMPROVEMENTS.md").write_text("plan", encoding="utf-8")

    cfg = configparser.ConfigParser()
    for s in ("search", "architect"):
        cfg.add_section(s)
    ing = RepoIngestor(base_dir=tmp_path, config=cfg)
    files = list(ing.walk())

    assert "chapter_1.txt" in files
    assert "synopsis.md" not in files
    assert "IMPROVEMENTS.md" not in files
