# HARDEN-22 — `Nothing major was found.`, `Nothing else to add.`, `Nothing is wrong.`, `Nothing needs fixing.`, `Not seeing any issues.` are rejections; `No contradictions. REVISE: fix the timeline.` is an approval; `Incorrect:` and `Nope` fail open; a dead constant

**Status:** landed (round 320: sensenova-6-8-flash-lite-var4 as the base — A: one `_EN_NOTHING_DENIAL` over the shared adjective, problem-word and `_EN_NOTHING_ACTIONS` lists approves `Nothing major was found`, `Nothing else to add`, `Nothing is wrong`, `Nothing needs fixing`, `Not seeing any issues` and the same after a leading `No,` with an empty reason; the HARDEN-14 rejections stay and a denial that goes on with a contrast or a named problem rejects; B: one `_EN_REJECT_HEAD` decides for a directive after a stop on the approval's own line and on a later line (`No contradictions. REVISE: …`, `Rejected -`, `Needs revision:`), plus my own finds `Not approved` and `Please revise`; C: `Incorrect` is a reject head and a reply that is the bare word `Nope` (any case) rejects, while `Nope, works` stays prose as HARDEN-10 C pinned it; D: the ticket was wrong, `_EN_NOT_NEGATES_APPROVAL` is read in `_EN_APPROVAL_HEAD` and stays; own finds `Nothing needs to change`, `Nothing remains to be fixed`, `Not finding any issues`, `Nothing is missing`, `Nothing is lacking`, `Nothing is off`)
**Severity:** MEDIUM (a clean chapter goes back to be "fixed" with its own approval as the reason; one rejection ships)
**File:** `tools/auto/inner_loop.py`
**Symbol:** `_parse_verdict_soft`, `_en_not_family_reason`, `_EN_NOTHING_OK`, `_EN_NOT_FILLERS`, `_EN_NO_PROBLEM_ADJ`, `_en_no_problem_verdict`, `_EN_NO_PROBLEM_CAVEAT`, `_EN_NOT_NEGATES_APPROVAL`
**Round:** 320
**Size:** S
**Depends on:** HARDEN-17 (315, the same function; land after it)
**Also touches:** `tests_bugfix/` pins of HARDEN-5 A, HARDEN-10 C, HARDEN-14 A (must stay green), `tests/`

---

## Why

Two outside reviews of `5c403f1` found these. Since HARDEN-14 A a reply that starts with `NO` / `NOT` / `NOTHING` is a rejection unless a problem word follows directly; the reviewer labelled 35 natural approvals and 20 were rejected, the reason handed to the Coder being the approval itself. All items were re-run on the head (`1e58233`) and are real.

---

## Part A — approvals written with NOTHING / NOT that are rejected

On the head, `_parse_verdict_soft(reply)[0]` is `False` for every line below; each is an approval:

```
Nothing major was found.            Nothing serious detected.
Nothing else to add.                Nothing to revise.
Nothing is wrong.                   NOTHING IS WRONG HERE.
NOTHING IS INCORRECT.               NOTHING SEEMS WRONG.
NOTHING IS PROBLEMATIC.             Nothing needs fixing.
Nothing looks inconsistent.         Nothing is out of character.
Not seeing any issues.              No, nothing is wrong with it.   (the reason is ", nothing is wrong with it.")
```

Causes: an adjective (`major`, `minor`, `serious`) between `NOTHING` and the problem word is only allowed after `NO`; `ELSE` interrupts `NOTHING (TO) <word>`; `IS`, `ARE`, `SEEMS`, `APPEARS`, `LOOKS`, `NEEDS` are not fillers, so the head word is not a problem word.

### What it does

* One place decides the NOTHING / NOT family (HARDEN-14's rule: do not add a second word list beside it): the adjective list of `_EN_NO_PROBLEM_ADJ` is shared with `NOTHING`; `NOTHING (ELSE) (TO|NEEDS) <verb>`; `NOTHING IS|ARE|SEEMS|APPEARS|LOOKS (<adverb>) <problem word or its negation>` and `NOT SEEING (ANY) <problem>` are approvals.
* The HARDEN-14 rejections stay: `Nothing matches`, `NOT APPROVED`, `Not ready`, `Not coherent`, `NOTHING IS RIGHT`, `NOTHING IS PERFECT`, `Nothing works`. A reply that goes on with a contrast (`Nothing is wrong, but …`) is a rejection (Part B's caveat rule).
* A leading `No,` before the approval keeps the reason clean: it never starts with a comma.

### Tests (sketch)

The lines above approve; the rejections listed stay rejections; the HARDEN-14 A table and `tests/test_verdict_review_q.py` green.

---

## Part B — `No contradictions. REVISE: fix the timeline.` is approved

`_en_no_problem_verdict` finds a `REVISE:` / `REJECT:` directive on a later **line** and a contrast or `;` on the same line, but not a directive after a period on the same line:

```
No contradictions. REVISE: fix the timeline.   -> approved (wrong)
No errors. REJECT: wrong character arc.        -> approved (wrong)
No issues; REVISE: fix X.                      -> rejected
No issues.\nREVISE: fix X.                     -> rejected
```

The directive is also missed when it is `REJECTED:` / `Rejected -` / `Needs revision:` (the words of HARDEN-17 A): `No issues found.\nREJECTED: the date breaks` is approved on every round-315 entry.

### What it does

After a "No <problem>" approval, any rejection word of HARDEN-17 A at the start of a sentence (after `.`, `!`, `?` or a line start) makes the reply a rejection with that sentence as the reason. One word list with Part A of 315.

---

## Part C — `Incorrect:` and `Nope` fail open

`Incorrect: the date is wrong.` and `Nope` have no verdict token and are passed on fail-open as approvals. They are rejections (the reason is what follows, or the reply itself for `Nope`). `Not sure`, `Incorrectly formatted but fine` stay as today (a judgement call: pin it in a test and say which way).

---

## Part D — a dead constant

`_EN_NOT_NEGATES_APPROVAL` (about line 1012) is assigned and never read since HARDEN-14 A (pyflakes). Delete it, keep its meaning in a comment next to the rule that replaced it.

---

## Contest

One commit. The judge's own checks are written after the round, from the entries, and counted against the head over a table in English and Russian; the entries' tests are cross-tested against every other entry's code and every diff is read.

## Acceptance

**Layer 1 — offline.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** A creative-mode run whose validator answers `Nothing is wrong.` ships the chapter instead of looping it back.

## Not in scope

Rust; the mask (HARDEN-15, 16, 19); Russian verdict words other than regressions.

---

## Why not the others (round 320)

A black box of 61 replies (the Part A approvals, the HARDEN-14 rejections that must stay, Part B directives after a stop, Part C, guards) scored the base 32, glm 49, nemotron 48 (it left a 2400-line `.backup` file), step-5 57, agnes 58, var3 59, var4 60; the ideal 61. var4 was the smallest entry that kept every pin of HARDEN-5 A, 10 C and 14 A green. var3 and agnes rewrote the HARDEN-1 A1 / 10 C pins wholesale (`NOPE` and `Nope, works`, the `unparseable` flag of the NOT-family approvals), which the ticket forbids; the only pin moved here is the bare `NOPE`, because Part C asks for it; step-5 deleted the constant Part D asked about and broke nothing, but its denials missed `Not finding` and `Nothing needs to be fixed`; glm's tests asserted the bug. A probe of 59 further replies found the gaps now folded in. The cross matrix showed no behaviour lead outside the `NOPE` choice and the shape of a reason.

Open for the next tickets: `Nothing here contradicts the outline.` and `No issues. Reject nothing.` are still read as rejections; a one-word `Nope` in a longer reply (`Nope, the date is wrong.`) is a fail-open approval until a pin of HARDEN-10 C is reopened.

Follow-up (after var1 and var2 finished): their tests found three holes in the landed ideal, now closed: `Not seeing anything wrong` / `anything amiss` ended the denial at `anything`, `Nothing needs a rewrite` / `any fixing` had no object slot, and a `NO,` / `NO;` token left its separator at the head of the reason. The rest of their failures are choices (`NOPE`, the deleted constant, `Nothing is not wrong`).
