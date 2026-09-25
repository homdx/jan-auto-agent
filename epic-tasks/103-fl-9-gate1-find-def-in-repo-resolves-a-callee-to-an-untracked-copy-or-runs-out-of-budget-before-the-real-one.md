# FL-9 — `_find_def_in_repo` resolves a callee to whatever copy `rglob` meets first — an untracked round worktree, a pinned fixture — or to nothing once the 4000-file cap runs out in them

**Status:** queued — found 2026-09-25 while judging round 88 (FL-5); kept out of FL-5, which forbids any change to Gate 1's behaviour.
**Severity:** MEDIUM. Stage B is shown a foreign or stale definition as "one call-hop" context, or none at all. On this repo today, the real definition sits past the walk's cap. No test sees it, because the corpus mock ignores downstream context.
**File:** `tools/auto/gate1_grounding.py`
**Symbol:** `_find_def_in_repo` (:519, `max_files=4000`), called by `callee_context` (:554)
**Round:** 103
**Size:** S
**Source:** Evidence from `/mnt-fs/auto-code` on `kc` (`5a19fc7` + FL-5), 2026-09-25:
- In `tests/test_gate1_corpus_precision.py`, AUTO-T11 (`tools/collect/bughunt_filter.py::suppress`) gets its "Downstream context … `is_safe(...)`" from `rounds/89-mimo-v2-5/tools/collect/loader.py`. That is another agent's untracked round-89 worktree inside the checkout.
- `Path('.').rglob('*.py')` walks 10 803 files. 9 972 of them are under `rounds/`. The real `tools/collect/loader.py` comes at position **10 798**, far past `max_files=4000`. Without the copy in `rounds/`, the answer would be "not found".
- In round-88 candidates that pin copies under `tests/fixtures/…` (sensenova-6-7-var2, sensenova-6-8-var2), the live run cites `tests/fixtures/gate1_corpus_pinned/tools/collect/loader.py` instead of `tools/collect/loader.py`.

**Depends on:** nothing. FL-5 (88) only documents the corpus's dependence on the live tree; this ticket changes what the live tree resolves to.
**Also touches:** `tests/test_gate1_grounding*.py`, or a new `tests/test_gate1_find_def.py`.

---

## What happens today

`_find_def_in_repo(name, base_dir)` runs `base_dir.rglob("*.py")` and returns the first file that matches `^\s*def name\s*\(`.
- It skips only `/.agent/` and `/node_modules/` (FIX-2 #11 moved that skip ahead of the counter).
- It ignores git: whether a file is tracked, and `.gitignore`.
- `rglob` order is directory-listing order, which the filesystem decides, so a different machine can give a different answer.
- It does not prefer the cited file, or its package, over the rest of the tree.

So any checkout that holds nested worktrees (`rounds/`), vendored copies, `build/` or `dist/` output, a virtualenv inside the repo, or copied fixtures can:
- hand Stage B a definition from a different revision or another agent's work;
- burn the whole 4000-file budget there and report "not found" for a definition that is in the repo.

This is the same failure as FIX-2 #11: budget spent on files the walk should never have read. There, `.agent/` and `node_modules/` were the culprits.

## What must change (sketch; the round decides)

1. When the cited file itself defines `name`, use it.
2. Next, look in the cited file's package (its directory and parents), before the rest of the repo.
3. When `base_dir` is a git work tree, walk only tracked files (`git ls-files '*.py'`). Nested worktrees, ignored and untracked output then drop out, and the cap counts only real files. With no git, or git failing, fall back to today's walk, fail-open.
4. Make the order deterministic (sorted), so two runs and two machines agree.
5. `existence_validator._repo_has_tests` also uses `rglob("*.py")`, but only asks whether any test file exists. It is out of scope unless the round finds a real misfire.

## Acceptance

- [ ] A tmp git repo has `tools/x.py::f` tracked, and an untracked `rounds/r1/tools/x.py::f` with a different body. `callee_context` cites `tools/x.py`.
- [ ] The same with a copy under a tracked `tests/fixtures/copy/tools/x.py`: the definition in the cited file's own package wins.
- [ ] Budget: `max_files` untracked files that sort ahead of the real definition do not hide it.
- [ ] Two runs give the same file.
- [ ] Fail-open: a directory that is not a git repo, or git missing or failing, falls back to the walk, and nothing raises.
- [ ] `tests/test_gate1_corpus_precision.py`: rejected set unchanged (AUTO-T1/T2/T3/T8/T9/T11), and AUTO-T11's downstream context cites `tools/collect/loader.py`.
- [ ] `pytest tests -n 4` then `pytest tests_bugfix -n 4`, sequentially, both green; `python3 scripts/sync_test_tiers.py --check` clean.

## Out of scope

- The notes' wording, `intentional_design_note`'s cue list, Stage B's prompt.
- The corpus and its entries (FL-5).
- `context_broker.py` walks (`rglob("*")`). A separate audit if they show the same symptom.
- `CollectBridge._shrink` stays byte-identical.

## Self-check before `append_task.py`

- [ ] `python3 --version`; no backslash and no nested same-quote inside an f-string expression.
- [ ] Exactly one commit on top of the base, only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/auto/gate1_grounding.py` and its tests. Never `epic-tasks/`.
- [ ] The new tests fail without the change.
- [ ] Both test roots green, sequentially.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the **sha** of the one commit; hand in `git format-patch <base>..HEAD`.

## Ground rules

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
