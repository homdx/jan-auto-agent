"""tests/test_gate1_corpus_precision.py — AUTO-H2-5: false-positive
regression corpus + precision/recall gate for Gate 1.

Where this corpus came from
----------------------------
Every candidate below is a REAL claim about REAL files in this repository,
each one individually verified by hand against the actual source during a
manual review session (see JIRA epic AUTO-H2). Six were confirmed false
positives; six were confirmed legitimate. They are encoded here exactly the
way this codebase already encodes every other found-bug-turned-regression-
test (e.g. ``test_loc_degrades_to_zero_on_undecodable_source_instead_of_
raising``, ``test_undecodable_test_file_is_skipped_without_crashing``): the
specific incident becomes a permanent fixture so it can never silently
regress.

Two tiers
---------
1. ``TestGroundingNotesUnit`` — deterministic, no LLM at all. Directly
   checks ``Gate1Filter._build_grounding_notes`` fires (or doesn't) for
   each corpus candidate. This is what actually proves AUTO-H2-1/-2/-3's
   logic is correct; it needs no network and can't flake.

2. ``TestCorpusPrecisionRecall`` — runs the FULL ``Gate1Filter.filter()``
   pipeline (existence -> grounding -> presence) end to end, with Stage B's
   LLM call mocked by a function that reads the actual constructed prompt
   and answers the way a model that pays attention to injected grounding
   evidence should. This is deliberately not a rubber-stamp mock: it only
   rejects a candidate when the prompt it was actually sent contains the
   grounding-notes marker text, so a regression in prompt construction
   (e.g. grounding_notes silently stops being injected) fails this test
   even though no assertion here mentions that plumbing directly.

   This tier proves the pipeline wiring; it is NOT a substitute for running
   the corpus against a real model (see ``test_corpus_against_real_model``,
   skipped by default — enable with GATE1_CORPUS_LIVE=1 and a real
   [api_local]/[api_remote] config to get the actual number this was all
   built to produce: does a real model, with grounding notes in front of
   it, actually do better than one without?
"""

from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import Gate1Filter

REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE_SIGNAL_WORDS = ("toy module", "deliberately", "negative case", "control case")


# ─────────────────────────────────────────────────────────────────────────────
# The corpus
# ─────────────────────────────────────────────────────────────────────────────
# label: "false" = confirmed false positive, "legit" = confirmed real bug.
# `note_kind` documents which grounding check (if any) is expected to fire —
# used only by TestGroundingNotesUnit to assert the RIGHT reason fired, not
# just any reason.

_CORPUS: list[dict] = [
    # ── Confirmed false positives ───────────────────────────────────────────
    dict(
        label="false", note_kind="config_fallback", id="AUTO-T1",
        title="Handle missing config sections in make_progress_display",
        instruction=(
            "make_progress_display calls config.getint('auto', "
            "'max_rounds_per_task', ...) without guarding against a config "
            "that has no [auto] section at all. If config is a ConfigParser "
            "with no [auto] section, getint raises NoSectionError."
        ),
        file="tools/auto/progress_display.py", symbol="make_progress_display",
    ),
    dict(
        label="false", note_kind="config_fallback", id="AUTO-T2",
        title="Validate config sections in setup_run_trace",
        instruction=(
            "setup_run_trace calls config.getboolean('trace', 'enabled', ...) "
            "without checking that the [trace] section exists. If the config "
            "has no [trace] section, this raises NoSectionError."
        ),
        file="tools/auto/run_trace.py", symbol="setup_run_trace",
    ),
    dict(
        label="false", note_kind="config_fallback", id="AUTO-T3",
        title="Guard RepoIngestor against missing [architect]/[search] sections",
        instruction=(
            "RepoIngestor.__init__ calls self._read_skip_dirs() which reads "
            "from [search] and [architect] sections. If neither section "
            "exists, this raises NoSectionError."
        ),
        file="tools/auto/repo_ingest.py", symbol="_read_skip_dirs",
    ),
    dict(
        label="false", note_kind="module_docstring", id="AUTO-T8",
        title="Add logging to fail-open except block in read_optional",
        instruction=(
            "read_optional silently passes on KeyError. Add a logger.warning "
            "call so this fail-open behavior is observable."
        ),
        file="tests/fixtures/collect_mini_repo/pkg/error_handling.py", symbol="read_optional",
    ),
    dict(
        label="false", note_kind="module_docstring", id="AUTO-T9",
        title="Add input validation to last_item in unguarded.py",
        instruction=(
            "last_item performs an unguarded items[-1] access that raises "
            "IndexError on an empty list. Add an explicit empty-input check."
        ),
        file="tests/fixtures/collect_mini_repo/pkg/unguarded.py", symbol="last_item",
    ),
    dict(
        label="false", note_kind=None, id="AUTO-T11",
        title="Validate BughuntCandidate location format in suppress",
        instruction=(
            "suppress passes candidate.location straight to model.is_safe "
            "with no format validation; a malformed location may crash "
            "model.is_safe."
        ),
        file="tools/collect/bughunt_filter.py", symbol="suppress",
        # note_kind=None: this is the one confirmed false positive that
        # needs two call-hops (suppress -> is_safe -> query) to disprove,
        # deeper than AUTO-H2-3's one-hop callee_context reaches. Left in
        # the corpus deliberately UNCAUGHT by grounding-notes, so this test
        # documents the known gap instead of silently pretending it's
        # solved. TestCorpusPrecisionRecall's mock therefore treats this
        # one as "confirmed" (Gate 1 does NOT catch it today) and recall is
        # computed accordingly — see that class's docstring.
    ),
    # ── Confirmed legitimate (must NOT be suppressed by grounding notes) ───
    dict(
        label="legit", note_kind=None, id="AUTO-T4",
        title="Handle read errors and encoding fallback failures in read_file",
        instruction=(
            "read_file catches UnicodeDecodeError for the UTF-8 attempt but "
            "not other OSError subclasses, and the latin-1 fallback read has "
            "no exception handling at all."
        ),
        file="tools/file_reader.py", symbol="read_file",
    ),
    dict(
        label="legit", note_kind=None, id="AUTO-T6",
        title="Guard OutputFormatter.render against missing or malformed input",
        instruction=(
            "render accesses parsed.target_type and improvement.get(...) "
            "without validating that parsed has the required attributes or "
            "that improvement is actually a dict."
        ),
        file="tools/formatter.py", symbol="render",
    ),
    dict(
        label="legit", note_kind=None, id="AUTO-T7",
        title="Validate RunRecord fields in MetricsCollector.record",
        instruction=(
            "record calls asdict(run) without validating that run is a "
            "RunRecord instance, before any error handling is in scope."
        ),
        file="tools/metrics_collector.py", symbol="record",
    ),
    dict(
        label="legit", note_kind=None, id="AUTO-T12",
        title="Guard loader against malformed artifact fields with per-record handling",
        instruction=(
            "_load_from_dir wraps the entire record-construction block in a "
            "single try/except that discards the whole artifact on any one "
            "bad record, instead of skipping just that record."
        ),
        file="tools/collect/loader.py", symbol="_load_from_dir",
    ),
    dict(
        label="legit", note_kind=None, id="AUTO-T13",
        title="Validate GateEntry config_default against fail_mode semantics",
        instruction=(
            "GateEntry.__post_init__ validates fail_mode but not that "
            "config_switch is non-empty when extra_llm_call is True."
        ),
        file="tools/collect/gates.py", symbol="__post_init__",
    ),
    dict(
        label="legit", note_kind=None, id="AUTO-T5",
        title="Validate skip_dirs parameter type in list_py_files",
        instruction=(
            "list_py_files accepts skip_dirs: list but never validates it is "
            "actually a list, so passing None raises an opaque TypeError "
            "deep inside set()."
        ),
        file="tools/file_reader.py", symbol="list_py_files",
    ),
]


def _candidate(entry: dict) -> CandidateTask:
    return CandidateTask(
        title=entry["title"],
        instruction=entry["instruction"],
        target_files=[entry["file"]],
        acceptance_check="true",
        cited_location=CitedLocation(file=entry["file"], symbol=entry["symbol"]),
        cluster="corpus",
    )


@pytest.fixture()
def minimal_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url": "http://localhost:1337/v1", "api_key": "test",
            "model": "test-model", "api_format": "openai",
        },
        "gate1": {"temperature": "0.0", "max_tokens": "512", "skip_llm": "false"},
        "loop":  {"timeout_seconds": "10"},
    })
    return cfg


@pytest.fixture()
def filt(minimal_config: configparser.ConfigParser) -> Gate1Filter:
    return Gate1Filter(
        config=minimal_config, base_url="http://localhost:1337/v1",
        api_key="test", model="test-model", api_format="openai", verify_ssl=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1 — deterministic, no LLM: does grounding fire on the RIGHT reason?
# ─────────────────────────────────────────────────────────────────────────────

class TestGroundingNotesUnit:
    """Runs Stage A for real (against this actual repo checkout) then checks
    Gate1Filter._build_grounding_notes directly — no LLM involved."""

    @pytest.mark.parametrize(
        "entry", [e for e in _CORPUS if e["note_kind"] is not None],
        ids=lambda e: e["id"],
    )
    def test_expected_grounding_note_fires(self, filt: Gate1Filter, entry: dict) -> None:
        candidate = _candidate(entry)
        ok, reason, block = filt._check_existence(candidate, REPO_ROOT, cluster_files=None)
        assert ok, f"existence check failed for {entry['id']}: {reason}"

        module_docstring = filt._module_docstring_for(candidate, REPO_ROOT)
        notes = filt._build_grounding_notes(candidate, block, module_docstring, REPO_ROOT)

        assert notes, f"{entry['id']}: expected a grounding note, got none"
        if entry["note_kind"] == "config_fallback":
            assert "fallback=" in notes and "NoSectionError" in notes
        elif entry["note_kind"] == "module_docstring":
            assert "Module docstring" in notes

    @pytest.mark.parametrize("entry", [e for e in _CORPUS if e["label"] == "legit"], ids=lambda e: e["id"])
    def test_legit_candidates_carry_no_disqualifying_signal(self, filt: Gate1Filter, entry: dict) -> None:
        """A legitimate task's target file may still have an ordinary module
        docstring (e.g. tools/collect/gates.py's design-rationale docstring)
        — that's fine, it's just background context. What must NOT happen
        is (a) a config_fallback_note firing on a call that has no
        fallback= to justify it, or (b) fixture-signal language appearing
        in a docstring that was never written to say "this is a toy/
        deliberately-bad example"."""
        candidate = _candidate(entry)
        ok, reason, block = filt._check_existence(candidate, REPO_ROOT, cluster_files=None)
        assert ok, f"existence check failed for {entry['id']}: {reason}"

        module_docstring = filt._module_docstring_for(candidate, REPO_ROOT)
        notes = filt._build_grounding_notes(candidate, block, module_docstring, REPO_ROOT)

        assert "NOTE (automated" not in notes, (
            f"{entry['id']}: config_fallback_note fired on a legitimate candidate: {notes!r}"
        )
        assert not any(w in notes.lower() for w in _FIXTURE_SIGNAL_WORDS), (
            f"{entry['id']}: fixture-signal language leaked into a legitimate candidate's "
            f"grounding notes (docstring context should be neutral here): {notes!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Tier 2 — full pipeline, Stage B mocked but prompt-aware
# ─────────────────────────────────────────────────────────────────────────────

def _prompt_aware_llm(*, payload: dict, **_kwargs) -> str:
    """Stand-in for a model that actually reads injected grounding evidence
    — NOT a model that treats "a module docstring was shown" as automatic
    grounds for rejection (an earlier version of this mock did exactly
    that and immediately caught a real bug: AUTO-T12/AUTO-T13 target files
    that have substantial, perfectly ordinary module docstrings with zero
    fixture-signal language, and a naive presence-only check flagged them
    as false positives too). This mirrors the explicit instruction the real
    prompt template now gives Stage B: only treat a docstring as
    disqualifying when it actually SAYS the code is deliberately bad, not
    merely because a docstring exists.

    - config_fallback_note rejection: any prompt containing that marker is
      always correct to reject — the note is only ever injected when the
      exact cited call already has fallback=, which is unconditionally
      disqualifying, no reading comprehension required.
    - module docstring rejection: only when the injected docstring text
      itself contains fixture-signal language.
    """
    user_msg = payload["messages"][-1]["content"]
    if "NOTE (automated" in user_msg:
        return json.dumps({"verdict": "rejected", "reason": "config call already has fallback="})
    if "Module docstring for this file" in user_msg and any(
        w in user_msg.lower() for w in _FIXTURE_SIGNAL_WORDS
    ):
        return json.dumps({"verdict": "rejected", "reason": "docstring marks this as an intentional fixture"})
    return json.dumps({"verdict": "confirmed", "reason": "no contradicting evidence found"})


class TestCorpusPrecisionRecall:
    """End-to-end Gate1Filter.filter() over the whole corpus.

    Expected outcome given today's implementation: 5 of 6 false positives
    caught (AUTO-T1/T2/T3 via config_fallback_note, AUTO-T8/T9 via module
    docstring), AUTO-T11 NOT caught (documented one-hop-too-shallow gap,
    see its corpus entry), all 6 legit tasks correctly kept. That gives:

        precision = TP / (TP + FP) = 5 / (5 + 0)  = 100%
        recall    = TP / (TP + FN) = 5 / (5 + 1)  ≈ 83%

    If this test's numbers change, either the corpus grew (good — update
    the baseline below) or a real regression happened (bad — investigate
    before touching the baseline).
    """

    def test_corpus_precision_recall_meets_baseline(
        self, filt: Gate1Filter,
    ) -> None:
        candidates = [_candidate(e) for e in _CORPUS]
        by_title = {e["title"]: e for e in _CORPUS}

        with patch("tools.llm_stream.request_completion", side_effect=_prompt_aware_llm):
            accepted, rejected = filt.filter(candidates, REPO_ROOT, cluster_files=None)

        accepted_ids = {by_title[c.title]["id"] for c in accepted}
        rejected_ids = {by_title[r.candidate.title]["id"] for r in rejected}
        assert accepted_ids | rejected_ids == {e["id"] for e in _CORPUS}

        false_ids = {e["id"] for e in _CORPUS if e["label"] == "false"}
        legit_ids = {e["id"] for e in _CORPUS if e["label"] == "legit"}

        tp = rejected_ids & false_ids          # correctly caught false positives
        fn = accepted_ids & false_ids          # false positives that slipped through
        fp = rejected_ids & legit_ids          # legit tasks wrongly rejected
        tn = accepted_ids & legit_ids          # legit tasks correctly kept

        precision = len(tp) / (len(tp) + len(fp)) if (tp or fp) else float("nan")
        recall    = len(tp) / (len(tp) + len(fn)) if (tp or fn) else float("nan")

        assert fp == set(), f"Gate 1 wrongly rejected legitimate task(s): {fp}"
        assert precision == 1.0, f"precision dropped: {precision:.0%} (fp={fp})"
        assert tn == legit_ids, f"expected all legit tasks kept, kept={tn}"

        # Baseline: 5/6 false positives caught. If this regresses below 5,
        # something broke. If it improves to 6 (AUTO-T11's gap gets closed
        # some day), update this assertion to match — a stricter bound is
        # a welcome failure here, not a bug.
        assert len(tp) >= 5, f"recall regressed: only caught {sorted(tp)} (recall={recall:.0%})"
        assert "AUTO-T11" not in tp or len(tp) == 6, (
            "AUTO-T11 is now caught — great, update this test's baseline "
            "docstring and drop this guard, the one-hop gap has been closed."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Optional — real model, real network. Opt-in only.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(
    os.environ.get("GATE1_CORPUS_LIVE") != "1",
    reason="set GATE1_CORPUS_LIVE=1 and a working agents.ini to run the corpus "
           "against a real model instead of the prompt-aware mock",
)
def test_corpus_against_real_model() -> None:
    """Not run in CI. This is the number the whole AUTO-H2 epic exists to
    move: precision/recall of a REAL model's Stage B verdicts, with
    grounding notes on, against this corpus. Run locally with:

        GATE1_CORPUS_LIVE=1 python -m pytest tests/test_gate1_corpus_precision.py::test_corpus_against_real_model -q
    """
    import configparser as _cp
    from tools.auto.gate1_filter import filter_candidates

    cfg = _cp.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(REPO_ROOT / "agents.ini", encoding="utf-8")
    candidates = [_candidate(e) for e in _CORPUS]
    accepted, rejected = filter_candidates(candidates, REPO_ROOT, cfg)
    print(f"\nLive corpus run: {len(accepted)} accepted, {len(rejected)} rejected")
    for r in rejected:
        print(f"  REJECTED {r.candidate.title!r}: {r.reason}")
