"""Round 184: the acceptance results.tsv as one markdown table, a tree per row."""
import collections
import statistics
import sys

WANT = {"quiet": "pass", "load": "pass", "m-nokill": "fail", "m-noname": "fail",
        "m-nobudget": "fail", "file": "pass", "m-noname-ld": "fail"}
cells = collections.defaultdict(list)
order = []
for line in open(sys.argv[1], encoding="utf-8"):
    name, case, _i, outcome, secs = line.rstrip("\n").split("\t")
    if name not in order:
        order.append(name)
    cells[name, case].append((outcome, secs))

cols = list(WANT)
print("| tree | " + " | ".join(cols) + " | score |")
print("|---" * (len(cols) + 2) + "|")
for name in order:
    row, score = [], 0
    for c in cols:
        got = cells.get((name, c), [])
        ok = sum(o == WANT[c] for o, _ in got)
        cell = f"{ok}/{len(got)}"
        skips = sum(o == "skip" for o, _ in got)
        if skips:
            cell += f" ({skips} skip)"
        if c in ("load", "quiet"):
            try:
                cell += f" · med {statistics.median(float(s) for _, s in got):.1f}s"
            except ValueError:
                pass
        score += ok == len(got) and bool(got)
        row.append(cell)
    print(f"| {name} | " + " | ".join(row) + f" | {score}/{len(cols)} |")
