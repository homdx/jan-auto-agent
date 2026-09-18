"""setup_worktrees.py <entrants.json> --wt <dir> [--repo <path>] [name ...]

One git worktree per entrant at the ticket base with that entrant's patch
applied and committed, plus a `base` worktree (unpatched) as the reference.

entrants.json:
{
  "base": "da1e9b3",                       # ticket base commit
  "entrants": {
    "<name>": {
      "source": "run9/Foo.patch",          # .patch/.diff, or a .zip
      "inner": "run9-zip/run9.patch",      # (zip only) path of the patch inside the zip
      "base": "777535b",                   # optional per-entrant base override
      "mail_index": 6,                     # optional: only this commit of a multi-commit format-patch
      "duplicate_of": "<other name>"       # optional: byte-identical to another entry — not set up
    }
  }
}

Format-patches (`From <sha>` header) go through `git am --3way`; plain diffs
through `git apply --3way` + one commit (`--no-verify`: an entrant that forgot
the smoke mirror is still measured, static_checks.py reports the miss).
"""
import argparse
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

IDENT = ["-c", "user.name=contest-bench", "-c", "user.email=contest-bench@local"]


def sh(args, cwd=None, check=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(map(str, args))}\n{p.stdout}{p.stderr}")
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("entrants")
    ap.add_argument("--wt", required=True)
    ap.add_argument("--repo", default=".")
    ap.add_argument("names", nargs="*")
    a = ap.parse_intermixed_args()
    repo = Path(a.repo).resolve()
    cfg = json.loads(Path(a.entrants).read_text())
    wt = Path(a.wt).resolve()
    wt.mkdir(parents=True, exist_ok=True)
    default_base = cfg["base"]
    names = a.names or ["base"] + list(cfg["entrants"])
    for name in names:
        d = wt / name
        if d.exists():
            print(f"[{name}] exists — skipped")
            continue
        if name == "base":
            sh(["git", "worktree", "add", "-q", "--detach", str(d), default_base], cwd=repo)
            print(f"[base] {default_base}")
            continue
        e = cfg["entrants"][name]
        if e.get("duplicate_of"):
            print(f"[{name}] duplicate of {e['duplicate_of']} — not set up")
            continue
        base = e.get("base", default_base)
        src = (repo / e["source"]).resolve() if not Path(e["source"]).is_absolute() else Path(e["source"])
        patch = src
        tmp = None
        if src.suffix == ".zip":
            tmp = tempfile.mkdtemp(prefix="cb-")
            with zipfile.ZipFile(src) as z:
                z.extract(e["inner"], tmp)
            patch = Path(tmp) / e["inner"]
        sh(["git", "worktree", "add", "-q", "--detach", str(d), base], cwd=repo)
        text = patch.read_text(errors="replace")
        is_mail = text.startswith("From ") or "\nFrom " in text[:4000]
        if is_mail and e.get("mail_index"):
            split = Path(tempfile.mkdtemp(prefix="cb-split-"))
            sh(["git", "mailsplit", f"-o{split}", str(patch)], cwd=d)
            patch = split / f"{int(e['mail_index']):04d}"
        if is_mail:
            p = sh(["git", *IDENT, "am", "-q", "--3way", str(patch)], cwd=d, check=False)
            if p.returncode:
                sh(["git", "am", "--abort"], cwd=d, check=False)
                print(f"[{name}] git am FAILED:\n{p.stdout}{p.stderr}"[:800])
                continue
            n = sh(["git", "rev-list", "--count", f"{base}..HEAD"], cwd=d).stdout.strip()
            print(f"[{name}] am OK ({n} commit(s) on {base})")
        else:
            p = sh(["git", "apply", "--3way", str(patch)], cwd=d, check=False)
            if p.returncode:
                print(f"[{name}] git apply FAILED:\n{p.stdout}{p.stderr}"[:800])
                continue
            sh(["git", "add", "-A"], cwd=d)
            sh(["git", *IDENT, "commit", "-q", "--no-verify", "-m", f"{name}: contest entry (plain diff)"], cwd=d)
            print(f"[{name}] apply OK (1 commit on {base}, --no-verify)")


if __name__ == "__main__":
    main()
