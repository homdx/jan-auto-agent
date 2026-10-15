# HARDEN-10 — four regressions of the last rounds: a bodyless arrow function is "not found", the secret mask rewrites code that holds no secret, "Nothing found" and "Not a problem" are rejections, a root-level package loses its imports in a monorepo

**Status:** open — FINAL, written 2026-10-15 against `2c5bd52`. Four defects from an outside review, each **reproduced by running the code** on that commit (the output is below). A and D are consequences of HARDEN-6 (C, D), B of HARDEN-5 B, C of HARDEN-5 A. The code is the authority, not this ticket: if a line has moved, the symbol names the place.
**Severity:** HIGH (A: Gate 1 drops a task citing a one-line arrow function); MEDIUM (B, C); LOW (D)
**File:** `tools/block_extractor.py` (A), `tools/llm_stream.py` (B), `tools/auto/inner_loop.py` (C), `tools/collect/graph.py` (D)
**Symbol:** A: `_bodyless_candidate_patterns`, `_BODYLESS_SCAN_BOUND`. B: `_API_KEY_LINE_RE`, `_BEARER_TOKEN_RE`, `mask_api_key`. C: `_en_rejection_reason`, `_parse_verdict_soft`, `_EN_NOT_TOKENS`. D: `build_module_index`, `resolve_import`, `ModuleIndex.ambiguous_aliases`
**Round:** —
**Size:** M
**Depends on:** HARDEN-5 (303), HARDEN-6 (304), HARDEN-7 (305), all landed
**Also touches:** `tests/`, `tests_bugfix/` (pins that must stay green: HARDEN-1 A8, HARDEN-5 B, HARDEN-6 C and D)

---

## Why

The last rounds closed real bugs and each one cost something it should not have: a pattern made too strict, an anchor dropped, a family of words taken whole. A part is done when it has its tests, the old cases it fixed still pass, and both suites are green unchanged.

---

## Part A — `const now = () => Date.now();` is "not found"

### What happens today

The two bodyless arrow patterns of HARDEN-6 C take the parameter list as `\([^)\n]{1,400}\)`: at least one character, no newline. On `2c5bd52`, `extract_block(source, name, "ts")`:

| source | name | result |
|---|---|---|
| `export const now = () => Date.now();` | `now` | `''` |
| `export const f = (): string => 'a';` | `f` | `''` |
| `const g = <T>() => 1;` | `g` | `''` |
| `class A {⏎  foo = () => 1;⏎}` | `foo` | `''` |
| `const h = (⏎  a,⏎  b⏎) => a + b;` | `h` | `''` |
| `const end = () => process.emit('x');` | `end` | `''` |

`''` is "not found": Gate 1 reads it as a hallucination and rejects the task that cites the function. A parameter list longer than 400 characters is lost the same way.

### What it does

* An empty parameter list, a parameter list over several lines and a return-type annotation or a type-parameter list in front of `=>` are found, in `.js`, `.jsx`, `.ts`, `.tsx`. The scan stays linear: a 267 KB source is still extracted in well under a second.
* No result changes for any name the extractor finds today.

### Tests (sketch)

The rows above; the HARDEN-4 D2 and HARDEN-6 C tables and the linear-time test green unchanged; the extractor over this repository's own `.py`, `.js`, `.ts`, `.c` and `.cpp` files before and after returns the same block for every name found before.

---

## Part B — the mask rewrites code that holds no secret

### What happens today

Removing the `^…$` anchors in HARDEN-5 B let the value pattern `[^\s'";]+` match a lone `=`, and the Bearer token class accepts `$`, `{` and `.`. On `2c5bd52`, through `mask_api_key`:

```
assert client.api_key == key                 ->  assert client.api_key =here_your_key key
f'Bearer {self.settings.api_key}'            ->  f'Bearer here_your_key}'
`Bearer ${process.env.OPENAI_API_KEY}`       ->  `Bearer here_your_key}`
the bearer authentication_scheme is used     ->  the bearer here_your_key is used
api_key = r"abc123abc"                       ->  api_key = here_your_key"abc123abc"
api_key = b'abc123abc'                       ->  api_key = here_your_key'abc123abc'
```

The model is shown broken code, and a reply that "fixes" the line no longer matches what `restore_masked_secrets` looks for.

### What it does

* A comparison (`==`, `=~`, `!=` written with `=`) is not an assignment; a string prefix (`r`, `b`, `f`, `u`, `rb` …) before the quote belongs to the quoted value, and the quote pair is kept; a Bearer token starts on a letter or a digit and stops at a bracket, `$`, `<` or `>`; a word in prose after "bearer" is not a token (the 16-character rule stays).
* The secrets HARDEN-5 B masks (`Authorization: Bearer sk-or-v1-…`, the trailing-comment and prose forms) are still masked.

### Tests (sketch)

The rows above; the HARDEN-1 A8 and HARDEN-5 B tables green unchanged; this repository's own `.py` files are byte-identical before and after the mask except for the lines that already changed at `1fa8599`.

---

## Part C — the NOT family rejects everything that begins with a negation

### What happens today

HARDEN-5 A made `NOT …` a rejection, and the family is taken whole: every line that begins with `NOT` or `NOTHING` is a rejection except three approvals. On `2c5bd52`, `_parse_verdict_soft(line)` returns a rejection (`False`) for `Nothing to flag`, `Nothing found`, `Nothing contradicts the story bible`, `Not a problem`, `Not contradictory`, `NOT REJECTED`; at `1fa8599` all of them were taken as approvals. A separate old gap: `No contradictions found`, `No errors`, `No inconsistencies`, `No conflicts` are rejected with the reason "contradictions found." (the word after `No`).

A creative-mode validator's rejection goes back to the Coder as a fix request, so each of these costs an iteration for nothing.

### What it does

* `NOT` or `NOTHING` is a rejection when the word after it is a verdict word on the negative side (`NOT APPROVED`, `NOT OK`, `NOT GOOD`, `NOT ACCEPTABLE`, `NOT VALID`, `NOT CONSISTENT` …); `NOT REJECTED`, `NOT A PROBLEM`, `NOT CONTRADICTORY`, `NOTHING TO FLAG`, `NOTHING FOUND` and `NOTHING CONTRADICTS …` are approvals.
* `No <contradiction | error | inconsistency | conflict | violation | objection | issue>s?` (and `found` after it) is an approval, as the Russian "нет противоречий" already is.

### Tests (sketch)

Both lists above; `NOT APPROVED`, `NOT BAD`, `NOT OK` and the other HARDEN-5 A pins unchanged.

---

## Part D — a package at the repository root loses its imports in a monorepo

### What happens today

HARDEN-6 D counts an alias claimed by two sub-projects as ambiguous and resolves it for nobody. With `app/` at the root and `svc_a/app/` and `svc_b/app/` beside it, the root package's own modules are among the claimants only by accident of the alias, and on `2c5bd52`:

```
resolve_import("app.models", index, "app/views.py")        ->  None     (should be app/models.py)
resolve_import("app.models", index, "tests/test_root.py")  ->  None     (should be app/models.py)
resolve_import("app.models", index, "svc_a/app/models.py") ->  svc_a/app/models.py   (right)
```

The graph shows the root package's views and its tests as importing nothing.

### What it does

* An alias that is the module's **own dotted name** (a root-level `app/models.py` is `app.models`) is not ambiguous: it resolves for an importer outside every sub-project's root to that module. The sub-projects' aliases stay contested among themselves.

### Tests (sketch)

The layout above; the HARDEN-6 D monorepo tests (two services, no root package) green unchanged.

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. Part A and Part B change what a shared tool answers for every file: the judge runs the extractor and the mask over this repository's own sources before and after.

## Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** A task citing `export const now = () => Date.now()` is confirmed by Gate 1; `/edit` of a file with `assert client.api_key == key` shows the model that line as written.

## Not in scope

The wording of the Coder's refusals, the codec guess of legacy files (HARDEN-9, Part C), and a bare identifier that does not name the key (`self.api_key = token`), which the restore step covers.
