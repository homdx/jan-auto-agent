"""contest-bench/129 — score round 129 (tests for tools/auto/delta_validator.py).

A test-only round has no behaviour to accept, so the score is how well the
entry's own tests/test_auto_delta_validator.py pins the module:
  * green   — the file passes on the base module (3 runs, -n 8);
  * cov     — branch coverage of tools/auto/delta_validator.py by that file alone;
  * killed  — hand-written mutants of the module the file turns red.
Usage: python3 contest-bench/129/judge_129.py [entry ...]
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BASE = "1e31867cc2e3"
OUT = REPO / "contest-out" / "129"
MOD = "tools/auto/delta_validator.py"
TEST = "tests/test_auto_delta_validator.py"

# (name, old, new) — each old must occur exactly once in the base module.
MUTANTS = [
    ("isdir_skipped", "if not base.is_dir():", "if False:"),
    ("isdir_exists", "if not base.is_dir():", "if not base.exists():"),
    ("isdir_reason", 'reason="base_dir unavailable"', 'reason="x"'),
    ("relpath_skipped", "if not rel_path:", "if False:"),
    ("relpath_reason", 'reason="no rel_path given"', 'reason="x"'),
    ("prior_none_skipped", "if prior is None:", "if False:"),
    ("prior_none_reason", 'reason="no prior version to compare"', 'reason="x"'),
    ("no_strip", "if text.strip() != prior.strip():", "if text != prior:"),
    ("differs_inverted", "if text.strip() != prior.strip():", "if text.strip() == prior.strip():"),
    ("differs_reason", 'reason="content differs from HEAD"', 'reason="x"'),
    ("unchanged_approved", "approved=False, rel_path=rel_path,", "approved=True, rel_path=rel_path,"),
    ("unchanged_relpath", "approved=False, rel_path=rel_path,", "approved=False, rel_path=\"\","),
    ("unchanged_reason", 'reason="unchanged from the version already committed at HEAD"', 'reason="x"'),
    ("error_rejects", 'return DeltaVerdict(approved=True, reason=f"error: {exc}")',
     'return DeltaVerdict(approved=False, reason=f"error: {exc}")'),
    ("error_reason", 'reason=f"error: {exc}"', 'reason="error"'),
    ("error_narrow", "except Exception as exc:  # noqa", "except ValueError as exc:  # noqa"),
    ("head_no_dot", 'f"HEAD:./{rel_path}"', 'f"HEAD:{rel_path}"'),
    ("git_timeout", "timeout=10,", "timeout=100,"),
    ("read_no_oserror", "except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError)",
     "except (subprocess.TimeoutExpired, UnicodeDecodeError)"),
    ("read_no_timeout", "except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError)",
     "except (OSError, UnicodeDecodeError)"),
    ("read_no_unicode", "except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError)",
     "except (OSError, subprocess.TimeoutExpired)"),
    ("returncode_neg", "if result.returncode != 0:", "if result.returncode < 0:"),
    ("stdout_changed", "return result.stdout\n", "return result.stdout + 'x'\n"),
    ("feedback_approved", 'if self.approved:\n            return ""', 'if False:\n            return ""'),
    ("feedback_no_ticks", 'f"`{self.rel_path}` is unchanged', 'f"{self.rel_path} is unchanged'),
    ("make_inverted", 'if not config.getboolean("validator_agent", "delta_check", fallback=True):',
     'if config.getboolean("validator_agent", "delta_check", fallback=True):'),
    ("make_fallback_off", '"delta_check", fallback=True', '"delta_check", fallback=False'),
    ("make_badbool_disables", 'logger.warning("DeltaValidator: delta_check is not a boolean — enabling.")',
     'return None'),
    ("make_maxrev_fallback", '"max_delta_revisions", fallback=1', '"max_delta_revisions", fallback=2'),
    ("make_badint", "        max_rev = 1\n", "        max_rev = 0\n"),
    ("make_maxrev_dropped", "DeltaValidator(max_delta_revisions=max_rev)", "DeltaValidator()"),
    ("init_default", "max_delta_revisions: int = 1", "max_delta_revisions: int = 2"),
    ("make_task_mode", '    try:\n        if not config.getboolean(',
     '    if task_mode != "creative":\n        return None\n    try:\n        if not config.getboolean('),
]


def sh(cmd, cwd, timeout=600):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def pytest(wt, extra=(), n="8"):
    r = sh([sys.executable, "-m", "pytest", TEST, "-q", "-n", n, "-p", "no:cacheprovider",
            "-x", *extra], wt)
    return r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[-1:]


def judge(entry: str, tmp: Path) -> dict:
    wt = tmp / entry
    sh(["git", "worktree", "add", "--detach", str(wt), BASE], REPO)
    try:
        ap = sh(["git", "apply", str(OUT / f"{entry}.patch")], wt)
        if ap.returncode:
            return {"entry": entry, "error": "apply: " + ap.stderr.strip()[:200]}
        prod = sh(["git", "diff", "--stat", BASE, "--", MOD], wt).stdout.strip()
        tiers = sh([sys.executable, "scripts/sync_test_tiers.py", "--check"], wt).returncode == 0
        green = [pytest(wt)[0] for _ in range(3)]
        cov_json = wt / "cov.json"
        sh([sys.executable, "-m", "pytest", TEST, "-q", "-n", "0", "-p", "no:cacheprovider",
            "--cov=tools.auto.delta_validator", "--cov-branch",
            f"--cov-report=json:{cov_json}"], wt)
        cov = None
        if cov_json.exists():
            files = json.loads(cov_json.read_text())["files"]
            cov = round(next(iter(files.values()))["summary"]["percent_covered"], 1)
        src = (wt / MOD).read_text()
        killed, survived = [], []
        for name, old, new in MUTANTS:
            assert src.count(old) == 1, (entry, name)
            (wt / MOD).write_text(src.replace(old, new))
            ok, _ = pytest(wt, n="4")
            (killed if not ok else survived).append(name)
        (wt / MOD).write_text(src)
        return {"entry": entry, "green": sum(green), "tiers": tiers, "prod_changed": bool(prod),
                "cov": cov, "killed": len(killed), "of": len(MUTANTS), "survived": survived}
    finally:
        sh(["git", "worktree", "remove", "--force", str(wt)], REPO)


def main() -> None:
    entries = sys.argv[1:] or sorted(p.stem for p in OUT.glob("*.patch"))
    with tempfile.TemporaryDirectory(prefix="j129-") as tmp:
        rows = [judge(e, Path(tmp)) for e in entries]
    rows.sort(key=lambda r: (-r.get("green", 0), -r.get("killed", 0), -(r.get("cov") or 0)))
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
