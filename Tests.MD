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

## Pre-commit hook

### Installing it

The repository ships a hook that runs the `--check` above before every
commit. **It is not active until you enable it** — git does not run hooks
from a version-controlled directory by default. One command, once per clone:

```bash
git config core.hooksPath githooks
```

That is the entire installation. Verify it took:

```bash
git config --get core.hooksPath     # -> githooks
bash githooks/pre-commit && echo OK  # -> OK on a clean tree
```

`core.hooksPath` is used instead of copying into `.git/hooks/` so the hook is
**version-controlled**: it arrives with `git pull`, changes to it show up in
review, and a fresh clone needs only the one command above rather than a
copy step somebody will forget. It requires git 2.9 or newer.

To bypass it for a single commit:

```bash
git commit --no-verify
```

To turn it off entirely:

```bash
git config --unset core.hooksPath
```

### Why it exists

The hook has already caught two real losses that no test run could have
found, because both make tests disappear rather than fail:

- `.smoke_tests/fixtures` went missing during a commit and was noticed only
  after it had been pushed;
- `.smoke_tests/test_hello_world_runbook.py` went missing the same way, one
  commit later.

Both were `git add` not picking up a new symlink. A missing test cannot fail,
so nothing else would have reported it.

That single command is the whole installation — the hook lives in
`githooks/pre-commit`, which is version-controlled, so it updates with
`git pull` and needs no re-copying. (`core.hooksPath` requires git 2.9+.)

It takes ~40ms and is read-only: it never rewrites the working tree or
stages anything for you. It checks two things:

1. **Tier integrity** — via `sync_test_tiers.py --check`. Catches a new
   test with no symlink, a link left behind by a deleted test, and links
   that are absolute, dangling, or mistargeted.
2. **Stray Python files in the repository root** — but only files *added*
   by the commit in question, so a pre-existing root script never blocks
   an unrelated commit. A genuinely new top-level script goes in the
   `ALLOWED_ROOT_PY` list inside the hook.

Both checks exist because these failures are invisible to the test suite:
a missing test cannot fail, and a dangling symlink is skipped silently.
`.smoke_tests/fixtures` went missing exactly this way and was only caught
after it had been pushed.

To bypass for one commit:

```bash
git commit --no-verify
```

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
