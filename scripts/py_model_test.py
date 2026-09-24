#!/usr/bin/env python3
"""Быстрый тест модели на Python: одна задача через `kilo run`, 15 проверок ответа.

    python3 scripts/py_model_test.py bynara/ling-3.0-flash-fin-free bynara/ling-3.0-flash-sante-free
    python3 scripts/py_model_test.py --kilo /path/to/kilo --timeout 300 kenary/hy3:free

Ключи не читает: запрос идёт через kilo с его собственным конфигом.
Код модели выполняется в отдельном процессе с таймаутом, во временной папке,
без stdin. Когда ответа нет, печатается причина: таймаут, код возврата kilo и
хвост его stderr (401, 404, "Database is busy" и т.п.).

Выход: 0 — каждая модель дала код и получила балл; 1 — хотя бы у одной нет
балла (нет ответа, нет кода, код упал).
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

PROMPT = (
    "Write a Python 3 function `merge_intervals(intervals: list[tuple[int,int]]) -> "
    "list[tuple[int,int]]` that merges overlapping or touching closed intervals "
    "(e.g. (1,3),(3,5) -> (1,5)), ignores intervals where start>end by swapping them, "
    "returns sorted result, and does not mutate input. Also `parse_duration(s: str) -> int` "
    'returning seconds for strings like "1h30m", "45s", "2h", "1h2m3s" (units h, m, s, '
    "each at most once, in that order); raise ValueError "
    'on empty or invalid input like "1x", "h" or "1h1h". Output ONLY one ```python code block, '
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
t("swap reversed", lambda: mi([(5, 1), (6, 8)]) == [(1, 5), (6, 8)])
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

# ```python, ```py, ```python3 или голые ```; \r\n тоже.
FENCE_RE = re.compile(r"```[ \t]*(?:python3?|py)?[ \t]*\r?\n(.*?)```", re.S | re.I)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


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


def run(cmd: list, cwd: str, timeout: int) -> tuple[str, str, int | None]:
    """stdout, stderr, код возврата (None — таймаут). По таймауту убивается вся
    группа процессов: `kilo run` поднимает свой сервер, он не должен остаться."""
    p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
        return out, err, p.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = p.communicate()
        return out, err, None


def pick_code(raw: str) -> str:
    """Блок с обеими функциями, иначе первый блок, иначе пусто."""
    blocks = FENCE_RE.findall(raw)
    for b in blocks:
        if "def merge_intervals" in b and "def parse_duration" in b:
            return b
    return blocks[0] if blocks else ""


def tail(text: str, n: int = 3) -> str:
    lines = [l.strip() for l in ANSI_RE.sub("", text).splitlines() if l.strip()]
    return " | ".join(lines[-n:])[:300]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("models", nargs="+", help="provider/model, как в kilo")
    ap.add_argument("--kilo", help="путь к бинарнику kilo")
    ap.add_argument("--timeout", type=int, default=240, help="секунд на ответ модели")
    ap.add_argument("--keep", action="store_true",
                    help="сохранить ответы и stderr в ./py_model_test_out/")
    args = ap.parse_args()
    kilo = find_kilo(args.kilo)

    results = []
    with tempfile.TemporaryDirectory(prefix="pymodeltest-") as work:
        checker = os.path.join(work, "checker.py")
        with open(checker, "w", encoding="utf-8") as f:
            f.write(CHECKER)
        for model in args.models:
            print(f"== {model}", flush=True)
            # отдельная пустая папка на модель: файлы, которые модель всё же
            # создаст, не попадут ни к следующей модели, ни в текущую папку
            mdir = tempfile.mkdtemp(prefix="m-", dir=work)
            start = time.time()
            out, err, rc = run([kilo, "run", "--pure", "-m", model, PROMPT], mdir, args.timeout)
            took = time.time() - start
            raw = ANSI_RE.sub("", out)
            if args.keep:
                os.makedirs("py_model_test_out", exist_ok=True)
                safe = model.replace("/", "_").replace(":", "_")
                with open(f"py_model_test_out/{safe}.txt", "w", encoding="utf-8") as f:
                    f.write(raw)
                with open(f"py_model_test_out/{safe}.stderr.txt", "w", encoding="utf-8") as f:
                    f.write(err)
            code = pick_code(raw)
            if not code:
                if rc is None:
                    why = f"таймаут {args.timeout}s"
                elif rc != 0:
                    why = f"kilo exit {rc}"
                elif not raw.strip():
                    why = "пустой ответ"
                else:
                    why = "нет блока ```python"
                print(f"  {why} ({took:.0f}s)")
                if raw.strip():
                    print(f"  stdout: {raw.strip()[:200]!r}")
                if tail(err):
                    print(f"  stderr: {tail(err)}")
                results.append((model, "-", took, why))
                continue
            src = os.path.join(mdir, "answer.py")
            with open(src, "w", encoding="utf-8") as f:
                f.write(code)
            cout, cerr, crc = run([sys.executable, checker, src], mdir, 30)
            check = cout + cerr
            score = re.search(r"SCORE (\d+/\d+)", check)
            if crc is None:
                print("  FAIL timeout (код модели завис)")
                results.append((model, "crash", took, "код завис"))
                continue
            if score:
                print(check.replace(score.group(0), "").rstrip() or "  все проверки пройдены")
                results.append((model, score.group(1), took, ""))
            else:
                print(f"  код упал: {tail(check, 4)}")
                results.append((model, "crash", took, "код упал"))

    print()
    print("модель".ljust(44), "балл ", "время", " причина")
    for model, score, took, why in results:
        print(model.ljust(44), score.ljust(5), f"{took:4.0f}s", "", why)
    return 0 if all(s not in ("-", "crash") for _, s, _, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
