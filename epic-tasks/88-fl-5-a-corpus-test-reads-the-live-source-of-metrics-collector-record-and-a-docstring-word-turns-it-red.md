# FL-5 — `test_gate1_corpus_precision` grades the *live* source of `MetricsCollector.record`: one word in that docstring turns an unrelated test red

**Status:** landed `01977ed` (2026-09-25) — round 88. Was queued after FL-1 (84, landed `e500d40`), which rewrote the very function this corpus reads. Found while scoring round 84: one candidate lost two tests to it, and that was a real regression in the candidate rather than a flake — but the trap is in the test's design, not in the candidate.

**Severity:** LOW — no failure on the current tree; a landmine under whoever next edits `tools/metrics_collector.py`.
**File:** `tests/test_gate1_corpus_precision.py`.
**Symbol:** the `AUTO-T7` corpus entry (`file="tools/metrics_collector.py", symbol="record"`), `TestGroundingNotesUnit`, `TestCorpusPrecisionRecall`, `Gate1Filter._check_existence`, `Gate1Filter._build_grounding_notes`, `REPO_ROOT`.
**Round:** 88
**Size:** S
**Source:** `tests/test_gate1_corpus_precision.py:55` sets `REPO_ROOT = Path(__file__).resolve().parent.parent`, and every tier passes it to the filter — `_check_existence(candidate, REPO_ROOT, …)` at :246 and :268, `_build_grounding_notes(candidate, block, module_docstring, REPO_ROOT)` at :250 and :272, `filt.filter(candidates, REPO_ROOT, …)` at :350. Twelve corpus entries name real files and symbols in this repository; `AUTO-T7` (:157–165) is `tools/metrics_collector.py::record`, labelled `legit` with `note_kind=None` — that is, the test asserts the gate does **not** fire a grounding note on it.

FL-1's family A rewrote that exact function: `record()` now owns its own write lock and fsyncs on a bounded 0.5 s cadence. The corpus test was verified green on `e500d40`, and `e500d40`'s own commit message records the hazard: adding "deliberately" or "by design" to that docstring makes `intentional_design_note` fire and turns `AUTO-T7` red. Round 84's `kc37-Dots3-note` hit exactly that.

**Depends on:** FL-1 (84) only for ordering — the function is already rewritten.
**Also touches:** possibly a fixture directory for pinned sources, if that is the shape chosen.

---

## What happens today

The corpus is a good idea carried out in a way that couples two unrelated parts of the repo. Its docstring explains the intent well: twelve claims about real files, each verified by hand, six confirmed false positives and six legitimate, frozen as a permanent fixture so Gate 1's precision can never silently regress. The two tiers are honest about what each proves — the unit tier checks `_build_grounding_notes` directly with no LLM, the pipeline tier runs `filter()` end to end with a mock that only rejects when the prompt it actually received carries the grounding-notes marker.

What is not stated anywhere is that **the fixture is not frozen**. Only the *claims* are checked in; the evidence they are graded against is read live off the working tree at `REPO_ROOT`. So the test's result depends on the current text of twelve source files, including their docstrings, and on nothing recording that dependency.

The consequences are all of the "surprising red in an unrelated module" kind:

- Editing a docstring in any of the twelve files can flip a `legit` entry to a fired note or a `false-positive` entry to a silent one. The word list that triggers `intentional_design_note` is the specific hazard called out for `AUTO-T7`, and a docstring saying a design is deliberate is exactly what a careful author writes when they have just made a deliberate design decision — as FL-1 did.
- Renaming or moving any of the twelve symbols makes `_check_existence` fail on a candidate the corpus asserts exists, in a test whose name is about Gate 1 precision and whose failure says nothing about the rename.
- A future FL-1-shaped rewrite of one of those functions has no way to know it is graded. `git grep metrics_collector tests/` finds it, but nobody greps for that before editing a docstring.

The blast radius is small and the fix is not obvious — which is why this is S and LOW rather than something to fold into a bigger round.

## What must change

Decide, and write down, whether the corpus grades **pinned evidence** or **the live tree**, and make the test say so. Both are defensible; what is not defensible is the current state, where it does the second while reading like the first.

**Option A — pin the evidence.** Copy the twelve relevant source blocks (and module docstrings) into a fixture directory at the revision they were verified against, point `REPO_ROOT` at it, and the corpus becomes what its docstring already claims: a frozen regression fixture that measures Gate 1 and only Gate 1. Cost: the corpus stops proving that the gate behaves on *today's* source, and a drift check is needed so the pinned copies do not silently become fiction.

**Option B — keep it live, and make the coupling loud.** Keep `REPO_ROOT`, but (a) the twelve files carry a one-line marker naming this test as a grader, so an editor sees it in the file they are editing; (b) a failure message names the coupling explicitly — "candidate AUTO-T7 grades the live source of `tools/metrics_collector.py::record`; that source changed" — instead of an assertion about precision; (c) the test's docstring states the dependency in the *Where this corpus came from* section, which currently implies the opposite.

Option B is the smaller change and keeps the property that makes the corpus worth having. Whichever is chosen, `AUTO-T7` must not be the only entry handled: all twelve have the same coupling and the ticket is about the shape, not about the one entry that got caught.

## Acceptance

- [ ] The test's docstring states plainly whether the corpus is graded against pinned evidence or the live tree, and the code matches the statement.
- [ ] A test proves the coupling is now visible: with a scratch copy of one graded source whose docstring gains the word "deliberately", the corpus test's failure message names the file, the symbol and the corpus entry — or, under option A, the corpus is unaffected and a separate drift check reports the divergence.
- [ ] All twelve entries are covered by whatever mechanism is chosen, not only `AUTO-T7`.
- [ ] Gate 1's measured precision/recall on the corpus is unchanged from the current tree — this ticket does not move the number, and a candidate that improves it has changed the wrong thing.
- [ ] Under option B, each of the twelve graded files carries the marker, and a test fails if a corpus entry names a file that lacks it.
- [ ] `test_corpus_against_real_model` stays skipped by default and still enables under `GATE1_CORPUS_LIVE=1`.
- [ ] `pytest tests -n 4` then `pytest tests_bugfix -n 4`, sequentially, both green; `python3 scripts/sync_test_tiers.py --check` clean.

## Out of scope

- Changing Gate 1's behaviour, its prompt, `intentional_design_note`'s word list, or the measured precision/recall.
- Adding, removing or relabelling corpus entries. The twelve were verified by hand in a review session that is not being repeated here.
- Editing any of the twelve graded source files to avoid the trigger. Rewording `MetricsCollector.record`'s docstring so a test stays green is the failure mode this ticket exists to remove.
- Weakening the pipeline tier's mock, which deliberately rejects only when the prompt it received carries the grounding-notes marker.
- `tools/auto/collect_bridge.py`. `CollectBridge._shrink` stays byte-identical.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version`; no backslash and no nested same-quote inside an f-string expression.
- [ ] Exactly one commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tests/test_gate1_corpus_precision.py`, any new fixture directory, and marker lines in the graded sources if option B is chosen. No behaviour change in `tools/collect/`. Never `epic-tasks/`.
- [ ] The coupling test above fails without the change.
- [ ] Both test roots green, sequentially.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the **sha** of the one commit — not `HEAD`; hand in `git format-patch <base>..HEAD`.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
