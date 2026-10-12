# CC-12 — callers in the evidence pack, so the Gate 1 vote can confirm a candidate and not only veto it

**Status:** FINAL, open — the numbers are from the first live run of CC-10's layer 2 (run `20261011T205848Z`,
2026-10-11/12, `docs/claim-check/GATE1-VOTE-LAYER2.md` section 6). Cut it into `epic-tasks/` with the first word `open` when
the round starts. Whether the round is needed at all is decided by the second layer-2 run (the `+veto` and `+notests`
variants, already running): if they leave the vote no worse than the single reader and still deciding a third, this ticket
turns an optional improvement into a plain backlog item; if not, it is the next ticket of the epic.
**Severity:** MEDIUM (it is what keeps CC-10's vote switched off)
**File:** `tools/claimcheck/pack.py`, `tools/auto/gate1_vote.py`
**Symbol:** `build_pack` (a new `callers` budget), `Gate1Vote._presence_by_vote` (`[gate1] vote_callers`)
**Round:** —
**Size:** M
**Depends on:** CC-5 (the pack, landed), CC-10 (the vote and the layer-2 instrument, landed)
**Also touches:** `scripts/gate1_vote_layer2.py` (a `+callers` variant), `agents.ini` (`[gate1]`), `tests/test_claimcheck_pack_callers.py`, `docs/claim-check/GATE1-VOTE-LAYER2.md`

---

## Why

CC-10 put a vote of three to eleven models in front of Gate 1's single reader. Its first live measurement
(56 candidates the Architect wrote for the goal "return a copy instead of the live object", 4 real, 52 false positives;
answers settled by reading the code) said:

| | result |
|---|---|
| the vote's unanimous FALSE | right **8 of 8** (4 + 3 + 1 + 0 over the four runs of 3, 5, 7, 9 voters) |
| the vote's unanimous TRUE | wrong **8 of 10** with three voters, up to 18 of 20 with nine |
| more voters | decide more (10, 20, 18, 21 of 53) and are no safer; false positives through 27, 30, 31, 32 against 26 without the vote |

So the vote is a good veto and a poor confirmation here. The wrong TRUEs have two causes, both read off the candidates:

1. About half are "add a test that …" candidates citing a test file; the claim is true (the test does not assert it) and
   the task is worthless. That is a matter of what to ask, and the cheap answers (`vote_skip_tests`, `vote_confirm = off`)
   are already in CC-10's code and being measured.
2. The rest are a method that *looks* wrong alone and is neutralised by its caller: `MetricsCollector._load_all_cached`
   returns its cache list, and its one caller copies it. The pack of CC-10 is the cited block and its git header and
   nothing else (CC-10 question 3, "no callers in the first version, a bigger pack is a later measured change"). The
   repository's own adjudication guide says it plainly: *"Most false positives in this codebase are shapes that look wrong in
   isolation and are neutralised one frame up."*

This ticket is the "bigger pack": the callers.

## What it does

* `build_pack(…, budget)` gains `PackBudget.callers` (default **0**: every existing pack is byte for byte what it is). With
  `callers = N`, for each resolved *symbol* anchor in a Python source file the builder finds call sites in the other `.py`
  files of the view and adds at most N chunks of them: the call line and a few lines around it, id `src:<path>:a-b` like any
  source chunk (so a quote taken from a caller is verified by the same code as a quote from the definition), a `why`
  marker `caller of <symbol>`, ranked after the definition and the git header, trimmed by the pack's existing budget.
* Finding a call site is the cheap, deterministic kind: an `ast` walk for `Call` nodes whose callee name is the symbol
  (`name(…)` or `….name(…)`), in files other than the definition's, tests excluded (a test is not the code's own
  caller), at most one site per file, nearest-looking first (same package, then by path). No LLM, no network, never raises:
  a file that does not parse is skipped, a view that cannot list files gives the pack it gives today.
* `[gate1] vote_callers = N` (default 0) hands the budget to `Gate1Vote`; with 0 nothing changes. The claim text is
  untouched (the claim wording question — the instruction is the *fix*, not the problem — is not this ticket's).
* `scripts/gate1_vote_layer2.py` takes a `+callers` option (`vote_callers = 3`) so the known-answer set measures it in the
  same table as every other variant.

## Tests (sketch)

`tests/test_claimcheck_pack_callers.py`, no network: a fixture repository of five small modules —
the definition returns its internal list, one caller copies it, one caller mutates it, one caller is a test, one file does
not parse. `callers = 0` gives the old pack byte for byte; `callers = 2` adds the two production call sites (not the test,
not the broken file) with `src:` ids and the `caller of` marker; the chunk budget still holds; a quote taken from a caller
chunk is verified and one invented is downgraded (`ask_with_packs`, real reply shapes: a quote over two lines, separate JSON
objects); a method called from nowhere gets no caller chunk and no note that is not a note today.

## Acceptance (two layers)

**Layer 1 — offline, in the round (no network, no key).**
* `tests/test_claimcheck_pack_callers.py` as above; the existing CC-5 pack tests and all of `tests/`, `tests_bugfix/` green
  unchanged (two roots, one after the other).
* Black box (written before the round, `contest-bench/294/blackbox_294.py`, a fake model server like boxes 1–3): the vote with
  `vote_callers = 3` on a repository where a candidate's definition returns its list and the caller copies it — three
  scripted voters that answer FALSE **only** when a `caller of` chunk is in the pack. With `callers = 0` the case is TRUE
  (today's behaviour, pinned); with `callers = 3` the voters see the caller and the candidate is rejected. A caller chunk that
  is a test file is never in the pack.

**Layer 2 — the operator, live (no new tooling).**
`python3 scripts/gate1_vote_layer2.py --detach --sets 3+callers,3+callers+notests,5+callers+notests` on the recorded
known-answer set. Pass: the vote's own confirmations are wrong at most 1 in 5 (today 8 in 10), false positives through
not more than the **better** of the two baselines, no real bug rejected, and the vote decides at least a third.
Anything less leaves `claim_vote = off`, and the layer-2 guide says which of the three changes was tried.

## Contest

A mini contest, **Opus, Sonnet and Haiku by hand** (the same shape as CC-9 … CC-11): the change is about two hundred lines in
two files plus tests, its acceptance is deterministic and offline (the unit tests and the box), and the risk is the
call-site finder and the budget interplay, not anything a free model's coding will settle. Free models are worth adding only
as a second, optional round if the cloud round leaves a question — in CC-10 the free models produced a passing entry for the
vote itself, but the winning patch was the cloud one, with a sixth of the change to the sensitive file.

## Questions

1. **Why not give the voters the callers' bodies whole.** The pack has a budget (6000 characters, 6 chunks) that the fixture
   measurements were made with; a caller is a window of the call site, not a function.
2. **Why Python only.** The target set is Python; a candidate in another language gets today's pack. A language-agnostic
   finder is a different ticket.
3. **What if the callers make the vote worse.** Then the `+callers` variants fail the rule above and the flag stays `off`; the
   ticket's value is the measurement either way.
4. **The claim wording.** "`<symbol>` in `<file>` has the problem: *<the fix>*" asks whether the premise of the task is
   present. A restated question ("does the cited code have the defect this task would fix") is the other half of the wrong
   TRUEs for non-test candidates; it needs its own measurement and is left for the layer-2 numbers to ask for.

## Not in scope

Changing the unanimity rule, the quorum, the families rule, `classify_delta`, Lenz, the Architect (a better planner would
also fix this at the source), or the default of `claim_vote`.
