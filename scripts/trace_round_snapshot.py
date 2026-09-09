#!/usr/bin/env python3
"""Snapshot what a round of `--auto` runs already reports, from their traces.

Read-only. Touches nothing but `.agent/trace_*.jsonl`, so it is safe to point
at runs that are still in flight — which is the case it was written for.

    python3 scripts/trace_round_snapshot.py ../testtext ../testtext3 \\
        --out docs/collect-epics/baseline-live.json

Why this exists: the probe already emits `ops / hits / misses / memo_hits /
by_op / chars_used / informed_facts / blind_facts` on every `probe_result`, and
gate 1's verdicts are in the trace verbatim. That is most of a Tier-2 baseline,
available before a single line of the epics is implemented. This reads it.

This is **not** ticket `M4`. `M4` adds runtime counters that do not exist yet
(`collect_block`, `collect_shrink`, `collect_miss`, the Gate-1 stage split).
This only reads what is already emitted, so a "before" number survives runs that
have since finished.
"""
import argparse
import collections
import datetime
import glob
import json
import os
import re
import sys

LOC = re.compile(r"^Location:\s*(.+)$", re.M)


def read_run(base):
    traces = sorted(glob.glob(os.path.join(base, ".agent", "trace_*.jsonl")))
    if not traces:
        return None
    out = {
        "run": os.path.basename(os.path.abspath(base)),
        "trace": os.path.basename(traces[0]),
        "goal": "", "probe_usable": None, "probe_reason": None,
        "llm_by_source": collections.Counter(),
        "gate1": {"requests": 0, "confirmed": 0, "rejected": 0, "unparsed": 0},
        "gate1_location_ext": collections.Counter(),
        "probe": {"ops": 0, "hits": 0, "misses": 0, "memo_hits": 0},
        "probe_by_op": collections.Counter(),
        "collect_block_occurrences": 0,
        "first_ts": None, "last_ts": None, "gate1_first_ts": None, "gate1_last_ts": None,
    }
    pending_ext = None
    for line in open(traces[0], encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        if "COLLECT MODEL (static facts" in line:
            out["collect_block_occurrences"] += 1
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts, kind, src, tgt = r.get("ts"), r.get("kind"), r.get("source"), r.get("target")
        if ts:
            out["first_ts"] = out["first_ts"] or ts
            out["last_ts"] = ts
        if kind == "run_start":
            out["goal"] = (r.get("params") or {}).get("goal") or r.get("content", "")[:400]
        elif kind == "probe_config":
            p = r.get("params") or {}
            out["probe_usable"] = p.get("usable")
            out["probe_reason"] = p.get("reason")
        elif kind == "llm_request":
            out["llm_by_source"][src or "?"] += 1
            if src == "gate1":
                out["gate1"]["requests"] += 1
                out["gate1_first_ts"] = out["gate1_first_ts"] or ts
                out["gate1_last_ts"] = ts
                m = LOC.search(r.get("content", "") or "")
                path = m.group(1).split(",")[0].strip() if m else ""
                pending_ext = (os.path.splitext(path)[1] or "<none>").lower()
        elif kind == "llm_response" and tgt == "gate1":
            try:
                v = json.loads(r.get("content", "") or "").get("verdict")
            except Exception:
                v = None
            if v in ("confirmed", "rejected"):
                out["gate1"][v] += 1
            else:
                out["gate1"]["unparsed"] += 1
            if pending_ext is not None:
                out["gate1_location_ext"][f"{pending_ext}:{v or 'unparsed'}"] += 1
                pending_ext = None
        elif kind == "probe_result":
            p = r.get("params") or {}
            for k in ("ops", "hits", "misses", "memo_hits"):
                try:
                    out["probe"][k] += int(p.get(k, 0))
                except (TypeError, ValueError):
                    pass
            for tok in (p.get("by_op") or "").split():
                name, _, hm = tok.partition("=")
                h, _, m2 = hm.partition("/")
                try:
                    out["probe_by_op"][f"{name}_hit"] += int(h)
                    out["probe_by_op"][f"{name}_miss"] += int(m2)
                except ValueError:
                    pass

    if out["gate1_first_ts"] and out["gate1_last_ts"] and out["gate1"]["requests"] > 1:
        t0 = datetime.datetime.fromisoformat(out["gate1_first_ts"])
        t1 = datetime.datetime.fromisoformat(out["gate1_last_ts"])
        out["gate1"]["seconds_per_candidate"] = round(
            (t1 - t0).total_seconds() / (out["gate1"]["requests"] - 1), 1)
    for k in ("llm_by_source", "gate1_location_ext", "probe_by_op"):
        out[k] = dict(out[k])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="run base dirs, each holding .agent/trace_*.jsonl")
    ap.add_argument("--out", default=None, help="write the JSON here as well as printing")
    a = ap.parse_args()

    snap = {"taken_at": datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds"), "runs": []}
    for base in a.runs:
        r = read_run(base)
        if r is None:
            print(f"  no trace in {base}/.agent/ — skipped", file=sys.stderr)
            continue
        snap["runs"].append(r)
    if not snap["runs"]:
        return 1

    hdr = f"{'run':12} {'probe':>6} {'reason':>14} {'arch':>5} {'gate1':>6} {'conf':>5} {'rej':>5} {'s/cand':>7} {'probe ops':>10} {'miss':>5} {'collect blocks':>15}"
    print(hdr)
    print("-" * len(hdr))
    tot = collections.Counter()
    for r in snap["runs"]:
        g = r["gate1"]
        tot["arch"] += r["llm_by_source"].get("architect", 0)
        tot["g1"] += g["requests"]
        tot["conf"] += g["confirmed"]
        tot["rej"] += g["rejected"]
        tot["ops"] += r["probe"]["ops"]
        tot["miss"] += r["probe"]["misses"]
        tot["blocks"] += r["collect_block_occurrences"]
        print(f"{r['run']:12} {str(r['probe_usable']):>6} {str(r['probe_reason']):>14} "
              f"{r['llm_by_source'].get('architect', 0):>5} {g['requests']:>6} "
              f"{g['confirmed']:>5} {g['rejected']:>5} "
              f"{g.get('seconds_per_candidate', '—'):>7} {r['probe']['ops']:>10} "
              f"{r['probe']['misses']:>5} {r['collect_block_occurrences']:>15}")
    print("-" * len(hdr))
    print(f"{'TOTAL':12} {'':>6} {'':>14} {tot['arch']:>5} {tot['g1']:>6} "
          f"{tot['conf']:>5} {tot['rej']:>5} {'':>7} {tot['ops']:>10} "
          f"{tot['miss']:>5} {tot['blocks']:>15}")
    snap["totals"] = dict(tot)

    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, indent=1, ensure_ascii=False)
        print(f"\n-> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
