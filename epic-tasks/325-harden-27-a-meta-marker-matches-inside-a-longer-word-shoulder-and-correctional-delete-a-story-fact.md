# HARDEN-27 — `_strip_meta_parentheticals` matches its markers inside a longer word: `(his left shoulder, ...)` and `(... a correctional facility)` are deleted as self-commentary

**Status:** open
**Severity:** MEDIUM (a story fact is dropped silently from the permanent chapter summary; nothing logs it)
**File:** `tools/auto/summary_memory.py`
**Symbol:** `_strip_meta_parentheticals`, `_META_PAREN_MARKERS`
**Round:** 325
**Size:** S
**Depends on:** none
**Also touches:** `tests_bugfix/test_cr16_edit_mode_and_cleanup.py` (its two pins must stay green), `tests/` (new file only)

---

## Why

Found on 2026-10-20 by the review of `tools/auto/summary_memory.py`, a file no HARDEN ticket had read. Reproduced on the head (`8d561bb`).

```
_strip_meta_parentheticals("The arrow struck him in the shoulder (his left shoulder, just below the collarbone).")
    -> "The arrow struck him in the shoulder."
_strip_meta_parentheticals("Bob was sent to prison (he was later moved to a correctional facility upstate).")
    -> "Bob was sent to prison."
```

---

## What happens today

The function removes a `(...)` segment when it is the model's commentary on its own bullet (`(this bullet should be removed because ...)`). The test is `any(k in inner for k in _META_PAREN_MARKERS)`: a plain substring test. A marker that is the beginning of an ordinary word fires on that word: `should` in `shoulder`, `correct` in `correctional`, `bullet` in `bulletin`, `remove` in `removal` of a story object, `original` in `originality`, `mention` is safe, `пункт` in `пунктуация`, and so on. The cleaned bullet goes into the summary file that is the story's only long-term memory, and a later fidelity pass is checked against a summary that is missing the detail.

## What it does

A marker matches a word of the parenthetical, not a piece of one. The markers are prefix families on purpose (`grammat` for grammar and grammatical, `ориги` for оригинал and оригинальный), so the rule is: a marker matches at the start of a word and only up to the word forms the commentary uses (`should`, `shouldn't`; `correct`, `corrected`, `correctly`, `correction`, `incorrect`; `remove`, `removed`; `relevant`, `irrelevant`), never a different word that merely begins with the same letters. The two-word markers (`does not`, `not explicitly`) match as phrases on word boundaries. The Russian markers follow the same rule.

## Tests

These parentheticals are kept: `(his left shoulder, just below the collarbone)`, `(a correctional facility upstate)`, `(read the bulletin board)`, `(his shoulders were bare)`, `(the originality of the plan)` when it is a story fact, `(пунктуация была безупречна)`.
These are still removed: `(this bullet should be removed because it's not relevant)`, `(it shouldn't be here)`, `(this is incorrect)`, `(corrected below)`, `(irrelevant detail)`, `(the original says otherwise)`, `(does not mention the cargo)`, `(not explicitly stated)`, `(следует удалить этот пункт)`, `(ошибочный пункт)`, `(подразумевается)`, `(grammatical slip)`.
A bullet with two parentheticals loses only the commentary one. A long parenthetical of 100 000 characters is cleaned in linear time.

---

## Contest

One commit. The judge's checks are written after the round, from the entries.

## Acceptance

**Layer 1 — offline.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** A chapter whose bullet says `the arrow struck him in the shoulder (his left shoulder, just below the collarbone)` keeps the parenthetical in the summary file.

## Not in scope

Rust, the verdict parser, other filters in the file, nested parentheses, the list of markers itself beyond the boundary rule.
