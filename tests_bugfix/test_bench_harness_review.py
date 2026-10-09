"""tests_bugfix/test_bench_harness_review.py — contest-bench/harness/static_checks.py and validate_inputs.py.

Neither script had a test; both grade or vet round submissions.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parent.parent / "contest-bench" / "harness"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


# ── static_checks.py: "key documented in agents.ini [section], with its default" ──
# Bug: the key was searched as a substring and the default as `key = default\b` with
# nothing on the left, so `retries` was "documented" by `max_retries = 3`, and a default
# of `0` was met by `timeout = 0.5` (`\b` sits between the 0 and the dot).

def _static(tmp_path: Path, ini: str, keys: list) -> dict:
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "main")
    (wt / "agents.ini").write_text(ini, encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "base")
    base = _git(wt, "rev-parse", "HEAD")
    (wt / "f.txt").write_text("x\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "entrant")
    scen = tmp_path / "scen.py"
    scen.write_text(f"CONFIG_KEYS = {keys!r}\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(HARNESS / "static_checks.py"), str(wt), "--base-ref", base],
                          capture_output=True, text=True, env={**ENV, "STATIC_SCENARIOS": str(scen)})
    line = next(l for l in proc.stdout.splitlines() if l.startswith("@@RESULT@@"))
    return {c["name"]: c["ok"] for c in json.loads(line[len("@@RESULT@@"):])["checks"]
            if "agents.ini" in c["name"]}


def test_a_key_that_is_only_the_tail_of_another_key_is_not_documented(tmp_path):
    ini = "[agents]\nmax_retries = 3\n[other]\nretries = 9\n"
    result = _static(tmp_path, ini, [("agents", "retries", 3)])
    assert result["S.retries documented in agents.ini [agents]"] is False
    assert result["S.retries default 3 in agents.ini"] is False


def test_a_default_of_zero_is_not_met_by_a_value_of_one_half(tmp_path):
    ini = "[agents]\ntimeout = 0.5\n"
    result = _static(tmp_path, ini, [("agents", "timeout", 0)])
    assert result["S.timeout documented in agents.ini [agents]"] is True
    assert result["S.timeout default 0 in agents.ini"] is False


def test_the_documented_key_and_its_default_still_pass(tmp_path):
    ini = "[agents]\nmax_retries = 3\nretries = 3   ; how often\ntimeout=0\n  indented = 1\n"
    result = _static(tmp_path, ini, [("agents", "max_retries", 3), ("agents", "retries", 3),
                                     ("agents", "timeout", 0), ("agents", "indented", 1)])
    assert all(result.values()), result
    assert len(result) == 8


def test_a_wrong_default_or_a_missing_key_still_fails(tmp_path):
    ini = "[agents]\nretries = 30\n"
    result = _static(tmp_path, ini, [("agents", "retries", 3), ("agents", "absent", 1)])
    assert result["S.retries documented in agents.ini [agents]"] is True
    assert result["S.retries default 3 in agents.ini"] is False
    assert result["S.absent documented in agents.ini [agents]"] is False



def test_a_commented_example_and_a_prose_mention_still_document_a_key(tmp_path):
    """agents.ini documents keys in comments (`#base_url = ...`); that stays accepted."""
    ini = "[agents]\n#retries = 3\n# idle_wait: seconds to wait before giving up\n;pace = 2   ; per turn\n"
    result = _static(tmp_path, ini, [("agents", "retries", 3), ("agents", "idle_wait", 5), ("agents", "pace", 2)])
    assert result["S.retries documented in agents.ini [agents]"] is True
    assert result["S.retries default 3 in agents.ini"] is True
    assert result["S.idle_wait documented in agents.ini [agents]"] is True
    assert result["S.idle_wait default 5 in agents.ini"] is False        # prose names no value
    assert result["S.pace documented in agents.ini [agents]"] is True
    assert result["S.pace default 2 in agents.ini"] is True


# ── validate_inputs.py: "the files it changed are exactly the files its patch touches" ──
# Bug: `patch_files` read only the `a/` path of `diff --git a/old b/new`, so a patch
# that renames a file was reported as differing from the worktree that applied it.

def _validate_inputs():
    spec = importlib.util.spec_from_file_location("_bugs_validate_inputs", HARNESS / "validate_inputs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RENAME = ("diff --git a/old_name.py b/new_name.py\nsimilarity index 100%\n"
          "rename from old_name.py\nrename to new_name.py\n")
EDIT = "diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


def test_patch_files_names_both_sides_of_a_rename():
    assert _validate_inputs().patch_files(RENAME) == {"old_name.py", "new_name.py"}


def test_patch_files_of_edits_adds_and_deletes_is_unchanged():
    patch = EDIT + "diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n"
    assert _validate_inputs().patch_files(patch) == {"x.py", "n.py"}


def test_the_files_a_worktree_changed_match_the_patch_of_a_rename(tmp_path):
    """End to end through the script: a worktree that applied a rename is not reported."""
    repo = tmp_path / "repo"
    (repo / "inputs").mkdir(parents=True)
    (repo / "wt" / "a").mkdir(parents=True)
    wt = repo / "wt" / "a"
    _git(wt, "init", "-q", "-b", "main")
    (wt / "old_name.py").write_text("print(1)\nprint(2)\nprint(3)\nprint(4)\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "base")
    base = _git(wt, "rev-parse", "HEAD")
    _git(wt, "mv", "old_name.py", "new_name.py")
    _git(wt, "commit", "-q", "-m", "rename")
    patch = repo / "inputs" / "a.patch"
    patch.write_text(subprocess.run(["git", "diff", "--no-ext-diff", f"{base}..HEAD"], cwd=wt,
                                    capture_output=True, text=True, env=ENV).stdout, encoding="utf-8")
    (repo / "entrants.json").write_text(json.dumps(
        {"base": base, "entrants": {"a": {"source": "inputs/a.patch"}}}), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(HARNESS / "validate_inputs.py"), str(repo / "entrants.json"),
                           "--inputs", str(repo / "inputs"), "--wt", str(repo / "wt"), "--repo", str(repo)],
                          capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all inputs accounted for" in proc.stdout
