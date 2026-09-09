# Epic round — 24 tickets, in order

One ticket per round. Every agent does the same ticket against the same
tree; you merge the winner; the next round starts from the merged tree.
The loop and the scorecard: `docs/collect-epics/RUN-THE-EPIC-COMPETITION.md`.

| # | id | severity | size | ticket | primary file |
|---|---|---|---|---|---|
| 1 | `L2` | HIGH | XS | [01-l2-probe-config-must-say-stale-not-no-artifact.md](01-l2-probe-config-must-say-stale-not-no-artifact.md) | `tools/auto/architect.py` |
| 2 | `M1` | CRITICAL | M | [02-m1-scripts-collect-metrics-py-the-static-baseline.md](02-m1-scripts-collect-metrics-py-the-static-baseline.md) | `scripts/collect_metrics.py` |
| 3 | `L1` | HIGH | S | [03-l1-gate-1-stage-a0-a-location-that-is-not-an-indexe.md](03-l1-gate-1-stage-a0-a-location-that-is-not-an-indexe.md) | `tools/auto/gate1_filter.py` |
| 4 | `V1` | HIGH | S | [04-v1-loader-keeps-the-import-graph-and-three-queries.md](04-v1-loader-keeps-the-import-graph-and-three-queries.md) | `tools/collect/loader.py` |
| 5 | `V2` | HIGH | M | [05-v2-the-block-becomes-an-ordered-row-list.md](05-v2-the-block-becomes-an-ordered-row-list.md) | `tools/auto/context_assembler.py` |
| 6 | `V3` | HIGH | S | [06-v3-rows-callers-calls-into-tests.md](06-v3-rows-callers-calls-into-tests.md) | `tools/auto/context_assembler.py` |
| 7 | `V4` | HIGH | S | [07-v4-rows-fails-open-risk.md](07-v4-rows-fails-open-risk.md) | `tools/auto/context_assembler.py` |
| 8 | `V5` | HIGH | M | [08-v5-row-neighbours-the-pass-b-payoff.md](08-v5-row-neighbours-the-pass-b-payoff.md) | `tools/auto/context_assembler.py` |
| 9 | `V6` | HIGH | S | [09-v6-wire-the-budget-and-the-memo-into-collectbridge.md](09-v6-wire-the-budget-and-the-memo-into-collectbridge.md) | `tools/auto/collect_bridge.py` |
| 10 | `M2` | CRITICAL | S | [10-m2-block-redundancy-the-number-epic-a-exists-to-mov.md](10-m2-block-redundancy-the-number-epic-a-exists-to-mov.md) | `scripts/collect_metrics.py` |
| 11 | `V7` | HIGH | M | [11-v7-collect-on-a-stale-tree-goes-incremental-fix-the.md](11-v7-collect-on-a-stale-tree-goes-incremental-fix-the.md) | `tools/collect/cli.py` |
| 12 | `V8` | HIGH | M | [12-v8-no-llm-preserves-existing-summaries.md](12-v8-no-llm-preserves-existing-summaries.md) | `tools/collect/cli.py` |
| 13 | `V9` | HIGH | M | [13-v9-freshness-is-checked-once-and-then-believed-all.md](13-v9-freshness-is-checked-once-and-then-believed-all.md) | `tools/auto/collect_bridge.py` |
| 14 | `V10` | MEDIUM | M | [14-v10-real-signatures.md](14-v10-real-signatures.md) | `tools/collect/ast_facts.py` |
| 15 | `M3` | CRITICAL | M | [15-m3-suppressor-ceiling-the-go-no-go-for-epic-b.md](15-m3-suppressor-ceiling-the-go-no-go-for-epic-b.md) | `scripts/collect_metrics.py` |
| 16 | `V11` | HIGH | M | [16-v11-pass-c-stops-eating-test-file-summaries.md](16-v11-pass-c-stops-eating-test-file-summaries.md) | `tools/collect/verifier.py` |
| 17 | `V12` | MEDIUM | S | [17-v12-gate-1-stage-a2-minimal-only-if-m3-says-the-ceil.md](17-v12-gate-1-stage-a2-minimal-only-if-m3-says-the-ceil.md) | `tools/auto/gate1_filter.py` |
| 18 | `V13` | LOW | S | [18-v13-one-safety-note-for-the-candidates-v12-does-not.md](18-v13-one-safety-note-for-the-candidates-v12-does-not.md) | `tools/auto/gate1_grounding.py` |
| 19 | `M4` | HIGH | M | [19-m4-runtime-counters-collect-events-gate-1-stage-spl.md](19-m4-runtime-counters-collect-events-gate-1-stage-spl.md) | `tools/auto/run_trace.py` |
| 20 | `M5` | HIGH | M | [20-m5-a-b-harness-one-goal-two-runs-one-diff.md](20-m5-a-b-harness-one-goal-two-runs-one-diff.md) | `scripts/collect_ab.sh` |
| 21 | `V14` | MEDIUM | S | [21-v14-turn-on-docs-mode.md](21-v14-turn-on-docs-mode.md) | `tools/auto/context_assembler.py` |
| 22 | `V15` | LOW | S | [22-v15-docs-sync-and-the-consumer-column.md](22-v15-docs-sync-and-the-consumer-column.md) | `README.md` |
| 23 | `M6` | MEDIUM | M | [23-m6-outcome-metric-precision-against-the-adjudicated.md](23-m6-outcome-metric-precision-against-the-adjudicated.md) | `scripts/` |
| 24 | `L3` | MEDIUM | S (measurement only) | [24-l3-gate-1-asks-a-presence-question-that-three-of-fi.md](24-l3-gate-1-asks-a-presence-question-that-three-of-fi.md) | `—` |

## Working these

Ground rules are restated inside every ticket. Two carry the round:
`CollectBridge._shrink` stays byte-identical, and nothing runs against a
live provider config.
