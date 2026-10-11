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
`~/.cache/lenz/claims.json` by section text: a second run sends nothing and
does not ask for the key (it is read only when a section is not cached yet).  A
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

### Stage 2, on the evidence: `--target` (CC-6)

A claim about the code can be judged when the voters see the code. Give the repository
the claims are about and the commit to judge at:

```bash
python3 scripts/claim_vote.py claims.json --target . --ref origin/kc --runs 3 --out votes.json
python3 scripts/claim_vote.py claims.json --target . --ref origin/kc --base origin/main \
    --report report.md --runs 3 --out votes.json      # + the git evidence of main..kc, the sha check
```

The commit is pinned once (`--ref`, resolved to a sha; `--fetch` fetches first) and checked
out as a read-only worktree under `--scratch` (default `claim-check-out/scratch/`); the
operator's own checkout is never read. Each claim's anchors are resolved there and
classified: a `world` claim goes as before; a `code` or `mixed` claim goes with its
**evidence pack** (source, git with `--base`, a note for an anchor that is not there),
`code_batch` claims per request. The rules in the prompt: the evidence is the only source
for a claim about the repository; UNSURE when it does not decide; a TRUE or FALSE names
the chunk (`[[id]]`) and copies up to 200 characters from it.

**The quote check.** A TRUE/FALSE on a claim with a pack stands only when the chunk it
names is in *that claim's* pack and holds the quote verbatim (gutters and whitespace
aside, `Pack.find_all`); otherwise it becomes UNSURE and is counted in `rejected` with the
reason, `chunk_not_in_pack` or `quote_not_in_pack`. A world claim needs neither. The
unanimity rule is unchanged and gets the checked votes, so a code claim can now be
accepted: no `CODE-CHECK` row with `--target`.

`votes.json` then has `target` (`repo`, `ref`, `sha`, `base`, `warnings`), each voter's
run with `rejected` (every downgrade, with the quote), and per claim `id`, `kind`,
`dangling`, `sha`, `evidence` (the chunk ids the votes that stood cited), `quotes`,
`rejected` (`{reason: n}`) and `downgrades`. The `verdicts:` line ends with
`rejected quotes: N`, `warnings: …` and the sha: a report that names another sha than
`--ref` (`--report report.md`, or `--expect-sha`) is a warning there, not a stop.

`[claim_vote]` sets `code_batch`, `pack_chars` (`--pack-chars`), `pack_chunks` and
`pack_chunk_chars`. The fabrication rate is a number: `rejected` per voter and per run.

Did a fix land? The same claims at two commits, `scripts/claim_diff.py claims.json --target . --base main --head origin/kc --check` (CC-7): FIXED / STILL / NEW / GONE / UNCLEAR, see [BEFORE-AFTER.md](BEFORE-AFTER.md).

The offline check of the chain (fake voters, the fixture's 80 claims, no network):

```bash
python3 contest-bench/280/acceptance_280.py
```


`claim_vote.py` prints one line per claim and writes `votes.json`:

| verdict | meaning | what to do |
|---|---|---|
| `TRUE` / `FALSE` | at least 3 models committed and agree | a world fact; accept only with the rule below |
| `SPLIT` | committed models tie | look it up, or run it |
| `UNSURE` | fewer than 3 models committed | nobody knows; ignore or check |
| `CODE-CHECK` | the claim is about this repo's code (no `--target`) | the models cannot see the code: read it, run the repro, or vote again with `--target` |

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

- **Never accept votes on a claim about our own code the voters did not see.**  (With
  `--target` they see it, and the quote check is what keeps them honest.)  A model that cannot see
  `gates.py` still says TRUE to "`_SUMMARY_LINE` does not parse node IDs with
  spaces" because it sounds right.  `needs_code` marks these; the check is a
  heuristic (paths, tickets, `identifiers`, a bare `snake_case` name the repo
  defines; ordinary slash words such as `read/write` or `TCP/IP` are not
  paths), so read the TRUE rows with a repo-specific look as well.
- **A model that gives no votes is a result.**  `votes=0/N` with an error or a
  `raw_head` in `votes.json` is a model to drop from the roster, not a bug to chase.
- **No credit is spent anywhere in this runbook.**  `/assess` and `/verify` (1
  and 10 credits) are the paid tie-break and live in `scripts/lenz_claim_filter.py`
  behind `--verify`; use them only on a `SPLIT` about a public fact.  That
  script drops repo claims too (`is_internal`), asks for the key only when a
  request must go out, and with `--json` writes `[]` when nothing is left to send.

## One command, logs included

```bash
scripts/run_claim_check.sh report.md              # extract -> vote, all in claim-check-out/<UTC>/
RUNS=5 scripts/run_claim_check.sh report.md --profiles a b c
```

Writes `claims.json`, `votes.json`, `extract.log`, `vote.log` there; the last
line of `vote.log` is the verdict counts and the models that returned no votes.

## With the code in front of the voters: into `truth.csv` and the stage-3 queue (CC-8)

```bash
scripts/run_claim_check.sh report.md --target . --ref origin/kc --truth truth.csv
scripts/run_claim_check.sh report.md --target . --ref origin/kc --base main --truth truth.csv --dry-run
python3 scripts/claimcheck_truth.py votes.json --out validate1/truth.csv --pending pending.md --append
```

`--target REPO`, `--ref REF`, `--base REF` and `--fetch` go to `claim_vote.py` as they are
(stage 2 on the evidence, above). `--truth FILE` runs `scripts/claimcheck_truth.py` at the end:
a bare name lands in `claim-check-out/<UTC>/` next to `votes.json`, and so does `pending.md`.
`--dry-run` prints the three commands and exits 0, with no network and nothing written. The
script takes both out of the options before `claim_vote.py` sees them. With neither option it
runs as it always did.

**The acceptance rule, restated.** A claim gets a `truth.csv` row only when it is settled:
`unanimous` is true and the verdict is `TRUE` (written `REAL`) or `FALSE`. A `verdict: TRUE`
with a dissenter (`by_model` TRUE, TRUE, FALSE; `unanimous: false`) is **not** settled. It goes
to `pending.md`, as do `SPLIT`, `UNSURE` and `CODE-CHECK`. A model with no vote at all is not a
voter: it is left out of `checked_by` and named as `dead:` in `pending.md`'s header.

The columns are `finding,truth,checked_by,duplicate_of,how`:

* `finding` is `<path>::<qualname>` of the claim's first found symbol, else its first found
  path. Without a target it is the path inside the first cited chunk id, else
  `claim:<sha1[:8]>`.
* `checked_by` is `claimcheck:<families>@<sha7>`, with `@<base7>..<head7>` when the votes had
  `--base`.
* `how` is the cited chunk ids plus the first quote, at most 160 characters; it is
  `world fact` for a world claim.

`votes.json` holds no pack and no resolved anchors, so the script rebuilds both at the recorded
sha. It opens a read-only worktree of exactly that commit under `--scratch`, with no model and
no network. `--target` overrides the recorded repository when a bench's clone is gone. A
target that cannot be opened is a warning, not a stop.

`--append` is idempotent: a `finding` already present with the same `truth` is skipped. One
present with another `truth` is printed as `CONFLICT` and kept as it was, with exit 3.

**Two claims about one place.** Two statements about one function can settle `REAL` and `FALSE`
in one run. Both rows are written, and the script prints a note. `harvest_report.load_truth`,
however, keeps only the last row of a `finding`: settle such a finding by hand before scoring.

Where the two files go:

* `truth.csv` is the scoring key: `harvest_report.py validate1/*.csv --truth truth.csv
  --report …`. It is also one adjudicator among others for `truth_consensus.py` (name it
  `truth-claimcheck.csv`).
* `pending.md` is the stage-3 queue (`docs/ADJUDICATE-findings.md`). Each unsettled claim
  appears once:
  * grouped SPLIT, then a plurality with a dissenter, then CODE-CHECK, then UNSURE;
  * with each voter's verdict and the rejected-quote count;
  * with the pack's chunk ids and their first three lines, so the adjudicator opens the right
    place.

Expect `pending.md` to be the larger file. Round 280 (three free voters, one run) settled 56
to 63 of the fixture's 80 claims and 10 to 16 of 30 real ones, with 0 wrong. The offline
check is `python3 contest-bench/290/acceptance_290.py`.

## Voters: which models, how many

By measured wrong rate over the 150 fixture claims (70 at base, 80 at head), two live runs
(`tickets/292-cc-10-claim-check-in-gate1.md` has the table). A claim is settled only when
every voter committed with a quote the code verified and all agree, so **more voters settle
fewer claims and are no safer**: 129 of 150 settled with 3, 125 with 5, 115 with 7, 103 with 9,
0 wrong in each.

* Default three (three families, all on kenary): `mimo-v2-6-flash`, `muse-spark-1-3-contributor`,
  `nemotron-3-ultra`. Spares: `agnes-3-0-flash`, `step-5-preview`, `hy3`.
* Keep out of the roster: `laguna-s-2-1` (8 wrong of 136), `deepseek-v4-flash` (4 + 1 wrong).
  `sensenova-6.8` is allowed as one voter of three, not as three: its free plan answers HTTP
  429 "rpm exhausted" (79 of 219 requests in the live run) and `6.7` decided nothing.
* Quorum is 3. `quorum=1` is never a default; with it set the output says so.
* Before a round, `python3 scripts/py_model_test.py MODEL…` or `arena model test` shows which
  of the roster still answers; a profile whose model is gone shows as `votes=0/N` in `vote.log`
  and is dropped, not chased.

## Closing a round: did the branch fix what its tickets claim (CC-7)

At the end of a round, or before a merge, take the claims the tickets make (a review's claims
after `claim_extract.py`, or hand-written `claims.json` with `"expect"` on each), and run
base = the branch point, head = the candidate:

```bash
python3 scripts/claim_diff.py claims.json --target . --base main --head origin/kc \
    --profiles a b c --runs 1 --out claim-check-out/diff-kc --check
python3 contest-bench/cc/score_cc.py claim-check-out/diff-kc/delta.json claims.json   # with a key
```

Read it in this order:

1. The exit code: `0` all `expect` met, `1` a mismatch (printed with both sides' evidence ids),
   `2` a refusal (a ref that does not resolve, an unknown `expect`).
2. `still_as_fixed` must be **0**: a claim that is still true reported as `FIXED` is the one
   costly error. `UNCLEAR` is an honest answer, not a failure; expect many.
3. `NEW` rows are claims that are true at head and were not at base: read them, they are what
   the branch introduced.

Cost: the two sides share one pool; a code claim is 0.5 request per voter at `code_batch = 2`.
The command opens two read-only worktrees under `--scratch` and removes them at the end.

## Planned, not landed yet

* **CC-9 (291), Lenz comparison hint.** `lenz_claim_filter.py` would pick which world claims
  are worth the one credit by the votes' disagreement. Until it lands, `--max` and `--dry-run`
  are the only controls, and no credit is spent without `--verify`.
* **CC-10 (292), claim check in Gate 1.** `[gate1] claim_vote = off` by default. The ticket's
  layer 2 is an operator step and lives here once the code has landed:
  1. `main.py --validate-plan --base REPO` on one real target; keep the Architect's candidates.
  2. `scripts/run_claim_check.sh … --truth` for the candidates the vote can settle, the rest by
     hand from `pending.md`. Record the known-answer set in the repository with its target sha.
  3. Gate 1 twice on the set, vote `off` and `on`; numbers to the round's `SUMMARY.md`.
  4. Pass only if false positives let through are not more, true bugs rejected are not more,
     and the vote decided at least a third; otherwise the flag stays `off`.

## When something goes wrong

| what you see | what it is | what to do |
|---|---|---|
| `votes=0/N` for one profile, `raw_head` in `votes.json` | the model is dead, out of credits, or rate-limited | drop it from the roster; the others still vote |
| HTTP 429 in `vote.log` | the provider's rpm (kenary 15 shared, sensenova "rpm exhausted") | lower `[claim_vote] per_provider` or raise `interval_sec`; retries are built in |
| a claim stays `UNSURE` on every run | the pack did not hold the place | check `pack_chars` and that the anchor resolves (`--target`/`--ref` right) |
| `CONFLICT` from `claimcheck_truth.py --append`, exit 3 | the same `finding` already has another `truth` | decide by hand; the file is kept as it was |
| a `finding` with both `REAL` and `FALSE` rows | two claims about one symbol | settle by hand before `harvest_report --truth` (it reads the last row only) |
| `target cannot be opened` (warning) | the recorded repository is gone | `--target PATH` to a clone that has the sha |

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
