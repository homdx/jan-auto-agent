#!/usr/bin/env python3
"""
view_trace.py — read the inter-agent trace produced by tools/agent_trace.py.

Usage:
    python view_trace.py [agent_trace.jsonl]          # flow summary, all runs
    python view_trace.py agent_trace.jsonl --run ID    # one run only
    python view_trace.py agent_trace.jsonl --full      # include full prompt/response content
    python view_trace.py agent_trace.jsonl --run ID --full

Each line in the trace is one event: who sent it, to whom, the kind, the
parameters the call was made with, and the content (rendered prompt / model
reply / parsed result).
"""
import json
import sys


def load(path):
    events = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def main():
    args = sys.argv[1:]
    path = "agent_trace.jsonl"
    run_filter = None
    full = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--run":
            run_filter = args[i + 1]; i += 2; continue
        if a == "--full":
            full = True; i += 1; continue
        path = a; i += 1

    try:
        events = load(path)
    except FileNotFoundError:
        print(f"No trace file at: {path}")
        return

    if run_filter:
        events = [e for e in events if e.get("run_id") == run_filter]

    current_run = object()
    for e in events:
        if e.get("run_id") != current_run:
            current_run = e.get("run_id")
            print(f"\n=== run {current_run} ===")
        ts = e.get("ts", "")[11:19]
        head = f"{e['seq']:>3} {ts}  {e['source']:>17} -> {e['target']:<17} {e['kind']}"
        meta = []
        if "model" in e:
            meta.append(f"model={e['model']}")
        if "temperature" in e:
            meta.append(f"temp={e['temperature']}")
        if "max_tokens" in e:
            meta.append(f"max_tokens={e['max_tokens']}")
        if meta:
            head += "  [" + " ".join(meta) + "]"
        print(head)

        if "params" in e:
            keys = ", ".join(e["params"].keys())
            print(f"        params: {keys}")
            if full:
                for k, v in e["params"].items():
                    print(f"          {k} = {v}")
        if "content" in e:
            if full:
                print("        content:")
                for ln in str(e["content"]).splitlines():
                    print(f"          {ln}")
            else:
                preview = str(e["content"]).replace("\n", " ")[:100]
                print(f"        content: {preview}")


if __name__ == "__main__":
    main()
