#!/usr/bin/env python3
"""CC-7 (285) black box: the candidate's command line, a fake model on a local port, nothing else.

No candidate function is imported or called. The fixture repository is built, a tiny
OpenAI-style server is started on 127.0.0.1, an ini file names three voters that point
at it, and the candidate's own `scripts/claim_diff.py` is run as a process:

    claim_diff.py claims_fixture.json --target FX --base <base> --head <head>
                  --profiles v1 v2 v3 --runs 1 --out OUT --check

The server is a **perfect pack-reader**: for a claim with a probe (the text whose presence
makes the claim true, `make_fixture.SPEC`) it answers TRUE when that text is in a *source*
chunk of the pack it was shown, FALSE when it is not, citing a line of the pack; a diff
chunk (`diff:…`) is never evidence of what the code says now. A claim without a probe
(world, git) is UNSURE. So base and head are told apart only by the packs.

Read back from `OUT/delta.json` (the shape `contest-bench/cc/score_cc.py` reads):
  fixed -> FIXED >= 19 of 20, still -> STILL >= 9 of 10, gone -> GONE 2 of 2, new -> NEW 2 of 2,
  no `still` claim FIXED. A second pass with one of the three voters dead (HTTP 404) must give
  the same table. Exit codes: `--check` is 0 with every expectation met.

    python3 contest-bench/285/blackbox_285.py --code DIR [-v] [--keep]   # DIR: a checkout with scripts/claim_diff.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CC = HERE.parent / "cc"
TARGETS = {"fixed": ("FIXED", 19), "still": ("STILL", 9), "gone": ("GONE", 2), "new": ("NEW", 2)}

_BLOCK = re.compile(r"=== CLAIM (\d+) ===\n(.*?)(?=\n\n=== CLAIM \d+ ===|\Z)", re.S)
_LABEL = re.compile(r"^\[\[(.+?)\]\]  \(\w+\)$", re.M)
_GUTTER = re.compile(r"^ *\d*\| ?")


def chunks_of(evidence: str) -> list:
    """(id, [content lines]) of each chunk in a rendered pack, gutters dropped."""
    labels = list(_LABEL.finditer(evidence))
    out = []
    for k, m in enumerate(labels):
        end = labels[k + 1].start() if k + 1 < len(labels) else len(evidence)
        lines = []
        for line in evidence[m.end():end].strip("\n").splitlines():
            if line.startswith("# …") or line.startswith("(evidence trimmed"):
                continue
            lines.append(_GUTTER.sub("", line, count=1).strip())
        out.append((m.group(1), [ln for ln in lines if ln]))
    return out


def has_probe(probe: str, lines: list) -> bool:
    """The probe's lines in a row among *lines* (stripped): the first may start mid-line, the last
    may end mid-line unless the probe ends in a newline, those between are whole lines."""
    end_nl = probe.endswith("\n")
    want = [ln.strip() for ln in probe.splitlines()]
    have = [ln.strip() for ln in lines]
    n = len(want)
    for i in range(len(have) - n + 1):
        ok = True
        for j, w in enumerate(want):
            h = have[i + j]
            last = j == n - 1
            if n == 1:
                ok = h.endswith(w) if end_nl else (w in h)
            elif j == 0:
                ok = h.endswith(w)
            elif last:
                ok = (h == w) if end_nl else h.startswith(w)
            else:
                ok = h == w
            if not ok:
                break
        if ok:
            return True
    return False


def answer(prompt: str, probes: dict) -> str:
    rows = []
    for m in _BLOCK.finditer(prompt):
        n, (claim, evidence) = int(m.group(1)), m.group(2).split("\n", 1)
        probe = probes.get(claim)
        chunks = [c for c in chunks_of(evidence) if not c[0].startswith("diff:") and c[1]]
        if probe is None or not chunks:
            rows.append({"id": n, "verdict": "UNSURE"})
            continue
        want = [ln.strip() for ln in probe.splitlines() if ln.strip()]
        present = any(has_probe(probe, lines) for _cid, lines in chunks)
        cited = next(((cid, ln) for w in want for cid, lines in chunks for ln in lines if w in ln), None)
        cid, quote = cited if present and cited else (chunks[0][0], chunks[0][1][0])
        rows.append({"id": n, "verdict": "TRUE" if present else "FALSE",
                     "chunk": f"[[{cid}]]", "quote": quote[:200]})
    return json.dumps(rows)


def serve(probes: dict) -> tuple:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if body.get("model") == "dead":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"no such model"}')
                return
            prompt = "\n".join(str(m.get("content", "")) for m in body.get("messages", []) if m.get("role") == "user")
            out = json.dumps({"choices": [{"message": {"role": "assistant", "content": answer(prompt, probes)},
                                           "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


def run_cli(code: Path, tmp: Path, fixture: dict, url: str, models: list, tag: str) -> tuple:
    root = tmp / f"root-{tag}"
    root.mkdir()
    ini = "[claim_vote]\ninterval_sec = 0\nper_provider = 8\n"
    for i, model in enumerate(models):
        ini += (f"\n[v{i}]\nbase_url = {url}\napi_key = x\nmodel = {model}\napi_format = openai\n"
                f"temperature = 0.3\nmax_tokens = 4000\n")
    (root / "contest.local.ini").write_text(ini, encoding="utf-8")
    out = tmp / f"out-{tag}"
    cmd = [sys.executable, str(code / "scripts" / "claim_diff.py"), str(CC / "claims_fixture.json"),
           "--target", str(tmp / "fx"), "--base", fixture["base_sha"], "--head", fixture["head_sha"],
           "--profiles", *[f"v{i}" for i in range(len(models))], "--runs", "1",
           "--out", str(out), "--repo-root", str(root), "--check"]
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, env=env, timeout=1800)
    delta = out / "delta.json"
    return p, (json.loads(delta.read_text(encoding="utf-8")) if delta.is_file() else None)


def table(delta: dict, claims: list) -> dict:
    by_id = {c["id"]: c for c in claims}
    got = {d.get("id"): d["change"] for d in delta["deltas"]}
    res = {k: Counter() for k in TARGETS}
    for cid, c in by_id.items():
        if c.get("expect") in res:
            res[c["expect"]][got.get(cid, "MISSING")] += 1
    return res


def misses(delta: dict, claims: list) -> list:
    """(id, expect, change) of every claim whose change is not its expect."""
    got = {d.get("id"): d["change"] for d in delta["deltas"]}
    return [(c["id"], c["expect"], got.get(c["id"], "MISSING")) for c in claims
            if c.get("expect") in TARGETS and got.get(c["id"]) != TARGETS[c["expect"]][0]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=Path, required=True)
    ap.add_argument("-v", "--verbose", action="store_true")
    opts = ap.parse_args()
    code = opts.code.resolve()
    sys.path.insert(0, str(CC))
    import make_fixture

    fixture = json.loads((CC / "claims_fixture.json").read_text(encoding="utf-8"))
    claims = fixture["claims"]
    probes = {s["claim"]: (s["probe"][1] if s.get("probe") else None) for s in make_fixture.SPEC}
    failures: list = []
    srv, url = serve(probes)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run([sys.executable, str(CC / "make_fixture.py"), "--build", str(tmp / "fx")],
                       check=True, capture_output=True, env=env)
        tables = {}
        for tag, models in (("three", ["m1", "m2", "m3"]), ("onedead", ["m1", "m2", "m3", "dead"])):
            p, delta = run_cli(code, tmp, fixture, url, models, tag)
            if delta is None:
                line = f"{tag}: no delta.json (exit {p.returncode}): {(p.stderr or p.stdout)[-300:]}"
                print("FAIL " + line)
                failures.append(line)
                continue
            res = table(delta, claims)
            tables[tag] = res
            for k, (want, need) in TARGETS.items():
                n = res[k][want]
                total = sum(res[k].values())
                ok = n >= need
                line = f"{tag}: {k:5} -> {want:5} {n}/{total} (need >= {need})  all={dict(res[k])}"
                print(("ok   " if ok else "FAIL ") + line)
                if not ok:
                    failures.append(line)
            for cid, want, change in misses(delta, claims):
                print(f"       miss {cid}: expect {want}, got {change}")
            bad = res["still"]["FIXED"]
            line = f"{tag}: still_as_fixed = {bad} (must be 0); exit of --check = {p.returncode}"
            print(("ok   " if bad == 0 else "FAIL ") + line)
            if bad:
                failures.append(line)
            if opts.verbose:
                print((p.stderr or "")[-600:])
        if len(tables) == 2 and tables["three"] != tables["onedead"]:
            line = "a dead voter changed the table"
            print("FAIL " + line)
            failures.append(line)
    srv.shutdown()
    print("PASS" if not failures else f"FAIL ({len(failures)})")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
