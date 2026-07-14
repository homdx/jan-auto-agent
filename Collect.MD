# `collect` mode — full review (all epics, A–H)

**Project:** `jan-auto-agent` · **Branches:** `fixes-bugs-3` (design + Python epics A–G),
`feature-collect` (Java epic H, built on top of the implemented Python pipeline)

---

## 1. What this is and why

Every bug-hunt session used to start from zero: the agent re-derived how the system
works by reading files one at a time, and that re-derivation is where fabrication crept
in — guards that already existed got missed, line numbers drifted, deliberate
`except: pass` fail-open sites got re-reported as "silent exception bugs," and claims
like "`stack[-1]` crashes" survived review because nobody checked that an early return a
few lines up already guarded it. The last audit report's false positives were
concentrated in exactly two categories: unguarded-access claims and silent-except
claims.

`collect` mode builds a **durable, git-pinned ground-truth model of the codebase once**,
persists it to files, and lets every other mode (`auto` in its `code`/`docs`/`creative`
task-modes, and a future `bughunt` mode) **read** that model instead of re-deriving it.
The model is grounded in structural facts (AST/tree-sitter, imports, config reads,
guard/except inventory) — never LLM prose — so a downstream reasoner can check every
candidate claim against it before reporting. That check is the direct antidote to the
false-positive problem.

Two requirements held from day one, and they shape every epic below:

1. **Produce → persist.** `collect` scans the code and writes files; it never edits code.
2. **Consume if present, degrade if absent.** Nothing that reads the artifact ever
   *depends* on it — remove `.collect/` and every mode behaves exactly as it does today.

---

## 2. Shape of the system (producer / artifact / consumer)

```
                       ┌─────────────────────────────┐
   PRODUCER            │        collect mode         │   reads the repo,
                       │  Pass A (static) → Pass B    │   writes ground-truth files
                       │  (LLM) → Pass C (verify)      │
                       └──────────────┬──────────────┘
                                      │ writes
                                      ▼
                          .collect/  (the artifact set)
                                      │ reads (if present & fresh)
                       ┌──────────────┴──────────────┐
   CONSUMER            │   auto (code/docs/creative)  │   inject relevant slices
                       │   doc mode / future bughunt  │   into context; else fall back
                       └─────────────────────────────┘
```

- **Pass A (structural, authoritative):** AST-walk every module, extract symbols,
  imports, config reads, except-handler classification, and guarded/unguarded indexed
  accesses. Zero LLM calls. Cannot hallucinate — it's reading the tree, not summarizing
  prose.
- **Pass B (LLM, advisory):** for each module, feed the already-extracted Pass A facts
  plus source to the existing `llm_stream` helper and ask for a one-paragraph `purpose`
  and any contract/edge notes. May only *annotate*, never introduce a structural claim
  Pass A didn't establish.
- **Pass C (verification gate):** every Pass B claim that references code passes a
  citation-check and a contradiction-suppression check before it's allowed into the
  artifact. This is the mechanism that keeps Pass B from being able to lie even if it
  tries.

The **artifact set** (`.collect/`) is the contract between producer and consumer — two
layers, JSON (authoritative, diff-able, queryable) and Markdown (a pure render of the
JSON, never hand-authored):

```
.collect/
├── collect_manifest.json      # provenance: git SHA, timestamp, per-file hash, tool versions
├── ARCHITECTURE.md            # top-level synopsis (Pass B, on top of Pass A facts)
├── modules.json               # per-module structural record (authoritative)
├── MODULE_MAP.md              # human render of modules.json
├── call_graph.json            # imports + call edges, entry points → leaves
├── CONFIG_MAP.md / config.json# every config key: section/key/fallback/reader/mode-override
├── CONTRACTS.md               # cross-module invariants ("already-safe" registry)
├── FAIL_OPEN_REGISTRY.md      # every intentional except/fail-soft/fail-open site + rationale
├── GATES.md                   # validation gate stack: protocol, order, fail mode
├── TEST_MAP.md / tests.json   # module ↔ test coverage; thin/zero-coverage list
├── RISK_INDEX.md              # ranked audit worklist
├── GLOSSARY.md                # domain vocabulary
└── verification_report.json   # Pass C: every dropped claim + reason (transparency)
```

Each `modules.json` record carries, per field: `path`/`sha`/`loc` (identity), `purpose`
(LLM, grounded), `public_symbols`, `imports`/`imported_by`, `config_reads`,
`except_sites` (type, body kind, `is_fail_open`), `guarded_accesses` (guard location or
`UNGUARDED`), `gates`, `tests`, `risk_score`, and — from Epic H — `language`.

---

## 3. Full epic breakdown

### Dependency graph

```
EPIC A (foundation: schema + provenance + determinism)
   └─> EPIC B (Pass A: structural extraction)
          ├─> EPIC C (killers: fail-open / contracts / guarded)
          ├─> EPIC D (maps: tests / risk / config / gates)
          └─> EPIC E (Pass B summaries + Pass C verification gate)
                 └─> EPIC F (CLI/config producer)
                        └─> EPIC G (consumer: loader / bughunt / auto-doc)

EPIC H (Java 17+ backend) — parallel to B/C, layered on top of the finished
Python pipeline; does not touch A, E, F, or G's logic, only extends their inputs.
```

Critical anti-hallucination path: **A → B → E (COLLECT-17) → G (COLLECT-22)**. These
four points carry the actual "the model won't make things up" guarantee — everything
else (D, F, most of G, all of H) is coverage, ergonomics, or a second language, not the
guarantee itself.

### EPIC A — Foundation (anti-hallucination scaffolding)

| Task | What | Anti-hallucination role |
|---|---|---|
| COLLECT-1 | Provenance-tagged dataclass schema; structural fields write-once from AST, LLM whitelist limited to `{purpose, notes}` | Physically prevents Pass B from overwriting a Pass A fact |
| COLLECT-2 | `collect_manifest.json`: git SHA, dirty flag, timestamp, per-file hash; `is_fresh()` | Stale artifact can never masquerade as fresh |
| COLLECT-3 | Byte-identical Pass A output on repeat runs; golden-fixture mini-repo harness | Determinism = checkability; non-determinism would hide fabrication |

### EPIC B — Pass A: structural extraction (authoritative, no LLM)

| Task | What |
|---|---|
| COLLECT-4 | AST scanner: modules, `public_symbols` (signature + docstring first line), imports. Reuses `RepoIngest.walk`. Broken file → `parse_error`, never an exception. |
| COLLECT-5 | `config_reads` extractor: `[section, key, fallback, reader_module, has_mode_override]`, honoring the `{key}_{task_mode}` convention |
| COLLECT-6 | `except_sites` classifier: type, body kind (`pass`/`log`/`re-raise`/`continue`/`return`), `is_fail_open` — the extractor for the category that caused 58% of the last audit's false positives |
| COLLECT-7 | `guarded_accesses`: local dataflow for `[i]`/`[-1]`/`[0]`, labeling `GUARDED(<location>)` or `UNGUARDED` — the second false-positive killer |
| COLLECT-8 | Import/call graph + `imported_by` reverse index + entry points |

### EPIC C — False-positive killer registries

| Task | What |
|---|---|
| COLLECT-9 | `FAIL_OPEN_REGISTRY` built from `except_sites`, with location + rationale |
| COLLECT-10 | `CONTRACTS` registry: seeded (hand-listed known invariants — e.g. `build_chat_request`'s 2-entry `messages`, ollama's `options.num_predict` cap, `_parse_verdict_soft`'s fail-open behavior, `prompt_store._save`'s atomic write) + derived from graph/guards. Seeds live in YAML, citation-checked so they can't silently rot. |
| COLLECT-11 | Unified "is X safe?" query stitching guarded/fail-open/contracts, consumed by the Epic G loader |

### EPIC D — Audit maps (worklist, not guarantee)

| Task | What |
|---|---|
| COLLECT-12 | `TEST_MAP`: module ↔ test cross-reference; thin/zero-coverage list |
| COLLECT-13 | `RISK_INDEX`: LOC + blast radius + UNGUARDED count + undocumented-silent-except count + coverage, ranked |
| COLLECT-14 | `CONFIG_MAP`: key × reader × fallback × mode-override matrix, cross-checked against sibling `.ini` profiles |
| COLLECT-15 | `GATES` registry: protocol, parser, fail-mode, extra-call cost, config switch per gate |

### EPIC E — Pass B summaries + Pass C verification (LLM enters here, and is caged here)

| Task | What |
|---|---|
| COLLECT-16 | Summarizer: one bounded `llm_stream` call per module on top of Pass A facts; writes only `purpose`/`notes`; `--no-llm` skips it entirely |
| COLLECT-17 ⭐⭐ | **Verification gate.** Citation-check (referenced symbol/line must exist and match) → drop `no-citation`; contradiction-suppression (claim vs. recorded guard/fail-open) → drop `contradicts-guard`; provenance stamp (survivors stay `llm`, never promote to `static`). All drops logged to `verification_report.json`. |
| COLLECT-18 | JSON→Markdown renderers for the whole human layer — Markdown can never disagree with JSON because it's derived from it |

### EPIC F — Producer: CLI + config

| Task | What |
|---|---|
| COLLECT-19 | `/collect`, `--collect`, `--check` (freshness only), `--refresh` (full rebuild), `--module <path>` (incremental) — same grain as existing `/auto`/`/edit` |
| COLLECT-20 | `[collect]` section in `agents.ini`: `dir`, `enabled`, `use_in_auto/doc/bughunt`, `staleness`, `llm_summaries`, `think` — all safe-fallback |

### EPIC G — Consumer: loader + bughunt + auto/doc (where the payoff lands)

| Task | What |
|---|---|
| COLLECT-21 | Loader: absent → "no model" (today's behavior); stale → per `staleness` (`warn`/`refresh`/`ignore`); fresh → query API. Malformed artifact treated as absent, never an error. `"safe?"` answers from `static` facts only. |
| COLLECT-22 ⭐⭐ | **Bughunt suppression.** Candidate contradicting a **static** fact → auto-suppressed/downgraded; candidate contradicting only LLM prose → not suppressed. Replayed against the "78 bugs" reference set: known false positives suppressed, the one real bug (verdict-parser preamble drop) is not. |
| COLLECT-23 | Opt-in context injection into `auto`/`doc`: loader attaches a file's `modules.json` record + contracts + mode config-override when `use_in_auto`/`use_in_doc=true` and the artifact is fresh. Disabled = byte-identical to today's context (regression-tested). |
| COLLECT-24 | Incremental refresh: only changed modules re-collected by manifest hash diff; reverse-index/risk cheaply recomputed; unchanged tree triggers zero LLM calls |

### EPIC H — Java 17+ support (second language backend, layered on the finished pipeline)

Built on `feature-collect`, on top of the already-implemented `tools/collect/*`
pipeline — **not** a rewrite of the Python path, and not built from the original design
doc. Target: Java 17+ (records, sealed classes/interfaces, pattern-matching `switch`,
text blocks), parsed via `tree-sitter-java` (a fixed decision up front — `javalang`'s
grammar lags 17+ syntax, so no separate parser-choice spike was needed, which is why
several of the original Python-epic-shaped tasks got merged here).

| Task | What |
|---|---|
| COLLECT-25 | Language dispatch (`Language = python | java`) in `scanner.py` by file extension; `tree-sitter-java` wrapper with the same fail-soft contract as Python (`parse_error`, never an aborting exception); `ModuleRecord.language` field, defaulted so existing artifacts don't break |
| COLLECT-26 | Java symbol extractor (classes/interfaces/enums/records/sealed types + `permits`, methods, access modifiers, Javadoc first line) and import extractor, feeding the same `ModuleRecord.imports` shape so `graph.py` stays language-agnostic except for a Java package-layout resolution branch |
| COLLECT-27 | Java `try/catch` classifier (empty `catch{}` ≈ Python's `except: pass`) and null-check/bounds-guard dataflow (NPE in place of IndexError as the dominant failure mode) — **reuses the existing `ExceptSite`/`GuardedAccess` types unmodified**, no Java-specific duplicate schema |
| COLLECT-28 | Verifies `render.py`/`loader.py`/`bughunt_filter.py` are already language-neutral (fix only what tests catch); adds `[collect] languages` (default `python`, opt-in `python,java`) and `java_extensions` config |
| COLLECT-29 | Java call graph, deferred and kept separate since overload/virtual-dispatch resolution is materially harder than Python's unique-name heuristic — only unambiguous method→class edges are recorded; explicitly **not** part of the anti-hallucination chain (mirrors COLLECT-8 vs. COLLECT-9/10 in the Python epic) |

**Critical path for this epic:** COLLECT-25 → COLLECT-26 → COLLECT-27 → COLLECT-29
(COLLECT-28 can land alongside or after COLLECT-27; nothing else in the epic depends on
it).

---

## 4. How the mode is actually used

### Command surface

Same grain as the existing `/`-prefixed commands (`/auto`, `/faq`, `/search`, `/edit`)
and the `--auto` flag — `collect` doesn't invent a parallel system:

- **`/collect`** (interactive) / **`--collect`** (one-shot) — build or refresh the
  artifact set for the current `--base` directory, then exit.
- **`/collect --check`** — freshness report only, writes nothing.
- **`/collect --module <path>`** — incremental re-collect of a single file.
- **`/collect --refresh`** — force a full rebuild even if fresh.

`collect` is **read-only against the source tree** — it never edits code, only writes
under its own artifact directory (`COLLECT-19`'s AC is literally that it's physically
incapable of touching a file outside `[collect] dir`). That's what makes it safe to run
anytime, including mid-session before an audit.

### Configuration (`[collect]` section, `agents.ini`)

| Key | Default | Meaning |
|---|---|---|
| `dir` | `.collect` | artifact directory |
| `enabled` | `false` | master switch for the producer |
| `use_in_auto` / `use_in_doc` / `use_in_bughunt` | `false` | per-mode consumption toggles |
| `staleness` | `warn` | `warn` \| `refresh` \| `ignore` |
| `llm_summaries` | `true` | run Pass B; `false` = structural-only artifact |
| `think` | `false` | strip reasoning tokens from the artifact, same convention as other agents |
| `languages` (Epic H) | `python` | explicit `python,java` turns on the Java backend |
| `java_extensions` (Epic H) | `.java` | which files the Java backend scans |

Every key follows the same rule: absence of the section or key reproduces exactly
today's behavior. Consumption toggles also support the existing `{key}_{task_mode}`
override convention (e.g. usable in `creative` mode but not `code` mode).

### Consumption / injection points

1. **`bughunt` (future, primary consumer):** before reporting any candidate, the loader
   supplies the guard inventory, fail-open registry, contracts, and risk index for the
   file in question. A candidate contradicting a recorded static guard is
   suppressed/downgraded automatically (COLLECT-22) — this is where the false-positive
   rate actually collapses.
2. **`auto` / `doc`:** when the architect/coder is about to touch a file, the loader
   injects that file's `modules.json` record and relevant contracts into context
   (COLLECT-23) — fewer regressions, fewer "I didn't know X called this" mistakes. Purely
   opt-in and off by default.

Consumption is always defensive: a malformed or partial artifact is treated as
"absent," never as an error that blocks a mode — this is asserted directly in
`COLLECT-21`'s tests (`test_collect_loader_query.py` covers corrupted JSON explicitly).

### Should the artifact be committed?

Two viable stances, called out in the design doc rather than settled by an epic:
git-ignore the JSON layer (always fresh-ish, no diff noise, must rebuild per clone) vs.
commit the Markdown synopsis under `docs/collect/` (reviewable in PRs, and a surprising
`CONTRACTS.md` diff becomes its own drift signal, at the cost of needing to refresh it
in the same commit as code changes). The manifest's staleness check protects either
choice.

---

## 5. Testing summary

- **29 new test files** across Epics A–H (list in the Jira breakdown), plus two fixture
  repos: `tests/fixtures/collect_mini_repo/` (Python, COLLECT-3) and
  `tests/fixtures/collect_mini_repo_java/` (Java, COLLECT-26).
- The core-guarantee tests are `test_collect_model_provenance.py` (A),
  `test_collect_determinism.py` (A), `test_collect_except_sites.py` /
  `test_collect_guarded_access.py` (B), and above all
  `test_collect_verifier_citations.py` / `test_collect_verifier_contradiction.py` /
  `test_collect_verifier_provenance.py` (E) and `test_collect_bughunt_suppression.py`
  (G) — these are what's actually being asserted when the system claims "the model
  won't make things up."
- Every mixed-repo test (`test_collect_graph.py`, `test_collect_render_java.py`,
  `test_collect_bughunt_suppression_java.py`) exists specifically so Epic H doesn't
  regress Epic G's guarantees for a two-language repo.

---

## 6. Non-goals & cautions (unchanged from the design doc, still binding)

- `collect` does not fix or edit code — the producer is read-only against the tree,
  full stop.
- Structural facts (Pass A / Epic B, and Epic H's Java equivalents) are authoritative;
  LLM prose (Pass B / Epic E) is advisory and cannot override a guard or fail-open
  record.
- A stale artifact must never masquerade as fresh — the manifest + hash check
  (COLLECT-2) is load-bearing; the loader (COLLECT-21) treats doubt as absence.
- Consumption is additive, never required — deleting `.collect/` returns every mode to
  exactly its current behavior.
- Java call-graph resolution (COLLECT-29) is deliberately out of the anti-hallucination
  chain — it's coverage, not guarantee, and is allowed to skip rather than guess.

---

## 7. MVP slices (if cutting scope)

- **Python pipeline:** `A(1,2,3) → B(4,6,7) → C(9,10,11) → E(17) → G(21,22)` — roughly
  14 tasks, no Pass B LLM summaries and no auto/doc injection: a purely structural
  collector plus the verification gate plus the bughunt filter. Everything else
  (maps, render, CLI polish, auto/doc injection, incremental refresh) is a second pass.
- **Java backend:** `COLLECT-25 → COLLECT-26 → COLLECT-27` — a working Java 17+ scan
  with symbols, imports, and the fail-open/guarded-access registries (the same category
  that killed 58% of Python-side false positives). COLLECT-28 (CLI toggle + consumer
  verification) and COLLECT-29 (call graph) can land in a second pass; nothing in the
  Java MVP depends on them.
