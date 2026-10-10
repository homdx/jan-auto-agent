# 265 — CC-3: source evidence: symbol bodies, keyword windows, collect facts

**Status:** landed
**Severity:** HIGH (this is most of what a voter reads)
**File:** `tools/claimcheck/evidence_source.py`
**Symbol:** `source_chunks`, `keyword_windows`, `collect_chunks`
**Round:** 265
**Size:** M
**Depends on:** CC-1 (`model.py`: `Chunk`, `ResolvedAnchor`, `RepoView`)
**Also touches:** `tests/test_claimcheck_source.py`, `tests/fixtures/claimcheck/source_golden/`

Runs in parallel with CC-4: this ticket owns `evidence_source.py`, CC-4 owns
`evidence_git.py`; both only import `model.py`.

---

## Why

The claims our reports make are about a function: "`_declared_paths` reads one line",
"`_commits_above()` returns 0 when `rev-list` fails". The voter needs that function's text,
with line numbers it can quote, and enough around it to know who calls it and what the
contract says. It does not need the file.

## What it does

### `source_chunks(resolved, view, model=None, *, claim="", max_chunk_chars=2400) -> list[Chunk]`

For each **found** anchor, in the order given, returns chunks; the same code is never
returned twice (overlapping ranges in one file are merged by line range).

| Anchor | Chunk(s) | id |
|---|---|---|
| `symbol` (function, method, class) | the definition's text, decorators and docstring included, line-numbered by the file's own numbers; if longer than `max_chunk_chars`: the signature and docstring, then the **keyword windows** (below), then an omission marker `# … N lines omitted (a-b)` | `src:<path>:<start>-<end>` |
| `test` | the test function's body, plus the fixture names it uses, resolved to their definitions if they are in the same file | `src:<path>:<start>-<end>` |
| `path` without a symbol | the import block and the list of top-level definitions (`name  line`), then the keyword windows; with `:LINE[-LINE]` the lines ±3 | `src:<path>:<start>-<end>` |

**Keyword windows.** `keyword_windows(text, claim) -> list[(start, end)]`: tokens of the
claim that look like code (identifiers with `_` or `.`, call names, string literals up to
40 characters, operators such as `==`, `<`, `check=False`, flags like `re.M`, numbers)
are looked up in the long body; each hit gives a window of ±3 lines; windows that touch
merge; at most 5 windows; total within the budget. A claim "reads one line" with `re.M` in
the body pulls in the `re.search(...)` line and its neighbours even when the function is
200 lines long.

**Collect facts** (`collect_chunks(resolved, model) -> list[Chunk]`, kind `collect`, only
when `model.available`): for the anchor's module,
`model.callers_of(path, limit=8)`, `model.calls_into(path, limit=8)`,
`model.contracts_for(qualname_or_path)`, `model.risk_for(path)`, `model.fail_open_for(path)`;
rendered as short plain lines (`caller: tools/x.py`, `contract: <text>`), one chunk per
fact kind, id `collect:<kind>:<path>`. An absent or stale-and-ignored model returns no
collect chunks and no error.

### Properties

* **Deterministic.** Same inputs, byte-identical chunks; no timestamps, no hash-order
  dependence (sorted before output).
* **Bounded read.** A file over 400 KB is read for the anchor's lines only (by seeking
  `ast` spans from a prior index) — never fully into a chunk; non-UTF-8 bytes are
  decoded with `errors="replace"`.
* **Line numbers are the file's.** 1-based, inclusive; a chunk's `start`/`end` match the
  numbering printed in its text, so a voter's quote can be located.
* **No evidence is invented.** A `found=False` anchor yields no chunk here (CC-5 adds the
  "does not exist" note).

## Tests (`tests/test_claimcheck_source.py`)

| Test | What it pins |
|---|---|
| `test_function_body_exact_lines` | span, text and `start`/`end` equal the file's |
| `test_method_in_class_and_nested_function` | qualified names resolve to the right body |
| `test_decorators_and_docstring_are_included` | the span starts at the first decorator |
| `test_long_body_keeps_signature_and_keyword_windows` | a 200-line function and a claim naming `check=False` → signature, docstring, the window around `check=False`, an omission marker with the omitted range |
| `test_keyword_tokens` | table: claim text → expected token list (identifiers, `re.M`, `==`, a number, a short literal) |
| `test_windows_merge_and_are_capped_at_five` | adjacent hits merge; ten hits → five windows |
| `test_path_anchor_gives_imports_and_definition_list` | no symbol → header + definitions index |
| `test_path_line_range_is_plus_minus_three` | `gates.py:40-44` → lines 37–47 |
| `test_test_anchor_includes_same_file_fixtures` | the used fixture's body is a second chunk |
| `test_overlapping_anchors_are_merged` | a method and its class → one chunk, not two copies |
| `test_collect_chunks_render_facts` | a stub `CollectModel` with callers, contracts, risk → one chunk per kind, ids stable |
| `test_absent_collect_model_is_silent` | `available=False` → no collect chunks, no exception |
| `test_missing_anchor_yields_nothing` | `found=False` → `[]` |
| `test_big_file_is_not_read_whole` | a 2 MB file: the read counter (a `RepoView` spy) stays under the cap |
| `test_non_utf8_file` | bytes `\xff` → a chunk with the replacement character, no exception |
| `test_determinism_and_order` | twice → equal; anchor order in, chunk order out |
| `test_golden_chunks` | `source_golden/`: 12 (claim, anchors) pairs → exact chunk texts |

## Acceptance (the operator's bench, `contest-bench/265/acceptance_265.py`)

Against the fixture repository at its base sha and the 50 fixture code claims:
**recall of the deciding lines** — for each claim the fixture records, in `how`, the lines
that settle it; the check is that those lines are inside the returned chunks. Target ≥ 90 %
of the claims, and the total chunk text per claim ≤ 6000 characters for 95 % of them.
Then the same on the 30 real claims at `real_sha` (target ≥ 80 %).

## Edge cases to handle

An anchor naming a class with 40 methods; a symbol defined twice in one file (a property and
its setter); a lambda assigned to a name; `__init__`; a function inside `if TYPE_CHECKING`;
a file with tabs and CRLF; a claim with no code-like tokens (windows are empty, not an error).

## Not in scope

Git evidence (CC-4), ranking across providers and the budget as a whole (CC-5), any prompt.

## As built

Places where the code is more exact than the text above:

* **A chunk's text** is the file's lines with a gutter, ` 120| def f():` (the number right-aligned
  to the width of the chunk's `end`, as CC-5's render shows it); a gap is one line
  `# … N lines omitted (a-b)`, also at the end when the span runs past the last printed line, so
  `start`/`end` (and the id) are the span, and every printed number is the file's. A path chunk's
  definition list follows its first printed range as `# definitions (name  line):` and
  `#   name  line` rows. Lines are split the way Python's parser splits them (`\r\n`, `\r`, `\n`),
  not by `str.splitlines`, which also breaks on a form feed and would shift every later number.
* **"Path without a symbol" is taken literally:** a path anchor adds no chunk when a found symbol or
  test of the same claim is in that file. And when a claim places a symbol in a file it names
  (`_commits_above` in `tools/contest/workspace.py`) but CC-1 picked another file's definition
  first, the candidate in the named file is used — only a candidate CC-1 found, nothing looked up.
* **A file that fits in `max_chunk_chars` comes whole.** Otherwise a definition keeps its
  signature and docstring (at most 60 % of the budget), a named file its import block and its
  definition list (a quarter each, at most), and the keyword windows get the rest.
* **Keyword tokens** (`keyword_tokens`, public, the table test pins it): backticked text; words with
  `_` or `.` (not `e.g`), plus a dotted word's last part when longer than two (`load_timeout`
  of `config.load_timeout`); a name before `(`; string literals up to 40 chars (a `'` only
  between non-word characters, so "doesn't" is no literal); `name=value`; `==`, `!=`, `<=`, `>=`,
  `<`, `>`; numbers (`-1`, `0.5`, not the `8` of `utf-8`). At most 40, from the first 4000
  characters. A token on more than a quarter of a body's lines (and more than five) is noise and
  is ignored — `0` on every `STEP_0xx` line. Windows are picked where the rarest tokens meet
  (each token weighs 1 / its hit count); a hit inside a window still extends it by ±3.
* **The same name twice in one file** (a property and its setter) comes as both definitions:
  CC-1's `candidates` with the same path and qualname are added, and merge when they touch.
* **A test's fixtures** are followed transitively, up to eight, by argument name and
  `@pytest.mark.usefixtures`; `@pytest.fixture(name="x")` is found by `x`; a fixture of the test's
  own class wins over the module's.
* **Over 400 KB** (`MAX_READ_BYTES`): the size comes from the view's `size(path)` when it has one,
  else from the file under `view.root`; such a file is never passed to `view.read` — its lines are
  streamed (`view.read_lines(path, a, b)` when the view has it, else the file under the root, line
  by line, after `view.exists(path)`, so what the view hides stays hidden). A file named without
  lines shows its first 40. Invalid UTF-8 (surrogate-escaped by `PathRepoView.read`) prints as
  U+FFFD.
* **Collect:** contracts are asked for `path:Qualname` and for `path`; rows are `caller: p`,
  `calls: p`, `contract: name: description`, `risk: score …, blast_radius …, loc …, unguarded …,
  undocumented_fail_open …, zero_coverage …`, `fail_open: location Type — rationale`. A model with
  `available` false, or `None`, gives nothing; a query that raises gives no chunk of that kind.
  `source_chunks(..., model)` appends the collect chunks after the source chunks.
* **A lambda assigned to a name** is not a definition CC-1 indexes, so `to_int` is not found and
  gives no chunk (no evidence invented); a claim that also names the file gets the whole file, or
  its windows, where the assignment is (golden case 9).
* **Linear:** window costs come from prefix sums and a merge looks only at its own file's pieces
  (`keyword_windows` on 40 000 dense-hit lines: 0.14 s; it was 15 s on 10 000 before).

Acceptance (`contest-bench/265/acceptance_265.py`, anchors from `extract_anchors` on the claim,
resolved in the pinned tree): fixture base 50/50 claims with the deciding lines printed, all 50
under 6000 chars; real `d7ff804` 13/15 (the 15 whose `how` reads lines; the 15 decided by running
code name none). The two misses: r006 (`Policy._mechanical`'s 17-line docstring takes the head
budget, the window around `deny_commands` stops at 2010 of 2001-2015) and r021 (no code token in
"the deleted side of a rename").

Found while building this, fixed by hand in their own commits: `PathRepoView.git` ran the operator's
textconv / external diff and took `--contents`, `-c`, `-O` (CC-2's judging fix had closed this only
in the target's view); `git blame` runs a textconv driver too and takes `--no-textconv` (260's
As built said it had no switch) — both views; `extract_anchors` checked each span against all taken
spans, quadratic (`"a.py:" × 4000` took 2 s; the edge-case test's 1 s bound flaked under load).
