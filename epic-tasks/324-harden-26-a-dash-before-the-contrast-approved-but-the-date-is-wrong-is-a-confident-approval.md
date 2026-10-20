# HARDEN-26 — `APPROVED — but the date is wrong.` is a confident approval: a dash before the contrast is no clause boundary

**Status:** open
**Severity:** MEDIUM (a rejection that says why is passed as an approval, with no fail-open warning)
**File:** `tools/auto/inner_loop.py`
**Symbol:** `_parse_verdict_soft`, `_EN_CLAUSE_CAVEAT`, `_EN_OK_CAVEAT`, `_en_approval_caveat_reason`
**Round:** (set when the round starts)
**Size:** S
**Depends on:** HARDEN-17 A (the approval-then-objection reader this extends), HARDEN-25 (lands the same reader)
**Also touches:** `tests/test_harden17_verdict_approval_and_reject_words.py`, `tests/test_harden25_verdict_contrast_and_nope.py`, `tests/`

---

## Why

Found on 2026-10-20 by the review of HARDEN-23/24 (CC review fixes 45/46). Reproduced on the head (`2724ace`, after HARDEN-25), on `3fdd834` (HARDEN-17) and on `ae9a30d`: not a regression, a shape HARDEN-17 A never read.

```
_parse_verdict_soft("APPROVED — but the date is wrong.")       -> (True, '', False)
_parse_verdict_soft("APPROVED - but the date is wrong.")       -> (True, '', False)
_parse_verdict_soft("APPROVED — however, the date is wrong.")  -> (True, '', False)
_parse_verdict_soft("APPROVED — except the date is wrong.")    -> (True, '', False)
_parse_verdict_soft("OK — but the seed entry is gone.")        -> (True, '', False)
_parse_verdict_soft("Approved? No — the test is wrong.")       -> (True, '', False)
_parse_verdict_soft("OK? Not really, the date is wrong.")      -> (True, '', False)
```

---

## Part A — a dash or a parenthesis before the contrast

### What happens today

HARDEN-17 A reads `Looks good, but …` and `Approved, however …` as the objection: `_EN_CLAUSE_CAVEAT` takes a contrast word that opens a clause after a stop, a comma, a semicolon, a colon, `!`, `?` or a newline, and `Approved but …` with no mark at all. A dash is none of them, and a dash is how GPT-family reviewers write that turn. The same sentences with a comma, `:` or `;` reject; with a dash they are confident approvals — `unparseable` is `False`, so not even the fail-open warning is logged.

### What it does

A dash (`—`, `–`, or `-` with a blank on both sides) or an opening parenthesis before the contrast word is a clause boundary like the comma, in `_EN_CLAUSE_CAVEAT` and in `_EN_OK_CAVEAT`. A dash that does not open a contrast keeps the approval.

### Tests

The first five replies above reject, with the clause after the contrast as the reason; `Approved — looks good.`, `APPROVED — no issues.`, `OK - fine` and `APPROVED (no issues)` approve.

---

## Part B — an approval head answered by `No` after a `?`

### What happens today

`Approved? No — the test is wrong.` and `OK? Not really, the date is wrong.` are read on their first word: the head approves, and the `No` that answers it is never looked at.

### What it does

An approval head followed by `?` and then `No`, `Nope` or `Not really` is a rejection with the rest as its reason.

### Tests

The last two replies above reject (`the test is wrong.`, `the date is wrong.`); `Approved? Yes.` and `OK? Sure.` approve.

---

## Contest

One commit. The judge's checks are written after the round, from the entries.

## Acceptance

**Layer 1 — offline.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged (the HARDEN-10 C, HARDEN-17, HARDEN-22 and HARDEN-25 pins included).
**Layer 2 — the operator.** A validator reply `APPROVED — but the date is wrong.` sends the chapter back with `the date is wrong.` as the reason.

## Not in scope

Rust, the Russian verdict words, the key mask, the stream reader, and the HARDEN-25 leftovers its ticket lists as open (`Nope, the ring is stolen.`, a conflict verb after another subject, `Nothing breaks.`).
