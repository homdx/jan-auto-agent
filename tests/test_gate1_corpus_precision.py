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

What is permanent is the CLAIMS, not the evidence (FL-5). The corpus is
graded against the LIVE TREE: every tier passes ``REPO_ROOT`` — this
checkout — to the filter, so each verdict is recomputed from the current
text of the twelve cited symbols and their files' module docstrings. That is
on purpose: it is what proves Gate 1 still behaves on today's source. The
price is that an edit to a graded source can move a verdict — one word such
as "deliberately" in a cited symbol's docstring fires
``intentional_design_note`` (FL-1's ``MetricsCollector.record`` is one
word away from turning AUTO-T7 red), and a rename fails the existence check.
So the coupling is kept loud instead of hidden:

  * every cited symbol carries a one-line marker right above its ``def``
    naming its entry and this file (``_marker``), so whoever edits it sees
    it is graded — ``TestLiveCoupling`` fails if an entry's symbol lacks it;
  * every tier's failure names the entry, the file and the symbol and says
    the graded source changed (``_coupling``), rather than reading like a
    precision regression;
  * every entry must still resolve in the live tree
    (``test_every_entry_resolves_in_the_live_tree``) — including AUTO-T11,
    which no other tier would notice renamed: the pipeline tier counts a
    false positive rejected at Stage A as caught.

When one of these goes red after an edit to a graded source, re-read the
entry's claim against the new source. Do not reword the source to make the
test green.

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
import re
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import Gate1Filter

#: The live tree — the corpus's evidence (see "What is permanent" above).
REPO_ROOT = Path(__file__).resolve().parent.parent
_THIS_TEST = "tests/test_gate1_corpus_precision.py"
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
        # FL-5: on the live tree it IS rejected today, and not because the
        # gap closed — intentional_design_note (AUTO-H3-1, landed a day after
        # this corpus) fires on suppress's own "deliberately failing closed".
        # A word in the graded source moved this verdict; see
        # TestCorpusPrecisionRecall.
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


def _marker(entry: dict) -> str:
    """The line that sits right above *entry*'s ``def`` (above its decorators)
    in the graded file. ``extract_block`` starts at the decorator, so the
    marker is never part of the evidence it announces."""
    return f"# Gate 1 corpus {entry['id']} grades `{entry['symbol']}` live: {_THIS_TEST}"


def _coupling(entry: dict, what: str) -> str:
    """The failure message every tier uses for one entry: which source it
    grades, and that the source — not necessarily Gate 1 — is what moved."""
    return (
        f"{entry['id']} grades the live source of {entry['file']}::{entry['symbol']} "
        f"— {what}. If that source changed, re-read the entry's claim against it; "
        f"this is a Gate 1 regression only if tools/auto/gate1_*.py changed too. "
        f"Do not reword the source to turn this green."
    )


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

def _resolve(filt: Gate1Filter, entry: dict, root: Path) -> str:
    """Stage A for one entry against *root*: its code block, or a failure
    naming the source the entry grades."""
    ok, reason, block = filt._check_existence(_candidate(entry), root, cluster_files=None)
    assert ok, _coupling(entry, f"the symbol no longer resolves there ({reason})")
    return block


def _notes(filt: Gate1Filter, entry: dict, root: Path) -> str:
    """Stage A + A2 for one entry against *root*: the grounding notes."""
    candidate = _candidate(entry)
    block = _resolve(filt, entry, root)
    module_docstring = filt._module_docstring_for(candidate, root)
    return filt._build_grounding_notes(candidate, block, module_docstring, root)


def _check_expected_note(filt: Gate1Filter, entry: dict, root: Path) -> None:
    notes = _notes(filt, entry, root)
    assert notes, _coupling(entry, f"expected a {entry['note_kind']} grounding note, got none")
    if entry["note_kind"] == "config_fallback":
        assert "fallback=" in notes and "NoSectionError" in notes, _coupling(
            entry, f"the config_fallback note did not fire: {notes!r}")
    elif entry["note_kind"] == "module_docstring":
        assert "Module docstring" in notes, _coupling(
            entry, f"the module docstring note did not fire: {notes!r}")


def _check_legit_is_neutral(filt: Gate1Filter, entry: dict, root: Path) -> None:
    notes = _notes(filt, entry, root)
    assert "NOTE (automated" not in notes, _coupling(
        entry, f"an automated counter-note fired on a legitimate candidate: {notes!r}")
    assert not any(w in notes.lower() for w in _FIXTURE_SIGNAL_WORDS), _coupling(
        entry, "fixture-signal language leaked into a legitimate candidate's grounding "
               f"notes (docstring context should be neutral here): {notes!r}")


class TestGroundingNotesUnit:
    """Runs Stage A for real (against this actual repo checkout — the live
    tree, see the module docstring) then checks
    Gate1Filter._build_grounding_notes directly — no LLM involved."""

    @pytest.mark.parametrize("entry", _CORPUS, ids=lambda e: e["id"])
    def test_every_entry_resolves_in_the_live_tree(self, filt: Gate1Filter, entry: dict) -> None:
        """All twelve: a renamed or moved symbol fails here, by name. The
        pipeline tier alone would not notice it for a false-positive entry
        without a note (AUTO-T11) — rejected at Stage A still counts as caught."""
        assert _resolve(filt, entry, REPO_ROOT)

    @pytest.mark.parametrize(
        "entry", [e for e in _CORPUS if e["note_kind"] is not None],
        ids=lambda e: e["id"],
    )
    def test_expected_grounding_note_fires(self, filt: Gate1Filter, entry: dict) -> None:
        _check_expected_note(filt, entry, REPO_ROOT)

    @pytest.mark.parametrize("entry", [e for e in _CORPUS if e["label"] == "legit"], ids=lambda e: e["id"])
    def test_legit_candidates_carry_no_disqualifying_signal(self, filt: Gate1Filter, entry: dict) -> None:
        """A legitimate task's target file may still have an ordinary module
        docstring (e.g. tools/collect/gates.py's design-rationale docstring)
        — that's fine, it's just background context. What must NOT happen
        is (a) a config_fallback_note firing on a call that has no
        fallback= to justify it, or (b) fixture-signal language appearing
        in a docstring that was never written to say "this is a toy/
        deliberately-bad example"."""
        _check_legit_is_neutral(filt, entry, REPO_ROOT)


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
    # AUTO-H3: a "confirmed" verdict now has to back itself with a real
    # quote from the code block Stage B was shown (see gate1_filter's
    # evidence check) — pull the cited symbol's own def line straight out
    # of the prompt so this mock satisfies the same contract a real,
    # attentive model would.
    m = re.search(r"```\n(.*?)\n```", user_msg, re.S)
    code = m.group(1) if m else ""
    evidence = next((ln.strip() for ln in code.splitlines() if ln.strip()), "def ")
    return json.dumps({
        "verdict": "confirmed", "evidence": evidence,
        "reason": "no contradicting evidence found",
    })


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
    before touching the baseline) — or, since the corpus grades the live
    tree, a graded source changed: the failure names which entries moved
    and the sources they grade.

    Measured on the live tree today (FL-5): 6 of 6 caught, precision and
    recall 100%. AUTO-T11 is rejected by intentional_design_note on
    suppress's own "deliberately failing closed", not by closing the
    one-hop gap — a verdict moved by a word in a graded source, which the
    bounds below let through silently. The bounds are left as they were.
    """

    def test_corpus_precision_recall_meets_baseline(
        self, filt: Gate1Filter,
    ) -> None:
        _check_precision_recall(filt, REPO_ROOT)


def _check_precision_recall(filt: Gate1Filter, root: Path) -> None:
    """The pipeline tier against *root*. Every failure names the entries
    that moved and the sources they grade (``_coupling``)."""
    candidates = [_candidate(e) for e in _CORPUS]
    by_title = {e["title"]: e for e in _CORPUS}
    by_id = {e["id"]: e for e in _CORPUS}

    def named(ids: set, what: str) -> str:
        return "\n".join(_coupling(by_id[i], what) for i in sorted(ids))

    with patch("tools.llm_stream.request_completion", side_effect=_prompt_aware_llm):
        accepted, rejected = filt.filter(candidates, root, cluster_files=None)

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

    assert fp == set(), (
        f"Gate 1 wrongly rejected legitimate task(s) {sorted(fp)} "
        f"(precision {precision:.0%}):\n" + named(fp, "a legit entry was rejected")
    )
    assert precision == 1.0, f"precision dropped: {precision:.0%} (fp={fp})"
    assert tn == legit_ids, f"expected all legit tasks kept, kept={tn}"

    # Baseline: 5/6 false positives caught. If this regresses below 5,
    # something broke. If it improves to 6 (AUTO-T11's gap gets closed
    # some day), update this assertion to match — a stricter bound is
    # a welcome failure here, not a bug.
    assert len(tp) >= 5, (
        f"recall regressed: only caught {sorted(tp)} (recall={recall:.0%}):\n"
        + named(fn, "a false positive was no longer rejected")
    )
    assert "AUTO-T11" not in tp or len(tp) == 6, (
        "AUTO-T11 is now caught — great, update this test's baseline "
        "docstring and drop this guard, the one-hop gap has been closed."
    )


# ─────────────────────────────────────────────────────────────────────────────
# FL-5 — the live-tree coupling is visible, for all twelve entries
# ─────────────────────────────────────────────────────────────────────────────

def _graded_copy(tmp_path: Path) -> Path:
    """A scratch root holding a copy of every graded file at its own path.
    The real checkout is never edited."""
    for f in {e["file"] for e in _CORPUS}:
        dst = tmp_path / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / f, dst)
    return tmp_path


def _def_line(lines: list[str], entry: dict) -> int:
    for i, ln in enumerate(lines):
        if re.match(rf"\s*(?:async\s+)?def {re.escape(entry['symbol'])}\b", ln):
            return i
    raise AssertionError(_coupling(entry, "no def of the symbol is left in the file"))


class TestLiveCoupling:
    """The corpus grades the live tree; these prove that an edit to a graded
    source says so — in the file being edited and in the failure."""

    @pytest.mark.parametrize("entry", _CORPUS, ids=lambda e: e["id"])
    def test_graded_symbol_carries_the_marker(self, entry: dict) -> None:
        lines = (REPO_ROOT / entry["file"]).read_text(encoding="utf-8").splitlines()
        i = _def_line(lines, entry)
        while i > 0 and lines[i - 1].lstrip().startswith("@"):
            i -= 1
        above = lines[i - 1].strip() if i else ""
        assert above == _marker(entry), (
            f"{entry['file']}::{entry['symbol']} is graded live by corpus entry "
            f"{entry['id']} but the line above its def is not the marker — add:\n"
            f"    {_marker(entry)}\ngot: {above!r}"
        )

    def test_the_scratch_copy_grades_like_the_live_tree(
        self, filt: Gate1Filter, tmp_path: Path,
    ) -> None:
        """Control for the two tests below: the unedited copy passes every
        check, so a failure there is the edit and nothing else."""
        root = _graded_copy(tmp_path)
        for entry in _CORPUS:
            _resolve(filt, entry, root)
            if entry["note_kind"] is not None:
                _check_expected_note(filt, entry, root)
            if entry["label"] == "legit":
                _check_legit_is_neutral(filt, entry, root)
        _check_precision_recall(filt, root)

    def test_a_trigger_word_in_record_fails_naming_the_entry_file_and_symbol(
        self, filt: Gate1Filter, tmp_path: Path,
    ) -> None:
        """FL-1's hazard, replayed on a scratch copy: "deliberately" in
        MetricsCollector.record's own docstring. Both tiers go red, and each
        failure names AUTO-T7, the file and the symbol."""
        entry = next(e for e in _CORPUS if e["id"] == "AUTO-T7")
        root = _graded_copy(tmp_path)
        path = root / entry["file"]
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        doc = _def_line(lines, entry) + 1
        assert '"""' in lines[doc], f"{entry['symbol']} has no docstring to aim at"
        lines[doc] = lines[doc].replace('"""', '"""Deliberately bounded. ', 1)
        path.write_text("".join(lines), encoding="utf-8")

        needles = (entry["id"], f"{entry['file']}::{entry['symbol']}", "live source")
        for check in (lambda: _check_legit_is_neutral(filt, entry, root),
                      lambda: _check_precision_recall(filt, root)):
            with pytest.raises(AssertionError) as err:
                check()
            for needle in needles:
                assert needle in str(err.value), f"failure omits {needle!r}: {err.value}"

    @pytest.mark.parametrize("entry", _CORPUS, ids=lambda e: e["id"])
    def test_a_renamed_symbol_fails_naming_the_entry_file_and_symbol(
        self, filt: Gate1Filter, tmp_path: Path, entry: dict,
    ) -> None:
        root = _graded_copy(tmp_path)
        path = root / entry["file"]
        path.write_text(
            re.sub(rf"\bdef {re.escape(entry['symbol'])}\b", f"def {entry['symbol']}_moved",
                   path.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
        with pytest.raises(AssertionError) as err:
            _resolve(filt, entry, root)
        for needle in (entry["id"], f"{entry['file']}::{entry['symbol']}"):
            assert needle in str(err.value), f"failure omits {needle!r}: {err.value}"


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
