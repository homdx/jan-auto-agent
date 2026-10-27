#!/usr/bin/env python3
"""CC-11 (293) black box: `--accept committed` through the candidate's command line, a fake model on a local port.

No candidate function is imported or called. The fixture repository of CC-0 is built, a tiny OpenAI-style
server is started on 127.0.0.1, an ini file names five voters that point at it, and the candidate's own
`scripts/claim_diff.py` is run as a process, over the fixture (70 claims at base, 80 at head):

    claim_diff.py claims_fixture.json --target FX --base <base> --head <head>
                  --profiles v0 v1 v2 v3 v4 --runs 1 --out OUT [--accept all|committed]

The server is the perfect pack-reader of 285 (TRUE when the claim's probe text is in a *source* chunk of
the pack it was shown, FALSE when it is not, citing a line of the pack), with a behaviour per model name:

    m1 m2 m3   always answer: they are the committed voters
    m4         answers UNSURE on about a third of the claims (by a hash of the claim text)
    m5         answers UNSURE on about a quarter of the claims (another hash)
    liar       answers the opposite verdict, with a quote that is in the pack, on one claim in five
    dead       HTTP 404

Read back from OUT/delta.json and OUT/votes_{base,head}.json (the rows of `tally`):

 1. no `--accept` is `--accept all`: the same delta table and the same rows (byte for byte of the table);
 2. five voters with m4 and m5 abstaining: `--accept all` leaves claims open that `--accept committed`
    accepts (strictly more `unanimous` rows), and `committed` gives the 285 table (fixed >= 19 of 20,
    still >= 9 of 10, gone 2 of 2, new 2 of 2), `still_as_fixed` 0, every accepted row says what the always-committed reader said;
 3. rows that were accepted under `all` are accepted, with the same verdict, under `committed`
    (the rule only adds), and an accepted row under `committed` lists its abstainers (`abstained`);
 4. five voters of which two are dead (404): `committed` gives the same table as the three live voters alone;
 5. one voter that disagrees (`liar`) among m1 m2 m3 m4 m5: a claim it contradicts is never accepted under
    `committed`, and no accepted row is wrong (a dissent among the committed is not an abstention);
 6. two voters that commit and three that abstain: below the quorum, nothing is accepted;
 7. `--accept bogus` is exit 2 and writes no delta.

    python3 contest-bench/293/blackbox_293.py --code DIR [-v]   # DIR: a checkout with scripts/claim_diff.py
"""

from __future__ import annotations

import argparse
import hashlib
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
sys.path.insert(0, str(HERE.parent / "285"))
import blackbox_285 as bb  # noqa: E402  (the perfect reader, the fixture, the table)

TARGETS = bb.TARGETS


def h(model: str, claim: str, mod: int) -> int:
    return int(hashlib.sha256(f"{model}|{claim}".encode()).hexdigest(), 16) % mod


def behave(model: str, claim: str, row: dict) -> dict:
    """The perfect reader's row for *claim*, changed by what this model is."""
    if model == "m4" and h("m4", claim, 3) == 0:
        return {"id": row["id"], "verdict": "UNSURE"}
    if model == "m5" and h("m5", claim, 4) == 0:
        return {"id": row["id"], "verdict": "UNSURE"}
    if model.startswith("quiet"):
        return {"id": row["id"], "verdict": "UNSURE"}
    if model == "liar" and h("liar", claim, 5) == 0 and row.get("verdict") in ("TRUE", "FALSE"):
        return {**row, "verdict": "FALSE" if row["verdict"] == "TRUE" else "TRUE"}
    return row


def serve(probes: dict) -> tuple:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            model = body.get("model")
            if model == "dead":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"no such model"}')
                return
            prompt = "\n".join(str(m.get("content", "")) for m in body.get("messages", []) if m.get("role") == "user")
            rows = json.loads(bb.answer(prompt, probes))
            texts = {int(m.group(1)): m.group(2).split("\n", 1)[0] for m in bb._BLOCK.finditer(prompt)}
            rows = [behave(model, texts.get(r["id"], ""), r) for r in rows]
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


def run_cli(code: Path, tmp: Path, fixture: dict, url: str, models: list, tag: str, accept) -> tuple:
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
           "--out", str(out), "--repo-root", str(root)]
    if accept:
        cmd += ["--accept", accept]
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, env=env, timeout=1800)
    delta = out / "delta.json"
    return p, out, (json.loads(delta.read_text(encoding="utf-8")) if delta.is_file() else None)


def rows_of(out: Path, claims: list) -> list:
    """(side, claim row of tally, truth) for every claim of both votes files."""
    truth = {("base", c["claim"]): c.get("truth_base") for c in claims}
    truth.update({("head", c["claim"]): c.get("truth_head") for c in claims})
    res = []
    for side in ("base", "head"):
        f = out / f"votes_{side}.json"
        if not f.is_file():
            continue
        for r in json.loads(f.read_text(encoding="utf-8"))["claims"]:
            res.append((side, r, truth.get((side, r["claim"]))))
    return res


def accepted(rows: list) -> dict:
    """{(side, claim): verdict} of the rows a rule settled (unanimous TRUE or FALSE)."""
    return {(s, r["claim"]): r["verdict"] for s, r, _t in rows if r.get("unanimous") is True and r["verdict"] in ("TRUE", "FALSE")}


def wrong(rows: list) -> list:
    """Accepted rows whose verdict is not the one the always-committed perfect reader (v0 = m1) gave.

    The reader is perfect about its probes, not about the fixture's truth (a handful of claims have none),
    so the fixture's truth_base/truth_head is not the key here: what the rule must never do is accept
    something the committed voters did not say."""
    return [(s, r["claim"][:50]) for s, r, _t in rows
            if r.get("unanimous") is True and r["verdict"] in ("TRUE", "FALSE")
            and r.get("by_model", {}).get("v0") != r["verdict"]]


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

    def check(ok: bool, line: str) -> None:
        print(("ok   " if ok else "FAIL ") + line)
        if not ok:
            failures.append(line)

    def table_ok(tag: str, delta: dict, need=True) -> dict:
        res = bb.table(delta, claims)
        for k, (want, n_need) in TARGETS.items():
            n = res[k][want]
            line = f"{tag}: {k:5} -> {want:5} {n}/{sum(res[k].values())} (need >= {n_need})  all={dict(res[k])}"
            if need:
                check(n >= n_need, line)
            elif opts.verbose:
                print("     " + line)
        check(res["still"]["FIXED"] == 0, f"{tag}: still_as_fixed = {res['still']['FIXED']} (must be 0)")
        return res

    srv, url = serve(probes)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run([sys.executable, str(CC / "make_fixture.py"), "--build", str(tmp / "fx")],
                       check=True, capture_output=True, env=env)
        five = ["m1", "m2", "m3", "m4", "m5"]

        # 1 + 2 + 3: five voters, two abstaining a third / a quarter of the time
        p_def, o_def, d_def = run_cli(code, tmp, fixture, url, five, "def", None)
        p_all, o_all, d_all = run_cli(code, tmp, fixture, url, five, "all", "all")
        p_com, o_com, d_com = run_cli(code, tmp, fixture, url, five, "com", "committed")
        for tag, p, d in (("default", p_def, d_def), ("all", p_all, d_all), ("committed", p_com, d_com)):
            if d is None:
                check(False, f"{tag}: no delta.json (exit {p.returncode}): {(p.stderr or p.stdout)[-300:]}")
        if d_def and d_all and d_com:
            check(bb.table(d_def, claims) == bb.table(d_all, claims)
                  and [(r["claim"], r["verdict"], r.get("unanimous")) for _s, r, _t in rows_of(o_def, claims)]
                  == [(r["claim"], r["verdict"], r.get("unanimous")) for _s, r, _t in rows_of(o_all, claims)],
                  "1. no --accept is --accept all (same table, same verdicts and unanimous flags)")
            rows_all, rows_com = rows_of(o_all, claims), rows_of(o_com, claims)
            a_all, a_com = accepted(rows_all), accepted(rows_com)
            check(len(a_com) > len(a_all), f"2. committed accepts more than all: {len(a_com)} vs {len(a_all)} rows")
            table_ok("committed", d_com)
            res_all = bb.table(d_all, claims)
            check(res_all["fixed"]["FIXED"] < bb.table(d_com, claims)["fixed"]["FIXED"],
                  f"2. all leaves FIXED claims open that committed settles: {res_all['fixed']['FIXED']} < "
                  f"{bb.table(d_com, claims)['fixed']['FIXED']}")
            check(not wrong(rows_com), f"2. every accepted row says what the committed perfect reader said, under committed: {wrong(rows_com)[:3]}")
            check(all(a_com.get(k) == v for k, v in a_all.items()),
                  "3. every row accepted under all is accepted, same verdict, under committed")
            with_abstainers = [r for _s, r, _t in rows_com if r.get("unanimous") is True and r.get("abstained")]
            check(bool(with_abstainers),
                  f"3. an accepted row lists its abstainers (`abstained`): {len(with_abstainers)} rows")
            check(all(r.get("abstained") in ([], None) for _s, r, _t in rows_all if r.get("unanimous") is True),
                  "3. under all an accepted row has no abstainers")
            check("accept" in json.dumps(d_com) or (o_com / "votes_head.json").read_text().count("committed") > 0,
                  "3. the rule is recorded in delta.json or votes_*.json")

        # 4: five voters, two dead
        _p3, _o3, d3 = run_cli(code, tmp, fixture, url, ["m1", "m2", "m3"], "three", "committed")
        _pd, _od, dd = run_cli(code, tmp, fixture, url, ["m1", "m2", "m3", "dead", "dead"], "dead", "committed")
        if d3 and dd:
            check(bb.table(d3, claims) == bb.table(dd, claims), "4. two dead voters of five: the table of the three live ones")
        else:
            check(False, "4. no delta.json for the dead-voter run")

        # 5: one voter that disagrees
        _pl, o_l, d_l = run_cli(code, tmp, fixture, url, five[:3] + ["liar", "m5"], "liar", "committed")
        if d_l:
            rows_l = rows_of(o_l, claims)
            contradicted = [(s, r) for s, r, _t in rows_l if "TRUE" in r.get("by_model", {}).values()
                            and "FALSE" in r.get("by_model", {}).values()]
            check(bool(contradicted), f"5. the liar contradicts {len(contradicted)} claims")
            check(all(r.get("unanimous") is not True for _s, r in contradicted),
                  "5. a claim with a dissent among the committed is never accepted")
            check(not wrong(rows_l), f"5. every accepted row follows the reader, with a liar in the set: {wrong(rows_l)[:3]}")
        else:
            check(False, "5. no delta.json for the liar run")

        # 6: below the quorum
        _pq, o_q, d_q = run_cli(code, tmp, fixture, url, ["m1", "m2", "quiet1", "quiet2", "quiet3"], "quorum", "committed")
        if d_q:
            check(not accepted(rows_of(o_q, claims)), "6. two committed voters of five: nothing accepted")
        else:
            check(False, "6. no delta.json for the quorum run")

        # 7: a bad value
        p_bad, _ob, d_bad = run_cli(code, tmp, fixture, url, five, "bad", "bogus")
        check(p_bad.returncode == 2 and d_bad is None, f"7. --accept bogus: exit {p_bad.returncode}, delta written: {d_bad is not None}")
    srv.shutdown()
    print("PASS" if not failures else f"FAIL ({len(failures)})")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
