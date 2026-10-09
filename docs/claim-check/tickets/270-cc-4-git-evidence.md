# CC-4 — git evidence: a commit, a diff range, a ticket's text, a file's history

**Status:** draft
**Severity:** HIGH (claims about "what the fix did" are claims about a diff)
**File:** `tools/claimcheck/evidence_git.py`
**Symbol:** `git_chunks`, `hunks_of`, `ticket_chunk`
**Round:** —
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
