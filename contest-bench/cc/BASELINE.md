# CC-0 baseline — today's `claim_vote.py`, no evidence

**Date:** 2026-10-09 · **checkout:** `0df21da` (branch `cc-epic-docs`, over `main` `d7ff804`)
**Voters:** three free models of three families (a Sensenova 6.8 flash-lite, an Agnes 3.0
flash, a Nemotron 3 super), `[claim_vote]` profiles, 3 runs each, `batch = 10`.
Every voter answered every claim in every run (80/80, 30/30); no errors.
**Command:** `contest-bench/cc/run_cc.sh --profiles <the three>`
**Fixture:** base `d36d59e`, head `5b24122` · **real claims:** `real_sha` `d7ff804`

`decided` is what the tool accepts (unanimous, TRUE/FALSE); `raw_decided` is what the
models said unanimously before the tool's `CODE-CHECK` veto — the honest measure of
"can they judge it without the code".

## Fixture, 80 claims (truth at base)

| kind | claims | decided | right | wrong | raw decided | raw wrong |
|---|---|---|---|---|---|---|
| code | 50 | 0 | 0 | 0 | **0** | 0 |
| mixed | 8 | 0 | 0 | 0 | 0 | 0 |
| dangling | 6 | 0 | 0 | 0 | 0 | 0 |
| commit | 3 | 0 | 0 | 0 | 0 | 0 |
| ticket | 3 | 0 | 0 | 0 | 0 | 0 |
| world | 10 | 1 | 1 | 0 | 9 | 0 |
| **overall** | 80 | 1 (1.2 %) | 1 | 0 | 9 (11.2 %) | 0 |

Verdicts: UNSURE 70, CODE-CHECK 8, TRUE 1, FALSE 1.

## Real claims, 30 (truth at `d7ff804`, settled by `real_checks.py`)

| kind | claims | decided | raw decided | raw wrong |
|---|---|---|---|---|
| code | 20 | 0 | **0** | 0 |
| mixed / dangling / commit / ticket | 2 / 2 / 1 / 1 | 0 | 0 | 0 |
| world | 4 | 0 | 2 | 0 |
| **overall** | 30 | 0 | 2 (6.7 %) | 0 |

Verdicts: UNSURE 28, CODE-CHECK 2.

## Reading

* **Code claims decided without evidence: 0 of 50 (fixture) and 0 of 20 (real)**, even
  in raw votes. The epic's premise (§1) holds: the voters cannot see the code and say
  so. The epic goes on.
* World claims stay what CLAIM-1 found: 9 of 10 raw-unanimous on the fixture, none wrong.
  The tool's own accept count is lower (1) because `needs_code` flags world claims that
  quote identifiers (`re.M`, `subprocess.run`) — CC-1's `classify` replaces that rule.
* No target in `thresholds.json` moved: the baseline sits at 0 where the §6.2 targets
  ask for ≥ 60 % coverage, so there is nothing to recalibrate against yet.

Raw output: `claim-check-out/cc-20261009T170317Z/` (not committed).
