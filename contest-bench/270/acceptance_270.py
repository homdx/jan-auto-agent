#!/usr/bin/env python3
"""CC-4 (270) acceptance, written from the ticket's "Acceptance" section.

  * contest-bench/cc fixture, the 20 fix claims (f001-f020) with base and head given: the
    deciding hunk is in the chunks — every line of the claim's `how` range that the diff
    adds is printed with its head line number (`N| +…`), every line of the base range it
    removes is printed (`| -…`), and no chunk is of another file (the anchored path's hunks,
    not a search of the whole range). The hunks are read off `git diff` by this script, not
    by `hunks_of`. Target >= 90 %.
  * the 6 commit and ticket claims (f075-f080): a ticket's deciding line is printed with
    its line number at each side the `how` names; a commit's `git show --stat` files and
    summary line, or its subject, are in the header. Target 100 %.
  * dangling commit claims (made up here: shas that are not in the fixture): a `note`
    chunk naming the sha and no `git:<sha>` chunk. Target 100 %.
  * this repository at real_sha, r026 (a commit) and r027 (a ticket): the same checks.

The anchors are the pipeline's own (`extract_anchors` + `resolve_anchors` at the pinned
tree), so an extractor miss shows here too.

    python3 contest-bench/270/acceptance_270.py [--code DIR] [-v]   # DIR: checkout holding tools/claimcheck/
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CC = HERE.parent / "cc"
REPO = HERE.parent.parent
_READ = re.compile(r"read\s+([\w./-]+\.\w+):(\d+)(?:-(\d+))?")
_AT = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DANGLING = ["Commit deadbee fixed `Store.load` in `store.py`.",
            "Commit 1234567abc adds `retry.py`.",
            "The fix in commit a73e389 left `gates.check_exit` as it was."]


def _git(root, *args) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True, env=env).stdout


def changed_lines(root, base, head, path) -> tuple:
    """({head line: text} the diff adds, {base line: text} it removes), off plain `git diff`."""
    added, removed = {}, {}
    old = new = 0
    for line in _git(root, "diff", "--no-color", "-U3", base, head, "--", path).split("\n"):
        m = _AT.match(line)
        if m:
            old, new = int(m.group(1)), int(m.group(3))
        elif line.startswith("+") and not line.startswith("+++"):
            added[new] = line[1:]
            new += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed[old] = line[1:]
            old += 1
        elif line.startswith(" "):
            old, new = old + 1, new + 1
    return added, removed


def side(how: str, name: str) -> list:
    part = [p for p in how.split(";") if p.strip().startswith(name + ":")]
    return [(m.group(1), int(m.group(2)), int(m.group(3) or m.group(2)))
            for m in _READ.finditer(part[0] if part else "")]


def chunks_for(claim, view, **kw):
    from tools.claimcheck.anchors import extract_anchors, resolve_anchors
    from tools.claimcheck.evidence_git import git_chunks
    return git_chunks(resolve_anchors(extract_anchors(claim), view), view, claim=claim, **kw)


def fix_ok(c, chunks, root, base, head) -> bool:
    texts = [ch.text for ch in chunks if ch.kind == "git"]
    named = {p for p, _, _ in side(c["how"], "head") + side(c["how"], "base")}
    if {ch.path for ch in chunks} - named:
        return False          # the range's hunks are those of the anchored file, not a keyword search
    need = 0
    for which, rng in (("head", side(c["how"], "head")), ("base", side(c["how"], "base"))):
        for path, a, b in rng:
            added, removed = changed_lines(root, base, head, path)
            for n in range(a, b + 1):
                if which == "head" and n in added:
                    need += 1
                    rx = re.compile(rf"^ *{n}\| \+{re.escape(added[n])}$", re.M)
                    if not any(rx.search(t) for t in texts):
                        return False
                if which == "base" and n in removed:
                    need += 1
                    if not any(f"| -{removed[n]}\n" in t + "\n" for t in texts):
                        return False
    return need > 0


def ticket_ok(c, view, sides) -> bool:
    lines_needed = side(c["how"], sides)
    chunks = chunks_for(c["claim"], view)
    text = "\n".join(ch.text for ch in chunks if ch.kind == "ticket")
    return bool(text) and all(re.search(rf"^ *{n}\| ", text, re.M) for _, a, b in lines_needed
                              for n in range(a, b + 1))


def commit_ok(c, view, root, sha) -> bool:
    header = "\n".join(ch.text for ch in chunks_for(c["claim"], view) if ch.id.endswith(":header"))
    if "--format=%s" in c["how"]:
        return _git(root, "log", "-1", "--format=%s", sha).strip() in header
    stat = _git(root, "show", "--stat=1000", "--format=", sha).strip().split("\n")
    names = [s.split("|")[0].strip() for s in stat[:-1]]
    return bool(header) and stat[-1].strip() in header and all(n in header for n in names)


def report(name, hit, counted, target, verbose_rows, failures) -> None:
    share = hit / counted if counted else 0.0
    line = f"{name}: {hit}/{counted} = {share:.0%} (target {target:.0%})"
    print(("ok   " if share >= target else "FAIL ") + line)
    for row in verbose_rows:
        print(row)
    if share < target:
        failures.append(line)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=Path, default=REPO)
    ap.add_argument("-v", "--verbose", action="store_true")
    opts = ap.parse_args()
    sys.path.insert(0, str(opts.code.resolve()))
    from tools.claimcheck.target import Target

    fixture = json.loads((CC / "claims_fixture.json").read_text(encoding="utf-8"))
    real = json.loads((CC / "claims_real.json").read_text(encoding="utf-8"))
    base, head = fixture["base_sha"], fixture["head_sha"]
    failures: list = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run([sys.executable, str(CC / "make_fixture.py"), "--build", str(tmp / "fx")],
                       check=True, capture_output=True, env=env)
        fx = tmp / "fx"
        by_id = {c["id"]: c for c in fixture["claims"]}
        with Target.open(fx, head, scratch=tmp / "s") as th, Target.open(fx, base, scratch=tmp / "s") as tb:
            vh, vb = th.view(), tb.view()
            rows, hit = [], 0
            for i in range(1, 21):
                c = by_id[f"f{i:03d}"]
                chunks = chunks_for(c["claim"], vh, base=base, head=head)
                ok = fix_ok(c, chunks, fx, base, head)
                hit += ok
                if opts.verbose or not ok:
                    rows.append(f"  {'ok  ' if ok else 'MISS'} {c['id']} {sum(len(x.text) for x in chunks):5d} ch "
                                f"{[x.id for x in chunks]}")
            report("fixture fix claims, deciding hunk", hit, 20, 0.90, rows, failures)

            rows, hit = [], 0
            for cid in ("f075", "f076", "f077", "f078", "f079", "f080"):
                c = by_id[cid]
                if c["kind"] == "ticket":
                    ok = all(ticket_ok(c, v, s) for v, s in ((vh, "head"), (vb, "base")) if side(c["how"], s))
                else:
                    ok = commit_ok(c, vh, fx, base)
                hit += ok
                if opts.verbose or not ok:
                    rows.append(f"  {'ok  ' if ok else 'MISS'} {cid} {c['claim'][:70]}")
            report("fixture commit and ticket claims", hit, 6, 1.0, rows, failures)

            rows, hit = [], 0
            for claim in DANGLING:
                chunks = chunks_for(claim, vh)
                sha = re.search(r"\b([0-9a-f]{7,40})\b", claim).group(1)
                # the note names the sha; no chunk passes itself off as that commit's evidence
                # (a `gitlog:` of a named path is the history words' chunk, not the commit's)
                ok = any(ch.kind == "note" and sha in ch.text for ch in chunks) \
                    and not any(ch.id.startswith(f"git:{sha[:7]}") for ch in chunks)
                hit += ok
                if opts.verbose or not ok:
                    rows.append(f"  {'ok  ' if ok else 'MISS'} {claim[:60]} {[x.id for x in chunks]}")
            report("dangling commit claims, a note", hit, len(DANGLING), 1.0, rows, failures)

        with Target.open(REPO, real["real_sha"], scratch=tmp / "s") as t:
            v = t.view()
            rc = {c["id"]: c for c in real["claims"]}
            rows, hit = [], 0
            r026 = rc["r026"]
            sha = re.search(r"\b([0-9a-f]{7,40})\b", r026["claim"]).group(1)
            header = "\n".join(ch.text for ch in chunks_for(r026["claim"], v) if ch.id.endswith(":header"))
            ok26 = "1 file changed" in header and "epic-tasks/73-kc34" in header and sha in header
            r027 = rc["r027"]
            ticket = "\n".join(ch.text for ch in chunks_for(r027["claim"], v) if ch.kind == "ticket")
            ok27 = bool(re.search(r"^ *3\| \*\*Status:\*\* landed", ticket, re.M))
            for cid, ok in (("r026", ok26), ("r027", ok27)):
                hit += ok
                if opts.verbose or not ok:
                    rows.append(f"  {'ok  ' if ok else 'MISS'} {cid}")
            report(f"real {real['real_sha'][:7]} commit and ticket claims", hit, 2, 1.0, rows, failures)
    print("PASS" if not failures else f"FAIL ({len(failures)})")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
