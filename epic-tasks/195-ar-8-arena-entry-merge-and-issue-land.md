# AR-8 — `arena entry merge NN AGENT` and `arena issue land NN`: landing a round's winner in two commits

**Status:** landed
**Severity:** MEDIUM
**File:** tools/arena/merge.py
**Symbol:** entry_merge, issue_land, trailer_model, read_entry, apply_patch, build_message
**Round:** 195
**Size:** M
**Also touches:** tools/arena/cli.py, tests/test_arena_entry_merge.py, tests/test_arena_issue_land.py

**Depends on:** AR-3 (`rounds.round_folder`, `rounds.round_alive`, the `.arena/rounds/NN.json` record), AR-6 (ticket lookup in `tools/arena/tickets.py`) and AR-14 (`tickets.set_status_text`, `status_word`), all landed.

---

## Why

Landing a round's winner is the most error-prone hand step left in the arena flow:

- a patch made on another machine carries that machine's `From:` address, and it must not become the commit's author;
- the model trailer (`Co-Authored-By: <model> <<model>@round-NN.contest>`) is typed by hand, and the model part has a naming rule (`GLM-4.7` → `glm-4-7`);
- the agent's own `epic-tasks/` and `runs/` files ride in its patch and must not be committed;
- the ticket's `**Status:**` line, its `INDEX.md` row and `contest-bench/NN/` belong in the **final** commit, and `git commit -a` sweeps in whatever else is dirty.

Today this is `git apply`, `git commit`, a `sed` on the status line (ticket 147 was set `landed` by one, see ticket 150) and an explicit path list, per round, by hand. This ticket makes it two commands. AR-14 deliberately left setting `landed` to `issue land`.

Two kinds of commit, never mixed: the winner as-is (`entry merge`), then the closing commit (`issue land`).

## `arena entry merge NN AGENT [--uncommitted] [--squash]` — commit 1

Registered as `OBJECTS["entry"]` verb `merge` in `tools/arena/cli.py`, the way `base check` (AR-25) and `run judge` (AR-64) are. The logic is in a new `tools/arena/merge.py`.

**Inputs.** The round's folder from `rounds.round_folder` (a bare `NN` is its last leg) and the `entrants.json` there:

```json
{"base": "<sha>", "entrants": {"<agent>": {"source": "<path from the repo root>", "state": "STALLED"} | {"duplicate_of": "<agent>"}}}
```

`state` is present only for an entry whose file name carries one (`GAVE_UP`, `STALLED`, `ERROR`); a READY entry has `source` alone (`tools/contest/export.write_entrants`). A `duplicate_of` entry has no `source`: follow it to the agent it names (the same patch text). The commit is still the asked-for agent's, with its own trailer.

**Refusals, all before anything is touched** (one line each, exit 2, the arena refusal path `output.refuse`):

- the round is alive (`rounds.round_alive`);
- no such agent in `entrants.json`;
- the current branch is not the one `.arena/rounds/NN.json` records under `branch`. **The operator checks the branch out; arena never switches.** A round started by the old command has no record: then the profile's `branch`, else no branch check;
- the working tree has any tracked change (`git diff --quiet` and `git diff --cached --quiet`).

A warning, not a refusal: the integration tip is no longer the round's base (`git rev-list --count <base>..HEAD`, printed).

**The entry**, by the exporter's own file names (`tools/contest/cli.export_patches`):

| File | What it holds | Taken |
|---|---|---|
| `<agent>.patch` | `format-patch` output of a READY agent | yes |
| `<agent>.<STATE>.patch` (`GAVE_UP`, `STALLED`, `ERROR`) | `format-patch` output of an agent that has a commit but did not end READY | refused (exit 2) unless `-y`, the state named in the refusal |
| `<agent>.<STATE>.diff` (`STALLED`, `ERROR`) | `git diff <base>` of an uncommitted tree; tracked files only, the untracked ones only named in a trailing comment | refused (exit 2) unless `--uncommitted`; the untracked names are printed as "not in the diff — copy them by hand" |

**The patch.**

- `git apply --index --exclude='epic-tasks/*' --exclude='runs/*' <file>`: staged, not committed (`git apply` skips a `format-patch` file's mail headers). **No `--3way`:** on a conflict it leaves conflict markers in the tree and unmerged index entries, which breaks "tree unchanged"; plain `--index` applies all or nothing.
- Before applying, print the dropped paths: those of `git apply --numstat -z` that the excludes match.
- A patch that does not apply: print git's error, exit 1, tree and index unchanged, with the hint "the integration branch moved since the round — apply by hand with `git am -3 <file>`".
- The paths the commit takes are `git diff --cached --name-only -z --no-renames` after the apply (`--numstat` names only the destination of a rename, so a source's deletion would be left out).

**The message** comes from `git mailsplit` + `git mailinfo` on the file (they decode a wrapped or RFC 2047 `Subject:` and drop `[PATCH n/m]`; hand parsing would not). A file with more than one message is refused (exit 2), naming the count, unless `--squash`: then one commit, the first message's subject and body plus the line `(squashed from N commits)`.

**The commit:** `git commit -F <msg> -- <the paths above>`.

```
<the patch's subject, "NN: " prepended unless it already starts with "NN:" or "AR-<n>:">

<the patch's body, as-is>

Co-Authored-By: <model> <<model>@round-NN.contest>
```

- An uncommitted entry (`.diff`): subject `NN: <agent>'s uncommitted tree, taken as-is`.
- **Author: the checkout's identity**, `git commit` without `--author`. Never the patch's `From:`.
- **`<model>`** is the agent's `model_id` from `agents[].agent.model_id` in the last leg's `state.json` (the agent name when missing), put through the exact rule `tools/contest/cli.agents_from_models` names an agent by, before any `-varN`: the part after the last `/`, cut before the first `:`, lower-cased, every character that is not alphanumeric, `_` or `-` turned into `-`, leading `_`/`-` stripped. So `GLM-4.7` is `glm-4-7`, a model `…-6.8-…` is `…-6-8-…`, and the agent's `-var2` is not in it (agent `sensenova-6-8-flash-lite-var2` has trailer `sensenova-6-8-flash-lite`). The `-varN` is never stripped from the agent name (a model may itself end in `-var1`): the id is recomputed from `model_id`. Pin it in a test: for a model that appears once, it equals `cli.agents_from_models(<model_id>)[0].name`.
- No `**Status:**` change, no bench: if the winner needs fixing, the operator fixes it now in the working tree.
- Record `.arena/merged.json`: `NN` → `{agent, sha, subject}` (written atomically, other rounds' records kept).
- Print `merged <sha7> — fix it now if needed, then: arena issue land NN`.

## `arena issue land NN [-m SUBJECT] [--score TEXT] [--note TEXT] [-- PATH…]` — commit 2

- **The ticket** is read from `arena-round/NN` (`git show arena-round/NN:epic-tasks/<name>`), else `epic-tasks/`, else `.arena/drafts/`. Only its `**Status:**` line is rewritten (`tickets.set_status_text`, AR-14), every other line byte-identical, to:
  - `landed — round NN, winner <agent> (<--score>), <sha7> as-is`; `<sha7> + follow-up` instead of `as-is` when paths follow `--`;
  - without `--score` the parenthesis is left out (READY/TOTAL is not the score; round 131's line says `28/28 on contest-bench/131`);
  - winner and sha from `.arena/merged.json`; with no record: `landed — round NN`.
  The result is written to `epic-tasks/<name>`.
- **`epic-tasks/INDEX.md`:** that ticket's row, when it exists, gets the same status word, with `scripts/ticket_status.py`'s `STATUS_RE` / `INDEX_ROW` imported, not a second regex.
- **The bench:** `contest-bench/NN/` when present, staged with `git add -- contest-bench/NN` (so `.gitignore`d files such as `__pycache__` stay out).
- **Extra paths after `--`:** the operator's follow-up edits. Each must exist and be changed or new; a path with no change → refusal.
- **Refusal** when any tracked change exists that is not in that list (nothing is left out by accident, nothing is swept in). Also refused: a round that is alive, a ticket not found, a wrong branch (same rule as `entry merge`).
- **Subject:** `-m`, else `NN: ticket landed, and contest-bench/NN holds the round's acceptance bench` (without the bench clause when there is no bench).
- **Body:** `Round NN: <READY>/<TOTAL> READY; winner <agent> taken as-is in <sha7>.` (READY/TOTAL from the last leg's `state.json`; the winner and sha from `.arena/merged.json`, the line left out when none is recorded), then `--note TEXT` as given (the bench score, the test totals). **Trailer:** the profile's `trailer`, if set.
- `git add -- <ticket> <INDEX file when edited> <bench files> <extra paths>`, then `git commit -F <msg> -- <the same paths>`. **Explicit paths only, never `-a`.** A new file is staged with `git add -- <path>` first: `git commit -- <path>` refuses a path git does not know.
- Then print, do not run:

  ```
  next: python3 -m pytest tests -n 8 -q
  then: python3 -m pytest tests_bugfix -n 8 -q
  push: git push <remote> <branch>
  ```

  `<remote>` is `git config branch.<branch>.remote`, else `origin`.

The ticket and the bench therefore go into the **final** commit, whether it is a pure "ticket landed" commit or the follow-up that fixed the winner. No separate status commit ever exists. A larger follow-up is its own commit by hand first, then `issue land` closes.

## Rules for the implementation

Never `git push`. Never `git commit -a`. Never switch the branch. Never `--3way`. No LLM. Do not change `tools/contest/` and do not touch `agents_128k.ini`. Every refusal is a line in `arena`'s refusal path with no traceback (a non-UTF-8 patch or subject included: use `gitref.git` / `gitref.printable`). Everything printed goes through `output.scrub`.

## Tests (`tests/test_arena_entry_merge.py`, `tests/test_arena_issue_land.py`)

Real git repositories in `tmp_path`, no mocks of git.

1. `entry merge`: a `format-patch` whose `From:` is another address lands as **one** commit; author = the repo's `user.email`; the trailer line exact; the subject gets `NN: ` when missing and is unchanged when it already starts with `NN:` or `AR-<n>:`; a wrapped / non-ASCII `Subject:` comes out whole.
2. A patch touching `epic-tasks/NN-x.md` and `runs/a/PROGRESS.csv`: those paths are not in the commit and are printed as dropped.
3. A non-applying patch (one that would conflict under `--3way`): exit 1, `git status --porcelain` identical before and after, no conflict markers, no commit, the `git am -3` hint printed.
4. A `.STALLED.diff` without `--uncommitted`: exit 2; with it, one commit and the untracked names printed. A `.STALLED.patch` without `-y`: exit 2; with it, one commit. A two-message patch: exit 2 naming `2`; with `--squash`, one commit with `(squashed from 2 commits)`.
5. Alive round / dirty tracked tree / wrong branch: exit 2, nothing staged.
6. `issue land --score "19/19 on contest-bench/NN"`: one commit with the ticket (`**Status:** landed — round NN, winner <agent> (19/19 on contest-bench/NN), <sha7> as-is`, every other line byte-identical; without `--score` no parenthesis), the INDEX row's word when a row exists, and `contest-bench/NN/*` (new, untracked files staged by the `git add` step; `__pycache__` not); the body names the merged winner.
7. `issue land NN -- tools/x.py` with `tools/x.py` changed: in the same commit, and the status line ends `<sha7> + follow-up`; another changed tracked file not listed: exit 2; a listed path with no change: exit 2.
8. Neither commit contains `arena-round/NN`'s ticket commit (`git log <branch>` has no `NN: ticket for the round`), and no `contest-legs` branch exists afterwards.
9. A patch with a rename: the source's deletion and the destination are both in the commit.
10. The trailer's model part equals `cli.agents_from_models(<model_id>)[0].name` for a model that appears once (`GLM-4.7`, `x/y-6.8-z:free`); a `duplicate_of` entry resolves to the named agent's patch with its own trailer; `.arena/merged.json` keeps another round's record.
11. `issue land` without a merge record: status `landed — round NN`, no winner line in the body; `--note` appended as given.

## Round 195 — what landed

Nine entrants; the ideal is `sensenova-6-7-flash-lite-var1`'s code and tests (own suite 224/225, the
fewest cross failures on its code) with two refusals found by the cross matrix and one test taken from
another entrant:

- `issue land` refuses a ticket whose checkout copy has uncommitted changes: the text is read from
  `arena-round/NN` and written over that copy, so the edit was lost without a word
  (`test_an_uncommitted_edit_of_the_ticket_is_not_overwritten`).
- `issue land` refuses a ticket already `landed`, instead of repeating the commit
  (`test_a_landed_ticket_is_not_landed_twice`).
- `entry merge` then `issue land` in a row: two commits, the second names the first's sha
  (taken from `sensenova-6-8-flash-lite-var2`'s `test_entry_merge_then_issue_land_is_two_distinct_commits`).

Not bugs, recorded so they are not filed again: the other entrants' tests that fail on this code differ in
the wording of a refusal or in the shape of the status line (`landed (round NN)` against the ticket's
`landed — round NN`); `agnes-2-5-flash`'s `issue land` tests have no `contest.ini` in their fixture, and
`issue land` needs the roster to find the round's folder.
