# HARDEN-6 — the extractor and the collect model: a call taken for the definition, declarations "not found", a quadratic pattern, a monorepo alias, one missing summary switching the pack off, long sentences deleted

**Status:** open — FINAL, written 2026-10-14 against `1fa8599`. Six defects from outside reviews, each **reproduced by running the code** on that commit (the output is below). Parts C, D and F are gaps left by the landed fixes
HARDEN-4 D2 and D3 and HARDEN-1 A5; Part E is a consequence of CC-15. The code is the authority, not this ticket: if a line has moved, the symbol names the place.
**Severity:** MEDIUM (A, B, D, E: the model is shown the wrong span, a task is dropped, or the pack is off); LOW–MEDIUM (C, F)
**File:** `tools/block_extractor.py` (A, B, C), `tools/collect/graph.py` (D), `tools/auto/collect_bridge.py` and `tools/collect/loader.py` (E), `tools/collect/verifier.py` (F)
**Symbol:** A/B: `extract_block`, `_find_definition_open_brace`, `_brace_candidate_patterns`. C: `_bodyless_candidate_patterns`. D: `build_module_index`, `resolve_import`. E: `CollectBridge.usable`, `load_readonly` / `is_fresh`. F: `extract_claims`, `_verify_text`
**Round:** —
**Size:** M
**Depends on:** HARDEN-1 (298), HARDEN-4 (301), CC-15 (297), all landed
**Also touches:** `tools/auto/gate1_filter.py` (reads A and B), `tests/`, `tests_bugfix/`

---

## Why

The block extractor is the one tool the model's context depends on for every non-Python language, and the collect model is what Gate 1 and `--auto` read. Both were hardened in rounds 298 and 301; these six are what the next reviews
found beside and inside those fixes. A part is done when it has its tests and both suites are green unchanged.

---

## Part A — a call before the definition is returned instead of the definition

### What happens today

`extract_block(source, name, ext)` looks for the head of a definition, then for an opening brace after the argument list. A **call** of the name written before the definition, with no `;` after it, matches the head, and the
brace search accepts a `{` beyond an intervening `}`: the block starts at the call and runs through the real definition. On `1fa8599`, `extract_block(src, "getError", ".js")`:

| source before the definition | first line of the block |
|---|---|
| `throw getError({ code: 1 })` inside `if (x) { … }`, no `;` | `  throw getError({ code: 1 })` |
| the same over three lines (`throw getError({⏎ code: 1,⏎ msg: 'x'⏎ })`) | `    throw getError({` |
| `return getError({ code: 1 })` | `  return getError({ code: 1 })` |
| the same in `.ts` | `    throw getError({ code: 1 })` |
| `throw getError({ code: 1 });` (with `;`) | `function getError (opts) {` (right) |

The reviewer measured 13 of 279 functions of real JS (npm, playwright) that are used before they are defined (5 %): extracted from the wrong place. Not a regression: the old extractor does the same. The model is shown the wrong span.

### What it does

* The brace search after an argument list stops at a `}` or at a statement boundary (a newline followed by a token that starts a statement) at depth zero; a match whose head is a call (preceded by `throw`, `return`, `await`, `new`, `=`, `(`, `,`, or by an
  operator) is not a definition head.
* A definition head is a declaration by its own prefix (`function`, `class`, `def`, `fn`, `func`, a type and a name in C/Java), which is what the bodyless patterns already rely on.

### Tests (sketch)

The table, row by row, for `.js`, `.ts`, `.java`, `.c`; a definition used before it is defined is still found; the existing call-site tests (`tests_bugfix/test_block_extractor_call_site_vs_definition.py`) green unchanged.

---

## Part B — a declaration with no body is "symbol not found"

### What happens today

HARDEN-4 D2 handled an expression body with no braces. A **declaration** that is in the file and has no body still gets `""`, which the caller reads as "not found": Gate 1 Stage A removes a task that cites it, without a message, and
`gate1_grounding` falls back to the first 40 lines. On `1fa8599`:

| source | name | result |
|---|---|---|
| `declare function ambient(x: number): void;` (`.ts`) | `ambient` | `''` |
| `int proto(int a);` (`.c`) | `proto` | `''` |
| `abstract class A { abstract run(): void; … }` (`.ts`) | `run` | `''` |
| `char *name_of(void);` (`.c`) | `name_of` | `''` |
| `interface Store { reset(): void; }` (`.ts`) | `reset` | `''` |
| `function real(): number { return 1; }` (`.ts`) | `real` | found (right) |

A call is not a declaration and stays `""` (review v8): `ambient(1);`, `return ambient(1);`, `const x = ambient(1);`, `x.ambient(1);`, `export default ambient(1);`, `int y = proto(1);`. A declaration whose parameter list spans several lines
(`declare function multi(⏎ a: number,⏎ b: string⏎ ): void;`) is also `''` today; it is wanted, but not required.

### What it does

* A declaration is returned: its signature line, or the lines up to the `;`. The empty string stays "not found" and means nothing else.

### Tests (sketch)

The three shapes above, and a task that cites an abstract method; an unknown name is still `""`.

---

## Part C — one pattern is quadratic

### What happens today

The pattern that recognises a `const`/class-field arrow function carries `(?::[^=;]+?)?`, an optional type annotation whose character class spans newlines. On a JS/TS data module whose records have no `=` or `;` in them, each line that starts
with the name scans to the end of the file. `extract_block(src, "id", ".js")` on a file of N records written one field per line (`{⏎ id: 1,⏎ name: 'x',⏎ },`):

| records | size | time |
|---|---|---|
| 1 000 | 41 KB | 0.45 s |
| 2 000 | 85 KB | 1.79 s |
| 4 000 | 173 KB | 7.27 s |

Gate 1 and the search call `extract_block` for common names (`id`, `name`, `type`). The reviewer measured 12.3 s at 267 KB and 11.6 s of it in this pattern; a bounded version (`[^=;\n]{1,200}`) took 0.01 s.

### What it does

* Every annotation and parameter-list class in `_bodyless_candidate_patterns` is bounded and does not cross a newline (or a bounded number of them). No result changes for any input the tests pin.

### Tests (sketch)

20 000 characters of the shape above finish in under a second for `id`, `name`, `type`; the D2 table of HARDEN-4 (every bodyless form, the class-field arrow, a wrapped arrow) still passes.

---

## Part D — in a monorepo an import resolves to the wrong sub-project

### What happens today

HARDEN-4 D3 gives each module a layout-relative alias (`app.models` for `svc_a/app/models.py`), and the first module by sorted path claims it. With `svc_a/app/` and `svc_b/app/` side by side:

```
index keys ending in app.models: svc_a.app.models, svc_b.app.models, app.models -> svc_a/app/models.py
svc_b/app/views.py imports app.models  ->  svc_a/app/models.py        <- the other service
```

Before D3 the edge was missing; now it is wrong, and the import graph, the blast radius and the test map name another service's file. A `services/*/app` layout triggers it.

### What it does

* An alias is resolved against the **importer's own source root** first (the root its dotted name was cut at); `svc_b/app/views.py` + `app.models` is `svc_b/app/models.py`.
* When two modules claim the same alias and the importer has no root of its own, the alias is left unresolved; it never picks the first.

### Tests (sketch)

The monorepo above: each service resolves inside itself; two claimants and an importer outside both: unresolved; the HARDEN-4 `src/` and flat layouts unchanged.

---

## Part E — one module without a summary switches the pack off for the whole session

### What happens today

CC-15 made a manifest that lists a module without a summary **stale**. `CollectBridge.usable` is true only when the model is `fresh`, so one module whose Pass B failed turns the collect pack off for the whole `--auto` session — for every
module, although every file is unchanged and the artifact is otherwise right. With one listed module (`pkg/store.py`):

```
staleness = warn      status=stale  reason='artifact is stale (1 module without a summary)'
staleness = ignore    status=stale  … 'staleness=ignore — not used'
staleness = refresh   status=stale  … 'this reader does not write, so the artifact was left alone'
```

(The reviewer also saw the bridge log the wrong reason, "a tracked file changed": not re-checked here; the test below pins the reason.)

### What it does

* A model that is stale **only** because of modules without a summary is usable: the pack serves every module that has its facts, and a module without a summary is simply served without one. `--check`, `load` and the collect line still
  say `stale (N modules without a summary)`, so the operator knows; the reason the bridge logs is that one.
* A model stale for any other cause (changed files, another collector version, another `skip_dirs`) is not usable, as today.

### Tests (sketch)

A bridge over a model listed with one module without a summary: usable, serves the other modules' blocks, serves none for the listed one; with a changed file as well: not usable; the log line names the right cause; CC-15's own tests green unchanged.

---

## Part F — a sentence over 2 000 characters disappears from a summary

### What happens today

HARDEN-1 A5 made `extract_claims` skip a sentence over 2 000 characters (a second line of defence against the quadratic walk). `_verify_text` rebuilds the text from the claims that were kept, so a true 2.5 KB run-on sentence of notes comes out
as nothing: `extract_claims("This module " + "does many things and " * 125 + "works.", "pkg/m.py")` returns **0 claims**; the reviewer saw the summary come out with 0 characters, `dropped_claims = 0` and no entry in the verification report.

### What it does

* An over-length sentence is kept as a claim with no citations (it has nothing to verify and is not dropped), or truncated to the cap with a mark; either way it is in the summary and the report says it was shortened. The time bound stays.

### Tests (sketch)

The 2.5 KB sentence survives `_verify_text` (whole or marked), `dropped_claims == 0`; 20 000 characters of `a[` still finish in under a second.

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. Parts A and B change what the
extractor answers for every language: the judge runs the extractor over this repository's own `.py`, `.js`, `.ts` and `.c` files before and after, and the blocks returned for names that were found must not change.

## Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** `--auto` on a repository with one module whose Pass B failed: the pack stays on for the others and the log says why one has no facts.

## Not in scope

The wording of the three CC-15 messages, `staleness` policies, the 2 000-character cap itself, and the extractor's languages beyond the shapes above.
