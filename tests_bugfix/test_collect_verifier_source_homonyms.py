"""tests_bugfix/test_collect_verifier_source_homonyms.py — V16.

`extract_claims` turns a bare name in a summary sentence into one `Claim`
per module that defines that name (`_symbol_patterns` is keyed per
qualname). V11 gave a *test* file a `citable_modules` set so
`_prefer_citable_homonyms` could resolve the bare name to the homonym it
means; a source module got `None`, the helper returned the unfiltered list,
and the pre-V11 behaviour stood. The homonyms outside the module being
summarised failed `citation_check` and `verify_claims`' sink-or-swim rule
then dropped the whole sentence — together with the correctly-resolved
claim that named exactly the symbol defined in that file. In this repo that
was 83 dropped claims and 13 emptied purposes across 97 source modules.

The fix is the one line in `verify_repo`: a source module gets
`citable_modules = {own path}` instead of `None`. `citation_check` treats
that set exactly as it treated `None`, so the gate and its detail strings
are unchanged for shipped code — only the bare-name resolution moves.
"""

from __future__ import annotations

from tools.collect.model import LLMSummary
from tools.collect.scanner import scan_module
from tools.collect.verifier import (
    REASON_NO_CITATION,
    REASON_SIBLING_CITATION_FAILED,
    extract_claims,
    verify_repo,
)

SRC_A = "def load_events():\n    return []\n"
SRC_B = "def load_events():\n    return []\n\ndef other_event_loader():\n    return []\n"
SRC_C = "def load_events():\n    return []\n"

A_PURPOSE = "load_events reads the trace."
B_ONLY_PURPOSE = "other_event_loader also reads the trace."


def _module(path, source, purpose=""):
    m = scan_module(source, path)
    return m.with_llm_summary(LLMSummary(purpose=purpose, notes="")) if purpose else m


def _run(modules, sources=None):
    if sources is None:
        sources = {m.path: "" for m in modules}
    verified, report = verify_repo(modules, sources)
    return {m.path: m.summary.purpose if m.summary else "" for m in verified}, report


# ── the bug: a source summary naming its own homonymous symbol ───────────────


def test_source_module_keeps_a_bare_name_it_defines_itself():
    a = _module("pkg/a.py", SRC_A, A_PURPOSE)
    purposes, report = _run([a, _module("pkg/b.py", SRC_B)])
    assert purposes["pkg/a.py"] == A_PURPOSE
    assert report["kept_count"] == 1
    assert report["dropped_count"] == 0


def test_source_module_with_three_homonyms_extracts_exactly_one_claim():
    a = _module("pkg/a.py", SRC_A, A_PURPOSE)
    purposes, report = _run(
        [a, _module("pkg/b.py", SRC_B), _module("pkg/c.py", SRC_C)],
    )
    assert purposes["pkg/a.py"] == A_PURPOSE
    assert report["kept_count"] == 1
    assert report["dropped_count"] == 0

    known = frozenset({"pkg/a.py:load_events", "pkg/b.py:load_events", "pkg/c.py:load_events"})
    claims = extract_claims(
        A_PURPOSE, "pkg/a.py", known, citable_modules=frozenset({"pkg/a.py"}),
    )
    assert [c.symbol for c in claims] == ["pkg/a.py:load_events"]


def test_a_source_module_is_not_widened_to_its_imports():
    # V11's widening is for tests only: a shipped module importing pkg/b may
    # not cite pkg/b.py:load_events just because b defines a homonym.
    a = _module("pkg/a.py", "import pkg.b\n" + SRC_A, A_PURPOSE)
    purposes, report = _run([a, _module("pkg/b.py", SRC_B)])
    assert purposes["pkg/a.py"] == A_PURPOSE
    assert report["kept_count"] == 1
    assert report["dropped_count"] == 0

    known = frozenset({"pkg/a.py:load_events", "pkg/b.py:load_events"})
    claims = extract_claims(
        A_PURPOSE, "pkg/a.py", known, citable_modules=frozenset({"pkg/a.py"}),
    )
    assert [c.symbol for c in claims] == ["pkg/a.py:load_events"]


# ── the gate is unchanged for shipped code ───────────────────────────────────


def test_source_module_citing_a_symbol_it_does_not_define_still_drops():
    a = _module("pkg/a.py", SRC_A, B_ONLY_PURPOSE)
    purposes, report = _run([a, _module("pkg/b.py", SRC_B)])
    assert purposes["pkg/a.py"] == ""
    assert [d["reason"] for d in report["dropped"]] == [REASON_NO_CITATION]
    assert report["dropped"][0]["detail"] == (
        "cited symbol 'pkg/b.py:other_event_loader' belongs to module "
        "'pkg/b.py', not 'pkg/a.py'"
    )


def test_source_module_citing_a_path_qualified_foreign_symbol_still_drops():
    a = _module("pkg/a.py", SRC_A, "pkg/b.py:other_event_loader reads the trace.")
    purposes, report = _run([a, _module("pkg/b.py", SRC_B)])
    assert purposes["pkg/a.py"] == ""
    assert report["dropped"][0]["reason"] == REASON_NO_CITATION
    assert report["dropped"][0]["detail"] == (
        "cited symbol 'pkg/b.py:other_event_loader' belongs to module "
        "'pkg/b.py', not 'pkg/a.py'"
    )


def test_a_wrong_citation_in_a_source_summary_still_sinks_the_sentence():
    # Sink-or-swim is untouched: a real own-module citation in a sentence
    # that also names a foreign symbol drops the whole sentence.
    a = _module("pkg/a.py", SRC_A, "other_event_loader and load_events both read the trace.")
    purposes, report = _run([a, _module("pkg/b.py", SRC_B)])
    assert purposes["pkg/a.py"] == ""
    reasons = [d["reason"] for d in report["dropped"]]
    assert REASON_NO_CITATION in reasons
    assert REASON_SIBLING_CITATION_FAILED in reasons
    assert report["kept_count"] == 0


def test_a_bare_single_word_is_still_not_a_citation():
    # `_symbol_patterns`' callers skip a plain single lowercase word, so a
    # three-way homonym on the name `load` produces one uncheckable generic
    # claim, not three — unchanged by giving source modules a citable set.
    known = frozenset({"pkg/a.py:load", "pkg/b.py:load", "pkg/c.py:load"})
    claims = extract_claims(
        "load reads the trace.", "pkg/a.py", known, citable_modules=frozenset({"pkg/a.py"}),
    )
    assert [c.symbol for c in claims] == [None]


def test_extract_claims_without_a_citable_set_still_returns_every_homonym():
    known = frozenset({"pkg/a.py:load_events", "pkg/b.py:load_events"})
    claims = extract_claims(A_PURPOSE, "pkg/a.py", known)
    assert sorted(c.symbol for c in claims) == sorted(known)


# ── the test-file path did not move ──────────────────────────────────────────


def test_test_file_importing_a_module_still_keeps_a_bare_homonym():
    test = _module("tests/test_events.py", "import pkg.a\n", "Covers load_events end to end.")
    purposes, report = _run([test, _module("pkg/a.py", SRC_A), _module("pkg/b.py", SRC_B)])
    assert purposes["tests/test_events.py"] == "Covers load_events end to end."
    assert report["kept_count"] == 1
    assert report["dropped_count"] == 0


def test_test_file_importing_neither_still_drops():
    test = _module("tests/test_events.py", "import pkg.unrelated\n",
                   "Covers load_events end to end.")
    purposes, report = _run([test, _module("pkg/a.py", SRC_A), _module("pkg/b.py", SRC_B)])
    assert purposes["tests/test_events.py"] == ""
    assert report["kept_count"] == 0
    assert all(d["reason"] in (REASON_NO_CITATION, REASON_SIBLING_CITATION_FAILED)
               for d in report["dropped"])


def test_source_and_test_modules_in_the_same_run_do_not_interfere():
    source = [
        _module("pkg/a.py", SRC_A, A_PURPOSE),
        _module("pkg/b.py", SRC_B),
    ]
    _, source_report = _run(source)
    test = _module("tests/test_events.py", "import pkg.a\n",
                   "Covers load_events. Also pkg/b.py:other_event_loader.")
    purposes, combined = _run(source + [test])
    # Every claim of the source modules is still counted the same way once a
    # test module joins the run — the per-module kept/dropped bookkeeping is
    # additive, so `kept_count == total_claims - dropped_count` per module.
    assert source_report["kept_count"] == 1
    assert source_report["dropped_count"] == 0
    assert combined["kept_count"] == source_report["kept_count"] + 1
    assert combined["dropped_count"] == source_report["dropped_count"] + 1
    assert purposes["tests/test_events.py"] == "Covers load_events."
