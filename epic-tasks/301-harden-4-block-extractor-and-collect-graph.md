# HARDEN-4 — seven defects in what the model of a repository gets wrong: JSX, bodyless functions, src/ layouts, C macros, except rationales, is_safe root, dirty flag

**Status:** open — FINAL, written 2026-10-14 against `e9e5d13`; every defect below was reproduced (`run`) or read in the code (`read`) on that commit. The code is the authority, not this ticket: if a line has moved, the symbol names the place.
**Severity:** MEDIUM (D2 and D3 change what Gate 1 and the risk index see on a whole language or layout; D1 and D4 are regressions of fix 32)
**File:** `tools/block_extractor.py`, `tools/auto/gate1_filter.py`, `tools/collect/graph.py`, `tools/collect/test_map.py`, `tools/collect/registries.py`, `tools/collect/loader.py`, `tools/collect/manifest.py`
**Symbol:** `_js_regex_can_start` / `_lang_skip`, `_find_definition_open_brace`, `_module_dotted_name`, `_handler_rationale`, `CollectModel._already_safe_index`, `is_dirty`
**Round:** —
**Size:** L
**Depends on:** CC review fixes 34–36 (landed, `c2ef95b`): this ticket starts from their code
**Also touches:** `tests/test_block_extractor_regressions.py`, `tests/test_collect_layouts.py` and one pinned test per defect in `tests_bugfix/`

---

## Why

Two outside reviewers listed small defects; each entry here was checked against the code and **reproduced** or **read** (marked `run` / `read` below). Most of their other entries were already fixed or wrong and are refused in HARDEN-1's table. Each defect below is a wrong answer on an input nobody tested; each needs the guard the sibling code already has. A defect is done when it has its test and the suites are green unchanged (`.smoke_tests/ .regression_tests/`, then `tests_bugfix -n 4`).

## the block extractor and the collect graph (what the model of a repository gets wrong)

Seven defects from a third list, each re-run on `307da52` before it was written here. D1 and D4 are **regressions of fix 32** (the lexer skip added to
`_lang_skip`): both worked in `35f6f98` before it. D2 and D3 change what Gate 1 and the risk index see on a whole language or layout, so they come first.

### D1. A JSX closing tag is read as a regex literal — `run`

**Where:** `tools/block_extractor.py`, `_js_regex_can_start` and the js branch of `_lang_skip`.
**Evidence:** in a `.tsx` file
```
export function List({items}) {
  return <ul>{items.map(i => <li key={i}>{i}</li>)}</ul>;
}
export function Other() { return 1; }
```
`extract_block(src, "List", ".tsx")` returns `List` **and** `Other`: after `<` a regex may start, so `</li>)}</ul>` is skipped from the first `/` to the next `/` on the
line, swallowing the `}` that closes the outer brace. Same for `{ok && <b>yes</b>}</p>`. Gate 1 grounding and the Coder's fetch get every following function as part of the target.
**Fix:** in the js branch, a `/` whose previous character is `<` is a closing tag, not a regex start: skip nothing.
**Test:** the three shapes above return exactly the one function; a real regex literal after `(`, `=`, `,` is still skipped (the case fix 32 was made for).

### D2. A function with no `{ … }` body is "not found" — `run`

**Where:** `tools/block_extractor.py`, `extract_block` / `_find_definition_open_brace`; the caller is Gate 1 Stage A (`tools/auto/gate1_filter.py`, `_check_existence`).
**Evidence:** `extract_block` returns `''` for every one of these, so Stage A answers *"symbol not found"* for a function that exists:

| source | ext |
|---|---|
| `const double = (x) => x * 2;` | `.ts` |
| `const clamp = (a, b) =>\n  a + b;` | `.ts` |
| `const f = function () {}` | `.js` |
| `module.exports = function m() {}` | `.js` |
| `var F = func() {}` | `.go` |
| `fun ff() = 1` | `.kt` |

`function triple() {}` is found. The arrow pattern matches, but `_find_definition_open_brace` finds no `{` and the match is thrown away; `const f = function` has no
pattern at all. The architect's schema asks for "a function or class name", so these are legitimate citations that Stage A drops as hallucinations (when the collect model is absent, stale,
or `skip_llm_for_unindexed = false`; with a usable model Stage A0 drops `.ts`/`.js` candidates first).
**Fix:** when the head matches and no `{` follows, return the text up to the `;` or the end of the logical line at depth 0 (a continuation line belongs to it). Add
`(const|let|var) name = (async )?function` and `module.exports = function` patterns; the Go `var F = func` and the Kotlin/Scala `fun f() = …` forms get the same rule.
**Test:** each row above returns its definition; `doesNotExist` still returns `''`; a body with braces is unchanged; a multi-line arrow ends at its `;`.

### D3. A `src/` layout has no import graph and no test map — `run`

**Where:** `tools/collect/graph.py`, `_module_dotted_name` / `build_module_index` / `resolve_import`; `tools/collect/test_map.py` (the importer resolution).
**Evidence:** `_module_dotted_name("pkg/core.py")` is `pkg.core` and `import pkg.core` resolves; `_module_dotted_name("src/pkg/core.py")` is `src.pkg.core`, so the same
import resolves to `None`. A flat and a `src/` copy of the same code give the flat one its edges and test map, the other none: every module looks like an entry point
with blast radius 0 and no tests, the risk index cannot rank anything, and the callers/tests rows of the collect block are empty.
**Fix:** start the dotted name at the first directory, walking up from the file, that is a package root (the directory is not itself a package: it holds no `__init__.py`
where its children do), and strip a leading `src` / `lib` the same way; `test_map`'s importer resolution uses the same rule. Keep the full path as a fallback key so
nothing that resolves today stops resolving.
**Test:** the four-file repo in both layouts yields the same edge and test-map content; a repo that has a real top-level package called `src` keeps resolving.

### D4. A multi-line C macro cuts the block — `run`

**Where:** `tools/block_extractor.py`, the C branch of `_lang_skip`.
**Evidence:** inside `int f(int x) { … }` a `#define SW(a,b) do { \⏎ a = b; \⏎ } while (0)` ends the block at the macro's `}`: the `#` skip jumps to the first
newline, so the `{` on that line is ignored and the `}` on the continuation line is counted. The extracted `f` stops before `return x;`.
**Fix:** while the skipped line ends in `\`, continue to the next newline.
**Test:** `f` ends at its own closing brace with the macro inside; a macro without a continuation is unchanged.

### D5. A rationale comment on its own line under `except` is not seen — `run`

**Where:** `tools/collect/registries.py`, `_handler_rationale`.
**Evidence:** `except OSError:  # why` gives `why`; `except OSError:` followed by `# why` on its own line, then `pass`, gives `None` (run). Only the `except` line and the first
statement's line are looked at. The reviewer counted 7 of 178 undocumented fail-open sites in `tools/` with a comment line directly under the `except` (for example
`tools/agent_trace.py:192`, `tools/backoff.py:221`); the two sites were read, the count was not repeated.
**Fix:** also accept own-line comments between the `except` line and the first body statement.
**Test:** the own-line case returns the comment; the inline case and a handler with no comment are unchanged.

### D6. `is_safe()` defaults its root to the `.collect/` directory — `read` (latent)

**Where:** `tools/collect/loader.py`, `CollectModel._already_safe_index` (`root=root or self.collect_dir`).
**Evidence:** the index joins repo-relative paths onto the root, and the default root is the artifact directory, so a query without `root=` answers `unknown`. In-repo callers
(`suppress_for_root`) pass the real root, so nothing hits it today, and it fails closed — that is why it is Low.
**Fix:** store the repo root on `CollectModel` when it is loaded and default to it.
**Test:** `is_safe()` with no `root` answers as it does with the repo root, for a known contract.

### D7. `dirty` is true on every build when the collect dir is nested under an untracked parent — `run`

**Where:** `tools/collect/manifest.py`, `is_dirty`.
**Evidence:** with the collect dir `out/collect` and `out/` untracked, `git status --porcelain` prints `?? out/`, a path that is not under `out/collect/`, so the filter
keeps it: `is_dirty` is `True` (run); with the plain `.collect` it is `False` (run). The flag is only written into the manifest, so the effect is cosmetic.
**Fix:** `--untracked-files=all` on the status call, so the entries are files and the exclusion by path works.
**Test:** the nested layout is not dirty; a really untracked source file still is.

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries, so nothing here can be written to pass them: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. D1 and D4 are one lexer (`_lang_skip`) and go together. D2 and D3 change a result on a whole class of repositories, so the judge runs Gate 1's `_check_existence` on the D2 table and builds a collect model of a `src/` repository for D3 before accepting. The black box also runs the extractor over this repository's own `.py`, `.js`, `.ts` and `.c` files before and after: the blocks it returns for names that were found must not change. D5–D7 are small.

## Not in scope

Parts A, B, C are HARDEN-1 (298), HARDEN-2 (299), HARDEN-3 (300). A rewrite of the extractor's lexer.
