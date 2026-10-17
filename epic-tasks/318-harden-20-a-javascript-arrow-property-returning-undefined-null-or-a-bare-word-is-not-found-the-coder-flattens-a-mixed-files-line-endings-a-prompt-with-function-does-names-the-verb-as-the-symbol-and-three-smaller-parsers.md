# HARDEN-20 — `h: (a) => undefined,` is not found in JavaScript (a regression of HARDEN-14); the Coder flattens the line endings of a mixed file; "what does the `handle_request` function return" names `return` as the symbol; a task removed from IMPROVEMENTS.md cuts the entry that quotes its heading; a ticket's claims are announced differently by the judge and by the export

**Status:** open
**Severity:** MEDIUM (A: Gate 1 rejects a real symbol as a hallucination; C: the prompt parser names the wrong symbol, silently); LOW-MEDIUM (B: a diff of every line that does not match the majority; E: claims judged but never announced, or lost on a CRLF ticket); LOW (D)
**File:** `tools/block_extractor.py` (A), `tools/auto/utils.py` and `tools/auto/coder.py` (B), `tools/prompt_parser.py` (C), `tools/auto/plan_validator.py` (D), `scripts/judge_epic_round.py` and `tools/contest/export.py` (E)
**Symbol:** A: the property-arrow patterns (HARDEN-14 B2). B: `in_style_of`, `_edit_line_ends`. C: `parse_prompt`, strategy A (`decl_match`). D: `_task_section_pattern`, `_without_section`. E: `_claims_section`, `_CLAIMS_HEADING`, `_has_claims_heading`
**Round:** (set when the round starts)
**Size:** M
**Depends on:** HARDEN-14 (312), HARDEN-16 (314), both landed
**Also touches:** the HARDEN-14 B and D tests (pins that must stay green), `tests/`, `tests_bugfix/`

---

## Why

Outside reviews of `277d632` and `5c403f1` found five small defects, each reproduced on `5c403f1`. None is in tickets 311 to 317. A part is done when it has its tests and both suites are green unchanged.

---

## Part A — a JavaScript property arrow whose body is a bare word (a regression)

### What happens today

In a `.js` or `.jsx` file:

```js
const o = {
  a: (a) => undefined,      // not found      (found at 264440c)
  b: (a) => null,           // not found      (found at 264440c)
  c: (a) => true,           // not found      (found at 264440c)
  d: (a) => fallback,       // not found      (found at 264440c)
  e: () => undefined,       // not found on every version
  f: (a) => 0,  g: (a) => init(),  h: (a) => x + 1,   // found
};
```

Gate 1 reads an empty extract as "symbol not found", so `noop: () => undefined` and `isOn: () => true` are rejected as hallucinations. HARDEN-14 B2 added a guard against a TypeScript type (`h: (a) => void`) and applied it to JavaScript, which has no types.

### What it does

The "a bare word after `=>` is a type" rejection applies to `.ts`, `.tsx` and `.d.ts` only. In `.js`/`.jsx` a bare word, `null`, `undefined`, `true`, `false` or an identifier after `=>` is a body. The TypeScript cases of HARDEN-14 and 314 stay green.

### Tests (sketch)

The six lines above in `.js` and `.jsx` found; the same text in `.ts` as a type member (`type H = { e: () => undefined }`) still not found.

---

## Part B — the Coder flattens a mixed file's line endings

### What happens today

A file with one LF line among CRLF lines (`alpha\n beta\r\n gamma\r\n delta\r\n`), edited at `delta` only: `/edit` keeps `alpha` as LF (HARDEN-14 `_edit_line_ends`), the Coder path (`in_style_of`, `coder.py` about line 2129) makes every line CRLF. A task that touches one line of a mixed file produces a diff of every line that does not match the majority.

### What it does

`in_style_of` gives each **untouched** line its own original ending and an edited or new line the file's majority ending: one shared rule with `_edit_line_ends` (call it, do not copy it). A file with a single ending style behaves as today.

### B2 — `/edit` glues two lines when the file has no final newline and a line repeats (LE-1, HIGH)

`_edit_line_ends` hands each reply line the next unused ending of an equal original line. An unterminated original last line has an **empty** ending, and the loop can pop it for a middle reply line: the original `alpha\nbeta` (no final newline) edited to `beta\nalpha\nbeta\n` is written as `betaalpha\nbeta\n`. A source file without a trailing newline where an edit repeats its last line (`return x`, `}`, `pass`, a blank line) becomes invalid. The reviewer's patch (`v13-line-endings-and-glued-lines.patch`: an empty ending may be used only by the reply's last line) is applied as a separate CC commit right after round 315; this part keeps the rest of the work: the single shared rule must carry that guard, and a test must pin it on both paths.

### B3 — the match decodes the original as UTF-8 with `errors="replace"` and keeps the BOM in line 1

* A cp1251 file `Привет\r\nмир\r\nok\nend\n` where the model changes only `ok`: both Cyrillic lines are written with `\n` (0 of 2 CRs kept), because the original lines decode to U+FFFD text that never equals the reply's correctly decoded line. The same structure in ASCII or UTF-8 keeps both. Decode the original with the codec `_read_edit_target` chose (or compare re-encoded lines).
* A UTF-8 BOM file `\ufeffa = 1\r\nb = 2\nc = 3\nd = 4\n`: the first line loses its CRLF because the BOM stays in that line's body (`\ufeffa = 1`) and never equals the reply's `a = 1`. Strip a leading BOM from the first original body before matching (the Coder path writes the BOM back separately).

### Tests (sketch)

The file above on both paths: `alpha` stays LF, `delta` takes the majority ending; an all-LF and an all-CRLF file unchanged. B2: the glued case on both paths, `}`/`pass`/blank repeated last lines. B3: the cp1251 file (both Cyrillic CRs kept) and the BOM file (first CRLF kept) on both paths.

---

## Part C — "what does the `X` function return" names the verb

### What happens today

`parse_prompt`'s first strategy is `re.search(r'\b(def|class|function|func)\s+([A-Za-z_]\w*)\b', remainder)`. It takes whatever word follows `function`, verb or not, and short-circuits the strategies that find the real symbol:

```
what does the handle_request function return in app.py     -> target_name='return'
why does the handle_request function fail in app.py        -> 'fail'
how does the handle_request function work in app.py        -> 'work'
what does the parse_data class represent in app.py         -> 'represent'
tell me what the handle_request function returns in app.py -> 'returns'
```

### What it does

A match of the declaration strategy is **validated** the way the later strategy validates a candidate: the name must exist in the source (or be followed by `(` / `:` as a code reference); when it does not, fall through to the next strategies. Prose of the "X function does/returns/fails" shape finds `handle_request`.

### Tests (sketch)

The five prompts above give `handle_request` / `parse_data`; `function handle_request` and `def handle_request(` phrasing still works; a name that exists after the keyword (`class Parser`) still wins.

---

## Part D — removing a task cuts the entry that quotes its heading

### What happens today

`_task_section_pattern` starts with `### AUTO-2:` and has no line-start anchor. When AUTO-1's body quotes `` ### AUTO-2: title `` mid-line, removing AUTO-2 cuts AUTO-1's sentence and the text after it, and the leftover runs into `### AUTO-3:`.

### What it does

The heading is anchored at a line start (`(?m)^### …`). Entries, fenced blocks and prefix-sharing ids (`AUTO-1`, `AUTO-10`, `AUTO-100`) are removed exactly as before.

### Tests (sketch)

AUTO-1 quoting `### AUTO-2: x` mid-line: removing AUTO-2 leaves AUTO-1 byte for byte; the existing removal tests unchanged.

---

## Part E — the judge and the export disagree about a ticket's claims

### What happens today

Two parsers: `judge_epic_round.ticket_claims` (the judge) and `export._has_claims_heading` (decides the `--claims` line of the summary).

* `_CLAIMS_HEADING = ^##\s+Claims…` is level 2 only; HARDEN-14 D taught the judge `###` and `####`. A ticket with `#### Claims` is judged but never announced.
* `_claims_section` splits on `"\n"` only; on a CRLF ticket the heading is `## Claims\r`, which does not match, and the ticket has no claims. The round judges nothing, silently; the export sees the claims.

### What it does

One reader for both (the export calls the judge's section reader), on text normalised with `splitlines()`; the heading pattern is `^#{2,6}\s+Claims…`. A ticket with an empty claims heading still refuses with a line number.

### Tests (sketch)

The 18 shapes of the review: judge and export agree on all; CRLF and LF give the same claims; `#### Claims` is announced.

---

## Contest

One commit. The judge's checks are written after the round, from the entries; the entries' tests are cross-tested on every other entry's code, and the judge reads every diff. The extractor corpus check (this repository's JS/TS/Python/Java/Kotlin sources before and after) expects no name that was found to stop being found.

## Acceptance

**Layer 1 — offline.** The tests above; both suites green unchanged.
**Layer 2 — the operator.** A mixed-ending file touched on one line produces a one-line diff; `contest run` on a CRLF ticket with claims judges them.

## Not in scope

Rust, Ruby, C# and Dart arrow members, React wrappers (`useCallback`, `memo`, `forwardRef`) and `module.exports.f = …` (JS/TS shapes the project does not target; Python, Java and Kotlin only).
