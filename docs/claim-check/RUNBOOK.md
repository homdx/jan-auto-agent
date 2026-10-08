# Claim check — a mini runbook

Take a report a model wrote (a reviewer's bug list, a round summary), cut it into
atomic claims for free, let several free models vote on each, take away a table
of what stands and what still needs a look at the code.  Three small steps, any
of them runnable alone; nothing here spends Lenz credits.

## Stage 1 — extract (free, cached)

Hand over: the report.  Run:

```bash
export LENZ_API_KEY=...        # or leave unset: the script asks on the console
python3 scripts/claim_extract.py report.md --out claims.json
python3 scripts/claim_extract.py report.md --dry-run      # sections only, no network
```

Take away: `claims.json`, a list of `{claim, section}`.  The report is cut at its
`##`/`###` headings and each section goes to Lenz `/extract`, which is free
(1000 calls a day) and returns self-contained claims.  Answers are cached in
`~/.cache/lenz/claims.json` by section text: a second run sends nothing.  A
report of 10 sections costs 10 of the 1000 calls.

## Stage 2 — vote (free models, several runs each)

The voters are ini profiles, resolved like every other caller in the tree
(`tools.auto.llm_profile.resolve_llm_profile` over `contest.ini` +
`contest.local.ini`, called through `build_chat_request` / `request_completion`
with the retry budget of `[claim_vote]` — `error_retries`, `error_retry_wait_sec`,
`max_retry_after_sec` — and a per-host pacer, `per_provider` / `interval_sec`,
because a free tier answers a burst with 429 and the answers are lost).

One-time setup, writes three profiles from Kilo's files into `contest.local.ini`
(gitignored; the key is copied, never printed, and never world-readable), or write the sections by hand:

```bash
python3 scripts/claim_vote.py --add-profiles \
    sensenova123/sensenova-6.8-flash-lite sensenova123/sensenova-6.7-flash-lite \
    kenary/agnes-3-0-flash:free kenary/nemotron-3-super-120b-a12b:free
```

The two Sensenovas are one family: for the unanimity rule below they count as one
vote, so the panel is 3 families (Sensenova, Agnes, Nemotron) with 4 voters.

then set `llm_profiles = <the three section names>` under `[claim_vote]`, and:

```bash
python3 scripts/claim_vote.py claims.json --runs 3 --out votes.json
```

Each model gets `batch` claims per request (a long list exhausts a reasoning
model's budget before the first verdict) and is asked `--runs` times; run N
rewords the first sentence and shuffles the claim order, so a provider-side
prompt cache cannot return one answer N times.  `--fixed-prompt` sends the
identical prompt every run: the control.  `--models provider/model` reads Kilo's
files directly when a model has no profile yet.

## Stage 3 — read the table

`claim_vote.py` prints one line per claim and writes `votes.json`:

| verdict | meaning | what to do |
|---|---|---|
| `TRUE` / `FALSE` | at least 3 models committed and agree | a world fact; accept only with the rule below |
| `SPLIT` | committed models tie | look it up, or run it |
| `UNSURE` | fewer than 3 models committed | nobody knows; ignore or check |
| `CODE-CHECK` | the claim is about this repo's code | the models cannot see the code: read it or run the repro |

**The acceptance rule: unanimity across families.**  Pick one model from each of
3 different families (Sensenova, Agnes, Nemotron, ...) and accept a verdict only
when all 3 commit to it.  On 40 claims with a known answer (all subsets of the
free models that answered every claim): 36.6 right, 0.14 wrong, 3.2 undecided
(0.4 % of the decided wrong); a majority of 3 families gave 1.0 wrong (2.5 %).
Four families, unanimous: 0.08 wrong, 3.9 undecided.  Asking one model again
adds almost nothing (see `REPORT.md`: Sensenova gave the same verdict in 38–40
of 40 claims across 6 identical-prompt runs).

A model's own verdict is its **strict** plurality across runs: if the runs tie
(even number, or three different answers) the model abstains (`UNSURE`), so a
self-contradicting model is visible rather than masked by the first run's
insertion order.  A voter with no vote on a particular claim — silent, lost
batch, or unreadable reply — counts against unanimity; the rule is "every
voter", not "every voter that happened to answer".

## Rules that keep it honest

- **Never accept votes on a claim about our own code.**  A model that cannot see
  `gates.py` still says TRUE to "`_SUMMARY_LINE` does not parse node IDs with
  spaces" because it sounds right.  `needs_code` marks these; the check is a
  heuristic (paths, tickets, `identifiers`), so read the TRUE rows with a
  repo-specific look as well.
- **A model that gives no votes is a result.**  `votes=0/N` with an error or a
  `raw_head` in `votes.json` is a model to drop from the roster, not a bug to chase.
- **No credit is spent anywhere in this runbook.**  `/assess` and `/verify` (1
  and 10 credits) are the paid tie-break and live in `scripts/lenz_claim_filter.py`
  behind `--verify`; use them only on a `SPLIT` about a public fact.

## One command, logs included

```bash
scripts/run_claim_check.sh report.md              # extract -> vote, all in claim-check-out/<UTC>/
RUNS=5 scripts/run_claim_check.sh report.md --profiles a b c
```

Writes `claims.json`, `votes.json`, `extract.log`, `vote.log` there; the last
line of `vote.log` is the verdict counts and the models that returned no votes.

## Check that a re-run is free (no network)

`tests/test_claim_check.py` stubs the network and fails if a cached section or
claim goes out; run it before trusting a cache change:

```bash
python3 -m pytest tests/test_claim_check.py -q
```

## Mapping to the competition runbook

The claim is the "finding", the model is the "reviewer", `votes.json` is the
merged table.  `claims.json` can be turned into an `IMPROVEMENTS.md`-style list
and fed to `next_finding.py` / `append_finding.py` / `merge_validations.py` /
`truth_consensus.py` (`docs/RUN-THE-COMPETITION.md`, stages 2–3) when the judge
must be an agent that reads the repo; the vote above is the cheap pre-pass that
leaves those agents only the `CODE-CHECK` and `SPLIT` rows.
