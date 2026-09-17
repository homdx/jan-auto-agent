"""validate_inputs.py <entrants.json> --inputs <dir> --wt <dir> [--results results.json]

Answers "did I forget a submission?":
  * every *.patch / *.diff / *.zip under --inputs is referenced by entrants.json
  * byte-identical inputs are declared as duplicate_of (and vice versa)
  * every non-duplicate entrant has a worktree whose HEAD differs from its base,
    and the files it changed are exactly the files its patch touches
  * (with --results) every entrant and `base` have a result for every scenario
Exit status 1 when anything is off.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def patch_files(text: str) -> set:
    return set(re.findall(r"^diff --git a/(\S+) b/", text, re.M))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("entrants")
    ap.add_argument("--inputs", required=True)
    ap.add_argument("--wt", required=True)
    ap.add_argument("--results")
    ap.add_argument("--repo", default=".")
    a = ap.parse_args()
    repo = Path(a.repo).resolve()
    cfg = json.loads(Path(a.entrants).read_text())
    inputs = Path(a.inputs).resolve()
    wt = Path(a.wt).resolve()
    problems = []

    files = sorted(p for p in inputs.rglob("*") if p.suffix in (".patch", ".diff", ".zip") and "harness" not in p.parts)
    referenced = {}
    for name, e in cfg["entrants"].items():
        src = (repo / e["source"]).resolve()
        if not src.exists():
            problems.append(f"{name}: source missing {src}")
            continue
        referenced.setdefault(src, []).append(name)
    for f in files:
        if f not in referenced:
            problems.append(f"input not referenced by entrants.json: {f.relative_to(repo)}")
    # duplicates by md5
    by_md5 = {}
    for f in files:
        by_md5.setdefault(md5(f), []).append(f)
    for h, fs in by_md5.items():
        if len(fs) > 1:
            names = [n for f in fs for n in referenced.get(f, [])]
            dups = [n for n in names if cfg["entrants"][n].get("duplicate_of")]
            if len(dups) != len(names) - 1:
                problems.append(f"byte-identical inputs {[str(f.name) for f in fs]} → entrants {names}: expected all but one marked duplicate_of")
            print(f"duplicate group: {[f.name for f in fs]} → {names} (kept: {[n for n in names if n not in dups]})")
    for name, e in cfg["entrants"].items():
        if e.get("duplicate_of"):
            continue
        d = wt / name
        if not d.is_dir():
            problems.append(f"{name}: no worktree at {d}")
            continue
        base = e.get("base", cfg["base"])
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True).stdout.strip()
        basesha = subprocess.run(["git", "rev-parse", base], cwd=d, capture_output=True, text=True).stdout.strip()
        if head == basesha:
            problems.append(f"{name}: worktree HEAD == base — patch not applied")
            continue
        changed = set(subprocess.run(["git", "diff", "--name-only", f"{base}..HEAD"], cwd=d, capture_output=True, text=True).stdout.split())
        src = (repo / e["source"]).resolve()
        if src.suffix == ".zip":
            with zipfile.ZipFile(src) as z:
                text = z.read(e["inner"]).decode(errors="replace")
        else:
            text = src.read_text(errors="replace")
        if e.get("mail_index"):
            parts = re.split(r"^From [0-9a-f]{40} ", text, flags=re.M)
            text = parts[int(e["mail_index"])]
        expected = patch_files(text)
        if expected != changed:
            problems.append(f"{name}: files changed in worktree {sorted(changed ^ expected)} differ from the patch")
        dirty = subprocess.run(["git", "status", "--short"], cwd=d, capture_output=True, text=True).stdout.strip()
        if dirty:
            problems.append(f"{name}: worktree dirty:\n{dirty[:300]}")
        print(f"{name:12s} ok  base={base} files={len(changed)}")
    if a.results:
        R = json.loads(Path(a.results).read_text())
        want = ["base"] + [n for n, e in cfg["entrants"].items() if not e.get("duplicate_of")]
        scen_all = set()
        for scs in R.values():
            scen_all |= set(scs)
        for n in want:
            if n not in R:
                problems.append(f"results: no entry for {n}")
                continue
            missing = sorted(scen_all - set(R[n]))
            if missing:
                problems.append(f"results: {n} lacks scenarios {missing}")
            errs = [s for s, r in R[n].items() if r.get("error")]
            if errs:
                print(f"results: {n} has errored scenarios {errs} (counted as failed)")
        print(f"results: {len(want)} trees × {len(scen_all)} scenarios checked")
    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print("\nall inputs accounted for")


if __name__ == "__main__":
    main()
