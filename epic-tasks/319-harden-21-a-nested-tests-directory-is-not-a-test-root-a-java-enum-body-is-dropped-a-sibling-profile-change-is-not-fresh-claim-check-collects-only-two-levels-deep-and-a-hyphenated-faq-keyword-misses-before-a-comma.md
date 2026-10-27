# HARDEN-21 — test detection knows only this repo's layout (`b/tests/test_b.py`, `src/pkg/tests/`, `*_test.py`, `__tests__/`), a Java enum body is dropped, a sibling-profile edit is "up to date", claim-check collects only two directories deep, and a hyphenated FAQ keyword is missed before a comma

**Status:** landed (round 319: sensenova-6-8-flash-lite-var4 as the base for A, B, E, with three borrowings — A: a path component named `tests`, `test` or `__tests__` makes the path test code at any depth, `*_test.py` is a test module, `contest.py` / `latest/` / `attestation/` are not; B: the enum body declarations are walked, a record's compact constructor is a symbol whose signature is the record's own components (`Point(int x, int y)`, var3's idea), an `@interface` and its elements are symbols, a Javadoc survives a run of line comments; C: var1's `profile_hashes` in the manifest, which `is_fresh` compares only when the caller passes them, so `loader.load` keeps calling a tree with sibling profiles fresh, and a tree that names no `sibling_profiles` tracks no profile, so an edit of its `agents.ini` costs nothing; D: `Target.collect` hands `RepoIngestor` a copy of the caller's config with `[search] max_depth = 8` when the config names no depth (the claim-check's `contest.ini` has no `[search]`), the claim scripts pass their ini, a truncated scan says so in `warnings`; E: `make_kw_pattern` at module scope, `\b` for a plain keyword as before, `(?<!\w)`-style edges for a keyword with a non-word character)
**Severity:** MEDIUM (A, B, D: wrong evidence for the Architect, Gate 1 and the claim voters on other target repos); LOW (C, E)
**File:** `tools/collect/test_paths.py` (A), `tools/collect/java_facts.py` (B), `tools/collect/cli.py`, `tools/collect/manifest.py` (C), `tools/claimcheck/target.py`, `scripts/claim_vote.py`, `scripts/claim_diff.py` (D), `tools/faq_agent.py` (E)
**Symbol:** A: `is_test_path`, `is_test_module`, `TEST_PATH_PREFIXES`. B: `_walk_type_body`. C: `_freshness`, `is_fresh`. D: `Target.collect`. E: `_make_kw_pattern` inside `_rank_candidates`
**Round:** 319
**Size:** M
**Depends on:** none
**Also touches:** `tests/` (new files only), `tests_bugfix/` pins of the collect test map must stay green

---

## Why

An outside review of `5c403f1` found these (its #44–#46, a depth report and a FAQ keyword report). All were reproduced by the reviewer on the current head; none is a regression, and none shows on this repository's own layout (root-level `tests/`, `tests_bugfix/`, Python, two directories deep), which is why they stayed hidden. They matter for the other target repos the Architect and the claim-check run on. A part is done when it has its tests and both suites are green unchanged.

---

## Part A — a test directory that is not at the root is not a test root

### What happens today

`is_test_path` is true only for a path under `TEST_PATH_PREFIXES` (root-level `tests/`, `tests_bugfix/`, …) or a known basename. So:

* a package with its own `b/tests/test_b.py`: `test_map["b/core.py"] == []`, `core.py` is reported as zero coverage, the test file is itself listed as an untested shipped module, and `callers_of("b/core.py")` returns it as a caller;
* `src/pkg/tests/`, `src/test/java/…`, `test/foo.test.js` and `__tests__/` are not test paths;
* `tests/m2_test.py` is not credited by `is_test_module` (pytest collects it: `tests/m2_test.py::test_run`).

### What it does

* Any path component named `tests`, `test` or `__tests__` makes the path test code, at any depth; the existing prefixes and basenames stay.
* `is_test_module` accepts `*_test.py` as well as `test_*.py`, inside a test root.
* A shipped module whose name only *contains* the word (`contest.py`, `latest/x.py`, `attestation/`) is not a test path.

### Tests (sketch)

The cases above, a table of paths both ways, and the Architect's untested-module list for a tree with `b/tests/test_b.py`.

---

## Part B — everything inside a Java enum body is dropped

### What happens today

`_walk_type_body` does not recurse into `enum_body_declarations`: for `enum Color { RED; Color() {} int rgb() {…} static Color parse(String s) {…} interface Visitor {…} class Helper {…} }` the methods, the constructor and the nested types are all missing; `pull_symbol` returns nothing for them. Compact record constructors (`public Point { … }`) and `@interface` types are skipped too. A Javadoc followed by a `// …` comment before the method also loses its doc line (minor).

### What it does

* Recurse into the enum body declarations; add `compact_constructor_declaration` (its own signature branch) and `annotation_type_declaration`.
* The Javadoc is still found when a line comment sits between it and the method.
* Java only; the Python extractor shapes are untouched.

### Tests (sketch)

An enum with a constructor, two methods, a nested interface and a nested class; a record with a compact constructor; an `@interface`; each member is listed and `pull_symbol` returns its body.

---

## Part C — editing a sibling profile does not make the collect model stale

### What happens today

Only `.py` files are hashed, but `CONFIG_MAP.md` also depends on the `[collect] sibling_profiles` `.ini` files. After fixing a sibling profile, `--collect` and `--check` answer "already up to date" and `CONFIG_MAP.md` keeps the fixed gap (a new gap does not appear either) until some `.py` file is touched.

### What it does

Hash the primary and sibling `.ini` files into the manifest (so `is_fresh` sees them), or recompute `sibling_gaps` on every run (cheap). Say which in the commit. A tree without the setting costs nothing.

### Tests (sketch)

Collect, edit the sibling profile, `--check` says stale, collect again rewrites `CONFIG_MAP.md`; an untouched tree stays "up to date".

---

## Part D — claim-check collects only the first two directory levels

### What happens today

`Target.collect(config=None)` takes a config but `scripts/claim_vote.py` and `scripts/claim_diff.py` call it without one, so `RepoIngestor` falls back to `max_depth = 2` while the pipeline's `agents.ini` sets 8. A project with the same file at depths 0–8 had only depths 0–2 scanned (3 of 27 files). In a `src/` layout every module of a nested package sits at depth 3 or more: `import beta.util.models` resolves to `src/beta/__init__.py` because `beta.util.models` is not in the index, and 11 of 18 import forms failed to resolve on a three-level project (the flat layout resolved all 18). The import graph, callers, config reads, gates and contracts in the evidence packs are missing for deep files, and the voters get fewer facts without any warning.

### What it does

* The claim-check path passes the pipeline config (or, with none, a default of 8) to `collect()`, so the depth is the same as in the pipeline.
* The collect cache key still tells two configs apart.
* A truncated scan says so in `warnings` when it hits the limit.

### Tests (sketch)

A tree with a module at depth 5: its import resolves to the right file under claim-check, with and without a config; the depth default is read from the config when given.

---

## Part E — a hyphenated or multi-word FAQ keyword matches only before a space, a period or a slash

### What happens today

In `_rank_candidates`, `_make_kw_pattern` builds, for a keyword with a non-word character, `(?:^|(?<=[/.\s]))<kw>(?=[/.\s]|$)`. For `ansible-playbook`: `Use ansible-playbook, then …`, `See ansible-playbook: …`, `Run (ansible-playbook) …` and `Did you try ansible-playbook?` do not match; `The ansible-playbook tool` and `ansible-playbook.yml` do. A comma, colon, bracket or question mark is the usual punctuation of both documentation and questions, so the best-scoring file can score zero and fall under the cutoff `_answer_smart` uses.

### What it does

The edges are "no word character next to the keyword" (`(?<!\w)` / `(?!\w)`), the same meaning `\b` has for plain keywords, and the slash and period cases keep matching. Move the builder to module scope so it can be tested.

### Tests (sketch)

The six lines above plus `ansible-playbooks` (no match), `preansible-playbook` (no match), a keyword with a space, and a keyword with a leading or trailing non-word character (`c++`, `.env`).

---

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries; the entries' tests are cross-tested against every other entry's code and the judge reads every diff. Java and Python shapes only.

## Acceptance

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** A claim-check run on a `src/`-layout project shows the deep module's import edge in the evidence pack.

## Not in scope

Rust, Ruby, C#, Dart (extractor shapes outside Python, Java, Kotlin), the `build_call_edges` builder and the Java import graph (not reviewed), the `IMPROVEMENTS.md` section anchor (HARDEN-20 D).

---

## Why not the others (round 319)

Ten entries (nemotron left only a task file, glm ended in ERROR on an overloaded provider, laguna fixed two files). A black box of 106 checks (the path table both ways and a collect of a tree with `b/tests/test_b.py`, Java enum / record / `@interface` shapes, sibling-profile edits with a relative and an absolute config path and a tree that names none, a git repository with a module at depth 5 and configs with and without `[search]`, 25 keyword lines) scored the base 32, nemotron 32, glm 35, laguna 64, var2 99, var3 99, agnes 100, step-5 101, var1 102, var4 103; the ideal 106. var4 was the only entry with the `@interface` elements and the cleanest Java, but its freshness check made `loader.load` call every tree with sibling profiles stale (agnes and step-5 the same) and its depth fix was never reached from `contest.ini`, which has no `[search]`, as was agnes's; var1 had both right but hashed `agents.ini` for every tree and changed a plain keyword's edge, so `docker` no longer matched `docker-compose`; var2 ignored a depth in the config; var3 kept the depth fix out of the claim scripts. var4 and step-5 also implemented ticket 320 in their trees (`inner_loop.py`); that part is left for round 320.

Open for the next tickets: `scripts/judge_epic_round.py` still calls `prepare_sides` / `judge_both` without the ini, so the judge's own claim-check walks at the default depth.
