"""tests/test_context_broker_stale_cache.py — Pass 0 whole-file resolution
must not cache a TARGET file's content across attempts.

ContextBroker.resolve() documents its cache policy explicitly: Pass 1
(target files, rewritten every attempt) is never cached; Pass 2 (project
scan, files this task doesn't edit) is cached and explicitly excludes
target_files via _iter_project_files. Pass 0 (_resolve_whole_file, AUTO-
CR-19-2's whole-chapter-file match) cached unconditionally with no such
exclusion — a side door around the documented invariant.

Reproduced directly: a CONTEXT_REQUEST naming a file that is ALSO one of
this task's own target_files resolved via Pass 0 on attempt 1, was cached,
and then served the SAME (pre-rewrite) content on attempt 2 even after the
coder had rewritten that file — reset_cache() runs once per run_task(), not
per attempt, so nothing else would have invalidated it.

Reachable specifically in multi-target-file creative tasks with
cross-chapter references, a supported and tested shape (AUTO-CR-16).
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.context_broker import ContextBroker


class TestWholeFilePassNeverCachesATargetFile:
    def test_target_file_request_does_not_return_stale_content(self, tmp_path):
        ch2 = tmp_path / "chapter_2.md"
        ch2.write_text("ORIGINAL content", encoding="utf-8")
        broker = ContextBroker()
        target_files = ["chapter_2.md", "chapter_3.md"]

        broker.resolve(["chapter_2"], target_files, tmp_path)   # attempt 1
        ch2.write_text("REWRITTEN content", encoding="utf-8")   # coder edits it
        result = broker.resolve(["chapter_2"], target_files, tmp_path)  # attempt 2

        # The bug returned "ORIGINAL content" here. The fix's chosen
        # behaviour is to not resolve a target file via this path at all
        # (the model already has direct access to its own target files),
        # rather than resolve-but-not-cache — either way, STALE content must
        # never come back.
        assert "ORIGINAL content" not in result.get("chapter_2", "")

    def test_target_file_is_not_cached_even_on_a_single_call(self, tmp_path):
        """The cache dict itself must never gain an entry for a target file."""
        ch2 = tmp_path / "chapter_2.md"
        ch2.write_text("content", encoding="utf-8")
        broker = ContextBroker()
        broker.resolve(["chapter_2"], ["chapter_2.md"], tmp_path)
        assert "chapter_2" not in broker._resolved_cache

    def test_non_target_file_still_resolves_and_caches_normally(self, tmp_path):
        """The fix must not break the legitimate, intended use of Pass 0:
        resolving a DIFFERENT chapter for cross-reference context."""
        ch1 = tmp_path / "chapter_1.md"
        ch1.write_text("chapter one content", encoding="utf-8")
        broker = ContextBroker()
        target_files = ["chapter_2.md"]   # chapter_1 is NOT a target

        result = broker.resolve(["chapter_1"], target_files, tmp_path)
        assert "chapter one content" in result.get("chapter_1", "")
        assert "chapter_1" in broker._resolved_cache

    def test_bare_stem_match_excludes_target_files_at_pass_0(self, tmp_path):
        """Isolate Pass 0 directly — resolve() alone can't isolate this,
        since Pass 1 legitimately (and safely — it is never cached) also
        searches target files, and would confound the assertion if the
        body text happens to contain the query word itself."""
        prologue = tmp_path / "prologue.md"
        prologue.write_text("unrelated body content", encoding="utf-8")
        broker = ContextBroker()
        whole = broker._resolve_whole_file("prologue", tmp_path, ["prologue.md"])
        assert whole == ""
        assert "prologue" not in broker._resolved_cache

    def test_chapter_number_match_excludes_target_files_at_pass_0(self, tmp_path):
        """Same isolation rationale as the bare-stem test above."""
        ch3 = tmp_path / "chapter_3.md"
        ch3.write_text("unrelated body content", encoding="utf-8")
        broker = ContextBroker()
        whole = broker._resolve_whole_file("chapter_3", tmp_path, ["chapter_3.md"])
        assert whole == ""
        assert "chapter_3" not in broker._resolved_cache

    def test_target_file_falls_back_to_a_non_target_duplicate_if_one_exists(self, tmp_path):
        """If a NON-target file also matches (e.g. an archived duplicate),
        Pass 0 may still resolve it — only the target file itself is
        excluded. Content deliberately excludes the query word so Pass 1
        cannot confound this Pass-0-specific assertion."""
        (tmp_path / "chapter_2.md").write_text("target body", encoding="utf-8")
        (tmp_path / "old").mkdir()
        (tmp_path / "old" / "chapter_2.md").write_text("archived body", encoding="utf-8")
        broker = ContextBroker()
        whole = broker._resolve_whole_file("chapter_2", tmp_path, ["chapter_2.md"])
        assert whole == "archived body"
