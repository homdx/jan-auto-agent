"""tests/test_cr27_no_rewriter_in_creative.py — the code-mode TaskRewriter must
not run on creative tasks (AUTO-CR-27).

Bug: in a creative run the rewriter's software-test prompt ("completely
different implementation", "exit 127", "gradlew test") fired after round 3 and
made the 8B emit a shell command (`diff -q chapter_4.txt … && echo 'Pass'`) as
the acceptance_check — code leaking into a prose pipeline.
"""
from __future__ import annotations

import configparser

from tools.auto.outer_loop import make_outer_loop


def _cfg():
    c = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    c["api"] = {"active": "local", "verify_ssl": "true"}
    c["api_local"] = {"base_url": "http://x", "api_key": "k",
                      "model": "m", "api_format": "ollama"}
    c["outer_loop"] = {"max_rounds": "10", "rewrite_every_n_rounds": "2",
                       "max_rewrites": "3"}
    c["loop"] = {"timeout_seconds": "300"}
    return c


class _State:
    def __getattr__(self, _):           # tolerate any state call
        return lambda *a, **k: None


def test_creative_has_no_task_rewriter(tmp_path):
    ol = make_outer_loop(_cfg(), str(tmp_path), _State(), task_mode="creative")
    assert ol.task_rewriter is None, "creative mode must not build a TaskRewriter"


def test_code_mode_still_builds_rewriter(tmp_path):
    ol = make_outer_loop(_cfg(), str(tmp_path), _State(), task_mode="code")
    assert ol.task_rewriter is not None, "code mode should keep the TaskRewriter"
