# HARDEN-16 — `void f(x);` in JavaScript or TypeScript is the `void` operator on a call but `extract_block` reports it as a declaration of `f`; the bare-token mask rewrites an `AKIA…` / `ghp_…` token that is used as an identifier (`if AKIA… in keys`, `AKIA….split()`, `{AKIA…}`) into `here_your_key` and an `/edit` writes broken code

**Status:** landed (round 314: sensenova-6-8-flash-lite-var4 as the base — in JS/TS a prefix that is only `void` is a call, so `void f(x);` is not the declaration of `f` while Java, C and the TS type members keep theirs (A); a bare provider token is left alone only where the syntax proves it is a name — an attribute, a call, an index, a comparison with a name, an f-string or template hole, an object-literal key, the name after `def`/`class`/`import`/`let`/`const` — and masked everywhere else (B))
**Severity:** MEDIUM (A: Gate 1's existence check reads a call as a definition, so a claim that a function is missing can be let through); LOW (B: a token that is also an identifier is rare, but the model is shown broken code)
**File:** `tools/block_extractor.py` (A), `tools/llm_stream.py` (B)
**Symbol:** A: `_find_earliest_declaration`, `_head_prefix_kind`, `_CALL_HEAD_WORDS`. B: `_SECRET_PREFIX_RE`, `mask_bare_tokens`, `mask_api_key`
**Round:** 314
**Size:** S
**Depends on:** HARDEN-12 (310, the bare-token masker), HARDEN-13 (311), HARDEN-15 (313, same mask file; land after it), all landed or ahead of this one
**Also touches:** `tests/`, `tests_bugfix/` (the HARDEN-12 C table and the block-extractor pins must stay green)

---

## Why

Two reports from an outside review of `264440c`, both reproduced on `0815666`. The review's third item (`redis-cli -a` is not redacted in the command log) is already Part C of HARDEN-15 (313); the two spellings it did not name (`docker login --password`, `redis-cli --pass` / `-u` URL) were added there. A part is done when it has its tests and both suites are green unchanged.

---

## Part A — `void f(x);` is a call in JavaScript and TypeScript

### What happens today

```ts
export async function track(event: Event) {
  void sendMetrics(event);
  await save(event);
}
```

`extract_block(source, "sendMetrics", ".ts")` returns `'  void sendMetrics(event);'`, a declaration; `save`, called with `await`, returns `''`. `_head_prefix_kind` reads any word before a name as a return type unless it is in `_CALL_HEAD_WORDS`; `void` is a return type in Java and C, so it is not there. Gate 1's Stage A calls `extract_block`, so `sendMetrics` counts as present in a file that never defines it.

### What it does

* In a JavaScript or TypeScript file (`lang == "js"`: `.js`, `.jsx`, `.ts`, `.tsx`) a prefix that is only `void` is a call's position. The same `void` in Java and C, and `declare function f(): void;`, an interface member `reset(): void;`, an abstract `run(): void;`, still find the declaration.
* The reviewer's patch (`v11-void-operator-is-a-call.patch`, 4 lines in `_find_earliest_declaration` and 7 test cases) is the reference: an entry that does the same by another route is as good, and an entry that makes a call of every `void` is not (the Java and C cases are the guard).
* Not only `void`: look at the other operators that can sit in front of a call in JS/TS (`await`, `typeof`, `delete`, `new`, `yield`, `!`) and say in the commit which of them the extractor already reads as a call and which it does not.

### Tests (sketch)

The `track` file: `sendMetrics` → `""`, `save` → `""`; the six declaration cases of the patch still return a declaration ending in `;`.

---

## Part B — a bare provider token used as an identifier

### What happens today

HARDEN-12 C masks `AKIA…`, `ASIA…`, `ghp_…`, `gsk_…`, `hf_…`, `xai-…`, `glpat-…` wherever they stand. On `0815666`:

```
if AKIAIOSFODNN7EXAMPLE in valid_keys:        ->  if here_your_key in valid_keys:
assert resp.key == AKIAIOSFODNN7EXAMPLE       ->  assert resp.key == here_your_key
print(f"Key: {AKIAIOSFODNN7EXAMPLE}")         ->  print(f"Key: {here_your_key}")
AKIAIOSFODNN7EXAMPLE.split(",")               ->  here_your_key.split(",")
x = (AKIAIOSFODNN7EXAMPLE).strip()            ->  x = (here_your_key).strip()
d = {AKIAIOSFODNN7EXAMPLE: 1}                 ->  d = {here_your_key: 1}
```

(`known = [AKIAIOSFODNN7EXAMPLE, AKIA11111]` also loses its first item; `AKIA11111` is too short to be a token.) The restore gives a line back that the model left identical, but an edit that touches the line writes the placeholder.

### What it does

Leaking is worse than a broken line, so the rule is narrow: a bare token is left alone only where the syntax proves it is a name, not a value:

* followed by `.`, `(` or `[` (attribute access, call, index), or preceded by `.` (an attribute of that name);
* the operand of `in`, `not in`, `is`, `==`, `!=` when the other side is not a quoted string (a comparison of two names);
* the whole content of `{…}` inside an f-string or a template literal (`${…}`), and a name after `def`, `class`, `import`, `from`, `let`, `const`, `var`, `function`.

A token in a value position stays masked: after `=` or `:` as the value (`AWS_ACCESS_KEY_ID=AKIA…`, `key: ghp_…`), inside any quotes, as a list element of an `.env`/YAML/INI line, in a header. The reviewer's suggested rule ("only on the right of `=` or `:`, in quotes or as a standalone argument") would let `export AWS_KEY AKIA…` and a bare token in prose through; do not take it as written.

### Tests (sketch)

The six lines above unchanged; `AWS_ACCESS_KEY_ID=AKIA…`, `key: ghp_…`, `token = "gsk_…"`, `Authorization: xai-…` and the HARDEN-12 C table still masked.

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. The judge runs the mask over this repository's own sources and a corpus of installed libraries and counts the lines it changes; the count must not grow against `0815666` except for real tokens.

## Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** Gate 1 on a claim "`sendMetrics` is not defined" over the `track` file is not refuted by the `void` line; `/edit` on a file with `if AKIA… in valid_keys:` that changes another line leaves that line byte-identical.

## Not in scope

Rust, the restore (HARDEN-13), the verdict parser (HARDEN-14), the other parts of the mask (HARDEN-15).

Why not the others: the round was cut at 91 minutes with no entry READY (nemotron GAVE_UP on `tests_slow`), so every worktree was committed as it stood. In a black box that drives each tree (60 checks: `extract_block` over `void`, `await`, `typeof`, `delete`, `new`, `yield`, `!`, `return`, `throw` before a call and 13 declaration shapes in ts, java, c, cpp, h, kt; `mask_api_key` over six provider token shapes in 18 name positions and 16 value positions, a mixed line, an edit round trip), sensenova-var4 and var3 passed 60, step-5-preview and var2 54, agnes 54, nemotron 53, laguna and var1 48, glm 41 (it loses the C++ and header declarations). var4 is the base because the other entries' tests ran on it 535 passed and the var4 tests fail 5 times on var3's code (a shorter sibling token, the edit of a neighbouring line); var3's own 57 pass on var4. agnes's verdict-parser work (ticket 315's part) and its tests were left out.

Found at judging and fixed in the same commit: var4's name check copied the text before the token and walked every region for each token, so a file with many tokens cost their product — it reads a window of 400 characters around the token and bisects the regions. Not taken: a token followed by `in x and …` as a name (step-5's test) — `the token ghp_… is valid and …` reads the same in prose, and leaking is worse than a broken line.
