# HARDEN-25 — `Nothing here contradicts the outline.` and `No issues. Reject nothing.` are read as rejections; `Nope, the date is wrong.` is a fail-open approval

**Status:** landed (round 323: sensenova-6-8-flash-lite-var3 as the base - A: `Nothing` / `No <noun>` + an optional place (`here`, `in the draft`, `anywhere`) + a verb of conflict is a denial that approves with an empty reason, `It / This / <the noun> contradicts the outline` rejects; B: a directive head (`Reject`, `Revise`, `Fix`, `Change`) after an approval phrase is the verdict unless its object is `nothing`, `none` or `no <noun>`; C: `Nope, <clause that names a problem>` rejects with the clause as the reason, `Nope, works` keeps its HARDEN-10 C reading; my own finds: a contrast word after the object of a conflict denial (`...contradicts the outline but the ending fails`), `in it` / `in them` as a place, and a `Nope,` clause that names its problem with a conflict verb, `error` or `not right` while a denied problem (`Nope, not bad`) stays prose)
**Severity:** MEDIUM (A, B: a clean review is sent back to the Coder in a loop; C: a rejection that says why is passed as an approval)
**File:** `tools/auto/inner_loop.py`
**Symbol:** `_parse_verdict_soft`, `_EN_NOTHING_DENIAL`, `_EN_NOTHING_ACTIONS`, `_EN_REJECT_HEAD`
**Round:** 323
**Size:** S
**Depends on:** HARDEN-22 (the shared NOTHING / NOT lists this extends)
**Also touches:** `tests/test_harden22_verdict_nothing_denials.py`, `tests_bugfix/test_harden10_verdict_not_family_308.py` (the `Nope, works` pin stays), `tests/`

---

## Why

Three leftovers of HARDEN-22 were recorded in its ticket as "open for the next tickets"; each was re-run on the current head (`3d3cc61`) and reproduced:

```
_parse_verdict_soft("Nothing here contradicts the outline.")  -> (False, 'Nothing here contradicts the outline.', False)
_parse_verdict_soft("No issues. Reject nothing.")              -> (False, 'nothing.', False)
_parse_verdict_soft("Nope, the date is wrong.")                -> (True, 'verdict unparseable — passed on fail-open', True)
```

---

## Part A — `Nothing here contradicts / conflicts with / breaks <X>.` is an approval

### What happens today

`Nothing contradicts the outline.` is read as prose (fail-open approval, `unparseable=True`) and the same sentence with `here`, `in the draft` or `in the plan` between `Nothing` and the verb is read as a rejection with the whole sentence as the reason. The denial word `Nothing` is followed by a place phrase, and the NOTHING shape knows adjectives, auxiliaries and gap words but not a place.

### What it does

`Nothing` (and `No <noun>`) followed by an optional place phrase (`here`, `there`, `in <the> <noun>`, `anywhere`) and a verb of conflict (`contradicts`, `conflicts with`, `breaks`, `violates`, `disagrees with`, `clashes with`, `undermines`) is a denial: the verdict approves with an empty reason and no `unparseable` flag. A denial that goes on with a contrast or a named problem (`Nothing here contradicts the outline, but the ring is stolen.`) still rejects.

### Tests

The three phrasings above approve; `Nothing here contradicts the outline, but the date is wrong.` rejects; `It contradicts the outline.` rejects; the HARDEN-22 table is green unchanged.

---

## Part B — `Reject nothing.` / `Nothing to reject.` after an approval phrase

### What happens today

`No issues. Nothing to reject.` approves, `No issues. Reject nothing.` is a rejection whose reason is `nothing.`: after the stop, the directive head `Reject` is taken as the verdict and `nothing.` as its reason.

### What it does

A directive head (`Reject`, `Revise`, `Fix`, `Change`) whose object is `nothing`, `none` or `no <noun>` is not a directive; the line stays an approval.

### Tests

`No issues. Reject nothing.`, `Looks good. Revise nothing.`, `LGTM. Fix nothing.` approve with an empty reason; `No issues. Reject this.` and `Looks good. Fix the date.` still reject with the sentence as the reason.

---

## Part C — `Nope, <a clause that names a problem>` is a fail-open approval

### What happens today

HARDEN-22 made a bare `Nope` a rejection and kept the HARDEN-10 C pin that `Nope, works` is prose. A longer reply that starts with `Nope` and then names the problem (`Nope, the date is wrong.`, `Nope, it breaks the build.`) is prose too: unparseable, so passed on as an approval — the opposite of what the reviewer said.

### What it does

`Nope,` followed by a clause that holds a problem phrase (the shared `_EN_PROBLEM_PHRASES`, a problem word such as `wrong`, `broken`, `fails`, `missing`, `incorrect`, `bad`) is a rejection with the clause as the reason. `Nope, works` and `Nope, fine` keep their HARDEN-10 C reading.

### Tests

`Nope, the date is wrong.`, `Nope, it breaks the build.`, `nope - this is missing a test` reject; `Nope, works` and `Nope, looks fine` stay prose; a bare `Nope` rejects.

---

## Contest

One commit. The judge's checks are written after the round, from the entries.

## Acceptance

**Layer 1 — offline.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged (the HARDEN-10 C and HARDEN-22 pins included).
**Layer 2 — the operator.** A validator reply `Nothing here contradicts the outline.` ends the self-check loop.

## Not in scope

Rust, the Russian verdict words, the key mask, the stream reader.

## Why not the others (round 323)

A black box of 93 replies (the Part A approvals and the rejections that must stay, the Part B approvals and directives, the Part C rejections and the prose that stays, the HARDEN-22 guards; its corpus of 110 replies compared with the base) scored the base 49, glm 0 (a regular expression that does not compile), nemotron 73, agnes 79, step-5 80, var4 83, var1 84, laguna 86, var3 86, var2 87; the ideal 93. Nobody held a contrast word after the object of the denial (`Nothing here contradicts the outline but the ending fails.` approved in six trees), `Nope, not right.` or `Nope, this contradicts the outline.`; var2, var3 and laguna missed `in it`. laguna's rule for a direct conflict statement and var3's `_EN_CONFLICT_HEAD` both reject `It contradicts the outline.`, as the ticket's tests list; step-5 rejected any `Chapter 3 breaks the timeline.` too, which I left out, because a parser that fails open should not turn every sentence with a verb of conflict into a rejection. The others' tests were run on the ideal as copies: every one passes except glm's pin that `Nope, the date is wrong.` stays prose (the opposite of the ticket) and two step-5 tests (`Chapter 3 breaks...`, `Nope, the ring is stolen.`).

Open for the next tickets: `Nope, the ring is stolen.` (a problem named with no problem word) is still prose; a sentence with a conflict verb and a subject other than `it`, `this` or `the <noun>` is prose; `Nothing breaks.` approves.
