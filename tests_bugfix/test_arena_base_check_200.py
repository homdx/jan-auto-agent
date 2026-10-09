"""tests_bugfix/test_arena_base_check_200.py — the summary line and the shared cache temp file.

`_summary` took the last non-empty line of stdout *and* stderr, so a step that prints a warning on
stderr after the pytest summary showed the warning instead of `N passed in …s`; and `write_cache`
wrote one fixed `base-check.json.tmp` for every process, so two `base check` runs at once could
interleave their writes into a corrupt cache or lose the file between `write_text` and `os.replace`
and report a lost cache.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

from tools.arena import basecheck

THREADS, ROUNDS = 8, 80


def _big_cache() -> dict:
    """One entry big enough that a `write_text` overlaps its neighbours' rename, so a shared
    temp name is not a theoretical race."""
    return {"at": 1, "ok": True,
            "steps": [{"step": f"step_{i}", "ok": True, "seconds": 1.0, "summary": "x" * 60}
                      for i in range(600)]}


def test_a_stderr_warning_after_the_pytest_line_is_not_the_summary():
    step = basecheck.Step("tests", ["python3", "-m", "pytest", "tests"], pytest=True)
    proc = subprocess.CompletedProcess(
        step.command, 0,
        "collected 3 items\ntests/test_a.py .. \n\n3 passed in 1.20s\n",
        "pkg_resources is deprecated as an API\n")
    assert basecheck._summary(step, proc) == "3 passed in 1.20s"


def test_stderr_is_the_summary_only_when_stdout_is_empty():
    step = basecheck.Step("tiers", ["python3", "scripts/sync_test_tiers.py", "--check"])
    proc = subprocess.CompletedProcess(step.command, 1, "", "tiers out of date\n")
    assert basecheck._summary(step, proc) == "tiers out of date"


def test_a_failed_pytest_step_still_names_its_failed_tests():
    step = basecheck.Step("tests", ["python3", "-m", "pytest", "tests"], pytest=True)
    stdout = "".join(f"FAILED tests/t.py::test_{i}\n" for i in range(3))
    stdout += "3 failed in 0.01s\n"
    proc = subprocess.CompletedProcess(step.command, 1, stdout, "deprecation warning\n")
    summary = basecheck._summary(step, proc)
    assert summary.startswith("3 failed in 0.01s")
    assert "FAILED tests/t.py::test_0" in summary and "FAILED tests/t.py::test_2" in summary


def test_an_empty_step_has_an_empty_summary():
    step = basecheck.Step("clocks", ["python3", "scripts/check_test_clocks.py", "--check"])
    assert basecheck._summary(step, subprocess.CompletedProcess(step.command, 0, "", "")) == ""


def test_concurrent_writes_share_no_temp_file_and_leave_a_valid_cache(tmp_path):
    repo = tmp_path / "repo"
    entry = _big_cache()
    results, errors = [], []
    barrier = threading.Barrier(THREADS)

    def writer(n: int) -> None:
        for round_ in range(ROUNDS):
            barrier.wait()
            try:
                results.append(basecheck.write_cache(repo, {f"sha_{n}_{round_}": dict(entry)}))
            except BaseException as err:  # the cache is fail-open: a write never raises into a run
                errors.append(err)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert len(results) == THREADS * ROUNDS
    assert all(results), f"{results.count(False)} of {len(results)} writes reported a lost cache"
    assert basecheck.read_cache(repo), "the cache is empty after every write succeeded"
    assert not list((repo / ".arena").glob("*.tmp")), list((repo / ".arena").glob("*"))
    assert not (repo / ".arena" / "base-check.json.tmp").exists()


def test_a_cache_that_cannot_be_written_is_fail_open(tmp_path):
    (tmp_path / ".arena").write_text("a file, not a directory\n", encoding="utf-8")
    assert basecheck.write_cache(tmp_path, {"abc": {}}) is False
    assert basecheck.read_cache(tmp_path) == {}
