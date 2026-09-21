# Repository Guidelines

`jan-auto-agent` (`main.py`) is the autonomous improvement pipeline: `--collect`
builds a structural model of a target repo, `--auto` plans (Architect → Gate 1 →
backlog) and executes tasks with a Coder, gates and validators. `--dry-run` and
`--validate-plan` exercise the same planning path without executing.

## Project Structure & Module Organization

- `main.py` — CLI entry point (`--collect`, `--auto`, `--dry-run`, `--validate-plan`).
- `tools/auto/` — pipeline stages: `architect.py`, `gate1_filter.py`, `controller.py`, `executor.py`, `inner_loop.py`, `coder.py`, `git_manager.py`.
- `tools/collect/` — repo scanner, symbol/AST facts, contract and test maps, renderer.
- `tools/` (root) — shared services: `llm_stream.py`, `prompt_store.py`, `skills/`, `validator_agent.py`.
- `tests/` — sole source of truth for feature tests; `.smoke_tests/` and `.regression_tests/` are symlink tiers into it. `tests_bugfix/` pins historical bug fixes.
- `scripts/` — repo tooling (`sync_test_tiers.py`, `collect_metrics.py`, `run_flows.sh`). `epic-tasks/`, `docs/`, `examples/` — specs and worked examples.

## Build, Test, and Development Commands

- `pip install -r requirements.txt` — optional Java parsing backend.
- `python3 -m pytest .smoke_tests/` — fast pre-commit gate.
- `python3 -m pytest .smoke_tests/ .regression_tests/` — full suite, each file once.
- `python3 -m pytest tests_bugfix -n 4 -q` — regression guards for fixed bugs.
- `python3 scripts/sync_test_tiers.py` — regenerate tier symlinks (never by hand); `--check` verifies them.
- `git config core.hooksPath githooks` — enable the pre-commit tier/stray-file hook.
- `python3 -m tools.contest run --ticket NN` — the round; patches in `contest-out/NN/`.

## Coding Style & Naming Conventions

Python 3.10+, 4-space indent, `snake_case` functions/modules, `PascalCase` classes,
type hints on public functions. Keep module-scope constants upper snake case. Match
the heavy explanatory comment style of the existing code; no new formatter is configured.

## Testing Guidelines

Use pytest with `pytest.ini` defaults (`-n auto --dist=loadgroup`); tests binding local
HTTP ports must carry `@pytest.mark.xdist_group("port_bound_http_servers")`. Name files
`tests/test_<area>_<feature>.py` and add a one-line docstring. New feature tests go in
`tests/` and must be tiered via `scripts/sync_test_tiers.py`.

## Commit & Pull Request Guidelines

Commit subjects are terse and prefixed by ticket (`GATE1-LEARN-2: …`, `RUN-3: …`),
describing the behavior change, not the file touched. Keep one concern per commit.
Pull requests should state the ticket, the behavior before/after, the exact test
command run, and link the relevant `epic-tasks/*.md` entry; include logs or
screenshots for run-output changes.
