# Claim check — a mini runbook

Take a report a model wrote (a reviewer's bug list, a round summary), cut it into
atomic claims for free, let several free models vote on each, take away a table
of what stands and what still needs a look at the code.  Two small steps (extract,
vote), either runnable alone, and with `--target` a third that writes `truth.csv` (CC-8);
nothing here spends Lenz credits.

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
python3 scripts/claim_vote.py claims.json --target . --ref origin/<head> --runs 3 --out votes.json
python3 scripts/claim_vote.py claims.json --target . --ref origin/<head> --base origin/main \
    --report report.md --runs 3 --out votes.json      # + the git evidence of main..<head>, the sha check
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
A pack also carries the target's collect facts (`collect:callers:…`, contracts, risk), built
once per tree — about 25 s on this repository, nothing on a small one; `--no-collect` leaves
them out. `votes.json`'s `target` records `base_sha`, `budget` and `collect`, so the CC-8
rebuild shows the packs the voters saw.

Did a fix land? The same claims at two commits, `scripts/claim_diff.py claims.json --target . --base main --head origin/<head> --check` (CC-7): FIXED / STILL / NEW / GONE / UNCLEAR, see [BEFORE-AFTER.md](BEFORE-AFTER.md).

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
insertion order.  A voter with no vote on a particular claim in any run — silent, lost
batch, or unreadable reply — counts against unanimity; the rule is "every
voter", not "every voter that happened to answer". A voter that voted on the claim in one run
and lost it in another is judged by the votes it cast (CC-11's ticket: "its own majority
decides, as today"): `--runs 3` with one vote and two lost batches is that one vote. That is `--accept all`, the default;
`--accept committed` (CC-11, below under "Voters") lets such a voter abstain without blocking.

## Rules that keep it honest

- **Never accept votes on a claim about our own code the voters did not see.**  (With
  `--target` they see it, and the quote check is what keeps them honest.)  A model that cannot see
  `gates.py` still says TRUE to "`_SUMMARY_LINE` does not parse node IDs with
  spaces" because it sounds right.  `needs_code` marks these; the check is a
  heuristic (paths, tickets, `identifiers`, a bare `snake_case` name the repo's own
  code defines — not a test, a bench or a fixture; a bare `_private` one only when that code
  defines it once and no article makes it a noun, "the _lock"; ordinary slash words such as
  `read/write` or `TCP/IP` are not paths), so read the TRUE rows with a repo-specific look as well.
- **A model that gives no votes is a result.**  `votes=0/N` with an error or a
  `raw_head` in `votes.json` is a model to drop from the roster, not a bug to chase.
- **No credit is spent anywhere in this runbook.**  (CC-9 prints the commands that would, never runs them.)  `/assess` and `/verify` (1
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
scripts/run_claim_check.sh report.md --target . --ref origin/<head> --truth validate1/truth.csv
scripts/run_claim_check.sh report.md --target . --ref origin/<head> --base main --truth validate1/truth.csv --dry-run
python3 scripts/claimcheck_truth.py votes.json --out validate1/truth.csv --pending pending.md --append
```

`--target REPO`, `--ref REF`, `--base REF` and `--fetch` go to `claim_vote.py` as they are
(stage 2 on the evidence, above). `--truth FILE` runs `scripts/claimcheck_truth.py` at the end:
a bare name lands in `claim-check-out/<UTC>/` next to `votes.json`, and so does `pending.md` —
**a new directory every run**, so `--truth truth.csv` writes a new file each time and never
appends to the shared key; give the shared file's path (`validate1/truth.csv`, as above) for that.
An existing FILE is appended to (`--append`: hand-checked rows stay, a disagreeing row is a
conflict, exit 3); `--force` replaces it. `--dry-run` prints the three commands and exits 0,
with no network and nothing written. The script takes these out of the options before
`claim_vote.py` sees them. A failed vote (a refused `--target`, a crash) ends the script with
the vote's exit code and no truth step.

**The acceptance rule, restated.** A claim gets a `truth.csv` row only when it is settled:
`unanimous` is true, the verdict is `TRUE` (written `REAL`) or `FALSE`, and the voters behind it
are of at least three families (three `agnes-*` models agreeing are one opinion: pending, under
"fewer than 3 model families"; a trailing version is not a family, `qwen3` = `qwen-2.5`). A `verdict: TRUE`
with a dissenter (`by_model` TRUE, TRUE, FALSE; `unanimous: false`) is **not** settled. It goes
to `pending.md`, as do `SPLIT`, `UNSURE` and `CODE-CHECK`. A model with no vote at all is not a
voter: it is left out of `checked_by` and named as `dead:` in `pending.md`'s header.

The columns are `finding,truth,checked_by,duplicate_of,how`:

* `finding` is `<path>::<qualname>` of the claim's first found symbol, else its first found
  path. Without a target it is the path inside the first cited chunk id, else
  `claim:<sha1[:8]>`.
* `checked_by` is `claimcheck:<families>@<sha7>`, with `@<base7>..<head7>` when the votes had
  `--base`. Under `--accept committed` the families are those that committed on that claim.
* `how` is the cited chunk ids plus the first quote, at most 160 characters; it is
  `world fact` for a world claim. Under `--accept committed` it starts with
  `<n> of <m> voters committed; `, so a row that stood over abstainers says so; `pending.md`
  names a pending claim's abstainers (`abstained:`).

`votes.json` holds no pack and no resolved anchors, so the script rebuilds both at the recorded
sha. It opens a read-only worktree of exactly that commit under `--scratch`, with no model and
no network. `--target` overrides the recorded repository when a bench's clone is gone. A
target that cannot be opened is a warning, not a stop.

`--append` is idempotent: a `finding` already present with the same `truth` is skipped. One
present with another `truth` is printed as `CONFLICT` and kept as it was, with exit 3.

**Two claims about one place.** Two statements about one function can settle `REAL` and `FALSE`
in one run. Both rows are written, and the script prints a note. `harvest_report.load_truth`
leaves such a finding unscored and names it on stderr, and `truth_consensus.py` counts this file as
UNDECIDED on it (its report lists it under "Contradicting themselves"): settle it by hand.

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
every voter committed with a quote the code verified and all agree (`--accept all`, the default),
so **more voters settle fewer claims and are no safer**: 129 of 150 settled with 3, 125 with 5,
115 with 7, 103 with 9, 0 wrong in each. With five voters or more run `--accept committed` (CC-11):
at least three committed and every committed voter agrees, an abstainer does not block.

* Default three (three families): `step-5-preview`, `muse-spark-1-3-contributor`,
  `nemotron-3-ultra`. Spares: `agnes-3-0-flash`, `hy3`, `mimo-v2-6-flash` (flips, never the only one).
* Measured again by running this runbook on free models only (nine live runs over the same 150
  fixture claims; "decided" = a verdict with a quote the code verified, "wrong" = against the
  fixture's truth, alone, before any unanimity):

  | voter | decided | wrong | use |
  |---|---|---|---|
  | nemotron-3-ultra, hy3, agnes-3-0-flash, muse-spark-1-3, step-5-preview, mimo-v2-6-flash | 139–145 | 0 | roster |
  | nemotron-3-super | 131 | 0 | spare |
  | vercel_8080 `inclusionai/ling-3.1-flash-free` | 130 | 0 | spare; the other ling-3.0 variants and `laguna` through the proxy gave 0 votes or 7 wrong |
  | agnes-2-5-flash | 124 | 0 (5 wrong of 128 in an earlier run) | spare, moves between runs |
  | north-mini-code | 98 | 2 | no |
  | laguna-s-2-1 | 141 | 7 | no |
  | publicai gemma-sea-lion-27b, qwen-sea-lion-32b, apertus 8b / 70b / v1.5-70b | 33–138 | 8–46 | no: what they commit to is wrong 9 to 52 % of the time, and gemma and apertus put a quote that is not in the pack in 20 to 35 % of their answers; bielik gave no vote |
  | zai `glm-4.7-flash` | 34 | 12 | no: z.ai answers 429 "overloaded" on most requests |

  `publicai` needs a `User-Agent` header (Cloudflare error 1010 otherwise); `claim_vote.py` sends one.
  A voter outside the roster is judged the same way before it joins: run the fixture with it and read the
  `wrong` column of its decided claims; a model that is wrong even 1 time in 50 alone is kept out.
* **Stability** (does a model say the same thing twice?). The same fixture asked again of the same
  model, 9 to 12 pairs of runs each, three of them with the prompt reworded and the claims reshuffled
  (`--runs 3`): `nemotron-3-ultra`, `muse-spark-1-3`, `step-5-preview`, `agnes-3-0-flash` never
  changed a verdict (0 flips), and the number of claims a run left undecided moved by 0 to 8 of 150.
  **`mimo-v2-6-flash` flips**: on one claim it said the wrong thing in 3 of its 6 runs, and only the
  other two voters not agreeing kept it out of `truth.csv`. It stays a voter, never the only one.
  So a roster is chosen by *flips* as well as by wrong alone: the 0-flip models first.
* Three voters, one run: 135 to 141 of 150 settled, 0 wrong, in five runs of four different trios
  (the same default trio gave 136 and 141). Five voters with **three runs each** (a model's verdict is
  the one two of its three runs agree on): **140 settled, 0 wrong**, with the current rule, and
  `FIXED` 18 of 20, `STILL` 10 of 10, `NEW` 2 of 2, `GONE` 2 of 2 in `claim_diff.py`: the best
  table of all the runs. It costs 5 × 3 = 15 voter runs, about 1000 requests: two hours at kenary's
  pace, so it is the setting for a decision, not for every round.
* More voters in a single run under `--accept all` (every voter that voted at all must commit,
  all to one verdict) only lose claims: five good voters settle 93 to 132 of 150, three settle 98 to
  140. Five or seven good voters settle **146 of 150** with 0 wrong under `--accept committed`
  ("at least three committed and every committed voter agrees"; a voter that said `UNSURE` or died
  does not block), replayed offline by `python3 contest-bench/cc/replay_rules.py`.
* **Which rule (CC-11).** `--accept all|committed` on `claim_vote.py`, `claim_diff.py` (both sides,
  one rule) and through `run_claim_check.sh`; `[claim_vote] accept` in the ini, the option wins; a
  value other than the two is one line and exit 2. Three voters need nothing (the two rules are the
  same when everyone commits). Five or more from the roster: `--accept committed`. Mixed with weak
  voters it is safe at seven (0 of 6435 sets wrong) and not at five (12 of 3003 sets settled a wrong
  claim), so a five is taken from the roster only. Under `committed` a row lists `abstained`,
  `votes.json` (and `delta.json`) record `"accept"`, and a `truth.csv` row says how it stood:
  `how` starts with `3 of 5 voters committed; `, `checked_by` names the families that committed on
  that claim. Families stay required: three committed voters of one family are no row.
  The default flips to `committed` only after CC-10's Gate 1 set shows no more wrong with it.
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
    --profiles claim_vote_llm.kenary_step_5_preview_free \
               claim_vote_llm.kenary_muse_spark_1_3_contributor_free \
               claim_vote_llm.kenary_nemotron_3_ultra_550b_a55b_free \
    --runs 1 --out claim-check-out/diff-kc --check
python3 contest-bench/cc/score_cc.py claim-check-out/diff-kc/delta.json claims.json   # with a key
```

`--profiles` takes the **full ini section name**, prefix included
(`claim_vote_llm.<slug>`, what `claim_vote.py --add-profiles` writes; `grep '^\[claim_vote_llm'
contest.local.ini` lists them). A bare slug fails in 1 s with `no [slug] section` and every
claim comes back `UNCLEAR`, which looks like dead models and is only a wrong name.

Read it in this order:

1. The exit code: `0` all `expect` met, `1` a mismatch (printed with both sides' evidence ids),
   `2` a refusal (a ref that does not resolve, an unknown `expect`).
2. `still_as_fixed` must be **0**: a claim that is still true reported as `FIXED` is the one
   costly error. `UNCLEAR` is an honest answer, not a failure; expect many.
3. `NEW` rows are claims that are true at head and were not at base: read them, they are what
   the branch introduced.

Cost: the two sides share one pool; a code claim is 0.5 request per voter at `code_batch = 2`.
The command opens two read-only worktrees under `--scratch` and removes them at the end.

What to expect from free models (five live runs of three voters on the fixture: 20 `fixed`,
10 `still`, 2 `gone`, 2 `new` claims): `FIXED` 17–18 of 20, `STILL` 8–10 of 10, `GONE` 2 of 2,
`NEW` 1–2 of 2, **`still_as_fixed` 0 in every run**; the rest is `UNCLEAR` (one voter said
`UNSURE`, or a quote was not in the pack). `--check` counts `UNCLEAR` against an `expect`, so it
exits **1** on a free-model run even when nothing is wrong: look at `still_as_fixed` and at the
`MISMATCH` lines, do not read the exit code alone. The same trio varies by 1–2 claims between
runs (136, 141 settled of 150). A run takes about 20 minutes: 213 requests at the 15 rpm of kenary.

## Claim check in Gate 1 (CC-10), and CC-11's operator step

* **CC-11 (293), landed: its operator step.** `--accept committed` is described under "Voters"
  above; the default stays `all`. Layer 2 of the ticket: `claim_diff.py … --accept committed` with
  the five-voter roster and `--runs 3` on the fixture (about two hours at kenary's pace) must give
  wrong settled 0, `still_as_fixed` 0, and not fewer accepted than the three-voter run.
* **CC-10 (292), landed (local): `[gate1] claim_vote`, off by default.** With `claim_vote = off`
  (or the key absent) Gate 1 is what it was: nothing is built, no counter is added. With `on`,
  Stage B asks the voters of `vote_profiles` (a profile section of the ini Gate 1 runs with, else of
  `contest.ini` / `contest.local.ini` where `claim_vote.py --add-profiles` writes them, or Kilo's
  `provider/model`; **three, of three families** — the family of the **model** a section names, never
  of the section's name — otherwise the vote stays off and says why in the log) before it asks the
  single reader. The claim is
  "`<symbol>` in `<file>` has the problem: <the instruction>", its pack is built from the candidate's
  own tree (`base_dir`, indexed once a run), each voter is asked once for it (**three requests a
  candidate**, in parallel, through the `claim_vote` pacer), and the verdict is `claim_vote.tally`'s
  with CC-8's families rule:
  * unanimous `FALSE` -> `rejected` with the voters' quotes, **unless the reader says
    `confirmed`** (`vote_veto_by_reader = on`, the default; the override is `votes_overridden`).
    A reader that gave no answer did not say `confirmed`, so the rejection stands (ticket question 2);
  * unanimous `TRUE` (three committed, three families, quotes found in the pack) -> `confirmed`
    without the reader (`vote_confirm = on`, the default); with `vote_confirm = off` the vote only
    vetoes and the single reader decides (the first layer-2 run below found the TRUEs mostly wrong);
  * anything else (split, UNSURE, a quote not in the pack, under `vote_accept = all` a voter with no
    vote on the candidate, a cited file not in the tree, an empty instruction, a test file under
    `vote_skip_tests = on`, a claim with no code anchor, the pack empty, past `vote_max_per_run`,
    any error) -> no decision, the single reader
    answers as before. A file the instruction says to create ("add a test in `tests/…`") does not
    count: only the cited file and symbol must exist.

  **What the vote does to Gate 1, both ways.** With `vote_confirm = on` it is *less* strict than the
  reader: a TRUE it confirms skips the reader and the reader's own grounds to reject (a docstring
  that calls the code a deliberate example, an intentional design, a test helper). It is *stricter*
  where a unanimous FALSE meets a reader with no answer: under `presence_unknown = keep` the reader
  alone would have kept that candidate.

  **Voters that fail.** A voter that cannot be resolved (no section, no key) is named in the log
  once, before the first vote, and left out. The vote waits as long as the `claim_vote` pacer's queue
  needs: on one host two requests at a time, four seconds apart, each with 2 × `vote_timeout_sec` once
  it has its slot, behind the requests of the other votes still in flight there (`presence_workers` > 1),
  plus 5 s. A voter that gives no reply twice in a row (nothing by that bound, or its request failed
  after its retries: a timeout, a refused connection, an HTTP error; or the voter raised) is dropped for the rest of the run;
  its requests carry the deadline, so they stop retrying, and its threads never keep the process alive. When the voters
  left cannot decide (under `all`: any voter gone; else fewer than three of three families) the vote
  stops for the run and the plan line says so.

  Keys: `vote_profiles`, `vote_max_per_run` (200), `vote_timeout_sec` (60, a fraction kept),
  `vote_confirm` (on), `vote_skip_tests` (off; on: a candidate citing a test file is not voted),
  `vote_veto_by_reader` (on), `vote_accept` (all). `vote_quorum` is not a knob:
  any value but 3 turns the vote off (one voter is not this ticket's). The plan phase prints
  `🗳 Gate 1 vote: asked …, decided …, overridden by the reader …, requests …, skipped …`, and
  `counters` gains `votes_asked`, `votes_decided`, `votes_overridden`, `votes_requests`,
  `votes_skipped` (candidates that reached the vote and were not voted on; only with the vote on).
  Default roster by measured wrong rate: step-5-preview, muse-spark-1-3-contributor, nemotron-3-ultra.

  **The single reader's bound (all of Gate 1, vote on or off).** `[gate1] presence_deadline_sec`
  (1800 in `agents.ini`; 0 = none) bounds one presence check of one candidate, every retry and
  re-ask of it included; `[loop] timeout_seconds` bounds one attempt only, and with the default 60
  retries a provider that accepts the connection and never answers held one check for hours. Past
  the bound the check is `unknown` (`presence_unknown` decides what happens to the candidate).

  **Layer 2 is one command, launch and forget** (`scripts/gate1_vote_layer2.py`; the full guide is
  `docs/claim-check/GATE1-VOTE-LAYER2.md`):

  ```bash
  python3 scripts/gate1_vote_layer2.py --detach          # off, then 3, 5, 7, 9 voters; add --sets 3,5,7,9,11 for the weak pair
  ```

  It clones the target at the recorded sha, runs `main.py --validate-plan` (the Gate 1 path `--auto` uses) once per
  variant, scores each against the recorded answers (`contest-bench/cc/gate1_known_answers/`) and writes
  `contest-out/gate1-layer2/<UTC>/SUMMARY.md`. Nothing to watch: `progress.log` ends with `DONE`. **Pass** (per `on`
  variant, against `off`): false positives let through not more, true bugs rejected not more, the vote decided at
  least a third of the candidates; otherwise `claim_vote` stays `off`. Five or more voters run with
  `vote_accept = committed` (CC-11), three with `all`.

  **First run (2026-10-12, free models, 56 candidates): no set of 3, 5, 7 or 9 voters passes, `claim_vote` stays `off`.**
  The vote's unanimous FALSE was right 8 of 8 times; its unanimous TRUE was wrong 8 of 10 (three voters) to 18 of 20
  (nine): "add a test that …" candidates and a pack without callers. More voters decide more and are not safer. Second run (veto only, no test-file candidates, the baseline twice): still no pass; the baseline itself moves by
  up to three candidates and disagrees with its own repeat on 14 of 56. Both runs were made on the code before the review
  fixes 15–21, so repeat them on the fixed code. Third run, on the code with every review fix (up to 28, no voter dropped, no candidate unresolved): still no pass. The vote's unanimous FALSE was right 11 of 11,
  its unanimous TRUE wrong 75 to 88 %, and the two baseline runs of the same code differ by six false positives (31 and 25). Not yet measured on fixed code: a veto-only
  vote of five or more voters (`--sets 5+veto,7+veto`). Details in `docs/claim-check/GATE1-VOTE-LAYER2.md` section 6.

## When something goes wrong

| what you see | what it is | what to do |
|---|---|---|
| `votes=0/N` for every profile in 1 second, `_pick = 'x' but the config has no [x] section` | `--profiles` was given a bare slug | the full section name: `claim_vote_llm.kenary_mimo_v2_6_flash_free` (`grep '^\[claim_vote_llm' contest.local.ini`) |
| `HTTP 403 … error code: 1010` from a provider | Cloudflare refuses a request with no `User-Agent` (publicai) | `claim_vote.py` sends one now; an older checkout needs the header |
| `votes=0/N` for one profile, `raw_head` in `votes.json` | the model is dead, out of credits, or rate-limited | drop it from the roster; the others still vote |
| HTTP 429 in `vote.log` | the provider's rpm (kenary 15 shared, sensenova "rpm exhausted") | lower `[claim_vote] per_provider` or raise `interval_sec`; retries are built in |
| a claim stays `UNSURE` on every run | the pack did not hold the place | check `pack_chars` and that the anchor resolves (`--target`/`--ref` right) |
| `CONFLICT` from `claimcheck_truth.py --append`, exit 3 | the same `finding` already has another `truth` | decide by hand; the file is kept as it was |
| a `finding` with both `REAL` and `FALSE` rows | two claims about one symbol | settle by hand before `harvest_report --truth` (it reads the last row only) |
| `target cannot be opened` (warning) | the recorded repository is gone | `--target PATH` to a clone that has the sha |

## If you want Lenz's opinion: the hint, never a call (CC-9)

```bash
scripts/run_claim_check.sh report.md --target . --ref HEAD --truth validate1/truth.csv          # hint included
scripts/run_claim_check.sh report.md --target . --ref HEAD --truth validate1/truth.csv --no-lenz-hint
python3 scripts/claimcheck_truth.py votes.json --out validate1/truth.csv --append --lenz lenz.json   # compare
```

No run of this epic calls `/assess` or `/verify`. After `claimcheck_truth.py` (and so after
`run_claim_check.sh --truth`), pending.md ends with a block that is printed on the console as
well: `If you want Lenz's opinion (costs credits — nothing below was run)`. It holds:

* the world claims the voters did not settle (`SPLIT`, a `TRUE`/`FALSE` with a dissenter,
  `UNSURE`; at most `DEFAULT_MAX` = 12). A code, mixed or dangling claim is never one: Lenz
  cannot see the repository;
* the cost: one credit per claim, `/verify` 10 more, from 100 a month + 400 extra. This
  month's balance is not known to the tool, so check the billing page;
* three commands, ready to copy: the script's dry run, the send (`--max N`), and this run's
  own command again with `--append --lenz`;
* a compare table that names the claims by their pending entry (`claim 5`), not by text.
  pending.md names every unsettled claim once.

The claims go to `lenz-claims.md` next to pending.md, one `###` section per claim, and the
commands hand that file to `lenz_claim_filter.py` in place of the report. Its `--dry-run` then
lists exactly the counted claims. Sending the report itself would spend the credits on
whatever the script picks from it. The script's own filters stay as they are, so the block
counts only what would get through them and names the rest by number:

* a claim without one of its public-fact words (`_HINTS`: pytest, POSIX, rename,
  `git status` …) is never sent;
* a section of 120 characters or fewer is dropped.

The cost is stated as "at most": the script may still skip a claim it reads as about this
repository, and a cached claim costs nothing.

`--lenz FILE` reads what `lenz_claim_filter.py --json` wrote (`[]` when nothing was left to
send) and adds a `lenz` column (`True/high`, `Mixed/low`) to the compare table. Rows are
matched to our claims by text, allowing for case, spacing and a light rewording by `/extract`.
A row that matches no claim, or two claims, is ignored and counted in a warning. A missing or
unreadable file is a warning: the table stays as it was and the exit code does not change.
The offline tests are `tests/test_claimcheck_lenz_hint.py`. Acceptance is the operator's: read
the block, run command 1, and check that the billing page is unchanged after the run.

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
