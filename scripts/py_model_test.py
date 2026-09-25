#!/usr/bin/env python3
"""Quick Python model test: one task through `kilo run`, 15 checks on the answer.

    python3 scripts/py_model_test.py bynara/ling-3.0-flash-fin-free bynara/ling-3.0-flash-sante-free
    python3 scripts/py_model_test.py --kilo /path/to/kilo --timeout 300 kenary/hy3:free

Reads no keys: the request goes through kilo with kilo's own config.
The model's code runs in a separate process with a timeout, in a temp dir,
with no stdin. When there is no answer, the reason is printed: timeout, kilo's
exit code and the tail of its stderr (401, 404, "Database is busy", ...).

Exit: 0 — every model gave code and got a score; 1 — at least one has no
score (no answer, no code, code crashed).

Direct mode — bypass kilo, hit any OpenAI-compatible endpoint:

    python3 scripts/py_model_test.py --base-url https://api.zyloai.net/v1 --api-key zk_... deepseek-v4

First pass — find a provider's free models (sends nothing to any model):

    python3 scripts/py_model_test.py --find-free vercel_8080 openrouter2 kilo

    # zyloai: pass --base-url and --api-key, use the provider name as a label
    python3 scripts/py_model_test.py --find-free --base-url https://api.zyloai.net/v1 --api-key zk_... zyloai

Prints the text models with tools and price 0, marks which ones kilo already
has under that provider, and the model list for the second pass (a normal run).
Prices come from the provider's public API, no key needed: a provider named
vercel* — ai-gateway.vercel.sh, every endpoint of every model; openrouter* —
openrouter.ai/api/v1/models. A provider named zyloai* (or any name when
--base-url is given) — hits that base URL's /models endpoint directly (needs
--api-key). Anything else — `kilo models <p> --verbose`, where 0 can also
mean an unknown price: such models are printed as "maybe paid", as are models
that have a paid endpoint next to the free one.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

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

# Runs in a separate process: argv[1] is the file with the model's code.
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

# ```python, ```py, ```python3 or a bare ```; \r\n too.
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
    sys.exit("kilo not found: pass --kilo /path/to/kilo")


def run(cmd: list, cwd: str, timeout: int) -> tuple[str, str, int | None]:
    """stdout, stderr, exit code (None = timeout). On timeout the whole process
    group is killed: `kilo run` starts its own server, which must not be left behind."""
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
    """The block with both functions, else the first block, else empty."""
    blocks = FENCE_RE.findall(raw)
    for b in blocks:
        if "def merge_intervals" in b and "def parse_duration" in b:
            return b
    return blocks[0] if blocks else ""


def tail(text: str, n: int = 3) -> str:
    lines = [l.strip() for l in ANSI_RE.sub("", text).splitlines() if l.strip()]
    return " | ".join(lines[-n:])[:300]


# --- first pass: --find-free --------------------------------------------

FREE = "free"
MAYBE = "maybe paid"
VERCEL_API = "https://ai-gateway.vercel.sh/v1/models"
OPENROUTER_API = "https://openrouter.ai/api/v1/models"
# in the model name: ":free", "-free", "/free" — the provider itself calls it free
FREE_NAME_RE = re.compile(r"[:/-]free\b", re.I)


def get_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": "py_model_test"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def is_zero(v) -> bool:
    """Price 0. A nested structure (video tiers etc.) is not zero."""
    if v is None or v == "":
        return True
    if isinstance(v, (list, dict)):
        return not v
    try:
        return float(v) == 0
    except (TypeError, ValueError):
        return False


def paid_fields(pricing: dict) -> dict:
    return {k: v for k, v in (pricing or {}).items()
            if k not in ("discount", "varies_by_provider") and not is_zero(v)}


def kilo_models(kilo: str, provider: str, verbose: bool) -> dict:
    """{model id without provider: metadata or {}} from `kilo models <p>`."""
    cmd = [kilo, "models", provider, "--pure"] + (["--verbose"] if verbose else [])
    out, err, rc = run(cmd, os.getcwd(), 120)
    if rc != 0:
        print(f"  kilo models {provider}: exit {rc} {tail(err)}", file=sys.stderr)
        return {}
    out = ANSI_RE.sub("", out)
    found = {}
    if verbose:
        for m in re.finditer(r"^(\S+?)/(\S+)\n(\{.*?\n\})", out, re.M | re.S):
            try:
                found[m.group(2)] = json.loads(m.group(3))
            except ValueError:
                pass
    else:
        for line in out.splitlines():
            if line.startswith(provider + "/"):
                found[line[len(provider) + 1:].strip()] = {}
    return found


def free_from_vercel() -> list:
    """(id, status, context, note). Every endpoint of every text model."""
    models = [m["id"] for m in get_json(VERCEL_API)["data"] if m.get("type") == "language"]

    def endpoints(mid):
        try:
            url = f"{VERCEL_API}/{urllib.parse.quote(mid)}/endpoints"
            return mid, get_json(url)["data"].get("endpoints") or [], None
        except Exception as e:  # noqa: BLE001 — one model does not fail the search
            return mid, [], str(e)[:80]

    rows, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        for mid, eps, err in ex.map(endpoints, models):
            if err:
                errors.append(f"{mid}: {err}")
                continue
            free = [e for e in eps if e.get("pricing") and not paid_fields(e["pricing"])
                    and "tools" in (e.get("supported_parameters") or [])]
            if not free:
                continue
            paid = [e["provider_name"] for e in eps if e not in free
                    and paid_fields(e.get("pricing"))]
            ctx = max(e.get("context_length") or 0 for e in free)
            if paid:
                rows.append((mid, MAYBE, ctx, "has a paid endpoint: " + ", ".join(paid)))
            else:
                rows.append((mid, FREE, ctx, ""))
    for e in errors:
        print(f"  vercel: could not read {e}", file=sys.stderr)
    print(f"  vercel: checked {len(models)} text models, {len(errors)} errors")
    return rows


def free_from_openrouter() -> list:
    rows = []
    data = get_json(OPENROUTER_API)["data"]
    for m in data:
        arch = m.get("architecture") or {}
        if "text" not in (arch.get("output_modalities") or ["text"]):
            continue
        if "tools" not in (m.get("supported_parameters") or []):
            continue
        if paid_fields(m.get("pricing")):
            continue
        ok = bool(m.get("pricing")) and (FREE_NAME_RE.search(m["id"]) or m["id"].endswith(":free"))
        rows.append((m["id"], FREE if ok else MAYBE, m.get("context_length") or 0,
                     "" if ok else "price 0 but no :free in the name (a router?)"))
    print(f"  openrouter: checked {len(data)} models")
    return rows


def free_from_direct_api(base_url: str, api_key: str) -> list:
    """(id, status, context, note) from an OpenAI-compatible /models endpoint.

    Expects the zyloai shape: {"text": [{id, pricing, capabilities, context_window,
    min_plan, ...}]}. Falls back to the standard {"data": [...]} shape.
    Marks models with all-zero pricing and 'tool-use' capability as FREE when
    min_plan is absent/BASIC, else MAYBE.
    """
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": "py_model_test"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    models = data.get("text") or data.get("data") or []
    rows = []
    for m in models:
        if "tool-use" not in (m.get("capabilities") or []):
            continue
        pricing = m.get("pricing") or {}
        if paid_fields(pricing):
            continue
        ctx = m.get("context_window") or 0
        plan = (m.get("min_plan") or "").upper()
        note = f"min_plan={plan}" if plan and plan not in ("BASIC", "FREE", "") else ""
        status = FREE if plan in ("BASIC", "FREE", "") else MAYBE
        rows.append((m["id"], status, ctx, note))
    print(f"  direct api: checked {len(models)} models")
    return rows


def free_from_kilo(meta: dict) -> list:
    rows = []
    for mid, d in meta.items():
        cost = d.get("cost") or {}
        caps = d.get("capabilities") or {}
        if not caps.get("toolcall"):
            continue
        if not ((caps.get("input") or {}).get("text") and (caps.get("output") or {}).get("text")):
            continue
        if not (is_zero(cost.get("input")) and is_zero(cost.get("output"))
                and all(is_zero(v) for v in (cost.get("cache") or {}).values())):
            continue
        named = bool(FREE_NAME_RE.search(mid))
        rows.append((mid, FREE if named else MAYBE, (d.get("limit") or {}).get("context") or 0,
                     "per kilo" if named else "kilo also shows 0 for an unknown price"))
    return rows


def find_free(kilo: str, providers: list, base_url: str | None = None,
              api_key: str | None = None) -> int:
    for provider in providers:
        print(f"== {provider}", flush=True)
        low = provider.lower()
        direct = base_url and api_key
        try:
            if direct or low.startswith("zyloai"):
                url = base_url or f"https://api.{low}.net/v1"
                if not api_key:
                    print("  --api-key required for direct API provider")
                    continue
                rows, source = free_from_direct_api(url, api_key), f"direct api {url}"
            elif low.startswith("vercel"):
                rows, source = free_from_vercel(), "api vercel"
            elif low.startswith("openrouter"):
                rows, source = free_from_openrouter(), "api openrouter"
            else:
                rows, source = free_from_kilo(kilo_models(kilo, provider, True)), "kilo models"
        except Exception as e:  # noqa: BLE001
            print(f"  could not get the list: {e}")
            continue
        rows.sort(key=lambda r: (r[1] != FREE, r[0]))
        print(f"  price source: {source}; found {len(rows)}\n")
        if not rows:
            print()
            continue
        w = max([len(r[0]) for r in rows] + [6]) + 2
        if direct or low.startswith("zyloai"):
            # no kilo cross-check in direct mode — just list models + second-pass command
            print("model".ljust(w), "status".ljust(12), " context", " note")
            for mid, status, ctx, note in rows:
                print(mid.ljust(w), status.ljust(12), f"{ctx // 1000:>7}k", f"  {note}")
            cmd = (f"python3 scripts/py_model_test.py"
                   f" --base-url {base_url} --api-key {api_key}"
                   f" " + " ".join(r[0] for r in rows))
            print(f"\n  second pass:\n  {cmd}")
        else:
            in_kilo = kilo_models(kilo, provider, False)
            print("model".ljust(w), "status".ljust(12), " context", " in kilo", " note")
            for mid, status, ctx, note in rows:
                mark = "yes" if mid in in_kilo else "no"
                print(mid.ljust(w), status.ljust(12), f"{ctx // 1000:>7}k", f" {mark:<7}", note)
            ready = [f"{provider}/{r[0]}" for r in rows if r[0] in in_kilo]
            missing = [r[0] for r in rows if r[0] not in in_kilo]
            if ready:
                print("\n  second pass:\n  python3 scripts/py_model_test.py " + " ".join(ready))
            if missing:
                print(f"\n  not in kilo under {provider} (add to kilo.jsonc to test them): "
                      + ", ".join(missing))
        print()
    return 0


def ask_direct(base_url: str, api_key: str, model: str, prompt: str, timeout: int) -> str:
    """Send one chat completion request directly to an OpenAI-compatible API.
    Returns the assistant's text content, or raises on HTTP error."""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2048,
    }).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"] or ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("models", nargs="+",
                    help="provider/model as in kilo; with --find-free, provider names")
    ap.add_argument("--kilo", help="path to the kilo binary")
    ap.add_argument("--timeout", type=int, default=240, help="seconds for the model to answer")
    ap.add_argument("--keep", action="store_true",
                    help="keep answers and stderr in ./py_model_test_out/")
    ap.add_argument("--find-free", action="store_true",
                    help="first pass: find the providers' free models, no test")
    ap.add_argument("--base-url", metavar="URL",
                    help="direct mode: OpenAI-compatible base URL (e.g. https://api.zyloai.net/v1)")
    ap.add_argument("--api-key", metavar="KEY",
                    help="direct mode: API key for --base-url")
    args = ap.parse_args()

    if args.base_url and not args.find_free:
        # Direct mode: no kilo, plain HTTP requests
        if not args.api_key:
            sys.exit("--api-key is required with --base-url")
        results = []
        with tempfile.TemporaryDirectory(prefix="pymodeltest-") as work:
            checker = os.path.join(work, "checker.py")
            with open(checker, "w", encoding="utf-8") as f:
                f.write(CHECKER)
            for model in args.models:
                print(f"== {model}", flush=True)
                mdir = tempfile.mkdtemp(prefix="m-", dir=work)
                start = time.time()
                try:
                    raw = ask_direct(args.base_url, args.api_key, model, PROMPT, args.timeout)
                    err_msg = ""
                except urllib.error.HTTPError as e:
                    body = e.read()[:300].decode(errors="replace")
                    raw, err_msg = "", f"HTTP {e.code}: {body}"
                except Exception as e:  # noqa: BLE001
                    raw, err_msg = "", str(e)[:200]
                took = time.time() - start
                if args.keep:
                    os.makedirs("py_model_test_out", exist_ok=True)
                    safe = model.replace("/", "_").replace(":", "_")
                    with open(f"py_model_test_out/{safe}.txt", "w", encoding="utf-8") as f:
                        f.write(raw)
                code = pick_code(raw)
                if not code:
                    why = err_msg or ("empty answer" if not raw.strip() else "no ```python block")
                    print(f"  {why} ({took:.0f}s)")
                    if raw.strip() and not err_msg:
                        print(f"  stdout: {raw.strip()[:200]!r}")
                    results.append((model, "-", took, why))
                    continue
                src = os.path.join(mdir, "answer.py")
                with open(src, "w", encoding="utf-8") as f:
                    f.write(code)
                cout, cerr, crc = run([sys.executable, checker, src], mdir, 30)
                check = cout + cerr
                score = re.search(r"SCORE (\d+/\d+)", check)
                if crc is None:
                    print("  FAIL timeout (model code hung)")
                    results.append((model, "crash", took, "code hung"))
                    continue
                if score:
                    print(check.replace(score.group(0), "").rstrip() or "  all checks passed")
                    results.append((model, score.group(1), took, ""))
                else:
                    print(f"  code crashed: {tail(check, 4)}")
                    results.append((model, "crash", took, "code crashed"))
        print()
        print("model".ljust(44), "score", " time", " reason")
        for model, score, took, why in results:
            print(model.ljust(44), score.ljust(5), f"{took:4.0f}s", "", why)
        return 0 if all(s not in ("-", "crash") for _, s, _, _ in results) else 1

    kilo = find_kilo(args.kilo) if not (args.base_url and args.find_free) else ""
    if args.find_free:
        return find_free(kilo, args.models, base_url=args.base_url, api_key=args.api_key)

    results = []
    with tempfile.TemporaryDirectory(prefix="pymodeltest-") as work:
        checker = os.path.join(work, "checker.py")
        with open(checker, "w", encoding="utf-8") as f:
            f.write(CHECKER)
        for model in args.models:
            print(f"== {model}", flush=True)
            # a separate empty dir per model: files the model creates anyway reach
            # neither the next model nor the current directory
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
                    why = f"timeout {args.timeout}s"
                elif rc != 0:
                    why = f"kilo exit {rc}"
                elif not raw.strip():
                    why = "empty answer"
                else:
                    why = "no ```python block"
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
                print("  FAIL timeout (model code hung)")
                results.append((model, "crash", took, "code hung"))
                continue
            if score:
                print(check.replace(score.group(0), "").rstrip() or "  all checks passed")
                results.append((model, score.group(1), took, ""))
            else:
                print(f"  code crashed: {tail(check, 4)}")
                results.append((model, "crash", took, "code crashed"))

    print()
    print("model".ljust(44), "score", " time", " reason")
    for model, score, took, why in results:
        print(model.ljust(44), score.ljust(5), f"{took:4.0f}s", "", why)
    return 0 if all(s not in ("-", "crash") for _, s, _, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
