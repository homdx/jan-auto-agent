#!/usr/bin/env python3
"""Быстрый тест модели на Python: одна задача через `kilo run`, 15 проверок ответа.

    python3 scripts/py_model_test.py bynara/ling-3.0-flash-fin-free bynara/ling-3.0-flash-sante-free
    python3 scripts/py_model_test.py --kilo /path/to/kilo --timeout 300 kenary/hy3:free

Ключи не читает: запрос идёт через kilo с его собственным конфигом.
Код модели выполняется в отдельном процессе с таймаутом.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

PROMPT = (
    "Write a Python 3 function `merge_intervals(intervals: list[tuple[int,int]]) -> "
    "list[tuple[int,int]]` that merges overlapping or touching closed intervals "
    "(e.g. (1,3),(3,5) -> (1,5)), ignores intervals where start>end by swapping them, "
    "returns sorted result, and does not mutate input. Also `parse_duration(s: str) -> int` "
    'returning seconds for strings like "1h30m", "45s", "2h", "1h2m3s"; raise ValueError '
    'on empty or invalid input like "1x" or "h". Output ONLY one ```python code block, '
    "no explanation, do not create files."
)

# Запускается в отдельном процессе: argv[1] — файл с кодом модели.
CHECKER = r'''
import sys
ns = {}
exec(open(sys.argv[1]).read(), ns)
mi, pd = ns["merge_intervals"], ns["parse_duration"]
ok = tot = 0
def t(name, f):
    global ok, tot
    tot += 1
    try:
        r = bool(f())
    except Exception as e:
        r = False
        name += f" ({type(e).__name__}: {e})"
    ok += r
    if not r:
        print("  FAIL", name)
inp = [(3, 5), (1, 3)]
t("merge touching", lambda: mi(inp) == [(1, 5)])
t("input not mutated", lambda: inp == [(3, 5), (1, 3)])
t("swap reversed", lambda: mi([(5, 1), (6, 8)]) in ([(1, 8)], [(1, 5), (6, 8)]))
t("empty list", lambda: mi([]) == [])
t("nested", lambda: mi([(1, 10), (2, 3)]) == [(1, 10)])
t("disjoint", lambda: mi([(1, 2), (4, 5)]) == [(1, 2), (4, 5)])
t("1h30m", lambda: pd("1h30m") == 5400)
t("45s", lambda: pd("45s") == 45)
t("2h", lambda: pd("2h") == 7200)
t("1h2m3s", lambda: pd("1h2m3s") == 3723)
for bad in ["", "1x", "h", "abc", "1h1h"]:
    def f(b=bad):
        try:
            pd(b)
            return False
        except ValueError:
            return True
    t(f"ValueError on {bad!r}", f)
print(f"SCORE {ok}/{tot}")
'''


def find_kilo(explicit: str | None) -> str:
    if explicit:
        return explicit
    found = shutil.which("kilo")
    if found:
        return found
    hits = sorted(glob.glob(os.path.expanduser(
        "~/.vscode/extensions/kilocode.kilo-code-*/bin/kilo")))
    if hits:
        return hits[-1]
    sys.exit("kilo не найден: укажи --kilo /path/to/kilo")


def ask(kilo: str, model: str, timeout: int, workdir: str) -> tuple[str, float]:
    start = time.time()
    try:
        out = subprocess.run([kilo, "run", "--pure", "-m", model, PROMPT],
                             cwd=workdir, capture_output=True, text=True,
                             timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        out = ""
    return re.sub(r"\x1b\[[0-9;]*m", "", out), time.time() - start


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("models", nargs="+", help="provider/model, как в kilo")
    ap.add_argument("--kilo", help="путь к бинарнику kilo")
    ap.add_argument("--timeout", type=int, default=240, help="секунд на ответ модели")
    ap.add_argument("--keep", action="store_true", help="сохранить ответы в ./py_model_test_out/")
    args = ap.parse_args()
    kilo = find_kilo(args.kilo)

    results = []
    with tempfile.TemporaryDirectory(prefix="pymodeltest-") as work:
        checker = os.path.join(work, "checker.py")
        with open(checker, "w", encoding="utf-8") as f:
            f.write(CHECKER)
        for model in args.models:
            print(f"== {model}", flush=True)
            raw, took = ask(kilo, model, args.timeout, work)
            m = re.search(r"```python\n(.*?)```", raw, re.S)
            code = m.group(1) if m else ""
            if args.keep:
                os.makedirs("py_model_test_out", exist_ok=True)
                safe = model.replace("/", "_").replace(":", "_")
                with open(f"py_model_test_out/{safe}.txt", "w", encoding="utf-8") as f:
                    f.write(raw)
            if not code:
                print(f"  нет блока ```python (ответ за {took:.0f}s: {raw.strip()[:120]!r})")
                results.append((model, "-", took))
                continue
            src = os.path.join(work, "answer.py")
            with open(src, "w", encoding="utf-8") as f:
                f.write(code)
            try:
                r = subprocess.run([sys.executable, checker, src], capture_output=True,
                                   text=True, timeout=30)
                out = r.stdout + r.stderr
            except subprocess.TimeoutExpired:
                out = "  FAIL timeout (код модели завис)\n"
            score = re.search(r"SCORE (\d+/\d+)", out)
            print(out.replace(score.group(0), "").rstrip() if score else out.rstrip())
            results.append((model, score.group(1) if score else "crash", took))

    print("\nмодель".ljust(45), "балл", "время")
    for model, score, took in results:
        print(model.ljust(44), score.ljust(5), f"{took:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
