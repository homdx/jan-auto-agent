# 270 — CC-4: git evidence: a commit, a diff range, a ticket's text, a file's history

**Status:** landed
**Severity:** HIGH (claims about "what the fix did" are claims about a diff)
**File:** `tools/claimcheck/evidence_git.py`
**Symbol:** `git_chunks`, `hunks_of`, `ticket_chunk`
**Round:** 270
**Size:** M
**Depends on:** CC-1 (`model.py`)
**Also touches:** `tests/test_claimcheck_git.py`, `tests/fixtures/claimcheck/git_golden/`

Runs in parallel with CC-3: this ticket owns `evidence_git.py`, CC-3 owns
`evidence_source.py`; both only import `model.py`.

---

## Why

Half of the code claims in a review are about history, not about the current text:
"Commit a73e389 found that gates read one line and dropped 3 of 11 paths, but it fixed only
the ticket text", "KC-23's refusal to reset exists to protect work", "the fix landed but
`_declared_paths` still reads one line". The evidence for such a claim is the commit's diff,
the diff range between two refs, or the text of the ticket the claim cites. Before/after
(CC-7) is built on the same diff chunks.

## What it does

### `git_chunks(resolved, view, *, base=None, head=None, claim="", max_chunk_chars=2400) -> list[Chunk]`

All reading goes through `view.git(...)`; **read-only** subcommands only (`show`, `diff`,
`log`, `cat-file`, `rev-parse`, `ls-tree`), `--no-ext-diff --no-textconv`,
`GIT_OPTIONAL_LOCKS=0`, a 20 s timeout per call, the user's git config ignored
(`GIT_CONFIG_GLOBAL=/dev/null`). Held-index waits are `tools.git_run.run_git`'s job; this
ticket does not reimplement them.

| Anchor / input | Chunk(s) | id |
|---|---|---|
| `commit` (found) | a header chunk: full sha, author, date, subject, body (cut at 1200 chars), `--stat`; then the **hunks** of the commit that touch a path or symbol the claim names (else the three files with the largest change) | `git:<sha7>:header`, `git:<sha7>:<path>:<newstart>-<newend>` |
| `base` and `head` both given | for each anchored path: the hunks of `git diff base..head -- <path>`; for a claim naming no path: the files changed, `--stat`, and the hunks that contain a claim keyword | `diff:<base7>..<head7>:<path>:<newstart>-<newend>` |
| `ticket` (found) | the ticket's title line, its `**Status:**` line, and the first section up to the budget (the ticket text *is* the spec the claim is measured against) | `ticket:<id>` (kind `ticket`) |
| `path` (found) when the claim talks about history (`fixed`, `changed`, `introduced`, `added`, `removed`, `regress`) | `git log -n 5 --format=%h %ad %s -- <path>` | `gitlog:<path>` |

`hunks_of(patch_text, *, keywords, paths=None) -> list[Hunk]` splits a unified diff at
`@@`, keeps the file header, and returns hunks ranked by claim keyword hits (ties by
position). A hunk that exceeds `max_chunk_chars` is cut at a line boundary with a marker.

Hunks keep **both** line numbers in the text (`-old` and `+new` lines as in the diff, with a
line-number gutter for the new side) so a quote like `+    check=False` is verbatim from the
chunk.

### Properties

* **Never raises on a bad rev.** A sha that is not in the repository, a path absent from a
  commit, a binary file: no chunk and one `note` chunk id `note:git:<what>` saying so
  (`commit a73e389 is not in this repository`). The absence is evidence for CC-5.
* **Binary and huge files** are skipped with the note `binary file: <path>`; a diff over
  200 KB is summarised by `--stat` and the keyword hunks only.
* **Renames** are shown as git shows them with default detection (`-M`), and the hunk header
  names both paths; a claim about the `old => new` form therefore finds it.
* **Deterministic.** Same repository, same inputs, byte-identical chunks.

### `ticket_chunk(resolved, view) -> Optional[Chunk]`

Finds `epic-tasks/NN-*.md` through `view.ticket_file`, reads it, and returns the chunk
described above; `None` for a missing ticket.

## Tests (`tests/test_claimcheck_git.py`)

All on temporary repositories with fixed identity and dates and `GIT_CONFIG_GLOBAL=/dev/null`.

| Test | What it pins |
|---|---|
| `test_commit_header_chunk` | sha, subject, body cut, stat |
| `test_commit_hunks_follow_the_claim_path` | a claim naming `b.py` gets b.py's hunks, not a.py's |
| `test_commit_without_a_named_path_takes_the_largest_files` | top three by change |
| `test_diff_range_hunks` | `base..head` hunks for the anchored path; line numbers match the head file |
| `test_hunks_ranked_by_keyword_hits` | the hunk containing the claim's token first |
| `test_rename_is_shown_as_git_shows_it` | `old => new` appears; the hunk header names both paths |
| `test_binary_file_is_skipped_with_a_note` | a `.png` in the commit → note chunk, no text |
| `test_huge_diff_is_summarised` | a 300 KB diff → stat + keyword hunks, under budget |
| `test_unknown_sha_is_a_note_not_an_error` | `commit deadbee` → note chunk |
| `test_ticket_chunk_title_status_first_section` | and a missing ticket → `None` |
| `test_history_chunk_only_for_history_words` | `fixed` → `gitlog`; "reads" → none |
| `test_read_only` | `git status`, `HEAD`, the index bytes unchanged; no `index.lock` created; only the allowed subcommands were run (a `view.git` spy) |
| `test_user_git_config_is_ignored` | a hostile global config (`diff.external`, `core.pager`, `color.ui=always`) → plain output |
| `test_path_with_spaces_and_unicode` | `a b/é.py` |
| `test_timeout_gives_a_note` | a spy that sleeps beyond the timeout → note chunk |
| `test_determinism` | twice → equal bytes |
| `test_golden_chunks` | `git_golden/`: 10 cases → exact chunk texts |

## Acceptance (the operator's bench, `contest-bench/270/acceptance_270.py`)

On the fixture (base and head shas): for the 6 commit/ticket claims and the 20 fix claims,
the deciding hunk or ticket text is inside the returned chunks (target 100 % of the commit
and ticket claims, ≥ 90 % of the fix claims); for each dangling commit claim a `note`
chunk is returned.

## Edge cases to handle

A merge commit (diff against the first parent); the root commit; an empty commit; a commit
that only renames; CRLF in a diff; a very long single line; a ticket file with no
`**Status:**`; two tickets with the same number prefix (take the lowest path).

## Not in scope

Source bodies (CC-3), ranking and the budget across providers (CC-5), before/after logic (CC-7).

## As built

Places where the code is more exact than the text above:

* **The views, not this module, own the git environment.** Building CC-4 found that both
  views' `git` read the operator's global config (`color.ui=always` put escape codes into
  `git show`, `diff.noprefix` dropped `a/` `b/`) and had no time limit — fixed in the views
  (`PathRepoView.git`, `Target.view().git`: `GIT_CONFIG_GLOBAL=/dev/null`,
  `GIT_CONFIG_NOSYSTEM=1`, killed after 20 s), a separate commit before this one.
  `git_chunks` adds its own bound for any `RepoView`: each call runs in a daemon thread
  joined for `GIT_TIMEOUT` (20 s), so a view that hangs costs a note, not the run.
* **Subcommands used:** `show`, `diff`, `log` (and the view's `rev_parse`); `cat-file` and
  `ls-tree` were not needed. The repository's own config cannot change the text either:
  every diff passes `--no-color -M --src-prefix=a/ --dst-prefix=b/ --no-relative`, a patch
  also `--unified=3 --diff-algorithm=myers --indent-heuristic`, `show`/`log` pass
  `--no-show-signature` (`log.showSignature` would start gpg).
* **A commit** is diffed with `git diff <first parent> <sha>`; the root commit with
  `git show --root`. Its files are chosen from `diff --numstat -z`: the anchored paths
  (a path anchor counts as written even when the pinned tree lacks it — a file the commit
  deleted or renamed away — and it matches either side of a rename), else the
  `LARGEST_FILES` = 3 largest by changed lines. At most `MAX_HUNKS` = 8 hunks per commit.
* **Hunk text:** the file header and `@@` line verbatim, then each diff line behind a gutter,
  ` 18| +    check=False` (the new side's number; blank for a `-` line). A hunk over the
  budget keeps its head (≤ 40 %) and ±3 lines around each line holding a claim keyword,
  gaps marked `# … N diff lines cut` — a cut at the head alone lost the line the claim was
  about. One line over the budget keeps 400 chars and `… [N chars cut]`. A trailing `\r`
  is dropped from each line (CRLF files). A file with no `@@` (a rename alone, a binary
  file, a mode change) is one header-only entry, id `…:<path>:0-0`.
* **Big diffs:** a file changing more than `MAX_FILE_LINES` = 20000 lines is not fetched
  (note `diff too large: <path> (+a -d lines); see --stat`); one call fetches at most
  `MAX_FETCH_LINES` = 40000 changed lines (note `N more changed file(s) not read`); a
  fetched patch over 200 KB keeps only the hunks with a claim keyword.
* **A range** (`base` and `head`): an unknown rev is `note:git:<rev>` (`commit X is not in this
  repository`); an anchored path the range leaves alone is `note:git:unchanged:<path>`;
  a claim naming no path gets `diff:<b7>..<h7>:stat` and the keyword hunks of every file
  (binary and huge files are not noted there — a search says nothing of them).
* **Tickets:** a found path `epic-tasks/NN-*.md` gives the ticket chunk too (the fixture's
  ticket claims name the file, not the number); the id is `ticket:<id>` for a ticket anchor,
  `ticket:<path>` for a path. "The first section" is the text before the second `##`
  heading; the title and `**Status:**` lines are kept whatever the budget; lines carry
  the gutter ` 3| **Status:** done`, the rest is `# … the rest of the ticket omitted (a-b)`.
* **History words:** `fix/fixes/fixed`, `change…`, `introduce…`, `add/adds/added`, `remove…`,
  `regress…` as whole words (`addresses` is not one); `git log -n 5 --date=short
  --format='%h %ad %s' -- <path>`.
* **Never raises:** a failing or hung git is a note with git's first line; an exception of
  this module's own parsing is `git evidence failed: <type>: <message>`. Chunk ids are
  unique per call (a commit named twice gives its chunks once).
* **Not handled here:** CC-1 cuts a backticked path at a space (`` `a b/é.py` `` is read as
  `b/é.py` — the text alone cannot tell it from `` `python3 x.py` ``); git's quoted form of
  such a path is parsed (`"a/a b/\303\251.py"`), the test gives the anchor resolved.
  A URL target is a partial clone (`--filter=blob:none`); with the global config ignored,
  a credential helper from it is not used to fetch an old blob of a private repository —
  that diff is a note.

Tests: `tests/test_claimcheck_git.py`, 36 (the 17 above, the edge cases, golden set of 10
cases in `tests/fixtures/claimcheck/git_golden/` — `history.json` builds the repository
with fixed identity and dates, so the shas are the same on every machine). Mutation run:
26 of 26 mutants of `evidence_git.py` killed. Bench `contest-bench/270/acceptance_270.py`:
fix claims 20/20 (target 90 %), commit and ticket claims 6/6, dangling commits 3/3, real
r026 + r027 2/2; it fails on 4 of 4 broken builds (paths ignored, status dropped, gutter
off by one, stat missing).

**Judging fixes (the three entries, cross-probed with one neutral script).** Three holes in
the landed code, each found in a probe that the other entries passed: a header's `--stat`
kept every file line (200 new files: 10 KB, 1500 files: 80 KB in one chunk — the other
entries cap it at 30 lines), a hunk over the budget dropped its keyword line when the ±3 lines
around it did not fit (1000 changed 300-char lines lost `+load_timeout = 9`; the window now
narrows to ±1 and to the line alone), and a binary file of a commit naming no file was in no
note (it has no changed lines, so the "largest files" never held it). `MAX_STAT_FILES` = 30.
Not ported, because the landed code does not share them: Sonnet's entry keeps `\r` in CRLF
chunks and, like Haiku's, loses the named file of a commit over 200 KB (≈1000 files), Haiku's
output depends on the operator's `diff.noprefix` (closed by the views' git environment).
