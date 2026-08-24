# Test layout

## The three directories

`tests/` holds every real test file and is the **single source of truth**.
`.smoke_tests/` and `.regression_tests/` contain nothing but relative
symlinks back into it, partitioned by how long each file takes:

| Directory | Contents | Wall time | When to run |
|---|---|---|---|
| `.smoke_tests/` | 190 fast files (< 2s each) | ~35s CPU, ~10s with `-n 4` | Before every commit |
| `.regression_tests/` | 34 heavy files | ~611s CPU | Before push / merge |
| `tests/` | all 224 real files | ~645s CPU | Final gate, and when in doubt |

The two tiers are **disjoint and complete**: every file in `tests/` appears
in exactly one of them. So

```bash
python3 -m pytest .smoke_tests/ .regression_tests/
```

runs the whole suite with each test executed exactly once, and

```bash
python3 -m pytest .smoke_tests/
```

is the cheap pre-commit gate.

The split is heavily skewed on purpose — measurement on 2026-08-24 showed
34 files accounting for 611s of the suite's 645s, with a single file
(`test_auto_b3.py`) at 182s. Splitting by an even file count would have
produced two slow tiers; splitting by measured cost produces one genuinely
fast one.

## Regenerating the symlinks

Never create these links by hand. Run:

```bash
python3 scripts/sync_test_tiers.py
```

It reads `tests/SLOW_TESTS.txt`, links everything listed there into
`.regression_tests/`, everything else into `.smoke_tests/`, and removes
links that no longer belong. It is idempotent — a second run reports
`+0 links, -0`.

To verify without touching anything (suitable for CI or a pre-commit hook):

```bash
python3 scripts/sync_test_tiers.py --check
```

Exit code is non-zero if any link is missing, dangling, mistargeted, or
present in the wrong tier.

### Why a script rather than hand-made links

Both of these had already happened in this repository before the script
existed:

1. **Absolute targets.** Two links in `.regression_tests/` pointed at
   `/home/renat/Project/opensource/github/agent-offline/qwen25/tests/...`.
   Those resolve on exactly one machine and dangle on every other clone.
   Worse, a dangling link is *silently skipped* — `RepoIngestor.walk`
   drops any path whose `stat()` raises — so the lost coverage produced no
   error anywhere. A missing test cannot fail.

2. **Uncovered files.** Roughly a third of `tests/` had no link in either
   tier, so those files only ran under an explicit `pytest tests/`.

`scripts/sync_test_tiers.py` makes both impossible: every link it writes is
relative, and the partition is derived from the manifest rather than
curated by hand. `tests/test_tier_symlinks.py` asserts the invariants
(relative targets, no dangling links, name matches target, tiers disjoint,
tiers complete, manifest has no stale entries) so a regression shows up as
a red test rather than as quiet coverage loss.

## Adding a test

1. Write the file in `tests/`.
2. Run `python3 scripts/sync_test_tiers.py`. A new file defaults to the
   smoke tier.
3. If it turns out to be slow, add its filename to `tests/SLOW_TESTS.txt`
   and re-run the script.

Commit the new file, the symlink, and any manifest change together.

## Re-measuring the tiers

The 2s-per-file threshold is a judgement call, not a law. To re-measure:

```bash
python3 -m pytest tests/ -q -n 4 --durations=0 > /tmp/dur.log
```

Sum the per-test durations per file, move anything that crosses the
threshold in either direction, then re-run `sync_test_tiers.py`. Worth
redoing after any change that adds or removes substantial LLM-stub or
repo-scanning work, since those dominate the heavy tier.

## Notes

- `pytest.ini` sets `-n auto --dist=loadgroup`. The `loadgroup` part is
  load-bearing: tests marked `@pytest.mark.xdist_group("port_bound_http_servers")`
  bind real `127.0.0.1` ports, and plain `--dist=load` scatters them across
  workers, which races for OS-level TCP ports. Don't drop it.
- `_pass_a_stub.py` and `fixtures/` are linked into **both** tiers; tests
  in each import them.
- Keep stray files out of the repository root. A stray copy of a test file
  sitting at the top level was once misread as a broken symlink for an
  entire debugging session — `tests/test_collect_ast_symbols.py` scans the
  whole tree and will report it as a parse error with a bare filename.
