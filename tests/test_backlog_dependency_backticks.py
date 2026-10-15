"""tests/test_backlog_dependency_backticks.py — HARDEN-5 E: a file name in
backticks is not a dependency.

The "a plain word is a dependency only as code" fix excluded ``run.log`` and
``run (when enabled)`` written in prose, but its first alternative — a backticked
span containing the word — accepted **any** backticked span. File names are
normally written in backticks, so an unrelated task became a dependent, and the
controller holds a task whose dependency is not done: one failed task blocked an
unrelated one. The extension and path exclusion now apply inside a backticked
span too, so `main`, `run()`, `Store.load` count and `main.py`, `run.log`,
`scripts/run.sh`, `docs/main.md`, `parse.json`, `config.ini` do not.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.auto.backlog_prioritiser import BacklogPrioritiser as BP  # noqa: E402


def _check(symbol: str, instruction: str) -> bool:
    """A cites *symbol* in a.py; B's instruction is *instruction*."""
    a = NS(target_files=["a.py"], cited_location=NS(symbol=symbol))
    b = NS(target_files=["b.py"], instruction=instruction)
    return BP._cross_file_symbol_ref(a, b)


# ── The ticket's table, row by row ───────────────────────────────────────────

@pytest.mark.parametrize("symbol, instruction, depends", [
    ("main", "Update `main.py` to log more.", False),
    ("run", "Rotate `run.log` weekly.", False),
    ("run", "Edit `scripts/run.sh`.", False),
    ("parse", "Fix `parse.json` schema.", False),
    ("config", "Rename `config.ini`.", False),
    ("main", "Call `main` first.", True),
    ("run", "Call `run()` first.", True),
    ("run", "the loop will run (when enabled)", False),
])
def test_a_backticked_file_name_is_not_a_dependency(symbol, instruction, depends):
    assert _check(symbol, instruction) is depends


# ── The file-name exclusion applies inside a span too ────────────────────────

@pytest.mark.parametrize("symbol, instruction", [
    ("main", "Update `docs/main.md` for the manual."),
    ("run", "The output goes to `run.out` and `run.err`."),
    ("run", "Ship `run.yaml` with the job."),
    ("run", "Tune `run.toml` and `run.cfg`."),
    ("parse", "The fixture is `parse.csv` and `parse.ini`."),
    ("store", "Read `store.txt` before starting."),
    ("log", "See `log.rst` for the format."),
])
def test_every_listed_extension_inside_a_span_is_a_file(symbol, instruction):
    assert _check(symbol, instruction) is False


@pytest.mark.parametrize("symbol, instruction", [
    ("main", "Call `main` first, then `main` again."),
    ("run", "Call `run()` once the queue is ready."),
    ("Store", "Subclass `Store.compact` for it."),
    ("load_rows", "Reuse `load_rows` for the import."),
    ("run", "Use `worker.run` instead of the inline loop."),
    ("config", "Read `config` from the environment."),
])
def test_a_symbol_written_as_code_inside_a_span_still_depends(symbol, instruction):
    assert _check(symbol, instruction) is True


def test_a_span_with_a_file_name_and_a_bare_symbol_depends():
    # `run.py` alone would not, but the same span also says `run` on its own.
    assert _check("run", "Edit `run.py` and call `run` from there.") is True


# ── The prose rules the fix already made are untouched ────────────────────────

@pytest.mark.parametrize("symbol, instruction, dep", [
    ("run", "Make the retry loop run (when enabled) only once.", False),
    ("run", "Write the results to the run.log file.", False),
    ("run", "Call run() after setup.", True),
    ("run", "Then self.run is replaced.", True),
    ("run", "Make sure the loop will run only when enabled.", False),
    ("parse", "Fail clearly when the file cannot parse its input.", False),
    ("run", "Call `run` once the queue is ready.", True),
    ("run", "Call run() once the queue is ready.", True),
    ("run", "Use worker.run instead of the inline loop.", True),
    ("load_rows", "Reuse load_rows for the import.", True),
    ("Store", "The Store must stay small.", False),
    ("Store", "Subclass Store.compact for it.", True),
])
def test_the_existing_plain_word_rules_are_unchanged(symbol, instruction, dep):
    assert _check(symbol, instruction) is dep


def test_two_tasks_on_the_same_file_are_never_a_symbol_reference():
    a = NS(target_files=["a.py"], cited_location=NS(symbol="main"))
    b = NS(target_files=["a.py"], instruction="Call `main` first.")
    assert BP._cross_file_symbol_ref(a, b) is False


def test_an_english_word_without_a_backtick_and_without_code_is_not_it():
    assert _check("run", "the loop will run only when enabled") is False
    assert _check("run", "run (when enabled) is prose") is False
