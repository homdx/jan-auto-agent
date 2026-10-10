# CC-2 — target: a pinned worktree and a cached collect model

**Status:** landed
**Severity:** HIGH (without a pinned tree there is nothing to read evidence from)
**File:** `tools/claimcheck/target.py`
**Symbol:** `RefHint`, `parse_report_ref`, `Target`, `Target.open`, `Target.view`, `Target.collect`, `TargetError`
**Round:** —
**Size:** M
**Depends on:** CC-0
**Also touches:** `tests/test_claimcheck_target.py`, `tests/fixtures/claimcheck/ref_hints.json`

Runs in parallel with CC-1: the two share no file. `Target.view()` must satisfy the
`RepoView` protocol that CC-1 defines in `tools/claimcheck/model.py`; until CC-1 has landed,
the test defines the protocol's six method names locally (`exists`, `read`, `files`,
`rev_parse`, `ticket_file`, `git`) and checks the object has them.

---

## Why

A report is written about a commit (`Code: origin/kc @ afa53f1`, `branch ctx-overflow-fix
(HEAD 7b4e5f9)`). The operator's checkout is on another branch, dirty, or both. Judging
the claims against the wrong tree is the quietest way to produce a confident wrong verdict,
and moving the operator's tree to the right one is not acceptable (principle 3 of the epic;
`tools/arena/gitref.py` states the same rule for the same reason).

`Target` gives a read-only tree at an exact sha, anywhere, with the collect model built for
it once.

## What it does

### `parse_report_ref(text: str) -> list[RefHint]`

`RefHint(ref: str, sha: Optional[str], source: str)` from the report text. Recognised, in
any combination, case-insensitive on the cue words:

* `Code: origin/kc @ afa53f1 (331 commits ahead of main)` → `ref="origin/kc"`, `sha="afa53f1"`
* ``branch `kc` at 9f99300`` → `ref="kc"`, `sha="9f99300"`
* `ctx-overflow-fix (HEAD 7b4e5f9)` and `HEAD 7b4e5f9` → `ref="HEAD"`, `sha="7b4e5f9"`
* a bare `` `7b4e5f9` `` after "commit" → `ref=None`, `sha="7b4e5f9"`

Returns every hint in order of appearance, deduplicated; the caller picks. Never raises.

### `Target.open(repo, ref, *, scratch, fetch=False, expect_sha=None) -> Target`

* `repo`: a local path to a git repository **or** a URL (`https://`, `ssh`, `file://`).
* A local path is read through `git worktree add --detach` (the operator's `HEAD`, index
  and working tree are not written). A URL is cloned once into
  `<scratch>/claimcheck/clones/<hash-of-url>/` (`--no-checkout`, `--filter=blob:none`),
  then treated like a local repository.
* `fetch=True` runs `git fetch <remote>` (all branches and tags) and nothing else — no
  `pull`, no merge, no reset. This is "pull from the cloud" for a tree nobody edits.
* `ref` is resolved to a full sha **once**, at open. `expect_sha` (a hint's sha) is
  compared by prefix: a mismatch is not a refusal; it adds a line to `target.warnings`
  ("the report says `afa53f1`, `origin/kc` is `9f99300`") and the run goes on at the ref's
  sha, so every verdict names the sha it was made at. When `ref` is itself a sha, it wins.
* The tree lives at `<scratch>/claimcheck/<repo-id>/<sha>/tree`; it is reused when it is
  already there and its `HEAD` is that sha, rebuilt when it is not. An atomic
  `mkdir`-based lock makes two concurrent opens of the same sha safe (the second waits,
  then reuses).
* `TargetError` (message: one line, the git command and its first stderr line) when: the
  repository cannot be read, the ref does not resolve, the sha is not in the object
  database (shallow clone, forgotten fetch), the clone fails. The message says whether
  `fetch=True` would have helped.

### `Target.view() -> RepoView`

An object with `exists`, `read`, `files`, `rev_parse`, `ticket_file` and a read-only
`git(*args)` pinned to the tree. It refuses (raises `TargetError`) any `git` subcommand not
in `{show, diff, log, cat-file, rev-parse, ls-tree, ls-files, grep, blame, merge-base,
rev-list}` and any option that writes (`--output`, `-c core.*`).

### `Target.collect(config=None) -> CollectModel`

Builds `<tree>/.collect/` through `tools.collect.cli.action_collect(root, config=config)`
and reads it back with `tools.collect.loader.load(root, config)`. Cached **by sha**: a
second call finds a fresh model and does not rebuild (the loader's own freshness check is
the test). A failure of collect (not a Python repository, a parse error, a timeout of 300 s)
does **not** fail the target: it returns the loader's *absent* model and appends a warning.
Evidence from source and git does not need collect (EPIC-CC §4.5).

### `Target.close()`

Removes the worktree it created (never one it reused from a previous run unless asked,
`close(remove=True)`), via `git worktree remove --force` and `prune`. `__enter__`/`__exit__`
call it.

## Tests (`tests/test_claimcheck_target.py`)

All on temporary git repositories with fixed identity, `GIT_CONFIG_GLOBAL=/dev/null`; a
"remote" is another temporary repository reached by `file://`.

| Test | What it pins |
|---|---|
| `test_parse_report_ref_table` | `ref_hints.json`: 20 report lines → exact `RefHint`s |
| `test_open_at_sha_gives_that_tree` | file content at the sha, not at the operator's HEAD |
| `test_operators_checkout_is_untouched` | `git status --porcelain`, `HEAD`, the index file's bytes **and** mtime equal before and after open, collect, view, close — with the operator's tree dirty |
| `test_branch_ref_is_pinned_to_a_sha` | a branch that moves after open does not move the target |
| `test_report_sha_mismatch_is_a_warning` | `expect_sha` differing → `warnings`, not an error; a matching prefix → none |
| `test_unknown_ref_is_a_one_line_error` | message holds the command and stderr's first line |
| `test_missing_object_says_fetch_would_help` | a shallow clone lacking the sha |
| `test_fetch_brings_new_commits_and_nothing_else` | after `fetch=True` the new commit resolves; the operator's branches are unchanged; no merge commit anywhere |
| `test_url_is_cloned_once_and_reused` | second open of the same URL does not clone again (counted) |
| `test_second_open_reuses_the_tree` | no new `worktree add`; same path |
| `test_concurrent_opens_of_one_sha` | two threads; one worktree; both succeed |
| `test_view_methods` | `exists`, `read`, `files` (sorted, no `.git`), `rev_parse`, `ticket_file` |
| `test_view_refuses_writing_git` | `checkout`, `reset`, `commit`, `-c core.editor=…`, `--output=` → `TargetError` |
| `test_collect_is_cached_by_sha` | `action_collect` monkeypatched and counted: called once over two `collect()` calls |
| `test_collect_failure_is_a_warning` | the stub raises → absent model, warning, no exception |
| `test_close_removes_what_it_made` | worktree gone, `git worktree list` clean; a reused tree stays unless `remove=True` |
| `test_no_network_without_a_url_or_fetch` | `socket` blocked; local-path open works |

## Acceptance (the operator's bench, `contest-bench/260/acceptance_260.py`)

On `contest-bench/cc` fixture: open at `base_sha` and at `head_sha` → two trees whose file
contents differ in exactly the files the head commit touched; the operator's checkout
unchanged; `collect()` on both returns a model whose `module("pipeline.py")` is not `None`.
On this repository at `real_sha`: open finishes in under 60 s (without collect) and the
tree's `HEAD` is `real_sha`.

## Edge cases to handle

A ref with a slash (`origin/kc`); a tag; an annotated tag (peel to the commit); an
abbreviated sha shorter than 7 characters (refuse, ambiguity is the caller's problem); a
repository with submodules (ignored, the tree has the gitlinks only); a path with spaces
or unicode as `scratch`; a `scratch` on a read-only filesystem (a one-line error);
`repo` equal to the operator's own checkout (the common case).

## Not in scope

Reading evidence (CC-3/4), classification (CC-1), any LLM call.

## As built

Places where the code is more exact than the text above:

* **`RefHint.source` is the matched text as written** (`origin/kc @ afa53f1`, `HEAD 7b4e5f9`); the
  `sha` is returned lower-case. A `branch X (HEAD sha)` yields the one `HEAD` hint, as listed above.
  A sha made only of letters is taken after `commit` / `@` only when it has a digit, so
  "commit defaced" is not a sha.
* **`Target.open` takes the scratch directory as absolute** (git runs in another directory) and a
  bare repository as `repo` (`--absolute-git-dir`). A ref starting with `-` and a hex string of 4–6
  characters that is not a branch or tag are refused. A branch of a URL clone is found as
  `origin/<ref>`. No hook of the operator's repository runs in the new tree (`core.hooksPath=/dev/null`).
* **A reused tree is checked, not trusted:** its `HEAD` must be the sha *and* no tracked file may
  be modified, else it is rebuilt. The lock is one `mkdir` per repository, not per sha, because
  `git worktree add` itself writes into the repository's `.git/worktrees`.
* **`fetch=True`** runs `git fetch --all --tags --no-write-fetch-head`; a repository without a
  remote makes it a no-op, and a missing sha then says "fetch=True would not help".
* **`collect()` remembers its answer on the target**, failures included: a failed build is one
  warning, not one per call. The tree's own freshness check (`loader.load`) runs first, so a
  second `Target` on a reused tree does not rebuild either.
* **`view()` hides `.git` and `.collect/`** (`files()`, and since the judging fix `exists()` and `read()` too). A worktree's `.git` is a file; it was listed
  as a repository file until `PathRepoView.files` learned to skip it
  (`tests_bugfix/test_claimcheck_pathrepoview_gitfile_260.py`). `view().git` returns the raw output
  (a blob keeps its `\r\n`) and accepts exactly the eleven subcommands listed above; `-c` alone
  stays legal (`git log -c`), `-c core.*` and `--textconv` do not.
* **`view().git` turns textconv and external diff off** for `show`, `diff` and `log`
  (`--no-textconv --no-ext-diff` are put in front): git runs a diff driver by default, the driver is
  named by the commit's `.gitattributes` and defined in the operator's config, so a plain
  `git show` ran the operator's command. `blame --contents` is refused (it reads a file outside
  the commit). `blame` still honours a textconv driver; git has no switch for it
  (`tests_bugfix/test_claimcheck_target_view_filters_260.py`).
* **`view().files()` is a walk of the tree**, so a symlink to a directory is not listed (the
  commit's own file list would list it); `.git` and `.collect/` are hidden either way.
