"""tests/test_cr28_reserved_files.py — control/memory files and archives must
never enter the editable corpus (AUTO-CR-28).

Bug: story_bible.md (and book.7z) were ingested as story files; the architect
made a task targeting story_bible.md, the coder rewrote the bible as prose, and
the redundancy gate looped ~1h. CR-15 had excluded only synopsis.md /
IMPROVEMENTS.md; story_bible.md (CR-23) and .7z were never added.
"""
from __future__ import annotations


from tools.auto.repo_ingest import RESERVED_META_FILES, _BINARY_EXTENSIONS, RepoIngestor


def test_reserved_set_contains_bible_and_plan():
    assert "story_bible.md" in RESERVED_META_FILES
    assert "plan.json" in RESERVED_META_FILES
    assert "synopsis.md" in RESERVED_META_FILES
    assert "improvements.md" in RESERVED_META_FILES


def test_archive_extensions_skipped():
    assert ".7z" in _BINARY_EXTENSIONS


def test_walk_excludes_reserved_and_archive(tmp_path):
    # a realistic messy book dir like the one in the log
    (tmp_path / "chapter_1.txt").write_text("Глава 1", encoding="utf-8")
    (tmp_path / "chapter_2.txt").write_text("Глава 2", encoding="utf-8")
    (tmp_path / "story_bible.md").write_text("• факт", encoding="utf-8")
    (tmp_path / "synopsis.md").write_text("## c1", encoding="utf-8")
    (tmp_path / "IMPROVEMENTS.md").write_text("plan", encoding="utf-8")
    (tmp_path / "plan.json").write_text("{}", encoding="utf-8")
    (tmp_path / "book.7z").write_bytes(b"\x37\x7a\xbc\xaf\x27\x1c")

    walked = set(RepoIngestor(str(tmp_path)).walk())
    assert "chapter_1.txt" in walked and "chapter_2.txt" in walked
    for forbidden in ("story_bible.md", "synopsis.md", "IMPROVEMENTS.md",
                      "plan.json", "book.7z"):
        assert forbidden not in walked, f"{forbidden} must not be ingested"


def test_architect_rejects_reserved_target():
    # _parse_candidates must bounce a task that targets a reserved file
    from tools.auto.architect import ClusterReviewer
    import configparser
    rv = object.__new__(ClusterReviewer)        # bypass heavy __init__
    rv._task_mode = "creative"
    rv._config = configparser.ConfigParser()
    raw = (
        '[{"title":"Trim","instruction":"remove repetition",'
        '"target_files":["story_bible.md"],"acceptance_check":"true"}]'
    )
    cands = rv._parse_candidates(raw, "support", ["chapter_1.txt", "story_bible.md"])
    assert cands == [], "a task targeting story_bible.md must be rejected"


def test_architect_keeps_chapter_target():
    from tools.auto.architect import ClusterReviewer
    import configparser
    rv = object.__new__(ClusterReviewer)
    rv._task_mode = "creative"
    rv._config = configparser.ConfigParser()
    raw = (
        '[{"title":"Trim","instruction":"remove repetition",'
        '"target_files":["chapter_1.txt"],"acceptance_check":"true"}]'
    )
    cands = rv._parse_candidates(raw, "support", ["chapter_1.txt"])
    assert len(cands) == 1
