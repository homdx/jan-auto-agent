# EPIC CC — claim check with evidence: models judge claims about our code with the code in front of them

**Status:** draft (not started; to be cut after `arena` is merged into `main`)
**Base:** `main` after the open `arena` tickets and their bugs have landed
**Branch:** `cc`, cut from that `main`
**Tickets:** CC-0 … CC-8, numbered **250 … 290 in steps of five** (§9). The four numbers
after each ticket are reserved for the fixes that real rounds will turn up; 291 … 299
are reserved for the epic's own close-out. Each ticket is written in full under
`docs/claim-check/tickets/` and is copied into `epic-tasks/` (with `arena issue create
--number NNN`) when its turn comes — not before: `run` refuses untracked files in
`epic-tasks/`.
**Predecessor:** commit `03a77c5` (`CLAIM-1`): `scripts/claim_extract.py`,
`scripts/claim_vote.py`, `scripts/lenz_claim_filter.py`, `scripts/run_claim_check.sh`,
`docs/claim-check/RUNBOOK.md`, `docs/claim-check/REPORT.md`. Read the report first.

---

## 1. Why

`CLAIM-1` made one thing work: free models of different families, asked about a claim
that is a fact about the world, are wrong on 0.4 % of what they decide when we accept
only a unanimous verdict (3 families, 40 claims with a known answer).

It also showed what it cannot do. Our reports are about our code:

| Report | claims | decided by the models | about our code |
|---|---|---|---|
| `kc-bug-report.md` | 59 | 1 | 58 |
| `review2.txt` / `review3.txt` / `kc-branch-review.md` / `review1.txt` | 157 | 0 unanimous (128 got no committed vote at all) | almost all |

The models answer `UNSURE`, which is correct: they cannot see `tools/contest/gates.py`.
The few `TRUE` votes they did cast on code claims were plausible guesses. So today the
tool *sorts* a report; it cannot *judge* it, and the judging — a stage-3 adjudicator
reading live code, 14–35 minutes per verdict — is still the expensive step of every
round that starts from a model-written report (`IMPROVEMENTS-*.md` audits, reviewer
CSVs, cloud branch reviews, the claims in a bug ticket).

This epic puts the code in front of the voters.

## 2. What the work looks like before and after

| | Before (`CLAIM-1`) | After (this epic) |
|---|---|---|
| A claim about `gates.py::_declared_paths` | `UNSURE`, 12 of 12 votes | the function's text, its callers, and the commit it mentions are in the prompt; each voter answers with a verbatim quote; verdict `TRUE`/`FALSE` with `file:line` evidence, or `UNSURE` when the code does not decide it |
| "is this claim about code?" | a regex (`needs_code`) that leaks | anchors resolved against the repository at a pinned commit: a claim is code when its path/symbol/commit/ticket exists (or conspicuously does not exist) there |
| A made-up justification | counts as a vote | a quote that is not in the evidence turns the vote into `UNSURE` |
| "did the fix fix it?" | read the diff, run the tests | the same claims judged at the base and at the head: `FIXED` / `STILL` / `NEW` / `GONE` |
| What the operator gets | a table | a table plus `truth.csv` rows and a pending list that the existing stage 3–5 tools read |

What the epic does **not** promise: it does not make models infallible. A claim about
runtime behaviour that the text does not show (a race, a performance number) stays
`UNSURE`; that is the right answer and the pending list is where it goes. The epic's
acceptance (§6) is measured, not asserted.

## 3. Principles (every ticket obeys these)

1. **Thin layer over what exists.** Reuse `tools.collect` (`CollectModel`, `action_collect`),
   `tools/arena/gitref.git`, `tools.git_run.run_git`, `tools.auto.llm_profile`,
   `tools.llm_stream`, `scripts/claim_vote.py`. No second LLM client, no second ini
   reader, no second git wrapper.
2. **Deterministic before generative.** Everything up to the prompt (anchors, worktree,
   evidence pack) is plain code with golden tests: no LLM, no network, byte-identical
   output for identical input. Only the vote itself calls a model.
3. **The operator's checkout is never touched.** The target repository is read through a
   detached worktree or a throw-away clone; no `checkout`, `pull`, `reset`, `stash` in
   anyone's working tree. The tools only read (`git show`, `git diff`, `git log`,
   `git cat-file`, `git rev-parse`).
4. **Evidence is the only source for claims about the repository.** Models may use their
   knowledge for facts about the world (pytest exit codes), never for what our code
   does. A verdict on a code claim must quote the evidence; the quote is checked by
   code, not trusted.
5. **A pinned commit, always.** Every verdict names the sha it was made at. A claim about
   a moving branch is judged at the sha the branch pointed to when the run started, and
   the report's own sha (`origin/kc @ afa53f1`) is compared with it and warned about.
6. **Unanimity, as in `CLAIM-1`.** Accept a verdict only when every voter committed to
   it, at least three voters, at least three families (two models of one family are one
   family). Majorities are reported, never accepted.
7. **No credit is spent.** Nothing here calls Lenz `/assess` or `/verify`. `/extract` is
   free and cached.
8. **Old commands keep working.** `scripts/claim_extract.py`, `claim_vote.py` and
   `run_claim_check.sh` keep their current behaviour when no `--target` is given.
9. **English in the repository.** Code, comments, docs, tickets, test names.

## 4. Architecture

### 4.1 The flow

```
report.md
  │  scripts/claim_extract.py            (exists, free, cached)
  ▼
claims.json  ─────────────────────────────────────────────────────────────┐
  │                                                                       │
  │  CC-1  anchors.py   extract_anchors + resolve_anchors + classify      │
  ▼                                                                       │
claims + anchors ──world──────────────────────────────────┐               │
  │ code / mixed                                          │               │
  │            CC-2  target.py   Target.open(repo, ref)   │               │
  │                  detached worktree + .collect/, by sha │               │
  ▼                                                       │               │
  CC-3 evidence_source.py   source chunks + collect facts │               │
  CC-4 evidence_git.py      git show / diff / ticket text │               │
  ▼                                                       │               │
  CC-5 pack.py   rank, merge, trim to budget, render      │               │
  ▼                                                       ▼               │
  CC-6 judge.py + claim_vote.py   prompt v2, quote check, unanimity       │
  ▼                                                                       │
votes.json  (verdict, unanimous, evidence ids, checked quotes)  ◀─────────┘
  │
  ├─ CC-7  claim_diff.py   same claims at base and head → FIXED/STILL/NEW/GONE
  └─ CC-8  claimcheck_truth.py → truth.csv + pending list → stages 3–5 of RUN-THE-COMPETITION.md
```

### 4.2 The package and the files each ticket owns

New package `tools/claimcheck/` (skeleton created by CC-0, so that CC-1 and CC-2 do not
both create `__init__.py`):

| File | Ticket | Holds |
|---|---|---|
| `tools/claimcheck/__init__.py` | CC-0 | docstring only |
| `tools/claimcheck/model.py` | CC-1 | `Anchor`, `ResolvedAnchor`, `Chunk`, `Pack`, `RepoView` (Protocol) |
| `tools/claimcheck/anchors.py` | CC-1 | `extract_anchors`, `resolve_anchors`, `classify` |
| `tools/claimcheck/target.py` | CC-2 | `RefHint`, `parse_report_ref`, `Target`, `TargetError` |
| `tools/claimcheck/evidence_source.py` | CC-3 | `source_chunks` |
| `tools/claimcheck/evidence_git.py` | CC-4 | `git_chunks` |
| `tools/claimcheck/pack.py` | CC-5 | `PackBudget`, `build_pack` |
| `tools/claimcheck/judge.py` | CC-6 | prompt v2, vote parser v2, `verify_quotes` |
| `tools/claimcheck/compare.py` | CC-7 | `compare`, `Delta` |
| `tools/claimcheck/truth.py` | CC-8 | `to_truth_rows`, `pending_markdown` |
| `scripts/claim_vote.py` | CC-6 (edit) | `--target/--ref/--base`, per-claim packs |
| `scripts/claim_diff.py` | CC-7 | before/after command |
| `scripts/claimcheck_truth.py` | CC-8 | `votes.json` → `truth.csv` |
| `scripts/run_claim_check.sh` | CC-8 (edit) | `--target/--ref/--base` pass-through, `--dry-run` |
| `contest-bench/cc/` | CC-0 | the shared test data and the scorer (§6) |

### 4.3 The data model (CC-1 owns it; CC-2 … CC-8 import it)

```python
@dataclass(frozen=True)
class Anchor:                       # something in the claim's text that names the repository
    kind: str        # "path" | "symbol" | "test" | "commit" | "ticket" | "ref"
    text: str        # as written, e.g. "Policy._mechanical", "tools/contest/gates.py", "a73e389"
    start: int; end: int            # span in the claim text

@dataclass(frozen=True)
class ResolvedAnchor:
    anchor: Anchor
    found: bool                     # exists at the pinned sha
    path: str = ""                  # repo-relative, for path/symbol/test
    qualname: str = ""              # "Policy._mechanical"
    lines: tuple[int, int] = (0, 0) # 1-based inclusive, the definition's span
    sha: str = ""                   # full sha, for commit/ref
    candidates: tuple = ()          # other definitions when the name is ambiguous (max 3)

@dataclass(frozen=True)
class Chunk:                        # one piece of evidence
    id: str          # stable, e.g. "src:tools/contest/gates.py:120-143", "git:a73e389:gates.py:88-97"
    kind: str        # "source" | "collect" | "git" | "ticket" | "note"
    path: str; start: int; end: int # 1-based inclusive; 0, 0 for non-file chunks
    text: str
    why: str         # which anchor asked for it

@dataclass(frozen=True)
class Pack:
    claim: str; sha: str
    chunks: tuple[Chunk, ...]
    truncated: bool
    def render(self) -> str: ...        # the text the voter reads
    def find(self, quote: str) -> Optional[str]: ...   # chunk id containing the quote
```

`RepoView` is the read-only window on a repository that CC-1 resolves against and CC-3/4
read from: `exists(path)`, `read(path) -> str`, `files() -> list[str]`,
`rev_parse(rev) -> Optional[str]`, `ticket_file(ticket_id) -> Optional[str]`,
`git(*args) -> str` (read-only). CC-1 ships `PathRepoView(root)`; CC-2's `Target.view()`
returns an object with the same methods, so CC-1 does not depend on CC-2.

### 4.4 The voter contract (CC-6)

System rules, in the prompt: *for a claim about this repository the evidence below is
your only source; for a fact about the world you may use what you know; if the evidence
does not decide the claim, answer UNSURE.* The reply is JSON only:

```json
[{"id": 3, "verdict": "TRUE|FALSE|UNSURE", "chunk": "src:tools/contest/gates.py:120-143",
  "quote": "<up to 200 characters copied from that chunk>"}]
```

`verify_quotes` downgrades a `TRUE`/`FALSE` to `UNSURE` (reason `quote_not_in_pack`) when
the chunk id is not in the claim's pack or the quote, after whitespace and line-number
prefix normalisation, is not a substring of that chunk. A world claim needs neither.
`tally` is unchanged (UNSURE abstains, quorum 3, `unanimous` as in `CLAIM-1`) and gains
`evidence` (chunk ids) and `quotes` per claim.

### 4.5 How it embeds in what exists

* `claim_vote.py` keeps its voters, pacer, retry budget and ini profiles. With no
  `--target` it behaves as today; the old `needs_code` flag is then still computed by
  `lenz_claim_filter.is_internal` (kept as the fallback).
* `claim_extract.py` is untouched.
* `[claim_vote]` in `contest.ini` gains `code_batch`, `pack_chars`, `pack_chunks`
  (CC-6) — placeholders only, no real names.
* `truth.csv` rows use the columns `finding,truth,checked_by,duplicate_of,how` (the minimum
  in `validate1/ANALYTICS-RUNBOOK.md` is `finding,truth,how`), `truth` being `REAL` or
  `FALSE`, so `harvest_report.py --truth` (`load_truth`) reads them unchanged (CC-8).
* The collect model is the same artifact `/collect` builds (`<root>/.collect/`); CC-2
  builds it in the worktree through `tools.collect.cli.action_collect` and reads it back
  with `tools.collect.loader.load`. A repository collect cannot model (not Python, parse
  failures) yields an absent model; evidence then comes from source and git only.

## 5. Ticket map

| # | Id | Title | Size | Depends on | Who | Wave |
|---|---|---|---|---|---|---|
| 250 | CC-0 | Shared test data, the scorer, the baseline, the package skeleton | L | — | operator (not a contest) | 0 |
| 255 | CC-1 | Anchors: what in a claim names the repository, and does it exist | M | CC-0 | contest | 1 |
| 260 | CC-2 | Target: a pinned worktree and a cached collect model | M | CC-0 | contest | 1 |
| 265 | CC-3 | Source evidence: symbol bodies, keyword windows, collect facts | M | CC-1 | contest | 2 |
| 270 | CC-4 | Git evidence: commit, diff range, ticket text, file history | M | CC-1 | contest | 2 |
| 275 | CC-5 | The pack: rank, merge, trim, render, find a quote | M | CC-3, CC-4 | contest | 3 |
| 280 | CC-6 | Evidence-bound voting: prompt v2, quote check, `--target` | L | CC-5, CC-2 | contest | 4 |
| 285 | CC-7 | Before and after: the same claims at two commits | M | CC-6 | contest | 5 |
| 290 | CC-8 | Into the pipeline: `truth.csv`, pending list, one command | M | CC-6 | contest | 5 |
| 291 | CC-9 | A Lenz comparison hint: the commands and the cost printed after a run, never run (`/extract` free 1000 a day; `/assess` 1 credit, `/verify` 10, from 100 a month + 400) | S | CC-6, CC-8 | contest | 6 |

Why eight contest tickets and not five: an earlier sketch folded CC-3/CC-4 pack assembly
and the voter changes into one ticket. A round gives one agent one session; an L ticket
that mixes a deterministic assembler with a prompt and a parser is a ticket where the
winner is whoever guessed the interface. Splitting at the `Chunk`/`Pack` types gives each
contest a bench with a golden answer.

## 6. How we accept it

### 6.1 Each ticket

The usual harvest of `python3 -m tools.contest run --ticket NNN`: the ticket's declared
files only, the tests named in the ticket pass (`tests/test_claimcheck_*.py`, tiered by
`scripts/sync_test_tiers.py`), the full suites stay green. Then the operator scores the
round by `contest-bench/NNN/acceptance_NNN.py` — written from the ticket alone, run
against every entry — and by `python3 -m tools.arena run judge` where the arena has it.
The ideal commit is the winner's code plus whatever the bench found, as in every round.

### 6.2 The epic

`contest-bench/cc/` (CC-0) holds the numbers that decide whether the epic worked:

| Measure | Baseline (no evidence, today) | Target after CC-6 |
|---|---|---|
| code claims decided unanimously (fixture, 80 claims) | recorded by CC-0, expected near 0 | ≥ 60 % |
| wrong among the unanimous verdicts (fixture) | — | ≤ 3 % |
| wrong among the unanimous verdicts (30 real claims about this repo, truth by repro) | — | ≤ 5 % |
| fabricated quotes caught | — | 100 % of the injected ones (deterministic test) |
| `FIXED`/`STILL` right on the fixture's 20 fix claims (CC-7) | — | ≥ 90 % |

The targets are hypotheses: CC-0 records the baseline first and may move a target with a
note, never silently. A round that misses a target is a result, and a fix ticket from the
gap numbers.

## 7. Parallelism

```
CC-0 ──┬─ CC-1 ──┬─ CC-3 ──┐
       │         └─ CC-4 ──┴─ CC-5 ── CC-6 ──┬─ CC-7
       └─ CC-2 ──────────────────────────────┤
                                              └─ CC-8
```

Pairs that run at the same time, and why they cannot collide:

| Pair | Files they own | Shared file risk |
|---|---|---|
| CC-1 ∥ CC-2 | `model.py`, `anchors.py` / `target.py` | none; `__init__.py` already exists (CC-0) |
| CC-3 ∥ CC-4 | `evidence_source.py` / `evidence_git.py` | both import `model.py` read-only |
| CC-7 ∥ CC-8 | `compare.py`, `claim_diff.py`, `BEFORE-AFTER.md` / `truth.py`, `claimcheck_truth.py`, `run_claim_check.sh` | `RUNBOOK.md`: only CC-8 edits it; CC-7 writes its own page |

Everything else is sequential: CC-5 needs both evidence providers, CC-6 needs the pack
and the target, CC-7/CC-8 need the voter. Five contest waves after CC-0. If the MVP is
enough, stop after CC-6.

## 8. Testing strategy

* **Offline.** No test uses the network or a real model. Git tests build tiny repositories
  in `tmp_path` with fixed author, date and config (`GIT_CONFIG_GLOBAL=/dev/null`) so shas
  are stable. Model calls are replaced by a fake `completion_fn` that decides from the
  prompt text — for example "TRUE if the pack contains `check=False`" — which makes the
  whole chain testable end to end without a model.
* **Golden files** under `tests/fixtures/claimcheck/` for anchors, chunks and rendered
  packs. A golden diff in a ticket's review is a behaviour change, not noise.
* **Determinism test in every module:** run twice, compare bytes.
* **Read-only tests:** `git status`, `HEAD`, the index file's bytes and mtime of the
  operator's checkout are equal before and after every target operation.
* **xdist:** the suite runs `-n auto --dist=loadgroup`. A test that hands a port to another
  process needs `@pytest.mark.xdist_group("port_bound_http_servers")`; none of these do.
* **Tiers:** new tests go in `tests/`, tiered with `python3 scripts/sync_test_tiers.py`.
* **Live checks** are the operator's, never pytest's: `contest-bench/cc/run_cc.sh` runs the
  chain on real free models over the fixture and the real claims and prints the §6.2 table.

## 9. Numbering and the gaps

* The epic owns **250–299**. Tickets sit at multiples of five: 250, 255, … 290.
* A fix found by round N is numbered N+1 … N+4 (a bug found while judging round 275 is
  276; the fourth is 279). The fifth bug at one ticket takes 291–299 or opens a new block.
* 291 … 299: close-out tickets of the epic (docs sync, the ideal-merge, the final bench run).
* `arena issue create --number NNN` fixes the number. Without it `next_number` returns
  max + 1, which after 290 exists is 291+: unrelated tickets created in the meantime should
  pass `--number` 300 or higher, or accept that they take a reserved number.
* A round's id is its ticket number (`contest-out/NNN/`).

## 10. Risks

| Risk | Why it matters | What the epic does |
|---|---|---|
| Voters invent evidence | a confident wrong verdict is worse than `UNSURE` | the quote must be a substring of the named chunk, checked by code (CC-6); a downgrade is counted and reported |
| The pack misses the relevant code | the voter says `UNSURE` or, worse, judges a neighbour | ranking by anchor kind and claim keywords, keyword windows inside long bodies, `note` chunks for anchors that do not exist (CC-3/CC-5); the bench measures coverage |
| The pack is too large | reasoning models spend the budget before the verdict (seen: a 59-claim batch returned nothing) | a hard budget, `code_batch = 2`, `pack_chars`; measured, not guessed |
| The report is about a branch that moved | the claim was true at its sha and false now | pin the sha (CC-2), warn on a mismatch with the report's own sha, name the sha in every verdict |
| A claim is half code, half world | "pytest exits with 4 because `--timeout=180` is unknown" | `mixed`: the voter gets the pack and may use knowledge for the world part; unanimity still required |
| Models share a mistake | seen: the same two claims wrong for almost every model | families, not count; the quote check; the pending list for anything not unanimous |
| Collect cannot model the target | not Python, or it fails | optional by design: absent model → source and git evidence only, with a warning |
| Rate limits of the free tiers | lost answers (seen: HTTP 429 on a burst) | the existing pacer and `[claim_vote]` retry budget |

## 11. Out of scope

* Running the repro of a claim (executing code to settle a runtime claim). A later epic;
  the pending list is where such claims wait.
* Languages other than Python in anchors and symbol bodies. Anchors of other kinds
  (paths, commits, tickets) work for any repository; symbol resolution is `ast`-based.
* Using Lenz `/assess` or `/verify` as a tie-break on code claims (it cannot see our code).
* Auto-landing anything. The operator decides, as everywhere.

## 12. To re-check on the real base before CC-1 runs

This epic was written against `main` at `03a77c5` plus the unmerged `arena` work. Before
the first round, check and note the result here:

1. `tools.collect.cli.action_collect(root, config)` and `tools.collect.loader.load(root, config)`
   still have those names and signatures, and `CollectModel` still has `module`,
   `callers_of`, `calls_into`, `contracts_for`, `risk_for` (CC-2, CC-3).
2. `tools/arena/gitref.git(repo, *args)` and `tools.git_run.run_git` are still importable
   and read-only usable (CC-2, CC-4).
3. `arena issue create --number NNN` still accepts an explicit number, and `epic-tasks/`
   still refuses untracked files at `run` (§ header).
4. `harvest_report.load_truth` still reads by column name and accepts `REAL`/`FALSE`
   (CC-8); `validate1/truth.csv` is not in the tree, so check against the runbook's text.
5. `scripts/claim_vote.py` at the base is the `03a77c5` version or a descendant; its
   `tally` signature is what CC-6 extends.

Result of the re-check, on `main` at `d7ff804` (arena merged), 2026-10-09:

1. Holds: `action_collect(root, *, config, config_path, llm_call, drop_summaries)` and
   `load(root, *, config, config_path)` exist; `CollectModel` has `module`, `callers_of`,
   `calls_into`, `contracts_for`, `risk_for`.
2. Holds: `tools.arena.gitref.git(repo, *args, env, stdin, strip)` and
   `tools.git_run.run_git(cmd, cwd, ...)` import fine.
3. Holds: `arena issue create` takes `--number NN`; `tools/contest/workspace.py` refuses
   uncommitted or untracked `epic-tasks/` at the base.
4. Holds: `load_truth` reads by column name (`finding`, `truth`) and `TRUTH_VALUES` is
   `REAL`/`FALSE`/`FIXED`; `validate1/truth.csv` is still absent from the tree.
5. Holds: `scripts/claim_vote.py` is in `main` and `tally(claims, results, ...)` is there.

## 13. Tickets

| # | File |
|---|---|
| 250 | `docs/claim-check/tickets/250-cc-0-test-data-scorer-baseline.md` |
| 255 | `docs/claim-check/tickets/255-cc-1-anchors.md` |
| 260 | `docs/claim-check/tickets/260-cc-2-target-worktree-collect.md` |
| 265 | `docs/claim-check/tickets/265-cc-3-source-evidence.md` |
| 270 | `docs/claim-check/tickets/270-cc-4-git-evidence.md` |
| 275 | `docs/claim-check/tickets/275-cc-5-pack.md` |
| 280 | `docs/claim-check/tickets/280-cc-6-evidence-bound-voting.md` |
| 285 | `docs/claim-check/tickets/285-cc-7-before-after.md` |
| 290 | `docs/claim-check/tickets/290-cc-8-into-the-pipeline.md` |
