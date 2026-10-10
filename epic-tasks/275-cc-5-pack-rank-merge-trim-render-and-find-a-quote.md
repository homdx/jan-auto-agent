# 275 — CC-5: the pack: rank, merge, trim, render, and find a quote

**Status:** landed
**Severity:** HIGH (the pack is what the voter reads; its size and order decide both cost and accuracy)
**File:** `tools/claimcheck/pack.py`
**Symbol:** `PackBudget`, `build_pack`, `Pack.render`, `Pack.find`
**Round:** 275
**Size:** M
**Depends on:** CC-3 (`source_chunks`), CC-4 (`git_chunks`)
**Also touches:** `tools/claimcheck/model.py`, `tests/test_claimcheck_pack.py`, `tests/fixtures/claimcheck/pack_golden/`

Sequential: it needs both evidence providers.

---

## Why

CC-3 and CC-4 each return as much evidence as they can find. A voter cannot read all of
it: a reasoning model that is handed too much spends its budget before writing a verdict
(seen: a 59-claim batch that returned nothing). The pack decides what survives, in what
order, and in what text. It also owns the one fact that the voter contract depends on: what
counts as "a verbatim quote from this chunk".

## What it does

### `build_pack(claim, resolved, view, *, model=None, base=None, head=None, budget=PackBudget()) -> Pack`

1. Collects chunks: `evidence_source.source_chunks(...)` and
   `evidence_git.git_chunks(...)` for the found anchors.
2. Adds a **note chunk** for every anchor that did **not** resolve — the missing code is
   evidence too:
   `note:dangling:<text>` (kind `note`): "`Policy._mechanical` does not exist at
   `<sha7>`; the file `tools/contest/policy.py` exists and defines: `Policy`, `decide`, …"
   (up to 10 names, to let a voter see a rename).
3. **Merges** chunks of the same file whose line ranges overlap or touch (the union, text
   rebuilt from the file's own lines; ids recomputed).
4. **Ranks** (higher first), deterministically:

   | Kind | Base score |
   |---|---|
   | `note` (dangling) | 110 |
   | `source` of a symbol anchor | 100 |
   | `git` diff hunk of an anchored path | 90 |
   | `source` of a path anchor | 60 |
   | `ticket` | 50 |
   | `collect` | 40 |
   | `git` log / header | 30 |

   plus **+10 per distinct claim keyword** present in the chunk, capped at +40; ties by id.
5. **Trims** to `PackBudget(chars=6000, chunks=6, per_chunk=2400)`: takes chunks in rank
   order until the next one would pass either limit; a top-ranked chunk longer than
   `per_chunk` is cut at a line boundary with a marker rather than dropped. `Pack.truncated`
   is `True` whenever anything was dropped or cut.
6. Returns a `Pack` whose `chunks` are in **rank order** (not file order).

### `Pack.render() -> str`

The exact text the voter reads, with a stable format (a golden file pins it):

```
EVIDENCE for the claim, at commit 3f2a9c1
[[src:tools/contest/gates.py:120-143]]  (source)
 120| def _declared_paths(body):
 121|     ...
[[git:a73e389:header]]  (git)
 commit a73e389...
[[note:dangling:Policy._mechanical]]  (note)
 `Policy._mechanical` does not exist at 3f2a9c1; policy.py defines: Policy, decide, ...
(evidence trimmed: 2 chunks omitted)
```

File chunks are line-numbered with the file's numbers (right-aligned, then `|`); other kinds
carry no numbers. An empty pack renders `EVIDENCE: none found for this claim.`

### `Pack.find(quote: str) -> Optional[str]`

Returns the id of the chunk containing the quote, or `None`. The comparison normalises both
sides the same way: strips the `NNN|` gutter from the quote's lines, collapses runs of
whitespace to one space, trims, and is case-sensitive. A quote shorter than 8 characters
after normalisation returns `None` (a one-word "quote" proves nothing). A quote that spans
two chunks returns `None`. This is the function CC-6's `verify_quotes` is built on, and the
only place the rule lives.

### `PackBudget`

A frozen dataclass; `from_config(parser)` reads `[claim_vote] pack_chars`, `pack_chunks`,
`pack_chunk_chars` (CC-6 adds the keys to `contest.ini`; this ticket reads them with the
defaults above when absent).

## Tests (`tests/test_claimcheck_pack.py`)

| Test | What it pins |
|---|---|
| `test_budget_is_never_exceeded` | 200 seeded random chunk sets, random budgets → rendered length ≤ budget + header allowance, chunk count ≤ limit |
| `test_rank_order_table` | one chunk of each kind with equal keywords → the order of the table above |
| `test_keyword_bonus_and_cap` | five keywords → +40 not +50; a lower-kind chunk with keywords can pass a higher-kind one only as the scores say |
| `test_ties_break_by_id` | equal scores → id order, twice the same |
| `test_overlapping_ranges_merge` | two chunks of one file, lines 10-20 and 18-30 → one chunk 10-30 with the file's text |
| `test_touching_ranges_merge` | 10-20 and 21-30 → one |
| `test_top_chunk_is_cut_not_dropped` | a 10 KB top chunk → cut at a line, marker, `truncated` |
| `test_dangling_anchor_becomes_a_note` | the note names what the file does define |
| `test_note_for_missing_file` | a missing path → a note that says the file is not at that sha |
| `test_render_format_golden` | `pack_golden/`: three packs → exact text |
| `test_render_empty_pack` | the fixed sentence |
| `test_find_normalises_whitespace_and_gutter` | a quote with `120|` prefixes and tabs → found |
| `test_find_rejects_short_and_spanning_quotes` | 7 characters → `None`; a quote across two chunks → `None` |
| `test_find_is_case_sensitive` | `check=false` ≠ `check=False` |
| `test_find_across_kinds` | a quote from a git hunk finds the `git:` chunk |
| `test_determinism` | twice → equal bytes |
| `test_unicode` | non-ASCII source lines survive render and find |

## Acceptance (the operator's bench, `contest-bench/275/acceptance_275.py`)

The whole chain **minus the model**, on the fixture's 50 code claims and the 30 real ones:
the pack's `truncated` rate is reported; the **deciding lines survive the trim** in ≥ 90 % of
the fixture claims (the same measure as CC-3, after ranking and the budget); the median
pack is ≤ 3500 characters and the 95th percentile ≤ 6000; the render of every pack parses
back (each `[[id]]` is a chunk of the pack and each chunk's text is `find`-able).

## Edge cases to handle

A claim with a dozen anchors; two anchors that resolve to one symbol; a note chunk plus real
chunks; a budget of 1 chunk; a chunk with a line over 2000 characters (cut with a marker,
the numbering intact); a pack for a `world` claim (not built: the caller must not ask;
`build_pack` on no anchors returns the empty pack).

## What CC-3 and CC-4 actually return (landed; read before you build)

The text above was written before the providers existed. Where it differs, this section wins.

* **Chunk ids and kinds.** `src:<path>:<a>-<b>` (`source`), `git:<sha7>:header` (`git`, no span),
  `git:<sha7>:<path>:<a>-<b>` and `diff:<b7>..<h7>:<path>:<a>-<b>` (`git`, a hunk; `a-b` is the
  hunk's **new-side** span), `diff:<b7>..<h7>:stat`, `gitlog:<path>`, `ticket:<id>` (`ticket`),
  `note:git:<what>` (`note`; a commit, rev or ticket that is not in the repository, a binary or
  too-large file). `collect_chunks` give `kind="collect"`. All carry `why`.
* **Gutters.** A `src:` chunk prints ` 120| def f():` (number right-aligned to the width of its
  `end`); a gap is one line `# … N lines omitted (a-b)`. A git hunk prints the diff's own
  lines behind the **new side's** number, ` 18| +    check=False`, and ` ` (blank number) for a
  `-` line and for `\ No newline`; its `diff --git`/`---`/`+++`/`@@` lines have no gutter. Header,
  stat, log, ticket and note chunks are plain text. `Pack.find` must strip a gutter of either
  kind, and must **not** treat a diff's own `+`/`-` as part of the gutter: a quote
  `+    check=False` is verbatim from the chunk.
* **Merging is for `src:` chunks of one path only.** Two hunks of one file are different
  chunks with different meanings (one diff each), and a `git` chunk never merges with a `source`
  chunk. Rebuild a merged `src:` chunk's text from `view.read(path)` with the same gutter and
  the gap markers of the lines that stay out; a merged span that no longer fits the budget goes
  through the trim like any other chunk. A chunk whose lines cannot be read back (the file is
  over 400 KB, or gone) stays as it is.
* **A cut chunk is cut the way the provider cuts**: at a line, with a marker line
  (`# … N lines omitted`, `# … N diff lines cut`); the new marker you add must look like those
  and must not count as a quoted line.
* **Notes.** A dangling anchor has `ResolvedAnchor.found=False` and the nearest place the
  repository does have in `.path` (`is_dangling(resolved)` in `anchors.py`); the "defines: …"
  list of the note comes from the file's definitions, which `source_chunks` already read — do not
  parse a file a second time when the chunk list holds its definition list.
* **Providers never raise** and return `[]` or notes on a bad view; `build_pack` must not raise
  either, whatever a provider or the view does (a `RepoView` whose `git` hangs, a `read` that
  fails): an empty pack or a pack with a note, `truncated=False`.
* The provider functions take `max_chunk_chars`; pass `PackBudget.per_chunk` so that a chunk
  arrives already within it.

## Not in scope

The prompt, the vote parser, the downgrade rule (CC-6).

**Judging fixes (seven entries, one neutral probe, the operator bench, the cross matrix).** The
round was run twice: the first run started from a base without `tools/claimcheck` (CC-0…CC-4
missing) and was thrown away; the second, on `3d0f10f`, gave six entries and the cloud patch
`cc-5-275-pack.patch`, which is the base of the landed code (best by the others' tests in every
column of the cross matrix, 80 own tests). One hole found in it by a probe the other entries
passed: a path written out in the claim that is neither a file nor a directory in the repository
(`no/such.py`, no `no/` either) got no note, and the pack said "none found" — as if the claim were
about nothing; it is a `note:dangling:` now ("does not exist … no file of that name"). Not ported,
because the landed code does not share them: the merge that looked only at the last chunk of the
file (`[1-10, 40-50, 8-45]` stayed in two pieces), a marker line counted as a quote, a chunk of
2391 characters cut by the block overhead taken off `per_chunk` (all `sensenova-var3`), the claim's
whole text in the header and another empty-pack sentence (`var1`), a view that raises not survived
(`nemotron`), the chunk id parsed as an integer (`glm`). Ranking every note at 110, CC-4's too, is
the landed code's choice (a voter must know that a commit is missing) and stays. Also CC-4:
`%aI` of a newer Git is `…Z` for a zero offset where an older one gives `…+00:00`; the header
date is written `+00:00` either way, so the golden fixtures do not depend on the Git version.
