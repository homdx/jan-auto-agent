# HARDEN-14 — a creative validator's `NOT READY` / `Not coherent` / `Nothing makes sense` is read as an approval; the extractor loses a prototype whose macro is on the next line (a regression) and does not find a zero-argument or async arrow property, `= 0`, `= delete`, `= default`; `/edit` shows a cp1251 file as mojibake and flattens the line endings of a mixed file; the judge script drops a `## Claims:` section without a word

**Status:** open
**Severity:** HIGH-MEDIUM (A: a rejected chapter ships; creative mode only); MEDIUM (B1: a regression, Gate 1 drops a real symbol; B2, B3: Gate 1 drops a real symbol); MEDIUM-LOW (C1); LOW (C2, C3, D)
**File:** `tools/auto/inner_loop.py` (A), `tools/block_extractor.py` (B), `tools/actions.py` and `tools/file_reader.py` (C), `scripts/judge_epic_round.py` (D)
**Symbol:** A: `_parse_verdict_soft`, `_en_not_family_reason`, `_EN_NOT_NEGATES_APPROVAL`. B: `_declaration_tail_ok`, `_find_definition_end_declaration`, `_property_arrow_candidate_patterns`. C: `run_edit` (the `read_file` call), `_edit_write_encodings`, `read_file`, `in_style_of`. D: `ticket_claims`, `_claims_section`
**Round:** (set when the round starts)
**Size:** M
**Depends on:** HARDEN-5 (303), HARDEN-6 (304), HARDEN-9 (307), HARDEN-10 (308), CC review fixes 38, all landed
**Also touches:** `tests_bugfix/` pins of HARDEN-5 A and HARDEN-10 C (the NOT family), of HARDEN-6 B and fix 38 (the extractor), `tests/`

---

## Why

Each part is a place where the code answers "yes" or "found" by default and the default is wrong: an unknown `NOT …` is an approval, a declaration with an unusual tail is "not found", a legacy file is shown to the model as a different text. A part is done when it has its tests and both suites are green unchanged (except the pins named above).

---

## Part A — a `NOT …` reply that is not on the approval-side list is approved

### What happens today

`_en_not_family_reason` rejects a `NOT` / `NOTHING` reply only when the word that follows (after `A`, `AN`, `ANY`, `VERY`…) is in `_EN_NOT_NEGATES_APPROVAL` (`APPROVED`, `OK`, `GOOD`, `VALID`…). Any other word returns `None`, the line falls through to the fail-open rule and the reply is **approved**. On `f2a66db`, `_parse_verdict_soft(text)[0]` is `True` for every one of these rejections:

```
NOT READY                               Not coherent                         Not enough tension
NOT SUITABLE: tone is off               Not quite — the ending is rushed     Not yet. Fix the timeline.
Not really; the motive is unclear       Not in character for Ana             Not a match for the established voice
Not matching the style guide            Nothing is right about this scene    Nothing makes sense in the final paragraph
Nothing was fixed from my last review.
Nothing matches the bible.             Nothing is consistent with chapter 1.   Nothing here fits the established characters.
Not a single detail matches the bible. Not in character: Anna would never say this.   Not plausible: the timeline breaks in chapter 4.
Not believable.   Not faithful to the canon: …   Not accurate: …   Not aligned with the bible.   Not usable.
```
The continuity, fact and theme validators have **no re-ask**: through `ContinuityValidator.check` three of three such replies come back `approved=True, reason='verdict unparseable — passed on fail-open'`, so the chapter passes. Gate 2 re-asks once. At `5dfd4d5` none of these was approved.
A false approval ships bad creative output; a false rejection costs one retry.

### What it does

* A reply whose first word is `NOT` or `NOTHING` is a **rejection by default**, and an approval only when it is one of the problem-side shapes: `NOT A PROBLEM`, `NOT REJECTED`, `NOT CONTRADICTORY`, `NOT IN CONFLICT (WITH …)`, `NOT A SINGLE CONTRADICTION`, `NOTHING TO REPORT / FLAG / FIX / ADD / CHANGE`, `NOTHING FOUND / WRONG / TO CORRECT`, `NOTHING CONTRADICTS …`, plus what `_explicit_en_approval` and `_EN_NOTHING_OK` already hold (`NOT BAD`, `NOTE:`). The approval list is the exception list, so a new rejection phrase needs no new code.
* The reason handed to the Coder is the whole reply, as today.
* The Russian rules and every reply that does not open with `NOT` / `NOTHING` are unchanged.

### Tests (sketch)

The 13 rejections above reject; `Nothing to report`, `Not in conflict with the bible`, `No major inconsistencies` (an adjective between `No` and the problem word is an approval, today a rejection), `Not a problem`, `Not rejected`, `Nothing found`, `Not bad` and the 23 approvals of the HARDEN-5 A and HARDEN-10 C tables approve. A reviewer's own attempt at a blanket fail-closed rule rejected 7 of 23 legitimate approvals, so the list above is required, not optional.

---

## Part B — the block extractor

### B1 — a prototype with a macro or attribute on the next line is "not found" (a regression of fix 38)

On `304a59e^` and `5dfd4d5` the extractor finds `api` in `int api(int a)\n    DEPRECATED_MACRO;\n`; on `f2a66db` it returns `""`. `_declaration_tail_ok` is stricter than the parameter list's real tails: after the `)` it accepts a `;`, a return type and the qualifiers, but not a bare upper-case macro on the next line (`DEPRECATED_MACRO;`, `EXPORT_API;`, `NORETURN;`). `__attribute__((deprecated));` on the next line is found. Accept an upper-case identifier (digits and underscores allowed) before the `;`, and keep every refusal of fix 38 (`x->f(1);`, `ready && f(1);`, `label: f(1);`, `timeout: f(5),`).

### B2 — a property arrow with no parameters, `async`, or a parameter list over several lines is "not found"

HARDEN-9 E finds `handler: (a) => {…}`; on `f2a66db` `handler: () => {…}`, `handler: async () => {…}` and `handler: (\n  a,\n  b\n) => {…}` return `""` (the parameter pattern needs one character and stops at a newline). **Do not widen one regular expression for the three shapes.** Do it in two steps: (1) a short pattern finds the head `name:` (with an optional `async`) followed by `(` or an identifier and `=>`; (2) a small scan from that `(` to its matching `)` (balanced, newlines and comments allowed, bounded to a few thousand characters) finds the `=>`, and the block runs from the head to the closing brace as HARDEN-9 E does. A property whose value is not a function (`name: 5`, `name: (a + b)`, `name: (a, b)` with no `=>`) is not a definition. **A TypeScript type member is not a definition either** (a reviewer found 40 of 114 newly found names were such): `onSave: (value: string) => void;` in an `interface`, `onOpen: (id: number) => Promise<void>,` in a type literal and a parameter `callback: (ev: Event) => void` have a **type** after the `=>` (`void`, `Promise<…>`, a bare identifier, then `;` `,` or `)`), no body; a definition has a `{` body or an expression after the `=>` (`save: async (u) => persist(u),` is still found). In the second step, look at what follows the `=>` before accepting the head.

### B3 — C++ `= 0`, `= delete` and `= default` declarations are "not found"

`virtual void f() = 0;`, `virtual int f() const = 0;`, `void g() = delete;`, `~I() = default;` return `""`: `_find_definition_end_declaration` stops at the `=`. A `= 0`, `= delete` or `= default` followed by `;` is the end of a body-less declaration (pre-existing; on `5dfd4d5` the same).

### Tests (sketch)

B1: the macro cases above found, the fix 38 call table still "not found". B2: the three shapes for `.js`, `.ts`, `.jsx`, `.tsx`, the non-function values not found, the HARDEN-9 E and HARDEN-10 tables unchanged. B3: the four declarations found with their `;`; `int x = 0;` and `a = b;` are not declarations of a function.

---

## Part C — `/edit` shows the file differently from the Coder

### C1 — a cp1251 file is shown to the model as latin-1

`run_edit` reads the file with `read_file`, which tries UTF-8 and falls back to `latin-1`, while the write codec comes from `_edit_write_encodings` (`cp1251` first). For `Привет мир` saved as cp1251 the model is shown `Ïðèâåò ìèð`, and that text does not encode back in `cp1251` (`cp1251 ENCODE FAIL`, `cp1252` and `latin-1` round-trip it). The Coder already reads such a file in its own codec (`_file_read_encoding`, HARDEN-9 C); `/edit` does not, so an instruction in Russian about a Russian file cannot be done. `/edit` reads the file with the codec `_edit_write_encodings` put first, from the same helper the Coder uses (one decision, as CC-16 Part C intends), and the refusal message names the characters of the text the model was shown. Test: the file, a faithful model that returns the shown text plus `Конец`, the file on disk ends in `Конец` in cp1251 (a reviewer's end-to-end test is in `contest-bench/r7/`).

### C2 — a file of mixed line endings is flattened

`in_style_of` takes CRLF for the whole file when more than half of its `\n` follow a `\r`. For `a\r\nb\r\nc\nd\ne\n` an edit of line `c` returns `a\nb\nC\nd\ne\n`: the untouched lines `a` and `b` lost their `\r`. A line of the reply that equals a line of the original (after the line ending) keeps that line's own ending; a changed or new line takes the majority style.

### C3 — a UTF-16 or UTF-32 file is not refused

A file with a UTF-16 or UTF-32 BOM is read as latin-1 with NULs, the model cannot find its line, the edit applies nothing and `/edit` still prints `status=approved` with a backup, while the file lost its `\r`. `/edit` and the Coder refuse such a file by name (`the file is UTF-16 — convert it to UTF-8 first; nothing was written`), as they refuse a file with undecodable bytes today. (The report asked for support; refusal is the ticket's decision.)

### C4 — a Windows-1252 file with `€` and no Western letter

A cp1252 file whose only non-ASCII character is `€` is still read as cp1251 (it wins every tie) and `/edit` writes the `€` as byte `0x88`, which reads back as `ˆ`. A lone `€` byte `0x80` read as cp1251 is `Ђ`, as cp1252 is `€`: when the only high bytes are ones both codecs hold but only one maps to a printable symbol of its script, the tie is broken by that. (Low; a carry-over.)

### Tests (sketch)

C1: the cp1251 file above is shown as `Привет мир` and a revision with a changed Russian line is written back in cp1251; UTF-8, BOM, cp1252 files unchanged. C2: the mixed-ending file keeps `a\r\n`, `b\r\n`; an all-CRLF and an all-LF file unchanged. C3: UTF-16 LE, UTF-16 BE and UTF-32 files are refused and left byte-identical.

---

## Part D — `ticket_claims` returns no claims for `## Claims:` and `### Claims`

`ticket_claims("## Claims:\n\n- [new] a\n")` and the same under `###` or `####` return `[]`, as does a file with no section: the judge then scores nothing and says nothing. A heading of level 2 to 4 whose text is `Claims` with an optional trailing colon opens the section (the `(3)` suffix and any case already do); a ticket with a `Claims` heading that yields no claim is a refusal naming the line, not `[]`.

Tests: the headings above give the claim; a ticket without any claims heading still gives `[]`.

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. The judge runs the extractor over a corpus of this repository's TS, JS, C and C++ sources before and after and expects no name that was found to stop being found.

## Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged except the pins named in the header.
**Layer 2 — the operator.** A creative run whose validator answers `Not coherent` retries instead of passing the chapter; `/edit "переименуй" in notes.txt` on a cp1251 file writes back a cp1251 file with only that line changed.

## Not in scope

Rust (`pub(crate)`, bounds, `extern "C"`, nested comments: the project does not fix Rust), Ruby, a codec guess for East Asian encodings, UTF-16 support (refusal only), and the plain-folder first commit that stages `.env` and `node_modules/` (kept as designed).
