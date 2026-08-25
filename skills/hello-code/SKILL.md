---
name: hello-code
description: Harden a tiny Python script — add argument handling, a docstring, and pytest coverage — without changing what it prints.
---

# Small-Script Hardening

## Overview

You are working on a single-file Python script. The bar is production
hygiene, not features. Every change must leave the script's observable
output byte-identical unless the task explicitly says otherwise.

## What to propose

Prefer, in this order:

1. A module-level and function-level docstring stating what the script does.
2. A `pytest` test that asserts the printed output, using `capsys`.
3. Explicit `sys.exit` return codes so the script is usable in a pipeline.
4. Type hints on every function signature.

Do not propose: packaging, CI configuration, logging frameworks,
dependency additions, or renaming the entry point.

## Acceptance criteria

Every task must state a check that can be run non-interactively. Prefer
`python -m pytest -q` or `python main.py` with an expected stdout.

## Constraints

- Standard library only. No new dependencies.
- One concern per task. A docstring task does not also add tests.
- Never change the text the script prints.
