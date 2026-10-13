Target: this repository at the sha in TARGET (4ff7d14, "epic-tasks: mark KC-5 open ...").
plan.json: 53 candidates the Architect wrote on 2026-09-08 for the goal "find every accessor that returns a mutable object from internal state ... return a copy", plus AUTO-R1..R3, three defects the earlier human adjudication fixed (patches 0001/0002/0004).
truth.csv: REAL = the cited place has the described defect at the sha; FALSE = it does not (read the callers). Settled by reading the code, not by the vote.

The collect model of the target (CC-13, for `scripts/gate1_vote_layer2.py --collect-from`)
`.collect/` is not in git, so a layer-2 clone has none unless it is handed one. Build it once for the sha in TARGET and keep it outside the tree:

    git worktree add --detach /tmp/target-4ff7d14 $(cat contest-bench/cc/gate1_known_answers/TARGET)
    # a config whose active provider is a free one that answers (the repository's agents_128k.ini with the provider you use), and:
    #   [collect]  enabled = true, llm_summaries = true, skip_dirs = contest-out, rounds, claim-check-out
    #   error_retries = 10, error_retry_wait_sec = 60, max_retry_after_sec = 600   (the free providers answer 429)
    python3 main.py --collect --base /tmp/target-4ff7d14 --config <that config>
    python3 scripts/gate1_vote_layer2.py --detach --collect-from /tmp/target-4ff7d14/.collect --sets 3,3+collect,5,5+collect --off-runs 3

About an hour with a free model for the 395-943 modules (Pass B is one request per module, ~10 s each when the provider is quiet).
Rebuild only when TARGET changes. `--no-llm` builds the static part in seconds (contracts, callers, risk, fail-open): enough to measure whether the
static facts alone change the judges, and the vote packs carry only those chunks (the summaries are not in a pack).
