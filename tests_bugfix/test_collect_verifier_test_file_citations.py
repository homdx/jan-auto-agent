"""tests_bugfix/test_collect_verifier_test_file_citations.py — V11.

Pass C (`verifier.citation_check`) drops a claim whose cited symbol belongs
to a module other than the one being summarized — right for shipped code
(Pass B only ever saw that one module's facts), inverted for a test file,
whose whole purpose is to name symbols from the module it imports. On a
full rebuild that rule emptied the purpose of 219 test modules.

The fix: `verify_repo` gives a test file (`test_paths.is_test_path` — the
L5 rule: under a test root or a `conftest.py`; a bare `test_*.py` name is
not a signal, `tools/collect/test_map.py` ships) a `citable_modules` set of
its own path plus every module `graph.import_edges` says it imports, and
threads that set as a parameter down to `citation_check`. Every other
module gets `None` — the own-module rule, unchanged.

One more thing the widening alone would not have fixed: `extract_claims`
turns a bare mention of `load_events` into one `Claim` per module that
defines a `load_events`; the non-imported homonyms failed and took the
sentence down with the imported one (`verify_claims`' sink-or-swim rule).
Now a bare name resolves to its citable homonyms when it has any.
"""

from __future__ import annotations

from tools.collect.graph import import_edges
from tools.collect.model import LLMSummary
from tools.collect.scanner import scan_module
from tools.collect.verifier import (
    REASON_NO_CITATION,
    Claim,
    citation_check,
    extract_claims,
    verify_repo,
)

TARGET_SRC = "def widget_helper():\n    return 1\n"
OTHER_SRC = "def other_helper():\n    return 2\n"


def _module(path, source, purpose=""):
    m = scan_module(source, path)
    return m.with_llm_summary(LLMSummary(purpose=purpose, notes="")) if purpose else m


def _run(modules, **kw):
    sources = {m.path: "" for m in modules}
    verified, report = verify_repo(modules, sources, **kw)
    return verified[0].summary.purpose, report


# ── the rule, end to end through verify_repo ─────────────────────────────────


def test_test_file_keeps_a_citation_to_a_module_it_imports():
    test = _module("tests/test_widget.py", "import pkg.widget\n",
                   "Exercises widget_helper from the module under test.")
    purpose, report = _run([test, _module("pkg/widget.py", TARGET_SRC)])
    assert "widget_helper" in purpose
    assert report["dropped_count"] == 0


def test_test_file_citing_a_module_it_does_not_import_still_drops():
    test = _module("tests/test_widget.py", "import pkg.widget\n",
                   "Exercises widget_helper. Also exercises pkg/other.py:other_helper.")
    purpose, report = _run([
        test, _module("pkg/widget.py", TARGET_SRC), _module("pkg/other.py", OTHER_SRC),
    ])
    assert "widget_helper" in purpose
    assert "other_helper" not in purpose
    assert [d["reason"] for d in report["dropped"]] == [REASON_NO_CITATION]
    assert "other_helper" in report["dropped"][0]["claim"]


def test_non_test_module_citing_its_own_import_still_drops():
    # "Non-test rules unchanged": a shipped module importing pkg.widget may
    # still not cite widget_helper — Pass B never saw pkg/widget.py.
    consumer = _module("pkg/consumer.py", "import pkg.widget\n",
                       "Calls widget_helper from its dependency.")
    purpose, report = _run([consumer, _module("pkg/widget.py", TARGET_SRC)])
    assert purpose == ""
    assert [d["reason"] for d in report["dropped"]] == [REASON_NO_CITATION]


def test_a_shipped_module_named_like_a_test_is_not_widened():
    # tools/collect/test_map.py is shipped code in this repo (L5): the ticket's
    # literal "test_*.py anywhere" would relax Pass C for it — it must not.
    shipped = _module("tools/test_map.py", "import pkg.widget\n",
                      "Wraps widget_helper for the map.")
    purpose, _ = _run([shipped, _module("pkg/widget.py", TARGET_SRC)])
    assert purpose == ""


def test_conftest_and_every_test_root_are_test_code():
    for path in ("conftest.py", "tests_bugfix/test_x.py", "tests/fixtures/helper.py",
                 ".smoke_tests/test_x.py", ".regression_tests/test_x.py"):
        test = _module(path, "import pkg.widget\n", "Uses widget_helper.")
        purpose, _ = _run([test, _module("pkg/widget.py", TARGET_SRC)])
        assert "widget_helper" in purpose, path


def test_a_line_citation_into_an_imported_module_still_drops():
    # The widening is for symbols only: a test never read pkg/widget.py's
    # line numbers, Pass B was not shown them either.
    test = _module("tests/test_widget.py", "import pkg.widget\n",
                   "Asserts on pkg/widget.py:1 behaviour.")
    purpose, report = _run([test, _module("pkg/widget.py", TARGET_SRC)])
    assert purpose == ""
    assert report["dropped"][0]["reason"] == REASON_NO_CITATION


def test_caller_supplied_import_edges_match_the_computed_ones():
    test = _module("tests/test_widget.py", "import pkg.widget\n", "Exercises widget_helper.")
    modules = [test, _module("pkg/widget.py", TARGET_SRC)]
    computed, _ = _run(modules)
    supplied, _ = _run(modules, import_edges=import_edges(modules))
    assert computed == supplied == "Exercises widget_helper."
    # An explicit graph with no edge for the test is the un-widened rule.
    none, _ = _run(modules, import_edges={m.path: frozenset() for m in modules})
    assert none == ""


# ── homonyms: the bare-name resolution the widening depends on ───────────────


def test_bare_name_defined_in_an_unimported_module_too_resolves_to_the_import():
    test = _module("tests/test_widget.py", "import pkg.widget\n",
                   "Covers widget_helper end to end.")
    modules = [
        test, _module("pkg/widget.py", TARGET_SRC),
        _module("pkg/legacy.py", TARGET_SRC),  # a second widget_helper, not imported
    ]
    purpose, report = _run(modules)
    assert purpose == "Covers widget_helper end to end."
    assert report["kept_count"] == 1 and report["dropped_count"] == 0


def test_extract_claims_keeps_every_homonym_without_a_citable_set():
    known = frozenset({"pkg/widget.py:widget_helper", "pkg/legacy.py:widget_helper"})
    plain = extract_claims("Covers widget_helper.", "pkg/consumer.py", known)
    assert sorted(c.symbol for c in plain) == sorted(known)
    scoped = extract_claims("Covers widget_helper.", "tests/test_w.py", known,
                            citable_modules=frozenset({"tests/test_w.py", "pkg/widget.py"}))
    assert [c.symbol for c in scoped] == ["pkg/widget.py:widget_helper"]
    # No citable homonym at all: nothing is filtered, they fail downstream.
    unrelated = extract_claims("Covers widget_helper.", "tests/test_w.py", known,
                               citable_modules=frozenset({"tests/test_w.py"}))
    assert sorted(c.symbol for c in unrelated) == sorted(known)


# ── the parameter itself, at the gate ────────────────────────────────────────


def test_citation_check_parameter_defaults_to_the_own_module_rule():
    known = frozenset({"pkg/widget.py:widget_helper"})
    counts = {"tests/test_w.py": 5, "pkg/widget.py": 5}
    claim = Claim(text="x", module="tests/test_w.py", symbol="pkg/widget.py:widget_helper")
    assert citation_check(claim, known, counts) is not None
    assert citation_check(claim, known, counts, citable_modules=frozenset({"tests/test_w.py"})) is not None
    assert citation_check(
        claim, known, counts, citable_modules=frozenset({"tests/test_w.py", "pkg/widget.py"}),
    ) is None
