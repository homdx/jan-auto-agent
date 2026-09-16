# Epic round — 37 tickets, in order

One ticket per round. Every agent does the same ticket against the same
tree; you merge the winner; the next round starts from the merged tree.
The loop and the scorecard: `docs/collect-epics/RUN-THE-EPIC-COMPETITION.md`.

| # | id | status | severity | size | ticket | primary file |
|---|---|---|---|---|---|---|
| 1 | `L2` | landed `8212df1` | HIGH | XS | [01-l2-probe-config-must-say-stale-not-no-artifact.md](01-l2-probe-config-must-say-stale-not-no-artifact.md) | `tools/auto/architect.py` |
| 2 | `M1` | landed `5fe7737` | CRITICAL | M | [02-m1-scripts-collect-metrics-py-the-static-baseline.md](02-m1-scripts-collect-metrics-py-the-static-baseline.md) | `scripts/collect_metrics.py` |
| 3 | `L1` | landed `ff22c04` | HIGH | S | [03-l1-gate-1-stage-a0-a-location-that-is-not-an-indexe.md](03-l1-gate-1-stage-a0-a-location-that-is-not-an-indexe.md) | `tools/auto/gate1_filter.py` |
| 4 | `V1` | landed `81658b6` | HIGH | S | [04-v1-loader-keeps-the-import-graph-and-three-queries.md](04-v1-loader-keeps-the-import-graph-and-three-queries.md) | `tools/collect/loader.py` |
| 5 | `V2` | landed `72bbc86` | HIGH | M | [05-v2-the-block-becomes-an-ordered-row-list.md](05-v2-the-block-becomes-an-ordered-row-list.md) | `tools/auto/context_assembler.py` |
| 6 | `V3` | landed `b609492` | HIGH | S | [06-v3-rows-callers-calls-into-tests.md](06-v3-rows-callers-calls-into-tests.md) | `tools/auto/context_assembler.py` |
| 7 | `V4` | open | HIGH | S | [07-v4-rows-fails-open-risk.md](07-v4-rows-fails-open-risk.md) | `tools/auto/context_assembler.py` |
| 8 | `V5` | landed `da78fa3` | HIGH | M | [08-v5-row-neighbours-the-pass-b-payoff.md](08-v5-row-neighbours-the-pass-b-payoff.md) | `tools/auto/context_assembler.py` |
| 9 | `V6` | landed `76fd4bd` | HIGH | S | [09-v6-wire-the-budget-and-the-memo-into-collectbridge.md](09-v6-wire-the-budget-and-the-memo-into-collectbridge.md) | `tools/auto/collect_bridge.py` |
| 10 | `M2` | landed `5ace54f` | CRITICAL | S | [10-m2-block-redundancy-the-number-epic-a-exists-to-mov.md](10-m2-block-redundancy-the-number-epic-a-exists-to-mov.md) | `scripts/collect_metrics.py` |
| 11 | `V7` | landed `25d5de8` | HIGH | M | [11-v7-collect-on-a-stale-tree-goes-incremental-fix-the.md](11-v7-collect-on-a-stale-tree-goes-incremental-fix-the.md) | `tools/collect/cli.py` |
| 12 | `V8` | open | HIGH | M | [12-v8-no-llm-preserves-existing-summaries.md](12-v8-no-llm-preserves-existing-summaries.md) | `tools/collect/cli.py` |
| 13 | `V9` | landed `2f9005d` | HIGH | M | [13-v9-freshness-is-checked-once-and-then-believed-all.md](13-v9-freshness-is-checked-once-and-then-believed-all.md) | `tools/auto/collect_bridge.py` |
| 14 | `V10` | open | MEDIUM | M | [14-v10-real-signatures.md](14-v10-real-signatures.md) | `tools/collect/ast_facts.py` |
| 15 | `M3` | open | CRITICAL | M | [15-m3-suppressor-ceiling-the-go-no-go-for-epic-b.md](15-m3-suppressor-ceiling-the-go-no-go-for-epic-b.md) | `scripts/collect_metrics.py` |
| 16 | `V11` | landed `9406601` | HIGH | M | [16-v11-pass-c-stops-eating-test-file-summaries.md](16-v11-pass-c-stops-eating-test-file-summaries.md) | `tools/collect/verifier.py` |
| 17 | `V12` | open | MEDIUM | S | [17-v12-gate-1-stage-a2-minimal-only-if-m3-says-the-ceil.md](17-v12-gate-1-stage-a2-minimal-only-if-m3-says-the-ceil.md) | `tools/auto/gate1_filter.py` |
| 18 | `V13` | open | LOW | S | [18-v13-one-safety-note-for-the-candidates-v12-does-not.md](18-v13-one-safety-note-for-the-candidates-v12-does-not.md) | `tools/auto/gate1_grounding.py` |
| 19 | `M4` | landed `13416fb` | HIGH | M | [19-m4-runtime-counters-collect-events-gate-1-stage-spl.md](19-m4-runtime-counters-collect-events-gate-1-stage-spl.md) | `tools/auto/run_trace.py` |
| 20 | `M5` | landed `b33cb1f` | HIGH | M | [20-m5-a-b-harness-one-goal-two-runs-one-diff.md](20-m5-a-b-harness-one-goal-two-runs-one-diff.md) | `scripts/collect_ab.sh` |
| 21 | `V14` | open | MEDIUM | S | [21-v14-turn-on-docs-mode.md](21-v14-turn-on-docs-mode.md) | `tools/auto/context_assembler.py` |
| 22 | `V15` | open | LOW | S | [22-v15-docs-sync-and-the-consumer-column.md](22-v15-docs-sync-and-the-consumer-column.md) | `README.md` |
| 23 | `M6` | open | MEDIUM | M | [23-m6-outcome-metric-precision-against-the-adjudicated.md](23-m6-outcome-metric-precision-against-the-adjudicated.md) | `scripts/` |
| 24 | `L3` | open | MEDIUM | S (measurement only) | [24-l3-gate-1-asks-a-presence-question-that-three-of-fi.md](24-l3-gate-1-asks-a-presence-question-that-three-of-fi.md) | `—` |
| 25 | `L4` | landed `6f2550f` | HIGH | S | [25-l4-graph-py-drops-from-pkg-import-module-as-alias-e.md](25-l4-graph-py-drops-from-pkg-import-module-as-alias-e.md) | `tools/collect/graph.py` |
| 26 | `L5` | landed `9664530` | HIGH | S | [26-l5-test-map-scans-only-tests-the-import-graph-scans.md](26-l5-test-map-scans-only-tests-the-import-graph-scans.md) | `tools/collect/test_map.py` |
| 27 | `L6` | landed `ced5940` | HIGH | S | [27-l6-the-budget-loop-lets-public-symbols-displace-the.md](27-l6-the-budget-loop-lets-public-symbols-displace-the.md) | `tools/auto/context_assembler.py` |
| 28 | `V16` | landed `ac28672` | HIGH | S | [28-v16-pass-c-a-bare-name-in-a-source-summary-means-the-symbol-defined-here.md](28-v16-pass-c-a-bare-name-in-a-source-summary-means-the-symbol-defined-here.md) | `tools/collect/verifier.py` |
| 29 | `RUN-1` | landed `236855a` | HIGH | S | [29-run1-extra-file-write-is-a-warning-when-every-target-file-landed.md](29-run1-extra-file-write-is-a-warning-when-every-target-file-landed.md) | `tools/auto/coder.py` |
| 30 | `RUN-2` | landed `6ca675c` | MEDIUM | S | [30-run2-the-task-budget-must-not-tick-while-the-run-is-stopped.md](30-run2-the-task-budget-must-not-tick-while-the-run-is-stopped.md) | `tools/auto/outer_loop.py` |
| 31 | `RUN-3` | landed `26b5d7f` | HIGH | XS | [31-run3-exec-feedback-shows-the-head-of-pytest-output-the-cause-is-at-the-tail.md](31-run3-exec-feedback-shows-the-head-of-pytest-output-the-cause-is-at-the-tail.md) | `tools/auto/inner_loop.py` |
| 32 | `RUN-4` | landed `ade28e6` | HIGH | S | [32-run4-the-coder-answers-a-cut-off-reply-with-the-same-budget-five-times.md](32-run4-the-coder-answers-a-cut-off-reply-with-the-same-budget-five-times.md) | `tools/auto/coder.py` |
| 33 | `RUN-5` | landed (after `24d9028`) | HIGH | S | [33-run5-a-technical-failure-in-the-presence-check-is-not-a-rejection.md](33-run5-a-technical-failure-in-the-presence-check-is-not-a-rejection.md) | `tools/auto/gate1_filter.py` |
| 34 | `RUN-6` | landed `7135844` | HIGH | S | [34-run6-a-stale-artifact-at-session-start-switches-the-pack-off-for-the-whole-session.md](34-run6-a-stale-artifact-at-session-start-switches-the-pack-off-for-the-whole-session.md) | `tools/auto/collect_bridge.py` |
| 35 | `RUN-7` | landed (the commit after `acbfd61`) | HIGH | S | [35-run7-validator-unavailable-is-not-a-rejection.md](35-run7-validator-unavailable-is-not-a-rejection.md) | `tools/auto/inner_loop.py` |
| 36 | `RUN-8` | landed (the commit after `97983d8`) | HIGH | S | [36-run8-a-transport-failure-mid-stream-is-charged-to-the-coder.md](36-run8-a-transport-failure-mid-stream-is-charged-to-the-coder.md) | `tools/auto/coder.py` |
| 37 | `RUN-9` | **open** | HIGH | M | [37-run9-an-empty-presence-reply-is-not-a-garbled-verdict.md](37-run9-an-empty-presence-reply-is-not-a-garbled-verdict.md) | `tools/auto/gate1_filter.py` |

## Next rounds — the order (as of `6ca675c`, 2026-09-14; step 1 landed — `7135844` (+`5788423`, `d4ffb12`); step 2 landed — RUN-3; step 3 landed — `ade28e6`; step 4 landed — RUN-5)

Set from the live runs on `../testtext` / `../testtext6`; the reasoning and
the measurement after each step are in
`docs/collect-epics/NEXT-ROUNDS.md`. Rounds are one ticket each, in this
sequence; a step is not started until the previous one is merged.

| step | ticket | why here | measure after |
|---|---|---|---|
| 1 | `RUN-6` | the pack is off in every resumed session; nothing collect-side can be measured live until it is on | `collect blocks > 0` on the next session's snapshot |
| 2 | `RUN-3` | 7 of 29 BLOCKED tasks ended on an exec error the coder never saw; XS | exec-rejected attempts per BLOCKED task |
| 3 | `RUN-4` | the most frequent coder failure in both trees; 6 BLOCKED tasks | `cut off` count, done/blocked ratio |
| 4 | `RUN-5` | *landed* — 25–35 % of candidates dropped by provider silence; plan size is a coin flip until fixed | `unparsed` → `unknown` column, plan size |
| 5 | `RUN-7` | *landed* — live `../testtext2` (`baa9da87a2ab`): a validator outage (ollama.com 429) was recorded as 5 rejections, the task went BLOCKED unreviewed | `validator_status = unavailable` rows, tasks left `todo` instead of BLOCKED |
| 6 | `RUN-8` | *landed* — same run: one 80-min hung coder stream = one burned attempt + the whole task budget → BLOCKED after one round | `cod transport` column, deadline credited |
| 7 | `L1` | *landed* `ff22c04` — deterministic: 82 / 30 gate-1 calls on non-`.py` locations | non-`.py` gate-1 rows → 0 |
| 8 | `M3` | measurement only; decides whether V12/V13 exist | ceiling number in `EPIC-M-metrics.md` |
| 9 | `V4`, `V10` | supply rows — worth it only once step 1 puts the pack in front of the coder | M2 static numbers, block sizes |
| 10 | `V12`/`V13` | only if step 8 says ceiling > 0 | gate-1 stage split |
| 11 | `M6` | outcome metric against `validate1/truth.csv`; needs a plan whose membership is not decided by step 4's bug | precision/recall |
| 12 | `V8`, `V14`, `V15`, `L3` | low value now; V14 only if docs mode is used at all | — |

After steps 1–4 land: reset one tree to its `pre_run_sha`, `--collect
--refresh`, one full `--auto` execution run, snapshot with `--run-id`, then
the M5 A/B with recordings from that run. That is the first point at which
"did the pack help" has a live answer.

## Hand-out

`scripts/next_task.py --tasks epic-tasks/ --progress runs/<name>/PROGRESS.csv`
offers only tickets whose `**Status:**` is neither `landed` nor `queued`
— i.e. the one ticket of the current round. Landing it (status → `landed`)
or the agent recording it (`append_task.py`, `DONE` = `FIXED`) takes it off
offer; flipping the next ticket from `queued` to `open` starts the next
round.

## Working these

Ground rules are restated inside every ticket. Two carry the round:
`CollectBridge._shrink` stays byte-identical, and nothing runs against a
live provider config.
