"""run_all_kc4.py --wt <dir> --out results.json [names ...] [-- scenarios ...]

Runs every (entrant, scenario) pair in a fresh `python3` subprocess (one at a
time — matches contest-bench's rule of never running entries in parallel),
merges into results.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

SCENARIOS = [
    "fresh_round",
    "rerun_after_dirty_crash",
    "foreign_folder_refused",
    "untracked_epic_tasks_refused",
    "unresolvable_base",
    "idempotent_reset_worktree",
    "attach_clone_dirty_then_force",
    "attach_clone_missing_base",
    "remove_round_cleans_up",
    "stale_branch_recreated",
    "worktree_of_other_repo",
    "repo_checkout_never_touched",
]


def _save(path: Path, results: dict) -> None:
    """The results file replaced whole: a failed write leaves the last good file, not half a JSON."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(results, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _well_formed(result) -> bool:
    """A cell is ``{"checks": [{"ok": true|false}, …]}``; valid JSON of another shape (``"ok": "yes"`` too) is a lost cell, not a crash."""
    checks = result.get("checks") if isinstance(result, dict) else None
    return isinstance(checks, list) and all(isinstance(c, dict) and isinstance(c.get("ok"), bool) for c in checks)


def _checkpoint_shape(results) -> bool:
    """A checkpoint is good when it is a JSON object whose every value is an object."""
    return isinstance(results, dict) and all(isinstance(v, dict) for v in results.values())


def _keep_aside(out_path: Path, raw: bytes) -> None:
    """Keep a damaged checkpoint's original bytes beside it as ``<name>.corrupt``
    (then ``<name>.corrupt.1``, ``.corrupt.2``, ...) — an existing diagnostic copy
    is never overwritten or replaced.  If the copy cannot be made, name both paths
    on stderr and stop the run (the caller exits non-zero); the original file is
    left untouched so the damage is preserved for inspection."""
    name = out_path.name
    idx = 0
    while True:
        cand = out_path.with_name(name + (".corrupt" if idx == 0 else f".corrupt.{idx}"))
        idx += 1
        try:
            fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue                    # a diagnostic copy already exists: try the next name
        except OSError as err:
            print(f"cannot keep the damaged checkpoint {out_path} aside as {cand}: {err}",
                  file=sys.stderr)
            raise SystemExit(2)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
        except OSError as err:
            try:
                cand.unlink()           # do not leave half a diagnostic copy behind
            except OSError:
                pass
            print(f"cannot keep the damaged checkpoint {out_path} aside as {cand}: {err}",
                  file=sys.stderr)
            raise SystemExit(2)
        print(f"damaged checkpoint {out_path} kept aside as {cand}", file=sys.stderr)
        return


def _load_checkpoint(out_path: Path) -> dict:
    """Read the results checkpoint guarded, so a broken one never kills the batch.

    A missing file is ``{}``.  A damaged checkpoint — malformed JSON, an invalid
    text encoding, or valid JSON of another shape (a top-level list/string/null, a
    per-entrant value that is not an object) — is kept aside by its original bytes
    and replaced by ``{}``: the batch runs on and the first atomic ``_save()``
    rewrites the damaged file, exactly as a healthy run does.  The cells inside an
    entrant are not judged here (a cell of the wrong shape is already one lost
    cell).  A read failure that is not damage — a permission error, a directory —
    is an explicit error: the file is left untouched and the run stops.
    """
    if not out_path.exists():
        return {}
    try:
        raw = out_path.read_bytes()
    except OSError as err:
        print(f"cannot read the results checkpoint {out_path}: {err}", file=sys.stderr)
        raise SystemExit(2)
    try:
        results = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):   # a document of 100000 brackets overflows the parser
        _keep_aside(out_path, raw)
        return {}
    if not _checkpoint_shape(results):
        _keep_aside(out_path, raw)
        return {}
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()
    wt = Path(a.wt)
    names = a.names or sorted(p.name for p in wt.iterdir() if p.is_dir())

    out_path = Path(a.out)
    results = _load_checkpoint(out_path)

    for name in names:
        entry = results.setdefault(name, {})
        for scen in SCENARIOS:
            try:
                p = subprocess.run(
                    [sys.executable, str(HERE / "run_one_kc4.py"), str(wt / name), scen],
                    capture_output=True, text=True, timeout=120,
                )
                line = next((l for l in p.stdout.splitlines() if l.startswith("@@RESULT@@")), None)
                detail = (p.stdout + p.stderr)[-500:]
            except subprocess.TimeoutExpired:
                line, detail = None, "timed out after 120 s"
            try:
                entry[scen] = json.loads(line[len("@@RESULT@@"):]) if line is not None else None
            except ValueError:
                entry[scen] = None
                detail = f"unreadable result line: {line[:200]!r}"
            if entry[scen] is not None and not _well_formed(entry[scen]):
                detail = f"result of the wrong shape: {json.dumps(entry[scen])[:200]}"
                entry[scen] = None
            if entry[scen] is None:
                # one lost cell is a failed check, not the end of the suite
                entry[scen] = {"checks": [{"name": "subprocess", "ok": False, "detail": detail}]}
            print(f"[{name}] {scen}: "
                  f"{sum(c['ok'] for c in entry[scen]['checks'])}/{len(entry[scen]['checks'])}")
            _save(out_path, results)   # kept if a later cell dies
    _save(out_path, results)


if __name__ == "__main__":
    main()
