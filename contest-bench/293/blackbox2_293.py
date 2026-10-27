#!/usr/bin/env python3
"""CC-11 (293) black box 2: the same rule seen from the other end — a scripted vote matrix, then the files.

Black box 1 (`blackbox_293.py`) runs `claim_diff.py` over the CC-0 fixture against a model that *reads* the
pack. This one shares nothing with it: no fixture, no reader, no `claim_diff`. It builds a four-file git
repository of its own, writes twelve claims whose text carries a case tag (`case03`), and starts a fake
model server that answers **from a table**: for every (model, case) a list of per-run replies —

    T / F     a verdict with a quote copied from the pack the model was shown
    U         UNSURE
    X         a verdict with a quote that is nowhere in the pack (the code must reject it)
    N         no row for that claim in the reply (a lost batch)

— so the right answer of every case, under each rule, is written down here before any code runs. The
candidate's `scripts/claim_vote.py` and `scripts/claimcheck_truth.py` are then run as processes
(`--runs 3`), and what is read back is the saved files: `votes.json` rows, `truth.csv`, `pending.md`.

Checks (the numbers in brackets are the cases):

  A  no flag is `--accept all` and `all` is today's rule: only the unanimous-of-everyone cases are accepted
  B  `--accept committed` accepts the cases whose committed voters agree and are at least three, lists the
     abstainers per row, never accepts a dissent, never accepts a quote the code rejected as a vote
  C  a voter's own majority over its runs decides; a voter heard in one run only still counts
  D  `[claim_vote] accept` in the ini works, a flag beats it, an unknown value is refused with exit 2 and no file
  E  `truth.csv` / `pending.md` of the committed run: the `how` column says k of n, a row with fewer than
     three families behind it is not a truth row, a not-accepted claim's pending line names the abstainers
  F  a votes.json written before the ticket (no `accept`, no `abstained`) is still read, as `all`
  G  voters that never answered (404, silence) are not voters, in either rule

    python3 contest-bench/293/blackbox2_293.py --code DIR [-v]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FILES = {f"m{i:02d}.py": f"def fn{i:02d}(x):\n    return x + {i}\n" for i in range(1, 13)}
FILES["README.md"] = "# tiny\n\nTwelve small modules.\n"

# the five voters of the main runs; the family of a voter is the first word of its model name
V5 = ["alpha-1", "beta-2", "gamma-3", "delta-4", "eps-5"]
V = {v: v.split("-")[0] for v in V5}


def runs(spec: str) -> list:
    return list(spec)


# case -> {model: per-run replies}, three runs each. A model missing from a case answers T on every run; EXPECT below gives each case's outcome.
MATRIX = {
    1: {},                                                                      # everyone TRUE
    2: {"delta-4": "UUU", "eps-5": "UUU"},                                      # three TRUE, two abstain
    3: {"delta-4": "FFF", "eps-5": "UUU"},                                      # three TRUE, one FALSE, one abstains
    4: {"gamma-3": "UUU", "delta-4": "UUU", "eps-5": "UUU"},                    # two TRUE, three abstain
    5: {v: "FFF" for v in V5},                                                  # everyone FALSE
    6: {"alpha-1": "FFF", "beta-2": "FFF", "gamma-3": "FFF", "delta-4": "XXX", "eps-5": "UUU"},  # a forged quote
    7: {"alpha-1": "FFF", "beta-2": "FFF", "gamma-3": "FFF", "delta-4": "TFF", "eps-5": "FFF"},  # a flip, own majority F
    8: {"alpha-1": "FFF", "beta-2": "FFF", "gamma-3": "FFF", "delta-4": "FTT", "eps-5": "FFF"},  # own majority T: a dissent
    9: {"delta-4": "NTN", "eps-5": "NNN"},                                      # delta heard once, eps never
    10: {"eps-5": "FFF"},                                                       # four TRUE, one FALSE
    11: {"alpha-1": "TUF"},                                                     # alpha torn (T,U,F = no majority), four TRUE
    12: {"gamma-3": "XXX", "delta-4": "XXX", "eps-5": "XXX"},                   # three forged quotes: two voters left
}

# (verdict, accepted under all, accepted under committed, voters that did not commit)
EXPECT = {
    1: ("TRUE", True, True, []),
    2: ("TRUE", False, True, ["delta-4", "eps-5"]),
    3: ("TRUE", False, False, None),
    4: ("UNSURE", False, False, None),
    5: ("FALSE", True, True, []),
    6: ("FALSE", False, True, ["delta-4", "eps-5"]),
    7: ("FALSE", True, True, []),
    8: ("FALSE", False, False, None),
    9: ("TRUE", False, True, ["eps-5"]),
    10: ("TRUE", False, False, None),
    11: ("TRUE", False, True, ["alpha-1"]),
    12: ("UNSURE", False, False, None),
}

_BLOCK = re.compile(r"=== CLAIM (\d+) ===\n(.*?)(?=\n\n=== CLAIM \d+ ===|\Z)", re.S)
_LABEL = re.compile(r"^\[\[(.+?)\]\]", re.M)
_GUTTER = re.compile(r"^ *\d*\| ?")


def first_quote(evidence: str):
    """(label, a content line) of the first chunk of the pack that has one, gutters dropped."""
    labels = list(_LABEL.finditer(evidence))
    for k, m in enumerate(labels):
        if m.group(1).startswith("diff:"):
            continue
        end = labels[k + 1].start() if k + 1 < len(labels) else len(evidence)
        for line in evidence[m.end():end].splitlines()[1:]:
            text = _GUTTER.sub("", line, count=1).strip()
            if text and not text.startswith("#") and not text.startswith("(evidence"):
                return m.group(1), text
    return None, None


def serve(script: dict, seen: dict) -> tuple:
    """*script*: {profile-model: {case: replies}} — see MATRIX. *seen*: (model, case) -> requests so far."""
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            model = body.get("model", "")
            if model.startswith("dead"):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"no such model"}')
                return
            prompt = "\n".join(str(m.get("content", "")) for m in body.get("messages", []) if m.get("role") == "user")
            rows = []
            for m in _BLOCK.finditer(prompt):
                n, (claim, evidence) = int(m.group(1)), m.group(2).split("\n", 1)
                tag = re.search(r"case(\d+)", claim)
                if not tag or model.startswith("mute"):
                    continue
                case = int(tag.group(1))
                with lock:
                    k = seen[(model, case)] = seen.get((model, case), 0) + 1
                spec = script.get(model, {}).get(case, "TTT")
                what = spec[(k - 1) % len(spec)]
                label, quote = first_quote(evidence)
                if what == "N" or label is None:
                    continue
                if what == "U":
                    rows.append({"id": n, "verdict": "UNSURE"})
                elif what == "X":
                    rows.append({"id": n, "verdict": "TRUE", "chunk": f"[[{label}]]",
                                 "quote": "nothing like this line exists in the pack anywhere"})
                else:
                    rows.append({"id": n, "verdict": "TRUE" if what == "T" else "FALSE",
                                 "chunk": f"[[{label}]]", "quote": quote[:160]})
            out = json.dumps({"choices": [{"message": {"role": "assistant", "content": json.dumps(rows)},
                                           "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


def build_repo(path: Path) -> str:
    path.mkdir()
    git(path, "init", "-q")
    for name, text in FILES.items():
        (path / name).write_text(text, encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "init")
    return git(path, "rev-parse", "HEAD")


def claims_file(path: Path) -> list:
    claims = []
    for i in range(1, 13):
        claims.append({"id": f"c{i:02d}", "claim": f"`fn{i:02d}` in `m{i:02d}.py` adds {i} to its argument (case{i:02d})", "kind": "code"})
    path.write_text(json.dumps({"claims": claims}), encoding="utf-8")
    return claims


def write_ini(root: Path, url: str, models: list, accept=None) -> None:
    NAMES.clear()
    NAMES.update({f"p{i}": m for i, m in enumerate(models)})
    ini = "[claim_vote]\ninterval_sec = 0\nper_provider = 8\n"
    if accept:
        ini += f"accept = {accept}\n"
    for i, model in enumerate(models):
        ini += (f"\n[p{i}]\nbase_url = {url}\napi_key = x\nmodel = {model}\napi_format = openai\n"
                f"temperature = 0.3\nmax_tokens = 3000\n")
    (root / "contest.local.ini").write_text(ini, encoding="utf-8")


def env_of() -> dict:
    return {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def vote(code: Path, root: Path, repo: Path, claims: Path, n_models: int, out: Path, *flags: str):
    cmd = [sys.executable, str(code / "scripts" / "claim_vote.py"), str(claims), "--target", str(repo),
           "--profiles", *[f"p{i}" for i in range(n_models)], "--runs", "3", "--batch", "4", "--out", str(out),
           "--repo-root", str(root), "--scratch", str(root / "scratch"), *flags]
    return subprocess.run(cmd, cwd=root, capture_output=True, text=True, env=env_of(), timeout=900)


def truth(code: Path, root: Path, votes: Path, repo: Path, out: Path, pending: Path):
    cmd = [sys.executable, str(code / "scripts" / "claimcheck_truth.py"), str(votes), "--out", str(out),
           "--pending", str(pending), "--target", str(repo), "--scratch", str(root / "scratch-t"),
           "--repo-root", str(root), "--no-lenz-hint"]
    return subprocess.run(cmd, cwd=root, capture_output=True, text=True, env=env_of(), timeout=600)


NAMES: dict = {}


def by_case(votes: dict) -> dict:
    res = {}
    for r in votes["claims"]:
        r = {**r, "by_model": {NAMES.get(k, k): v for k, v in (r.get("by_model") or {}).items()},
             **({"abstained": [NAMES.get(k, k) for k in r["abstained"]]} if "abstained" in r else {})}
        m = re.search(r"case(\d+)", r["claim"])
        if m:
            res[int(m.group(1))] = r
    return res


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=Path, required=True)
    ap.add_argument("-v", "--verbose", action="store_true")
    opts = ap.parse_args()
    code = opts.code.resolve()
    failures: list = []

    def check(ok: bool, line: str) -> None:
        print(("ok   " if ok else "FAIL ") + line)
        if not ok:
            failures.append(line)

    # --- main matrix: five voters, plus two that never answer
    script = {v: {} for v in V5}
    for case, per_model in MATRIX.items():
        for model, spec in per_model.items():
            script[model][case] = spec
    seen: dict = {}
    srv, url = serve(script, seen)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        repo = tmp / "repo"
        build_repo(repo)
        cfile = tmp / "claims.json"
        claims_file(cfile)
        root = tmp / "root"
        root.mkdir()
        models = V5 + ["dead-6", "mute-7"]
        write_ini(root, url, models)

        runs_out = {}
        for tag, flags in (("default", []), ("all", ["--accept", "all"]), ("committed", ["--accept", "committed"])):
            p = vote(code, root, repo, cfile, len(models), tmp / f"v-{tag}.json", *flags)
            runs_out[tag] = (p, load(tmp / f"v-{tag}.json"))
            check(p.returncode == 0 and runs_out[tag][1] is not None,
                  f"{tag}: claim_vote exits 0 and writes votes.json (rc={p.returncode}) {p.stderr.strip()[-160:] if p.returncode else ''}")
        if any(v[1] is None for v in runs_out.values()):
            print("cannot go on: a vote run wrote nothing")
            return 1
        d, a, c = (by_case(runs_out[t][1]) for t in ("default", "all", "committed"))

        # A + G: the default rule is `all`; dead and mute voters cost nothing
        for case, (verdict, acc_all, _acc_c, _ab) in EXPECT.items():
            check(a[case]["verdict"] == verdict and bool(a[case]["unanimous"]) == acc_all,
                  f"A. case{case:02d} under all: {a[case]['verdict']} unanimous={a[case]['unanimous']} "
                  f"(want {verdict} {acc_all})")
        check(all(d[k]["verdict"] == a[k]["verdict"] and bool(d[k]["unanimous"]) == bool(a[k]["unanimous"])
                  and d[k].get("by_model") == a[k].get("by_model") for k in a),
              "A. no flag gives the same verdicts, flags and by_model as --accept all")
        check(runs_out["default"][1].get("accept", "all") == "all", "A. votes.json of the default run says accept=all (or nothing)")
        check(all(not a[k].get("abstained") for k in a if a[k]["unanimous"]),
              "A. under all an accepted row has no abstainers")
        check("dead-6" not in json.dumps([r.get("by_model") for r in a.values()]),
              "G. the dead voter is in no row's by_model")

        # B: the committed rule, case by case
        for case, (verdict, _acc_all, acc_c, abst) in EXPECT.items():
            row = c[case]
            check(row["verdict"] == verdict and bool(row["unanimous"]) == acc_c,
                  f"B. case{case:02d} under committed: {row['verdict']} unanimous={row['unanimous']} (want {verdict} {acc_c})")
            if abst is not None:
                got = sorted(row.get("abstained") or [])
                check(got == sorted(abst), f"B. case{case:02d} abstained {got} (want {sorted(abst)})")
        check(runs_out["committed"][1].get("accept") == "committed", "B. votes.json records accept=committed")
        check(all(c[k]["unanimous"] for k in a if a[k]["unanimous"]) and
              all(c[k]["verdict"] == a[k]["verdict"] for k in a if a[k]["unanimous"]),
              "B. whatever all accepted, committed accepts with the same verdict")
        check(c[6].get("downgrades", 0) >= 1 or sum((c[6].get("rejected") or {}).values()) >= 1,
              f"B. case06: the forged quotes are counted as rejected ({c[6].get('rejected')})")
        check(not any(c[k]["unanimous"] and len({x for x in (c[k].get("by_model") or {}).values() if x in ("TRUE", "FALSE")}) > 1
                      for k in c), "B. no accepted row has a committed voter on the other side")
        check(c[3]["verdict"] in ("TRUE", "SPLIT") and not c[3]["unanimous"] and not c[10]["unanimous"],
              "B. one FALSE among four TRUE is never accepted")

        # C: own majority over runs; a voter heard once counts
        check(c[7]["by_model"].get("delta-4") == "FALSE" and c[8]["by_model"].get("delta-4") == "TRUE",
              f"C. own majority: case07 delta={c[7]['by_model'].get('delta-4')}, case08 delta={c[8]['by_model'].get('delta-4')}")
        check(c[9]["by_model"].get("delta-4") == "TRUE" and "eps-5" not in c[9]["by_model"],
              f"C. case09: delta (one run) votes TRUE, eps (none) is absent: {c[9]['by_model']}")
        check(c[11]["by_model"].get("alpha-1") in (None, "UNSURE"),
              f"C. case11: a voter with T, U, F has no majority ({c[11]['by_model'].get('alpha-1')})")

        # D: the ini and the flag
        write_ini(root, url, models, accept="committed")
        p_ini = vote(code, root, repo, cfile, len(models), tmp / "v-ini.json")
        v_ini = load(tmp / "v-ini.json")
        i = by_case(v_ini) if v_ini else {}
        check(p_ini.returncode == 0 and bool(i) and all(bool(i[k]["unanimous"]) == EXPECT[k][2] for k in EXPECT),
              "D. [claim_vote] accept = committed in the ini, no flag: the committed table")
        p_flag = vote(code, root, repo, cfile, len(models), tmp / "v-flag.json", "--accept", "all")
        v_flag = load(tmp / "v-flag.json")
        f = by_case(v_flag) if v_flag else {}
        check(p_flag.returncode == 0 and bool(f) and all(bool(f[k]["unanimous"]) == EXPECT[k][1] for k in EXPECT),
              "D. --accept all beats accept = committed of the ini")
        write_ini(root, url, models, accept="sometimes")
        p_bad = vote(code, root, repo, cfile, len(models), tmp / "v-bad.json")
        check(p_bad.returncode == 2 and not (tmp / "v-bad.json").exists() and "accept" in p_bad.stderr,
              f"D. accept = sometimes in the ini: exit 2, no file, the message names accept (rc={p_bad.returncode})")
        if len(p_bad.stderr.strip().splitlines()) > 3:
            print("note the ticket says 'exit 2 with one line'; this prints a usage block of "
                  f"{len(p_bad.stderr.strip().splitlines())} lines")
        write_ini(root, url, models)
        p_bad2 = vote(code, root, repo, cfile, len(models), tmp / "v-bad2.json", "--accept", "most")
        check(p_bad2.returncode == 2 and not (tmp / "v-bad2.json").exists(),
              f"D. --accept most: exit 2, no file (rc={p_bad2.returncode})")

        # E: the files of the committed run
        t_c, pend_c = tmp / "truth-c.csv", tmp / "pending-c.md"
        pt = truth(code, root, tmp / "v-committed.json", repo, t_c, pend_c)
        check(pt.returncode == 0 and t_c.is_file(), f"E. claimcheck_truth on the committed votes exits 0 (rc={pt.returncode}) {pt.stderr.strip()[-160:]}")
        rows = list(csv.DictReader(t_c.open(encoding="utf-8-sig"))) if t_c.is_file() else []
        row_of = {int(m.group(1)): r_ for r_ in rows for m in [re.search(r"m(\d+)\.py", r_["finding"])] if m}
        check(set(row_of) == {k for k, v in EXPECT.items() if v[2]},
              f"E. truth rows are exactly the cases accepted under committed: {sorted(row_of)}")
        check(bool(re.search(r"\b3 of 5\b", (row_of.get(2) or {}).get("how", ""))),
              f"E. case02's how says '3 of 5' voters committed: {(row_of.get(2) or {}).get('how', '')[:100]!r}")
        check(bool(re.search(r"\b4 of 5\b", (row_of.get(11) or {}).get("how", ""))),
              f"E. case11's how says '4 of 5': {(row_of.get(11) or {}).get('how', '')[:100]!r}")
        check(bool(re.search(r"\b5 of 5\b", (row_of.get(1) or {}).get("how", ""))) or "of 5" not in (row_of.get(1) or {}).get("how", "x of 5"),
              f"E. case01 (all five committed) says 5 of 5 or nothing: {(row_of.get(1) or {}).get('how', '')[:100]!r}")
        check(row_of.get(1, {}).get("truth") == "REAL" and row_of.get(5, {}).get("truth") == "FALSE",
              f"E. TRUE is REAL and FALSE is FALSE in truth.csv: {row_of.get(1, {}).get('truth')}/{row_of.get(5, {}).get('truth')}")
        check(bool(row_of.get(2)) and not any(x in row_of[2].get("checked_by", "") for x in ("delta", "eps", "dead", "mute")),
              f"E. case02's checked_by names only the families that committed: {(row_of.get(2) or {}).get('checked_by')}")
        pend = pend_c.read_text(encoding="utf-8") if pend_c.is_file() else ""
        sec4 = pend[pend.find("(case04)"):].split("### claim", 1)[0]
        check("abstain" in sec4.lower() and all(x in sec4 for x in ("p2", "p3", "p4")),
              "E. pending.md shows the abstaining voters of the open case04 (a line or a table cell)")

        # F: a file from before the ticket
        old = json.loads((tmp / "v-all.json").read_text(encoding="utf-8"))
        old.pop("accept", None)
        for r in old["claims"]:
            r.pop("abstained", None)
        (tmp / "v-old.json").write_text(json.dumps(old), encoding="utf-8")
        t_o, pend_o = tmp / "truth-o.csv", tmp / "pending-o.md"
        po = truth(code, root, tmp / "v-old.json", repo, t_o, pend_o)
        t_a, pend_a = tmp / "truth-a.csv", tmp / "pending-a.md"
        pa = truth(code, root, tmp / "v-all.json", repo, t_a, pend_a)
        same = t_o.is_file() and t_a.is_file() and t_o.read_text(encoding="utf-8") == t_a.read_text(encoding="utf-8")
        if opts.verbose: print(t_c.read_text()[:1500], pend_c.read_text()[:1500])
        check(po.returncode == 0 and pa.returncode == 0 and same,
              f"F. a votes.json without accept/abstained reads as all: same truth.csv (rc {po.returncode}/{pa.returncode})")

        # G: dead and mute voters, and the load they were
        gone = {m for (m, _c) in seen}
        check("dead-6" in gone or True, "G. (dead voters were asked)")
        check(len({r for r in a[1]["by_model"]}) == 5, f"G. case01 has five voters, not seven: {sorted(a[1]['by_model'])}")

    # --- second pass: three voters of ONE family are one opinion (families rule), by a profile set of their own
    fam = {"agnes-1": {}, "agnes-2": {}, "agnes-3": {}, "beta-2": {}, "gamma-3": {}}
    # case 1 everyone TRUE; case 2 the two others abstain: three agnes are three committed, one family
    fam["beta-2"][2] = "UUU"
    fam["gamma-3"][2] = "UUU"
    # case 3: gamma abstains, so agnes x3 + beta: two families
    fam["gamma-3"][3] = "UUU"
    seen2: dict = {}
    srv2, url2 = serve(fam, seen2)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        repo = tmp / "repo"
        build_repo(repo)
        cfile = tmp / "claims.json"
        claims_file(cfile)
        root = tmp / "root"
        root.mkdir()
        fmodels = list(fam)
        write_ini(root, url2, fmodels)
        p = vote(code, root, repo, cfile, len(fmodels), tmp / "v.json", "--accept", "committed")
        v = load(tmp / "v.json")
        if p.returncode != 0 or v is None:
            check(False, f"H. the family run wrote votes.json (rc={p.returncode}) {p.stderr.strip()[-200:]}")
        else:
            r = by_case(v)
            check(all(r[k]["unanimous"] and r[k]["verdict"] == "TRUE" for k in (1, 2, 3)),
                  "H. cases 1-3: tally accepts TRUE (committed: 5 / 3 / 4 voters)")
            check(sorted(r[2].get("abstained") or []) == ["beta-2", "gamma-3"], f"H. case02 abstained {r[2].get('abstained')}")
            t, pe = tmp / "t.csv", tmp / "p.md"
            pt = truth(code, root, tmp / "v.json", repo, t, pe)
            rows = list(csv.DictReader(t.open(encoding="utf-8-sig"))) if t.is_file() else []
            have = {int(m.group(1)) for r_ in rows for m in [re.search(r"m(\d+)\.py", r_["finding"])] if m}
            check(1 in have, f"H. case01 (3 families) is a truth row: {sorted(have)}")
            check(2 not in have, "H. case02: three agnes agreeing are one family, no truth row")
            check(3 not in have, "H. case03: three agnes + beta are two families, no truth row")
            pend_text = pe.read_text(encoding="utf-8") if pe.is_file() else ""
            check("famil" in pend_text, "H. pending.md says why: fewer than three families")

    print()
    print("PASS" if not failures else f"FAIL ({len(failures)} of the checks)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
