# Gate 1 claim vote — the live measurement (CC-10, layer 2), launch and forget

`[gate1] claim_vote` (CC-10, ticket 292) is `off` by default. The ticket's pass rule needs a live measurement before it
may be turned `on`; this page is how to run it, how to read it, and what to do with the answer. Everything is one command.

## 1. Run it

```bash
python3 scripts/gate1_vote_layer2.py --detach
```

That returns at once. The run goes on in the background and needs no attention:

| where | what |
|---|---|
| `contest-out/gate1-layer2/<UTC>/progress.log` | one line per variant start/end; its last line is `DONE — …/SUMMARY.md` |
| `contest-out/gate1-layer2/<UTC>/SUMMARY.md` | the answer; written after every variant, so a partial one exists while it runs |
| `contest-out/gate1-layer2/<UTC>/<variant>/run.log` | the full `--validate-plan` output of that variant (the vote line, every reader reason) |

Check on it, if you want to: `tail -3 contest-out/gate1-layer2/*/progress.log`. It never asks anything.

What it needs: `contest.local.ini` with the `claim_vote_llm.*` sections (the keys; they are copied into a 0600 ini inside
the output directory and never printed), and the target sha in this checkout's history. Nothing else; no Lenz, no Kilo.

Options worth knowing:

| option | meaning |
|---|---|
| `--sets 3,5,7,9` | voter sets to test (the default is `3:all,5:committed,7:committed,9:committed`); `--sets 3,5,7,9,11` adds the 11-voter set with two weak voters |
| `--sets 5:all,5:committed` | the same size under both acceptance rules, to see what `committed` buys (the rule that is not the size's default is in the name: `on-5-all`) |
| `--sets 3+veto,3+notests,5+veto+notests` | the variants of section 6: `+veto` = `vote_confirm = off` (a unanimous FALSE rejects, a unanimous TRUE is the reader's), `+notests` = `vote_skip_tests = on` (candidates citing a test file are not voted); a `+veto` variant is not asked to decide a third, only not to be worse |
| `--off-runs 2` | how many times the baseline runs (default: `[gate1_layer2] off_runs` of `contest.ini`, which ships 2): the reader is a model and does not repeat itself, the second run sizes its noise and `SUMMARY.md` prints the disagreement |
| `--dry-run` | prints the variants, their voters and the reader; runs nothing |
| `--resume DIR` | continues a stopped run; a variant whose `result.json` says it ran is not run again, one that failed (an error, a timeout) is |
| `--reader NAME` | the `claim_vote_llm.NAME` section used as Gate 1's single reader (default `kenary_agnes_3_0_flash_free`) |
| `--known DIR` | another known-answer set (`plan.json`, `truth.csv`, `TARGET`) |

The variants run **one after the other** on purpose: the free providers share a rate limit (kenary 15 requests a minute
across all its models), and two variants side by side would only turn each other's votes into missing votes.

## 2. What it measures

For every variant the script clones this repository at the sha in `TARGET`, puts the recorded Architect plan into
`.agent/plan.json` and runs `main.py --validate-plan`: the same Gate 1 (stage A, then the presence check) that
`--auto` runs at plan time, once per candidate. The tasks Gate 1 removes are read off its report.

| variant | `claim_vote` | voters | `vote_accept` |
|---|---|---|---|
| `off` | off | — | — |
| `on-3` | on | step-5-preview, muse-spark-1-3, nemotron-3-ultra | `all` |
| `on-5` | on | + agnes-3-0-flash, hy3 | `committed` |
| `on-7` | on | + nemotron-3-super, ling-3.1-flash (vercel) | `committed` |
| `on-9` | on | + agnes-2-5, mimo-v2-6 | `committed` |
| `on-11` (optional) | on | + laguna-s-2-1, north-mini-code — the two **weak** voters (7 and 2 wrong alone in the CC-11 table) | `committed` |

Why `committed` from five up: under `all` an abstainer blocks the vote for everybody, so more voters decide *fewer*
candidates (CC-11 measured 129, 125, 115, 103 of 150 claims for 3, 5, 7, 9 voters). `committed` keeps the unanimity among
the voters that committed and the three-families rule; a dissent still sends the candidate to the single reader.

The single reader is the same in every variant, so a difference between two variants is the vote's.

### The known-answer set

`contest-bench/cc/gate1_known_answers/`:

* `plan.json` — 56 candidates: the 53 the Architect wrote on 2026-09-08 for the goal "find every accessor that returns a
  mutable object from internal state and return a copy", and three (`AUTO-R1..R3`) taken from defects an earlier round
  fixed by hand (patches that still apply at the sha);
* `truth.csv` — the answer for each, `REAL` or `FALSE`, with the reason. The rule is the one of
  `ADJUDICATE-findings.md`: `REAL` means the defect is in the code at the sha (latent counts), `FALSE` means it is not, read
  against the callers. These answers come from reading the code, not from the vote;
* `TARGET` — the sha (`4ff7d14…`) the answers are valid at.

Composition, so the numbers are read fairly: **52 FALSE and 4 REAL.** That is what an Architect asked an over-broad
question produces (the repository's own audit of such lists found them almost all false positives), and it is the realistic
case for the vote's first job, *not letting false positives through*. It is a thin base for the other half, *not rejecting
true bugs*: four real bugs can only show a vote that rejects them, not prove that it never does. Add rows to `truth.csv`
and `plan.json` as real defects are settled; the script reads whatever is there.

## 3. Reading `SUMMARY.md`

The table has one row per variant:

* `unresolved` — candidates Gate 1 could not judge in this run: the reader never answered (plan_validator's "could not be
  checked" list, or a `presence_unknown = keep` line in `run.log`). They are left out of the next two columns **on both
  sides** of a comparison (a provider's outage is not the vote's false positive), and the verdict says on how many
  candidates it was made;
* `false positives through` — kept by Gate 1 although the answer is `FALSE` (lower is better);
* `true bugs rejected` — removed by Gate 1 although the answer is `REAL` (lower is better);
* `asked / decided / overridden / requests / skipped` — the vote's own counters: candidates put to the voters, candidates
  the voters settled (the reader not asked, or its veto not used), unanimous `FALSE` verdicts the reader overruled with
  `confirmed`, HTTP requests made, and candidates that reached the vote and were not voted on (nothing in the tree to
  quote, a test file under `+notests`, the cap, the vote stopped);
* `verdict` — `PASS` when, against **every** `off` run (not only the first: the reader's noise flipped the verdict with
  their order), false positives are not more, true bugs rejected are not more, and `decided` is at least a third of the
  candidates; a `+veto` variant needs no third but at least one veto (one that never vetoed did nothing).
  Otherwise `FAIL (...)` says which.

Below it is a per-candidate matrix (`R` removed, `k` kept, `?` not judged) next to the recorded answer. A column that
disagrees with `off` on a candidate with an answer is where to look: `<variant>/run.log` has the vote line and the
reader's reason.

Exit code: `0` all `on` variants passed, `1` one failed the rule, `2` a variant could not run (`--resume DIR` runs it
again), `3` an `on` variant has no verdict (no recorded answers, or no baseline). The last line of `progress.log` before
`DONE` names every variant's verdict in full.

## 4. What to do with the answer

* A variant `PASS`es and others do not: turn `claim_vote = on` with that variant's `vote_profiles` (and `vote_accept`) in
  your `agents.ini` / profile; the smallest passing set is the cheapest (three requests a candidate against five, seven, nine).
* None passes: leave `claim_vote = off`. The cause is in `run.log` (voters dead, rate-limited, or wrong), not in the rule.
* All pass equally: take three. More voters cost requests and the CC-11 table says they buy nothing measured.
* After flipping the flag, rerun this on every new Architect goal you care about once: add its candidates and their
  answers to the known-answer set first.

## 5. Gotchas met while building it

* Gate 1's reader is **not** `[gate1_llm]` unless `presence_llm_profile = gate1_llm` is set; by default it is the shared
  `[api_local]` provider (an Ollama on `localhost:11434`). The script sets it; a hand-made ini must too.
* The voters' sections (`claim_vote_llm.*`) live in `contest.local.ini`, not in `agents.ini`. The script copies them into the
  run's ini; a hand-made one has to contain them or the vote stays off and says why in the log.
* `main.py --validate-plan` edits and commits `plan.json` of the repository it is pointed at: that is why every variant has a
  clone of its own and why you never point it at a working checkout.
* The vote is a shortcut for the candidates that reach the presence check; a candidate removed by stage A (the cited place
  does not exist) is removed in every variant alike.

## 6. Result of the first run (2026-10-11/12, run `20261011T205848Z`)

Target `4ff7d141`, 56 candidates (4 real, 52 false positives), reader agnes-3-0-flash, voters from the roster in section 2,
all free models. **No set passes the rule; `claim_vote` stays `off`.**

**Read with care (review of 2026-10-12):** this run's voters were section names (`claim_vote_llm.…`), and the families
rule read the names, not the models — every voter counted as its own family, so the 7- and 9-voter sets (two nemotron,
two agnes) were not held to "three families". Its verdicts were also taken against the first `off` run only, and a
candidate the reader could not judge counted as kept. All three are fixed (CC review fixes 15 and 20, and the
comparison with every baseline); rerun before reading the numbers below as the vote's.

| variant | accept | false positives through | true bugs rejected | asked / decided / overridden | requests | seconds | verdict |
|---|---|---|---|---|---|---|---|
| off | — | 26 | 1 | — | — | 1105 | baseline |
| on-3 | all | 27 | 1 | 53 / 10 / 0 | 159 | 3689 | FAIL: more false positives, decided < 19 |
| on-5 | committed | 30 | 1 | 53 / 20 / 1 | 265 | 4081 | FAIL: more false positives |
| on-7 | committed | 31 | 1 | 53 / 18 / 1 | 371 | 5194 | FAIL: more false positives, decided < 19 |
| on-9 | committed | 32 | 1 | 53 / 21 / 2 | 477 | 6186 | FAIL: more false positives |
| on-11 | committed | — | — | — | — | — | timed out at the old 7200 s limit; rerun with the new default of 10800 s (see below) |

How to read it. The difference to `off` mixes two things: the vote, and the single reader's own run-to-run noise
(the reader is a model; on ~20 candidates the vote never touched, `off` and the `on` runs disagree). The clean
number is the vote's **own decisions** against the recorded answers (from the `claim vote:` reasons in `run.log`):

| variant | vote confirmed | of which wrong (a false positive confirmed) | vote rejected | of which wrong (a real bug rejected) |
|---|---|---|---|---|
| on-3 | 10 | 8 | 0 | 0 |
| on-5 | 16 | 14 | 4 | 0 |
| on-7 | 15 | 13 | 3 | 0 |
| on-9 | 20 | 18 | 1 | 0 |

* **A unanimous FALSE was right every time it occurred (8 of 8 over the four runs)**, and the two real bugs of the
  defect family it could judge (`AUTO-R2`, `AUTO-R3`, a path-qualified `skip_dirs` entry) were confirmed by every
  voter set. No real bug was rejected by the vote. This is the safe half of the rule, as in the fixture.
* **A unanimous TRUE is mostly wrong on this set**: 8 of 10 with three voters, and more voters do not fix it
  (14 of 16, 13 of 15, 18 of 20). Voters agree, quote real lines, and are wrong: unanimity measures agreement, not
  whether the claim is the right one to ask. That is why the unanimity rule that held on the fixture does not carry over.
* **More voters decide more but are no safer**: decided 10, 20, 18, 21; false positives through 27, 30, 31, 32. Three
  is not worse than nine. The 11-voter set was not expected to change this.

Why the TRUEs are wrong (read from the confirmed candidates, not guessed):

1. **"Add a test that …" candidates** (about half of the wrong confirmations: `AUTO-T3`, `T21`, `T24`, `T25`, `T27`, `T28`,
   `T33`, `T34`, `T35`, `T44`). The claim built from them is "`<test function>` in `<test file>` has the problem: *add a test
   that X is a fresh copy*". Every voter finds, correctly, that the test does not assert that, and says TRUE. The claim is
   literally true and the task is still useless, because X is not a bug. The claim asks "is this instruction's premise
   present in the cited block", not "is there a defect worth a task".
2. **No callers in the pack.** The ticket kept the pack to the cited block and its git header (question 3, "no callers in
   the first version"). Most false positives of this kind are neutralised one frame up (`ADJUDICATE-findings.md`): for
   `MetricsCollector._load_all_cached` the voters see a method returning its cache and say TRUE; the one caller copies the
   list. (The single reader let this one through too: the vote is not the only line that misses it. What the vote adds is
   unanimity and a quote on top of the same blind spot.)
3. **Debatable answers.** A public mutable field of a dataclass (`T8`, `T53`) is "exposed mutable state" in the literal
   sense the voters judge. I recorded it as FALSE (not an accessor) and the earlier round recorded the same shape for
   `AutoController.config` as REAL (latent). If the answers for these are changed the numbers move by a handful; they do
   not change the result, since the wrong TRUEs of the first two groups alone exceed the baseline's margin.

How much of the difference to `off` is the vote and how much is the reader's noise: of the false positives the vote
confirmed, the reader of the `off` run had removed 1 (on-3), 4 (on-5), 3 (on-7) and 6 (on-9), so those are the false
positives the vote *added*. In the same runs the reader alone let 3 to 6 false positives through that it had removed in
`off`, with the vote not involved. The reader is a model and does not repeat itself, so a single `off` run is a noisy
baseline: **run `off` twice** (a second `off` variant is the next improvement of the script) before reading a difference of
one or two false positives. The vote's own decisions above do not depend on this.

What this says about the design, not about the voters: the vote is a good **veto** and a poor **confirmation** at Gate 1
on this set. Do not turn it on as it is. Candidate changes, each measurable on this same set (`--known` and the tables
above are the instrument):

* confirm by vote only for candidates whose cited file is production code, and leave "add a test" candidates to the reader;
* put the callers of the cited symbol into the pack (the "bigger pack" of the ticket's question 3, a later measured change);
* keep only the veto: a unanimous FALSE rejects, a unanimous TRUE is the reader's to decide (a `vote_confirm = off`
  switch). All eight unanimous FALSEs of this run were right.

The last is one small code change and the cheapest to try; the second addresses the cause. Neither is part of this run.

### The second run: the changes tried, and the noise (same run directory, 2026-10-12)

Added to the same run: the baseline a second time (`off-2`), `+veto` (`vote_confirm = off`), `+notests` (`vote_skip_tests = on`).

| variant | false positives through | true bugs rejected | asked / decided | vote's own confirmations (wrong) | vote's own rejections (wrong) | verdict |
|---|---|---|---|---|---|---|
| off | 26 | 1 | — | — | — | baseline |
| off-2 | 26 | 1 | — | — | — | baseline repeat |
| on-3-veto | 28 | 1 | 53 / 0 | none by design | 0 | FAIL |
| on-3-notests | 26 | 1 | 24 / 6 | 6 (4) | 0 | FAIL (decided < 19) |
| on-3-notests-veto | 27 | 2 | 24 / 0 | none by design | 0 | FAIL |
| on-5-notests | 29 | 1 | 24 / 9 | 9 (7) | 0 | FAIL (decided < 19) |

What the second run says:

1. **The baseline is noisy and the rule did not allow for it.** `off` and `off-2` have the same count (26 false positives
   through, 1 true bug rejected) but disagree on **14 of 56 candidates**. A single variant lands at 26 to 28 with no vote
   involved: `on-3-veto` decided nothing (0 of 53) and still reads 28; `on-3-notests-veto` decided nothing and removed a
   *real* bug (`AUTO-R2`) the baselines kept, which is the reader alone. A difference of 1 to 3 against one baseline run is
   not a result, in either direction. The pass rule ("not more than `off`") needs the better of two baselines and a margin, or
   more candidates; the layer-2 patch 20 of the review fixes (candidates the reader could not judge) removes one source of
   this noise and not the reader's own.
2. **Dropping the test-file candidates helped the vote's own accuracy and did not fix it.** Wrong confirmations: 8 of 10
   became 4 of 6 (three voters), 14 of 16 became 7 of 9 (five). What is left are the debatable answers (`T8`, `T53`, a public
   mutable field), `T14` (the field is already copied at the sha) and `T13`: the claim ("has the problem: *<the fix>*") is
   true in the literal sense and the answer recorded for it is FALSE. This is the claim-wording question of CC-12.
3. **A veto with three voters is a no-op on this set.** Unanimous FALSE needs the three to agree on "absent"; with three it never
   happened in these runs (0 rejections). With five to nine it happened 4, 3 and 1 times in the first run, all right.
   So the veto is only useful with more voters, and the more voters, the more the abstainers matter (`vote_accept`).
4. **No variant passes; `claim_vote` stays `off`.** The two things most likely to change that are CC-12 (callers in the
   pack, and a claim that asks for the defect rather than the fix) and a calmer instrument: more real bugs in the set (four
   is too few to say the vote never rejects one) and a repeat count for the baseline.

**Read this result with one caveat.** The whole run used the code **before** the review fixes of 2026-10-12 (fix 15 reads the
model a profile names for the three-families rule; before it the rule read the *section names*, so two agnes sections counted
as two families, and 61 retries held one check for hours; fix 20 takes the candidates the reader could not judge out of the
comparison). The numbers for 5, 7 and 9 voters are the most affected (agnes-3 and agnes-2-5 are one family). Measure again on
the fixed code before drawing the line between "the vote is a poor confirmation" and "the vote was run wrong":

```bash
python3 scripts/gate1_vote_layer2.py --detach --sets 3,5,7,9,5+notests
```
