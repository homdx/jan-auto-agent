# KC-13 round — `_extract_paths` scans `metadata.command`, scored black-box

Ticket: `epic-tasks/52-kc13-…md` (round 52), base `5ee8417` (the ticket as
corrected after run 1). Six entries, all produced by the manager
(`python3 -m tools.contest run --ticket 52`): run 2 on the roster
(`laguna`/`mistral`/`hy3`), run 3 on `--models agnes-2-5-flash,glm-4-7-flash,
mimo-v2-5,north-mini-code,step-3-7-flash,nex-n2-5-pro` (the last two do not
exist on kenary — `ERROR` in two seconds each). `mistral`'s commit was on its
branch when the 1800 s turn expired (`STALLED`, not exported — KC-21); it was
pulled by hand and scored like the others.

Method: `ingest_kc13.sh` (worktree at the base + `git am` + the ticket's
mechanical checks), then `scenarios_kc13.py` (`KC13_REPO=<worktree>`), 27
scenarios driven through `_extract_paths` and `Policy.decide` with a stub
gate — the ticket's Acceptance bullets one by one, the two rewritten KC-3
tests verbatim, the bash-only rule (`external_directory`, `edit`, a missing
`permission`, `doom_loop`), fail-open on garbage, and the splits the KC-3
design implies (dedup across sources, every shell separator, quotes, `~`,
a first path inside and a second outside). The base scores 14/27 — the
13 red ones are the change.

## Scores

| entry | model | bench 27 | own tests | new tests | one commit | on-ticket files | other KC-3 tests unmodified | Py 3.10 | tiers |
|---|---|---:|---|---:|---|---|---|---|---|
| **mimo** | mimo-v2-5 | **27** | 69 green | 9 | yes | yes | yes | yes | clean |
| step | step-3-7-flash | **27** | 69 green | 5 | yes | yes | yes | yes | clean |
| hy3 | hy3 | 26 (s15) | 66 green | 6 | yes | yes | yes | yes | clean |
| agnes | agnes-2-5-flash | 25 (s04, s24) | 67 green | 7 | yes | yes | yes | yes | clean |
| mistral | mistral-medium-3-5 | 25 (s04, s24) | 69 green | 9 | yes | yes | yes | yes | clean |
| glm | glm-4-7-flash | 24 (s04, s22, s24) | 60 green | **0** | yes | yes | yes | yes | clean |
| ideal | `kc13-ideal` | **27** | 70 green | 10 | yes | yes | yes | yes | clean |

Every entry rewrote exactly the two KC-3 tests the corrected ticket names and
nothing else in `tests/test_contest_policy.py` (the removed lines are those
two tests' old assertions and docstrings only).

## What the losses are

- **s04 / s24 — `~` is not the home directory** (agnes, mistral, glm): the
  scan accepts `~/.ssh/x` (`_pathlike` does) but hands it to
  `Path(...).resolve()`, which lands under the cwd — so the second path of
  `cat … > ~/.ssh/x` is "inside the worktree" and the command is `once`.
  The ticket's third Acceptance bullet is exactly this case.
- **s15 — a missing `permission` key is scanned** (hy3): `permission == ""`
  is treated like `bash` "so the scanner never mis-classifies command text";
  the ticket says the scan is for `bash` only.
- **s22 — `;` and `(` do not end a token** (glm): `ls)|tee /tmp/o1/y;echo z>>/tmp/o2/z`
  loses `/tmp/o1/y`.
- glm shipped **no new test** (its 15 test lines are the two rewrites) and
  gave up on `commit_not_on_branch` twice — its `PROGRESS.csv` row named a sha
  it had since amended away.

## Winner and the ideal

**mimo-v2-5** — 27/27 with the most complete test set (dedup across sources,
bash-only, NUL, non-string command). `step` ties on the bench with a
character-level tokenizer that keeps quoted strings whole (`"/tmp/my dir/f"`
is one token) and — alone among the six — expands `~` inside the shared
loop, so a `~/.ssh/*` in `patterns` meets the denylist too.

The ideal takes mimo's shape (a helper that returns the command's path
tokens, `_extract_paths` unchanged in structure) with step's two ideas:
one regex `_CMD_TOKEN` that keeps a quoted string whole and ends a token at
every shell operator, and `os.path.expanduser` in the shared loop for every
source. mimo's second dedup loop is gone — the command tokens simply join
`raw`, and the existing loop dedups them with `patterns`/`directories`.
mimo's nine tests plus one for `~` in `patterns`.
