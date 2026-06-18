"""tests/test_cr18_duplication_guard.py — AUTO-CR-18.

Field failure: a "fix inconsistencies" run produced 20 overlapping tasks and the
coder "synchronised" chapters by copying chapter_2 verbatim into chapter_3 (and
mislabelling the heading). Three defences:
  (a) hard cap on creative tasks;
  (b) prompt hardening (covered by prompt-content asserts);
  (c) duplication guard rejecting an edit that copies one chapter into another.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path

import pytest

from tools.auto.coder import Coder
from tools.auto.architect import ClusterReviewer


CH2 = (
    "Глава 2\n\nКапитан Рейес стояла на мостике «Альбатроса», наблюдая за "
    "горизонтом. Ей сорок лет, седина уже начала появляться на висках. Рядом "
    "стояла юнга Мира, впервые в открытом море. Капитан знала, что впереди "
    "Ледяные проливы и ценный груз, о котором команде знать не положено."
)
CH4 = (
    "Глава 4\n\nКапитан Рейес вернулась на мостик и увидела Миру у "
    "радиопеленгатора. Сигнал маяка был слабым. Иван проверял навигационное "
    "оборудование, а свечение на экране приближалось к кораблю."
)


def _coder():
    cfg = configparser.ConfigParser()
    for s in ("coder", "api", "api_local"):
        cfg.add_section(s)
    cfg.set("api", "active", "local")
    cfg.set("api_local", "num_ctx", "32768")
    cfg.set("coder", "dup_reject_ratio", "0.92")
    return Coder(config=cfg, base_url="http://x", api_key="x", model="m",
                 api_format="ollama", task_mode="creative")


# ── (c) duplication guard ─────────────────────────────────────────────────────

def test_flags_chapter_copied_into_another(tmp_path):
    (tmp_path / "chapter_2.txt").write_text(CH2, encoding="utf-8")
    (tmp_path / "chapter_3.txt").write_text("Глава 3\n\nНечто иное.", encoding="utf-8")
    c = _coder()
    # The coder produced chapter_3 as a verbatim copy of chapter_2.
    parsed = [{"path": "chapter_3.txt", "content": CH2}]
    err = c._creative_duplication_error(parsed, tmp_path, ["chapter_3.txt"])
    assert err
    assert "chapter_3.txt" in err and "chapter_2.txt" in err


def test_distinct_chapters_pass(tmp_path):
    (tmp_path / "chapter_2.txt").write_text(CH2, encoding="utf-8")
    c = _coder()
    parsed = [{"path": "chapter_4.txt", "content": CH4}]
    assert c._creative_duplication_error(parsed, tmp_path, ["chapter_4.txt"]) == ""


def test_two_produced_files_identical_flagged(tmp_path):
    c = _coder()
    parsed = [
        {"path": "chapter_2.txt", "content": CH2},
        {"path": "chapter_3.txt", "content": CH2},
    ]
    err = c._creative_duplication_error(parsed, tmp_path,
                                        ["chapter_2.txt", "chapter_3.txt"])
    assert err


def test_disabled_when_ratio_zero(tmp_path):
    cfg = configparser.ConfigParser()
    for s in ("coder", "api", "api_local"):
        cfg.add_section(s)
    cfg.set("api", "active", "local")
    cfg.set("coder", "dup_reject_ratio", "0")
    c = Coder(config=cfg, base_url="http://x", api_key="x", model="m",
              api_format="ollama", task_mode="creative")
    parsed = [{"path": "chapter_3.txt", "content": CH2},
              {"path": "chapter_2.txt", "content": CH2}]
    assert c._creative_duplication_error(parsed, tmp_path, ["chapter_2.txt"]) == ""


# ── (a) creative task cap ─────────────────────────────────────────────────────

def _arch():
    cfg = configparser.ConfigParser()
    for s in ("architect", "auto"):
        cfg.add_section(s)
    cfg.set("architect", "max_tasks_creative", "1")
    return ClusterReviewer(cfg, base_url="http://x", api_key="x", model="m",
                           api_format="ollama", task_mode="creative")


def test_creative_task_cap_truncates():
    tasks = [{
        "title": f"Sync {i}", "instruction": "fix",
        "target_files": ["chapter_1.txt"],
        "acceptance_check": "true",
        "cited_location": {"file": "chapter_1.txt", "symbol": None,
                           "line_start": None, "line_end": None},
    } for i in range(20)]
    cands = _arch()._parse_candidates(json.dumps(tasks), "support", ["chapter_1.txt"])
    assert len(cands) == 1
