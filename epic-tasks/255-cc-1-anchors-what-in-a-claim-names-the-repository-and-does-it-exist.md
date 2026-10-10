# 255 — CC-1: anchors: what in a claim names the repository, and does it exist

**Status:** landed
**Severity:** HIGH (every later ticket starts from the anchors; the regex `needs_code` leaks today)
**File:** `tools/claimcheck/anchors.py`
**Symbol:** `extract_anchors`, `resolve_anchors`, `classify`, `PathRepoView`
**Round:** 255
**Size:** M
**Depends on:** CC-0 (the package skeleton and `contest-bench/cc/`)
**Also touches:** `tools/claimcheck/model.py`, `tests/test_claimcheck_anchors.py`, `tests/fixtures/claimcheck/anchors_golden.json`

Runs in parallel with CC-2: the two share no file.

---

## Why

Whether a claim is about our code decides everything downstream: a world claim goes to
the voters as it is, a code claim needs evidence. Today that decision is
`lenz_claim_filter.is_internal`, a regex over paths, ticket ids and backticked
identifiers. It misses claims whose subject is plain prose ("Nothing checks whether the
plugin is installed", "Commit a73e389 fixed only the ticket text") and it flags world
claims that happen to contain a slash or a dot (`dir/{a => b}.py` is a git output format,
not our file).

An anchor is a thing in the claim that names the repository. An anchor that **resolves**
against the repository at a pinned commit is proof the claim is about it. An anchor that
**does not resolve** (a function that is not there) is the most valuable kind: the claim
may simply be false. So classification needs the repository, not only the text.

## What it does

### `tools/claimcheck/model.py` (this ticket creates it)

The dataclasses and the `RepoView` protocol exactly as specified in `EPIC-CC.md` §4.3
(`Anchor`, `ResolvedAnchor`, `Chunk`, `Pack`, `RepoView`). `Chunk` and `Pack` are defined
here, with `Pack.render` and `Pack.find` left to CC-5: this ticket gives them a minimal,
tested body (`render` joins chunk texts; `find` is a plain substring search) so that CC-3
and CC-4 can import real types; CC-5 replaces the bodies.

### `extract_anchors(claim: str) -> list[Anchor]` (pure, no I/O)

Finds, in this order of priority, with spans and without overlaps:

| Kind | What matches | Notes |
|---|---|---|
| `commit` | 7–40 hex characters containing at least one digit and one letter a–f, **and** introduced by `commit`, `@`, `at`, `HEAD`, `sha`, or inside backticks | `deadbeef` in prose is not an anchor; resolution is the final judge |
| `ticket` | `\b(KC|FL|AR|AUTO|SLOW|GATE1|CC)-\d+\b` and `ticket \d{1,4}` | |
| `test` | `path/to/test_x.py::test_name`, `test_x::test_y` | before `path` and `symbol`, so it is not split |
| `path` | tokens with a directory part and a known suffix (`py md ini json csv sh toml yaml yml txt`), or a bare `name.py`; optional `:LINE` or `:LINE-LINE` | `dir/{a => b}.py` and `old => new` are rejected: a rename is not a path |
| `symbol` | dotted or qualified names in backticks or followed by `()`: `Policy._mechanical`, `gates.run_tests_detail`, `_pytest()`, `tools.contest.policy.Policy`; bare snake_case or CamelCase words **only** inside backticks | a backticked word that is a Python keyword, a builtin or a pytest option (`--timeout`) is not a symbol |
| `ref` | `origin/<name>`, `branch <name>`, `HEAD`, `main` when introduced by `branch`/`on` | used by CC-2, never counted as proof the claim is about code |

Quoting and markdown (backticks, `**`, trailing punctuation) are stripped from `text`;
`start`/`end` index the original claim.

### `resolve_anchors(anchors, view: RepoView) -> list[ResolvedAnchor]`

* `path`: `view.exists`; a bare file name resolves to the unique file with that name, or to
  `found=False` with up to 3 `candidates` when ambiguous.
* `symbol`, `test`: an index of definitions built **once per view** by `ast`-parsing the
  `.py` files (`def`, `async def`, `class`, methods, with `lineno`/`end_lineno`, decorators
  included in the span). Resolution order: path-qualified name > exact qualname > a
  module-qualified name > a bare name that is unique. An ambiguous bare name resolves to
  the first candidate by path order and lists the next two in `candidates`. A file that
  does not parse is skipped, not fatal. Files over 400 KB are skipped.
* `commit`: `view.rev_parse(text)` → full sha or `found=False`; a unique-prefix ambiguity is
  `found=False`.
* `ticket`: `view.ticket_file(id)` looks for `epic-tasks/NN-*.md` (the number after the
  dash, leading zeros ignored).
* `ref`: `view.rev_parse`.

### `classify(claim, resolved) -> "code" | "world" | "mixed"`

* `code`: at least one `path`, `symbol`, `test`, `commit` or `ticket` anchor resolved
  (`found=True`), **or** one failed to resolve but is *shaped like ours* — its first path
  component is a top-level directory of the repository, or its symbol is qualified with
  a module that exists. That is a `dangling` claim and it stays `code`: CC-5 turns it into
  evidence ("`X` does not exist at `<sha7>`").
* `world`: no such anchor at all.
* `mixed`: `code` conditions hold **and** the claim contains a world-fact trigger (the same
  words `lenz_claim_filter._HINTS` uses: exit codes, plugin, root, chmod, git subcommands,
  `numstat`, POSIX, `re.M`).

`ref` anchors alone never make a claim `code`.

### `PathRepoView(root: Path)`

A `RepoView` over a directory: `exists`, `read`, `files` (sorted, `.git` excluded),
`rev_parse` and `git` through `tools/arena/gitref.git` run read-only in `root`,
`ticket_file` over `epic-tasks/`. CC-2's `Target.view()` implements the same protocol.

### Hook in `scripts/claim_vote.py`

`claim_vote.tally` currently asks `lf.is_internal`. After this ticket it asks a small
adapter: with a `RepoView` available it uses `classify`; without one it keeps
`is_internal`. `needs_code` in `votes.json` keeps its name and meaning
(`classify != "world"`), and gains `kind` (`code|world|mixed`) and `dangling` (bool).
`claim_vote.py` gets no `--target` yet (CC-6); the adapter is exercised by a
`--symbols-root` that now builds a `PathRepoView`.

## Tests (`tests/test_claimcheck_anchors.py`)

| Test | What it pins |
|---|---|
| `test_paths_backticked_bare_and_with_lines` | `` `tools/contest/gates.py` ``, `gates.py`, `gates.py:12-30` |
| `test_rename_form_is_not_a_path` | `dir/{a => b}.py`, `old => new` → no anchor |
| `test_symbol_forms` | `Policy._mechanical`, `_pytest()`, `gates.run_tests_detail`, `tools.contest.policy.Policy.decide` |
| `test_backticked_words_that_are_not_symbols` | `--timeout`, `True`, `pytest`, `None` → none |
| `test_commit_anchor_needs_a_cue_and_resolution` | `deadbeef` in prose → none; `commit a73e389`, `@afa53f1`, `` `7b4e5f9` `` → anchors; unresolvable → `found=False` |
| `test_ticket_ids_and_numbers` | `KC-5`, `AR-25`, `ticket 123`, `CC-3` |
| `test_test_ids_are_not_split` | `tests/test_x.py::test_y` is one `test` anchor |
| `test_resolve_symbol_in_class_and_nested_and_decorated` | spans include decorators; nested function resolves by dotted name |
| `test_ambiguous_symbol_lists_candidates` | two files define `run`; first by path, two candidates listed |
| `test_resolve_skips_unparsable_and_huge_files` | a syntax-error file and a 500 KB file do not raise |
| `test_commit_prefix_ambiguity_is_not_found` | a 4-char collision → `found=False` |
| `test_classify_code_world_mixed` | a table of 40 claims (from the style of `kc-bug-report.md`, embedded as data) → expected kind |
| `test_dangling_symbol_stays_code` | `gates.vanished_fn()` where `gates.py` exists → `code`, `found=False` |
| `test_ref_alone_is_world` | "the branch `kc` is ahead of main" with no other anchor → `world` |
| `test_extract_is_deterministic_and_pure` | twice → equal; a monkeypatched `socket` and `subprocess` are never called |
| `test_claim_vote_uses_classify_with_a_view` | `tally(..., view)` marks a prose claim about an existing symbol `code`; without a view the old rule applies |
| `test_golden_anchors` | `anchors_golden.json`: 60 claims → exact anchor lists (a diff is a behaviour change) |

## Acceptance (the operator's bench, `contest-bench/255/acceptance_255.py`)

Against `contest-bench/cc/claims_fixture.json` and the fixture repository (base sha):
`classify` agrees with each claim's `kind` on ≥ 95 % of the 80, and on **every** dangling
claim; no `world` claim in the file is classified `code`. Then the same on the 30 real
claims against this repository at `real_sha` (≥ 90 %).

## Edge cases to handle

Claims quoting a git output format; a symbol and a path in one sentence; a backticked
shell command (`` `git rev-list` ``) is not a symbol; unicode in a claim; an empty claim;
a claim that is only a quotation of code; a very long claim (10 KB) must finish in under a
second.

## Not in scope

Pack building, the worktree, any LLM call, anchors in languages other than Python for
symbols.
