# Release notes — the collect / auto-mode tuning epics (2026-09-10 … 2026-09-18)

**Branch:** `tickets`. **Range:** `5fe7737` (M1) … `e84240b` (RUN-11), 79
commits, 38 tickets landed. Every ticket was run as a per-round agent
contest (`RUN-THE-EPIC-COMPETITION.md`); the winning pieces were merged into
one ideal commit per ticket. `CollectBridge._shrink` is byte-identical to
where it started.

Plans, ledgers and the reasoning behind each round are in `archive/`;
this file is the summary: where it started, what was tuned, what it gave.

---

## 1. Where it started (2026-09-10, `competition` branch)

A traced audit of `--collect` against a 483-module tree, driven entirely
through a stub LLM, split the 2.37 MB artifact like this:

| fate of the collected facts | share |
|---|---|
| reachable by an `--auto` prompt | 38 % |
| never read by any consumer (`imports`, `except_sites`, every Pass B `summary`, …) | 48 % |
| loaded, no consumer | 6 % |
| dropped by the loader (`import_edges`, `imported_by`, `entry_points`, `sibling_gaps`) | 5 % |

The per-task context block described the very file the coder already had in
its prompt: a median block of 544 chars, 477 of them a `public_symbols`
line; **4 of 477 blocks carried a single row the file did not already
say**. Every `facts <symbol>` answer elided the signature as `name(...)`.
Pass C dropped 61 % of Pass B's claims.

Five live `--auto` runs (2026-09-09) added the operational picture: the
pack switched itself off for ~80 % of executed tasks (the artifact fell
behind HEAD after the agent's own commits; `staleness = warn` turned the
pack off for the whole session, silently); a third of the architect's
candidates got no verdict from the provider and were logged *rejected*;
blocked tasks were blocked on cut-off coder output, on a 400-char feedback
window that hid the pytest error, and on provider outages charged to the
coder as rejections.

## 2. What was tuned — by area

### 2.1 The fact pack (producer → consumer)
- **V1** loader keeps `import_edges` / `imported_by` / `entry_points`.
- **V2 · V3 · V5** the block is an ordered row list (`_PACK_ROWS`): `callers`,
  `calls_into`, `tests`, `neighbours (llm)` — the first consumer of Pass B.
- **L4 · L5 · L6 · V6** `from pkg import mod as alias` reaches the graph;
  `test_map` scans every root the graph scans; the budget loop cuts the pack
  from the least valuable end and the budget actually reaches the assembler
  (one assembly per target file per run, `[collect] pack_enabled`).
- **V7 · V9 · V8 · RUN-6** `--collect` on a stale tree is incremental,
  `--rebuild` is the full build; a committed edit invalidates only that
  path's facts; `--no-llm` keeps paid-for summaries (`--drop-summaries`
  opts out); a stale artifact at session start is refreshed when
  `auto_refresh_between_tasks = true` and stdout says stale / refreshing / OFF.
- **V10** real signatures from `facts <symbol>` (`COLLECTOR_VERSION` 1 → 2).
- **V11 · V16** Pass C stops eating test-file summaries; a bare name in a
  source summary is the symbol defined here.
- **RUN-11** Pass B checkpoints every landed summary
  (`collect_summarize_state.json`); a Ctrl-C costs one module, not the run;
  the entry line bills the true rebuild (`reason: version | sha`).

### 2.2 Gate 1 (plan validation)
- **L1** Stage A0: an unindexed location (`.md/.ini/.yaml/.json` in code
  mode) is rejected before any LLM call.
- **RUN-5** a presence check the provider never answered is `unknown`, not a
  rejection (`[gate1] presence_unknown = keep | reject`).
- **RUN-9** an empty presence reply is classified from the stream metadata
  (`finish_reason`, `usage.completion_tokens`, reasoning chars) before the
  re-ask ladder runs: *transport* is re-issued unchanged, *exhausted* keeps the
  pinned ladder plus a `think=off` last rung.
- GATE1-LEARN-1/-2, GATE1-PAR-1 (on the branch, not epic tickets): the check
  starts at the budget that answered last time, the ladder is configurable,
  presence checks run `[gate1] presence_workers` at a time.

### 2.3 The task loop (coder · executor · validator)
- **RUN-1** an extra file outside `target_files` is a warning, not a verdict.
- **RUN-2** the per-task wall-clock budget does not tick while the run is stopped.
- **RUN-3** exec feedback shows the *tail* of pytest output (the last
  `ERRORS`/`FAILURES` box, 1 500 chars); exit 5 says *no tests collected*;
  workspace pytest runs in-process (`-n 0`).
- **RUN-4** a reply cut off mid-JSON raises the next attempt's `max_tokens`
  ×2 up to `[coder] max_tokens_cap`; the learned budget is kept per task.
- **RUN-7 · RUN-8** a validator or coder call that died on the wire is
  `unavailable`, not a rejection: only that call is re-run, no attempt is
  charged, the task goes back to `todo`, the seconds inside the failing call
  are credited back to the deadline.
- **RUN-10** one HTTP retry budget for every auto-mode LLM call:
  `[loop] error_retries / error_retry_wait_sec / max_retry_after_sec`
  (defaults 60 / 10 / 180), logged at run start.

### 2.4 Measurement (the yardsticks the rounds were judged by)
- **M1 · M2** `scripts/collect_metrics.py` — one read-only pass over the
  artifact, stable JSON keys, committed `baseline.json`; block redundancy
  became a number.
- **M4** runtime counters: `collect_block / collect_shrink / collect_summary /
  collect_miss` events and the Gate-1 stage split, read by
  `scripts/trace_round_snapshot.py`.
- **M5** `scripts/collect_ab.py` — one goal, one seeded plan, two runs
  against a replay stub, one diff. Its `serve` / `configs` subcommands are
  also the standard way to run anything without touching a live provider
  config (loopback-only guard; see RUN-THE-EPIC-COMPETITION.md, "Running
  against a stub").
- **contest-bench/** (`e9a19d9`) — black-box scoring of contest entries by
  shared test data against a fake provider; first used for RUN-9 (20
  submissions, 18 unique, 45 scenarios, 211 checks; base scored 73/211).

## 3. What it gave — before / after

### 3.1 Static (`collect_metrics.py`, deterministic)

| number | before | after |
|---|---:|---:|
| tables dropped by the loader | 3 | 0 |
| blocks with ≥ 1 row the file did not already say | 4 / 477 | 497 / 497 |
| new (non-redundant) rows per block, mean | 0.008 | 3.608 |
| redundant chars / total chars | ~100 % | 65.1 % |
| signatures elided as `name(...)` | 4 030 / 4 030 | 0 |
| a re-run of `--collect` on a stale tree | full rebuild | incremental (`--rebuild` is explicit) |

### 3.2 Live — Gate 1 presence on the same tree, same goal (`e5d46fc`)

Baseline: qwen25 `777535b` (before RUN-9); after: qwen25 `945e610` (RUN-9
landed, RUN-10/11 not yet). Last complete Gate-1 pass of each clone
(`run.log`), provider sensenova-6.7-flash-lite, `think = true`.

| clone | candidates | confirmed | rejected | **unknown** | empty replies | re-asked | gate-1 wall |
|---|---:|---:|---:|---:|---|---:|---:|
| testtext, before | 744 calls | 58 | 216 | **164** | 470 / 744 (63 %) | — | 5 h 35 m |
| testtext, after | 529 | 66 | 370 | **2** | 318, all `exhausted` | 316 | 6 h 00 m |
| testtext6, before | 630 calls | 63 | 223 | **122** | 344 / 630 (55 %) | — | 3 h 30 m |
| testtext6, after | 504 | 66 | 349 | **1** | 230, all `exhausted` | 229 | 4 h 15 m |

Unknown went 286 → 3 of 1 033 candidates. The provider still returns an
empty reply for ~45 % of presence calls, but every one is now classified
and re-asked; the price is +20–25 % Gate-1 wall clock. Seconds per
candidate fell (27.1 → 19.1 and 19.6 → 14.4, over all traces incl. restarts).
Data: `baseline-run9-testtext*.json`, `after-run9-live.json`.

### 3.3 Live — the task loop (`after-r31-live.json` → `after-run6-live.json`)

| symptom in the 13–14.09 runs | ticket | after |
|---|---|---|
| pack off for ~80 % of executed tasks (stale artifact, silent) | RUN-6 | refreshed on entry, `collect_refresh` event, stdout says so |
| 142 / 403 and 95 / 375 gate-1 candidates lost to empty replies | RUN-5, RUN-9 | `unknown` kept with a note; empty replies re-asked (§3.2) |
| 6 tasks blocked on coder output cut at `max_tokens = 3000` | RUN-4 | budget raised ×2 per cut-off, per task |
| 7 tasks blocked on an error beyond the 400-char feedback cut | RUN-3 | pytest tail, 1 500 chars |
| HTTP 429 outages charged as validator/coder rejections (≈ 13 min each) | RUN-7, RUN-8 | `unavailable`, retried, nothing charged |
| 82 / 30 gate-1 LLM calls on `.md/.ini/.yaml/.json` locations | L1 | 0 (rejected in Stage A0) |

### 3.4 What stayed the same
`CollectBridge._shrink`; the artifact schema (one `collector_version` bump
for V10); the config ladder — every new behaviour is one key with its old
behaviour as an option.

## 4. Open (not part of this release)
`L3`, `V12`–`V15`, `M6` (each waits on a measurement, see
`archive/NEXT-ROUNDS.md` steps 6–10). Next epic: **KC** — the Kilo contest
(`epic-tasks/40-kc1-…` onward, `docs/kilo-contest/`).

## 5. Commit ledger
`git log --oneline 5fe7737^..e84240b` — one commit per ticket, the ticket id
is the commit's first word. Per-ticket status: `epic-tasks/INDEX.md`; the
older ledger with premises and verification notes: `archive/STATUS.md`.
