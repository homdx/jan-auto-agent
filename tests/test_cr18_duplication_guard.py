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


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-CR-18-2 — within-file duplication (a single file, multiple entries)
# ─────────────────────────────────────────────────────────────────────────────
# Field failure: a "narrative changelog" task looped 8 attempts across 7
# rounds. Each attempt APPENDED a new "### Greeting <verb> Hello world"
# entry to CHANGELOG.md — a synonym-swapped rewrite of the entry before it
# — rather than writing one entry, because nothing ever rejected the
# growing file: the guard above only compares DIFFERENT file names, and a
# single target file (CHANGELOG.md) is always compared against itself and
# skipped (`oname == pname`). The file kept growing until the bloated,
# heavily-repetitive prompt made the Gate-2 validator itself degenerate.
#
# A second, independent bug compounded it: even a direct, deliberate
# comparison of the two near-duplicate entries scored ratio=0.085 with
# difflib's default autojunk=True — nowhere near the 0.92 threshold —
# because autojunk discards frequently-recurring substrings (ordinary
# prose's spaces and common short words) from matching once a string
# passes ~200 characters, exactly the length any real chapter or entry
# reaches. autojunk=False on the same two entries: 0.938.

_ENTRY_A = (
    "We taught the program its first and only line of speech by writing one "
    "statement in main.py that prints Hello world to standard output and "
    "then ends. We wanted the earliest run to prove the toolchain was "
    "alive, and a plain greeting was the smallest honest signal we could "
    "ask for.\n\n"
    "That line became the quiet reference we check against. We left it "
    "clear of flags and conditions, because its worth is in being a result "
    "we can name before we see it. When the rest of the work is silent and "
    "we need to know the base still holds, we run this and read the one "
    "line it returns.\n\n"
    "A reader can now run the program and see its greeting without opening "
    "the file that produces it."
)
# A synonym-swapped rewrite of _ENTRY_A — same structure, ~15% of words
# changed. This is the realistic near-duplicate shape a model produces
# when it rewrites rather than repeats verbatim; a verbatim-copy fixture
# (ratio 1.0 regardless of autojunk) would not have caught the autojunk
# bug at all.
_ENTRY_B = (
    "We gave the program its opening words by writing one line in main.py "
    "that prints Hello world to standard output and then ends. We wanted "
    "the first run to show the toolchain was working, and a plain greeting "
    "was the smallest honest signal we could ask for.\n\n"
    "That line became the quiet reference we check against. We left it "
    "clear of flags and conditions, because its worth is in being a result "
    "we can name before we see it. When the rest of the work is silent and "
    "we need to know the base still holds, we run this and read the one "
    "line it returns.\n\n"
    "A reader can now execute the program and see its greeting without "
    "opening the file that produces it."
)


def _changelog(*entries: tuple[str, str]) -> str:
    body = "# Changelog\n\n"
    for title, text in entries:
        body += f"### {title}\n\n{text}\n\n"
    return body


def test_flags_paraphrased_entry_within_the_same_file(tmp_path):
    content = _changelog(
        ("Greeting prints Hello world", _ENTRY_A),
        ("Greeting declares Hello world", _ENTRY_B),
    )
    c = _coder()
    parsed = [{"path": "CHANGELOG.md", "content": content}]
    err = c._creative_duplication_error(parsed, tmp_path, ["CHANGELOG.md"])
    assert err
    assert "Greeting prints Hello world" in err
    assert "Greeting declares Hello world" in err
    assert "CHANGELOG.md" in err


def test_distinct_entries_in_one_file_pass(tmp_path):
    content = _changelog(
        ("Greeting prints Hello world", _ENTRY_A),
        (
            "Config file support added",
            "We added a config.yaml loader with three options: verbosity, "
            "output path, and a retry count. None of this touches the "
            "greeting logic; it is a separate, unrelated feature with its "
            "own tests and its own section of the README.",
        ),
    )
    c = _coder()
    parsed = [{"path": "CHANGELOG.md", "content": content}]
    assert c._creative_duplication_error(parsed, tmp_path, ["CHANGELOG.md"]) == ""


def test_single_entry_file_is_not_flagged(tmp_path):
    """Only one `### ` heading — nothing to compare within the file."""
    content = "# Changelog\n\n### Only entry\n\n" + _ENTRY_A
    c = _coder()
    parsed = [{"path": "CHANGELOG.md", "content": content}]
    assert c._creative_duplication_error(parsed, tmp_path, ["CHANGELOG.md"]) == ""


def test_no_headings_at_all_is_not_flagged(tmp_path):
    """A single-scene chapter with no `### ` structure — the within-file
    check must not misfire on ordinary narrative prose."""
    c = _coder()
    parsed = [{"path": "chapter_5.txt", "content": _ENTRY_A + "\n\n" + _ENTRY_B}]
    assert c._creative_duplication_error(parsed, tmp_path, ["chapter_5.txt"]) == ""


def test_within_file_check_disabled_when_ratio_zero(tmp_path):
    cfg = configparser.ConfigParser()
    for s in ("coder", "api", "api_local"):
        cfg.add_section(s)
    cfg.set("api", "active", "local")
    cfg.set("coder", "dup_reject_ratio", "0")
    c = Coder(config=cfg, base_url="http://x", api_key="x", model="m",
              api_format="ollama", task_mode="creative")
    content = _changelog(
        ("Greeting prints Hello world", _ENTRY_A),
        ("Greeting declares Hello world", _ENTRY_B),
    )
    parsed = [{"path": "CHANGELOG.md", "content": content}]
    assert c._creative_duplication_error(parsed, tmp_path, ["CHANGELOG.md"]) == ""


def test_split_creative_entries_pairs_titles_with_bodies():
    from tools.auto.coder import _split_creative_entries

    text = "# Changelog\n\n### First\n\nbody one.\n\n### Second\n\nbody two.\n"
    entries = _split_creative_entries(text)
    assert [title for title, _ in entries] == ["First", "Second"]
    assert "body one." in entries[0][1]
    assert "body two." in entries[1][1]


def test_split_creative_entries_needs_at_least_two_headings():
    from tools.auto.coder import _split_creative_entries

    assert _split_creative_entries("# Changelog\n\n### Only\n\nprose") == []
    assert _split_creative_entries("no headings here at all") == []
    assert _split_creative_entries("") == []


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-CR-18-2 — the autojunk regression itself, isolated from entry-splitting
# ─────────────────────────────────────────────────────────────────────────────

def test_autojunk_true_would_have_missed_this_real_paraphrase():
    """Documents the bug directly: this is the exact pair of entries a live
    run produced, and difflib's default (autojunk=True) scores them far
    below any reasonable duplication threshold. If this assertion ever
    starts failing, autojunk stopped collapsing the ratio for this input
    and the regression test below may need a different fixture — but as
    of this fix, the low score is precisely what autojunk=False corrects."""
    import difflib

    def _norm(s: str) -> str:
        return " ".join((s or "").split()).lower()

    n1, n2 = _norm(_ENTRY_A), _norm(_ENTRY_B)
    junky = difflib.SequenceMatcher(None, n1, n2).ratio()
    clean = difflib.SequenceMatcher(None, n1, n2, autojunk=False).ratio()
    assert junky < 0.5, f"expected the autojunk bug to still reproduce, got {junky}"
    assert clean >= 0.92, f"expected a real near-duplicate to score high, got {clean}"


def test_cross_file_check_also_uses_autojunk_false(tmp_path):
    """The ORIGINAL (cross-file) comparison had the exact same autojunk
    exposure — a paraphrased (not verbatim-copied) chapter_3 could have
    sailed past the existing guard. Same fixture, cross-file this time."""
    (tmp_path / "chapter_2.txt").write_text(_ENTRY_A, encoding="utf-8")
    c = _coder()
    parsed = [{"path": "chapter_3.txt", "content": _ENTRY_B}]
    err = c._creative_duplication_error(parsed, tmp_path, ["chapter_3.txt"])
    assert err
    assert "chapter_3.txt" in err and "chapter_2.txt" in err

