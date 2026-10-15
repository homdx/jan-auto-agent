# HARDEN-8 — `claim_diff` reuses the base side against the wrong claims and crashes after the votes are paid, a claims list is skipped without a word, a C++ method returning `std::string` is "not found", a prompt entry without a version, and an IMPROVEMENTS.md entry with a letters-only id takes the others with it

**Status:** open — FINAL, written 2026-10-15 against `6ddbe7c`. Six defects from outside reviews, each **reproduced by running the code** on that commit (the output is below). Parts A and B are gaps left by CC-16 Part E; Part C is a gap in the claims
parser CC-16 and HARDEN-1 left; Part D is beside HARDEN-4 and HARDEN-6. The code is the authority, not this ticket: if a line has moved, the symbol names the place.
**Severity:** HIGH (A: the judging gives a claim another claim's base verdict); MEDIUM (B, D); LOW (C, E, F)
**File:** `scripts/claim_diff.py` (A, B), `scripts/judge_epic_round.py` (C), `tools/block_extractor.py` (D), `tools/prompt_store.py` (E), `tools/auto/plan_validator.py` (F)
**Symbol:** A/B: `judge_both` (`slot`, `fresh`, `base_index`, `voted_b`), `_rekey_votes`, `_record_slots`. C: `ticket_claims`, `_claims_section`, `_LIST_ITEM`. D: `extract_block` and the candidate patterns that read a definition head. E: `PromptStore.get_version_label`. F: `_NEXT_ENTRY`, `_task_section_pattern`, `_remove_from_improvements_md`
**Round:** —
**Size:** M
**Depends on:** CC-16 (302), HARDEN-4 (301), HARDEN-6 (304), all landed
**Also touches:** `tests/`, `tests_bugfix/`

---

## Why

Six findings of the last outside reviews that were reproduced on the current commit and are not in any landed ticket. Two of them (A, B) are in the judge that decides every round's claim check, so they are the ones that matter; the other four are small and
local. A part is done when it has its tests and both suites are green unchanged.

---

## Part A — a recorded base side is re-keyed in the wrong direction

### What happens today

CC-16 Part E reuses a recorded base side claim by claim. `judge_both` builds `slot = {this side's position: the record's position}` and hands it to `_rekey_votes`, whose own docstring says its mapping is `old position -> new`. The two directions agree only
when the claims are in the same order as in the record, which is the only case the shipped tests put under test. With `CLAIMS = [new, fix, still]` (the three claims of `tests/test_judge_epic_round_claims.py`), a record voted over all three, and the same three
claims reordered to `[still, new, fix]`, through the real `judge_both` with the fake voter of that file, on `6ddbe7c`:

```
base verdict per claim      voted afresh      reused from the record
[still, new, fix]           TRUE FALSE TRUE   TRUE TRUE FALSE        <- two claims have another claim's verdict
```

The deltas (`NEW` / `FIXED` / `STILL`) that follow are computed from them.

### What it does

* The mapping handed to `_rekey_votes` is the record's position to this side's position, for every claim the record holds; the claims it does not hold are voted here as today. A reused side gives, for the same claims in any order and for any subset, the same base
  verdicts as a fresh vote of those claims.

### Tests (sketch)

The reordered case above, a subset, a superset (a claim the record does not hold), and the order the shipped tests use; each compared with a fresh vote.

---

## Part B — a second entry crashes after the votes are paid

### What happens today

`judge_both` hands the record back through `voted_b`, keyed by `base_index`, which holds **claim** indices, with `{p: base_index.index(fresh[p]) …}`, where `fresh[p]` is a **position in the code-claim list** `idx_b`. The two coincide only while every claim is a code claim. With three
claims and no `expect`, the first entry judging only the middle one (the other two are world claims there) and the second entry judging only the last one, on `6ddbe7c`:

```
judge_both(…, base_side=<record of the first entry>)   ->   ValueError: 0 is not in list     (scripts/claim_diff.py, the `voted_b` merge)
```

after the head and the missing base claim have been voted and paid for. In a round `claims_phase` reports it as a refusal of that entry.

### What it does

* The record is kept in claim indices throughout: `base_index.index(idx_b[fresh[p]])`, or an equivalent that cannot mix the two spaces. No entry of a round is refused for the claims another entry anchored.

### Tests (sketch)

The two-entry case above; the same round judged entry by entry gives the same verdicts as each entry judged alone (the reorder test of Part A and this one share the fixture).

---

## Part C — a list of claims is skipped without a word

### What happens today

The docstring of `ticket_claims` promises that a list item without its word in brackets is refused: "the section is a claims list, not prose". `_LIST_ITEM` is `^\s*[-*+]\s+`, so only dash, star and plus items count; on `6ddbe7c`:

```
## Claims ⏎ - [new] a ⏎ 2. [still] b ⏎ 3. [gone] c      ->  [{'claim': 'a', 'expect': 'new'}]          claims b and c silently gone
## Claims ⏎ 1. [new] a ⏎ 2. [still] b                   ->  []                                         the whole list silently gone
a fence:  ``` ⏎ ## Claims ⏎ - [new] a ⏎ ```            ->  [{'claim': 'a', 'expect': 'new'}]          a heading inside a code block is a section
```

### What it does

* A numbered item (`1.` or `1)`) is a list item like the others: with its word in brackets it is a claim, without it a refusal naming the line. A line inside a fenced block is not a heading and not a claim. Both parsers that read the section (`scripts/judge_epic_round.py`
  and `tools/contest/export.py`) agree.

### Tests (sketch)

The three rows above; a ticket with `- [new] …` bullets and a numbered list of notes after another heading is unchanged; the existing claims tests green unchanged.

---

## Part D — a C++ method returning `std::string` is "not found"

### What happens today

`extract_block` finds an out-of-line method whose return type is a plain word, and loses one whose return type is qualified or templated. On `6ddbe7c`:

| source | name | result |
|---|---|---|
| `void Widget::run()⏎{⏎}` (`.cpp`) | `run` | found |
| `int⏎Widget::label()⏎{ … }` | `label` | found (the type line is in the block) |
| `std::string Widget::label() const⏎{ … }` | `label`, `Widget::label`, `Widget.label` | `''` |
| `std::vector<int> Widget::items()⏎{ … }` | `items` | `''` |
| `std::string⏎Widget::label()⏎{ … }` | `label` | `''` |

`''` is "not found": Gate 1 drops the task that cites it and the grounding falls back to the head of the file.

### What it does

* A definition head may be preceded by a return type that contains `::`, `<…>`, `*`, `&` and qualifiers, on the same line or on the line above; the block starts at the type, as it does for `int Widget::label()`.
* No result changes for any name the extractor finds today.

### Tests (sketch)

The table above for `.cpp`, `.h` and `.cc`; the HARDEN-4 and HARDEN-6 tables green unchanged.

---

## Part E — a prompt entry without a version breaks the label

### What happens today

HARDEN-1 A3 made `get_store_summary` read `stack[-1].get("version")`. `get_version_label` still reads `stack[-1]["version"]` as the default of `entry.get("current_version", …)`, and the default is evaluated eagerly: an entry in `prompts.json` that has a `stack` with a
record without a `version` key (hand-edited, or written by an older version) raises `KeyError: 'version'` on `6ddbe7c`, where `get_store_summary` lists it.

### What it does

* `get_version_label` reads the version the way `get_store_summary` does and answers `hardcoded` (or the label of the last record that has a version) instead of raising.

### Tests (sketch)

An entry with a version-less record: `get_store_summary` and `get_version_label` both answer; an entry with `current_version` set is unchanged.

---

## Part F — removing an IMPROVEMENTS.md entry with a letters-only id deletes the entries after it

### What happens today

`_NEXT_ENTRY` accepts a next heading `### <id>: ` only when the id contains a digit, `.`, `_` or `-`. Entries are separated by `---`; an entry whose id is only letters is not a boundary, so removing the first of three entries removes the rest of the file up to
`## Manual Suggestions`. On `6ddbe7c`, entries joined by `---` lines and `_remove_from_improvements_md(dir, [first])`:

```
ids AUTO-T1, AUTO-T2, AUTO-T3   remove AUTO-T1   ->  AUTO-T2 and AUTO-T3 stay            (right)
ids t1, t2, t3                  remove t1         ->  t2 and t3 stay                       (right)
ids alpha, beta, gamma          remove alpha      ->  beta and gamma are gone
ids FIX, BAR, BAZ               remove FIX        ->  BAR and BAZ are gone
```

### What it does

* The end of an entry is its separator followed by the next heading of any id (`### <id>: `), or `## Manual Suggestions`, or the end of the file; a `### Note: …` line inside an entry's text (not after a separator) still does not end it.

### Tests (sketch)

The table above; the existing FIX-2 #8 tests (fenced blocks, `---` inside a fence) green unchanged.

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. Part D changes what the extractor answers for every
language: the judge runs the extractor over this repository's own `.py`, `.js`, `.ts`, `.c` and `.cpp` files before and after, and the blocks returned for names that were found must not change.

## Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** A round whose entries are judged in any order gives each claim the verdicts a fresh vote gives it.

## Not in scope

The wording of the refusals, the claims heading variants (`## Claims:` and `### Claims` are not headings of the section), the options of `scripts/gate1_vote_layer2.py` (a repeated option and a base ini without a section are accepted there), and the Windows-1252 codec guess of `/edit`.
