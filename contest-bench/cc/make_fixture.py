#!/usr/bin/env python3
"""CC-0: the fixture repository and the 80 claims with a known answer.

``build_fixture(dest)`` turns the two trees under ``fixture/base`` and
``fixture/head`` into a two-commit git repository whose shas are the same on
every machine: fixed author, committer and dates, ``GIT_CONFIG_GLOBAL`` and the
system config switched off, files written byte for byte from this checkout.

The base tree seeds 30 behaviours of the kinds our reports are about (a swallowed
exception, a ``0`` on a failed subprocess, ``re.M`` without ``re.S``, a default
read from the wrong section, an off-by-one slice, a shallow copy handed out, a
guard after its action, an unbounded retry). The head commit fixes 20 of them,
leaves 10, renames ``paths.norm_path`` to ``normalize_path`` (the ``GONE`` case)
and brings two regressions of its own (the ``NEW`` case).

Every claim below carries a *probe*: the text whose presence at a commit makes
the claim true there. ``truth_base``/``truth_head`` are computed from the probe
against the built repository, and ``how`` names the lines that decide it, so the
key is a reading of the fixture, re-checked by ``tests/test_claimcheck_bench.py``
— never typed in by hand.

    python3 contest-bench/cc/make_fixture.py --write    # regenerate the JSON files
    python3 contest-bench/cc/make_fixture.py --check    # exit 1 when they are stale
    python3 contest-bench/cc/make_fixture.py --build DIR
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Optional

HERE = Path(__file__).resolve().parent
TREES = HERE / "fixture"
FACTS_FILE = HERE / "fixture_facts.json"
CLAIMS_FILE = HERE / "claims_fixture.json"

KINDS = ("code", "world", "mixed", "dangling", "commit", "ticket")
EXPECTS = ("fixed", "still", "new", "gone", None)

# Fixed identity and clock: the shas depend on nothing but the trees.
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "TZ": "UTC", "LC_ALL": "C",
}
_DATES = {"base": "2026-01-01T00:00:00+00:00", "head": "2026-01-02T00:00:00+00:00"}

BASE_MESSAGE = "fixture: base"


class Fixture(NamedTuple):
    root: Path
    base_sha: str
    head_sha: str


# ── the seeded behaviours and the claims about them ─────────────────────────
#
# One row per claim. ``probe`` is (path, text) for a claim about a file: true at
# a commit iff the file exists there and contains *text*. ``world`` is the truth
# of a claim's world part (a world claim is only that; a mixed claim is the probe
# AND the world part). ``git`` is (args, text) for a claim about a commit: true
# iff ``git <args>`` prints *text*. ``fact`` marks the row as a seeded behaviour
# (listed in fixture_facts.json). ``gone``: the symbol that no longer exists at
# head — the claim is undecided there (truth_head None).

def _c(id, claim, kind, anchors, *, probe=None, world=None, git=None,
       expect=None, fact=None, gone=None, how=None):
    return dict(id=id, claim=claim, kind=kind, anchors=list(anchors), probe=probe,
                world=world, git=git, expect=expect, fact=fact, gone=gone, how=how)


SPEC = [
    # ── code: the 20 behaviours the head commit fixes ───────────────────
    _c("f001", "`config.load_timeout` reads `timeout` from the `[defaults]` section, not from `[pipeline]`.",
       "code", ["config.py", "load_timeout"], probe=("config.py", 'parser.getfloat("defaults", "timeout"'),
       expect="fixed", fact="wrong ini section"),
    _c("f002", "`config.parse_bool` returns True for the string \"0\".",
       "code", ["config.py", "parse_bool"], probe=("config.py", "return bool(value)"),
       expect="fixed", fact="truthiness of a non-empty string"),
    _c("f003", "`config.get_list` does not strip the whitespace of the items it returns.",
       "code", ["config.py", "get_list"], probe=("config.py", 'return [item for item in raw.split(",") if item]'),
       expect="fixed", fact="items not stripped"),
    _c("f004", "`paths.is_inside` compares with a bare `startswith`, so `/a/bc` counts as inside `/a/b`.",
       "code", ["paths.py", "is_inside"], probe=("paths.py", "return child.startswith(parent)\n"),
       expect="fixed", fact="prefix without a separator"),
    _c("f005", "`paths.split_ext` drops the last character of the stem.",
       "code", ["paths.py", "split_ext"], probe=("paths.py", "name[:i - 1]"),
       expect="fixed", fact="off-by-one slice"),
    _c("f006", "`paths.write_marker` writes the file before it checks that the target is inside the root.",
       "code", ["paths.py", "write_marker"],
       probe=("paths.py", "handle.write(text)\n    if not is_inside(target, root):"),
       expect="fixed", fact="guard after the action"),
    _c("f007", "`Store.items` in `store.py` returns a shallow copy, so nested values are shared with the store.",
       "code", ["store.py", "Store.items"], probe=("store.py", "return dict(self._data)"),
       expect="fixed", fact="shallow copy handed out"),
    _c("f008", "`Store.load` turns a file that is not valid JSON into an empty store instead of raising.",
       "code", ["store.py", "Store.load"], probe=("store.py", "except ValueError:\n            self._data = {}"),
       expect="fixed", fact="exception swallowed in one branch"),
    _c("f009", "`Store.get` returns the default when the stored value is falsy, such as 0.",
       "code", ["store.py", "Store.get"], probe=("store.py", "return self._data.get(key) or default"),
       expect="fixed", fact="`or` hides falsy values"),
    _c("f010", "`Store.delete` raises KeyError for a key that is not in the store.",
       "code", ["store.py", "Store.delete"], probe=("store.py", "del self._data[key]"),
       expect="fixed", fact="delete of a missing key raises"),
    _c("f011", "`pipeline.run_step` swallows a KeyError raised by the step and returns None.",
       "code", ["pipeline.py", "run_step"], probe=("pipeline.py", "except KeyError:\n        return None"),
       expect="fixed", fact="exception swallowed in one branch"),
    _c("f012", "`pipeline.run_command` returns 0 when the command fails.",
       "code", ["pipeline.py", "run_command"], probe=("pipeline.py", "if proc.returncode != 0:\n        return 0"),
       expect="fixed", fact="0 on a failed subprocess"),
    _c("f013", "`pipeline.parse_header` reads only the first line of a `Files:` field that wraps onto the next line.",
       "code", ["pipeline.py", "parse_header"], probe=("pipeline.py", 're.search(r"^Files:\\s*(.+)$", text, re.M)'),
       expect="fixed", fact="re.M without re.S reads one line"),
    _c("f014", "`pipeline.count_steps` never counts the first step.",
       "code", ["pipeline.py", "count_steps"], probe=("pipeline.py", "range(1, len(steps))"),
       expect="fixed", fact="off-by-one range"),
    _c("f015", "`gates.check_exit` treats pytest's exit code 5 as a pass.",
       "code", ["gates.py", "check_exit"], probe=("gates.py", "returncode in (0, 5)"),
       expect="fixed", fact="exit 5 passes"),
    _c("f016", "`gates.gate_paths` reports a file under a declared directory such as `docs/` as undeclared.",
       "code", ["gates.py", "gate_paths"], probe=("gates.py", "if f not in declared]"),
       expect="fixed", fact="declared directory not a prefix"),
    _c("f017", "`gates.run_gate` applies the changes before it checks `allowed`.",
       "code", ["gates.py", "run_gate"], probe=("gates.py", "apply(changes)\n    if not allowed:"),
       expect="fixed", fact="guard after the action"),
    _c("f018", "`retry.retry_call` retries forever; it has no limit on the number of attempts.",
       "code", ["retry.py", "retry_call"], probe=("retry.py", "while True:"),
       expect="fixed", fact="retry loop without a bound"),
    _c("f019", "`retry.retry_call` retries on any Exception, not only on TransientError.",
       "code", ["retry.py", "retry_call"], probe=("retry.py", "except Exception:\n            attempt += 1"),
       expect="fixed", fact="retries a permanent error"),
    _c("f020", "`retry.is_transient` matches \"timeout\" case-sensitively, so \"Timeout\" is not transient.",
       "code", ["retry.py", "is_transient"], probe=("retry.py", "text = str(exc)\n"),
       expect="fixed", fact="case-sensitive match"),
    # ── code: the 10 behaviours the head commit leaves ──────────────────
    _c("f021", "`config.load_workers` returns 0 when `workers` is not an integer.",
       "code", ["config.py", "load_workers"], probe=("config.py", "except ValueError:\n        return 0"),
       expect="still", fact="bad value silently 0"),
    _c("f022", "`config.read_config` opens the file and never closes it.",
       "code", ["config.py", "read_config"], probe=("config.py", 'handle = open(path, encoding="utf-8")'),
       expect="still", fact="file handle leaked"),
    _c("f023", "`paths.safe_join` does not reject an absolute `name`, so its result can leave `root`.",
       "code", ["paths.py", "safe_join"], probe=("paths.py", "return os.path.join(root, name)"),
       expect="still", fact="absolute name escapes the root"),
    _c("f024", "`Store.save` writes the file in place, not through a temporary file and a rename.",
       "code", ["store.py", "Store.save"], probe=("store.py", 'with open(self.path, "w"'),
       expect="still", fact="non-atomic write"),
    _c("f025", "`pipeline.run_all` stops at the first failing step and drops the steps after it.",
       "code", ["pipeline.py", "run_all"], probe=("pipeline.py", "except StepError:\n            break"),
       expect="still", fact="stops at the first failure"),
    _c("f026", "`pipeline.log_step` prints with `print()` rather than through `logging`.",
       "code", ["pipeline.py", "log_step"], probe=("pipeline.py", 'print(f"{name}: {status}")'),
       expect="still", fact="print instead of logging"),
    _c("f027", "`gates.gate_size` adds the deleted lines to the added lines before comparing with the limit.",
       "code", ["gates.py", "gate_size"], probe=("gates.py", "abs(added) + abs(deleted) <= limit"),
       expect="still", fact="deletions count as size"),
    _c("f028", "`gates.gate_flaky` reports `PASS*` when the rerun passes, without checking that `failed_ids` holds as many ids as `bad` tests failed.",
       "code", ["gates.py", "gate_flaky"], probe=("gates.py", 'if rerun(failed_ids) == 0:\n        return f"PASS*{bad}"'),
       expect="still", fact="PASS* without comparing the ids with the failed count"),
    _c("f029", "`retry.backoff` has no upper bound on the wait it returns.",
       "code", ["retry.py", "backoff"], probe=("retry.py", "return base * 2 ** attempt"),
       expect="still", fact="uncapped backoff"),
    _c("f030", "`retry.with_deadline` checks the deadline only after the call has returned.",
       "code", ["retry.py", "with_deadline"], probe=("retry.py", "result = fn()\n    if clock() > deadline:"),
       expect="still", fact="deadline checked after the call"),
    # ── code: the renamed function (GONE) and the two regressions (NEW) ──
    _c("f031", "`paths.norm_path` lower-cases the path it is given.",
       "code", ["paths.py", "norm_path"], probe=("paths.py", 'def norm_path(path):\n    """Lower-case *path* and drop trailing slashes."""\n    return path.lower()'),
       expect="gone", gone=("paths.py", "def norm_path("), fact="renamed at head"),
    _c("f032", "`paths.norm_path` strips every trailing slash, not just one.",
       "code", ["paths.py", "norm_path"], probe=("paths.py", 'def norm_path(path):\n    """Lower-case *path* and drop trailing slashes."""\n    return path.lower().rstrip("/")'),
       expect="gone", gone=("paths.py", "def norm_path(")),
    _c("f033", "`Store.save` writes the JSON without a trailing newline.",
       "code", ["store.py", "Store.save"], probe=("store.py", "ensure_ascii=False))\n"),
       expect="new", fact="regression: newline dropped"),
    _c("f034", "`retry.retry_call` waits the same `delay` before every retry instead of backing off.",
       "code", ["retry.py", "retry_call"], probe=("retry.py", "            sleep(delay)\n"),
       expect="new", fact="regression: backoff dropped"),
    # ── code: near misses, the right function and a wrong detail ────────
    _c("f035", "`config.load_timeout` falls back to 60 seconds when the key is unset.",
       "code", ["config.py", "load_timeout"], probe=("config.py", "fallback=60")),
    _c("f036", "`config.load_name` returns None when `name` is unset.",
       "code", ["config.py", "load_name"], probe=("config.py", "fallback=None")),
    _c("f037", "`config.get_list` splits the value on semicolons.",
       "code", ["config.py", "get_list"], probe=("config.py", 'split(";")')),
    _c("f038", "`paths.is_inside` resolves symlinks with `os.path.realpath` before comparing.",
       "code", ["paths.py", "is_inside"], probe=("paths.py", "realpath")),
    _c("f039", "`paths.split_ext` treats the whole of `.bashrc` as an extension.",
       "code", ["paths.py", "split_ext"], probe=("paths.py", "if i < 0:")),
    _c("f040", "`paths.marker_names` returns the marker files in modification-time order.",
       "code", ["paths.py", "marker_names"], probe=("paths.py", "getmtime")),
    _c("f041", "`Store.load` raises StoreError when the file does not exist.",
       "code", ["store.py", "Store.load"], probe=("store.py", "except FileNotFoundError:\n            raise")),
    _c("f042", "`Store.put` refuses to overwrite a key that is already in the store.",
       "code", ["store.py", "Store.put"], probe=("store.py", "if key in self._data:\n            raise")),
    _c("f043", "`Store.__len__` counts only the keys whose value is not None.",
       "code", ["store.py", "Store.__len__"], probe=("store.py", "is not None")),
    _c("f044", "`pipeline.run_command` passes `check=True`, so a failing command raises CalledProcessError.",
       "code", ["pipeline.py", "run_command"], probe=("pipeline.py", "check=True")),
    _c("f045", "`pipeline.run_step` retries a failing step once before raising StepError.",
       "code", ["pipeline.py", "run_step"], probe=("pipeline.py", "retry")),
    _c("f046", "`pipeline.parse_header` matches the `Files:` label case-insensitively.",
       "code", ["pipeline.py", "parse_header"], probe=("pipeline.py", "re.I")),
    _c("f047", "`gates.check_exit` treats exit code 1 as a pass.",
       "code", ["gates.py", "check_exit"], probe=("gates.py", "(0, 1")),
    _c("f048", "`gates.summary_line` prints `PASS` for a gate that passed.",
       "code", ["gates.py", "summary_line"], probe=("gates.py", "'PASS' if ok")),
    _c("f049", "`retry.backoff` starts from a base of 1 second by default.",
       "code", ["retry.py", "backoff"], probe=("retry.py", "base=1")),
    _c("f050", "`retry.with_deadline` uses `time.time` as its default clock.",
       "code", ["retry.py", "with_deadline"], probe=("retry.py", "clock=time.time")),
    # ── world ───────────────────────────────────────────────────────────
    _c("f051", "In Python's `re` module, `re.M` makes `^` and `$` match at every line boundary.",
       "world", [], world=True, how="world fact: re.MULTILINE (Python docs, `re`)"),
    _c("f052", "`subprocess.run` does not raise on a non-zero exit status unless `check=True` is passed.",
       "world", [], world=True, how="world fact: subprocess.run (Python docs)"),
    _c("f053", "`copy.copy` of a dict that holds lists shares those lists with the original.",
       "world", [], world=True, how="world fact: shallow copy (Python docs, `copy`)"),
    _c("f054", "`configparser` section names are case-sensitive.",
       "world", [], world=True, how="world fact: configparser docs (sections are case-sensitive, keys are not)"),
    _c("f055", "`json.dumps` escapes non-ASCII characters by default.",
       "world", [], world=True, how="world fact: ensure_ascii defaults to True"),
    _c("f056", "`re.S` makes `^` match at the start of every line.",
       "world", [], world=False, how="world fact: re.S is DOTALL, `.` matches newline; `^` is re.M"),
    _c("f057", "`str.rstrip('/')` removes at most one trailing slash.",
       "world", [], world=False, how="world fact: rstrip removes every trailing character in the set"),
    _c("f058", "`dict.get(key, default)` returns the default when the stored value is None.",
       "world", [], world=False, how="world fact: get returns the stored None; default only for a missing key"),
    _c("f059", "Python's `range(1, n)` yields n items.",
       "world", [], world=False, how="world fact: range(1, n) yields n - 1 items"),
    _c("f060", "`except Exception` catches KeyboardInterrupt.",
       "world", [], world=False, how="world fact: KeyboardInterrupt derives from BaseException"),
    # ── mixed: a code part and a world part, both must hold ─────────────
    _c("f061", "`pipeline.run_command` passes `check=False` to `subprocess.run`, which then does not raise on a non-zero exit.",
       "mixed", ["pipeline.py", "run_command"], probe=("pipeline.py", "check=False"), world=True),
    _c("f062", "`pipeline.parse_header` uses `re.M` without `re.S`, so `.` in its pattern does not match a newline.",
       "mixed", ["pipeline.py", "parse_header"], probe=("pipeline.py", "text, re.M)"), world=True),
    _c("f063", "`Store.save` passes `ensure_ascii=False`, so non-ASCII characters are written as themselves.",
       "mixed", ["store.py", "Store.save"], probe=("store.py", "ensure_ascii=False"), world=True),
    _c("f064", "`config.read_config` uses `configparser.ConfigParser`, whose option names are case-sensitive.",
       "mixed", ["config.py", "read_config"], probe=("config.py", "configparser.ConfigParser()"), world=False),
    _c("f065", "`paths.split_ext` uses `str.rfind`, which returns -1 when the dot is absent.",
       "mixed", ["paths.py", "split_ext"], probe=("paths.py", 'name.rfind(".")'), world=True),
    _c("f066", "`gates.check_exit` accepts exit code 5, which is the code pytest uses for an internal error.",
       "mixed", ["gates.py", "check_exit"], probe=("gates.py", "returncode in (0, 5)"), world=False),
    _c("f067", "`retry.retry_call` uses `time.sleep` as its default sleep, which takes seconds as a float.",
       "mixed", ["retry.py", "retry_call"], probe=("retry.py", "sleep=time.sleep"), world=True),
    _c("f068", "`Store.items` builds its copy with `dict()`, and `dict()` of a dict copies the nested lists too.",
       "mixed", ["store.py", "Store.items"], probe=("store.py", "return dict(self._data)"), world=False),
    # ── dangling: a path or symbol that does not exist at either commit ─
    _c("f069", "`scheduler.py` runs the pipeline's steps in parallel threads.",
       "dangling", ["scheduler.py"], probe=("scheduler.py", "")),
    _c("f070", "`Store.compact` in `store.py` removes the keys whose value is None.",
       "dangling", ["store.py", "Store.compact"], probe=("store.py", "def compact(")),
    _c("f071", "`gates.check_coverage` fails a change whose coverage is below 80 %.",
       "dangling", ["gates.py", "check_coverage"], probe=("gates.py", "def check_coverage(")),
    _c("f072", "`retry.retry_async` awaits the call between retries.",
       "dangling", ["retry.py", "retry_async"], probe=("retry.py", "def retry_async(")),
    _c("f073", "`config.load_profile` reads the `[profile]` section.",
       "dangling", ["config.py", "load_profile"], probe=("config.py", "def load_profile(")),
    _c("f074", "`paths.expand_user` expands a leading `~` in a path.",
       "dangling", ["paths.py", "expand_user"], probe=("paths.py", "def expand_user(")),
    # ── tickets and commits by name ─────────────────────────────────────
    _c("f075", "Ticket `epic-tasks/01-store-errors.md` has the status done.",
       "ticket", ["epic-tasks/01-store-errors.md"], probe=("epic-tasks/01-store-errors.md", "**Status:** done")),
    _c("f076", "Ticket `epic-tasks/02-store-get.md` declares `store.py` in its **File:** field.",
       "ticket", ["epic-tasks/02-store-get.md"], probe=("epic-tasks/02-store-get.md", "**File:** `store.py`")),
    _c("f077", "Ticket `epic-tasks/03-retry-bound.md` is marked done.",
       "ticket", ["epic-tasks/03-retry-bound.md"], probe=("epic-tasks/03-retry-bound.md", "**Status:** done")),
    _c("f078", "Commit {base7} adds `retry.py`.",
       "commit", ["{base7}"], git=(["show", "--stat", "--format=", "{base}"], "retry.py ")),
    _c("f079", "Commit {base7} changes only `config.py`.",
       "commit", ["{base7}"], git=(["show", "--stat", "--format=", "{base}"], " 1 file changed")),
    _c("f080", "The subject of commit {base7} is `fixture: base`.",
       "commit", ["{base7}"], git=(["log", "-1", "--format=%s", "{base}"], BASE_MESSAGE + "\n")),
]


def _head_message() -> str:
    lines = ["fixture: fix 20 seeded behaviours, rename norm_path", ""]
    for row in SPEC:
        if row["expect"] == "fixed":
            lines.append(f"- {row['anchors'][0]}::{row['anchors'][1]}: {row['fact']}")
    lines.append("- paths.py: norm_path is now normalize_path")
    return "\n".join(lines) + "\n"


# ── building the repository ────────────────────────────────────────────────

def _tree_files(tree: Path) -> dict[str, bytes]:
    return {p.relative_to(tree).as_posix(): p.read_bytes()
            for p in sorted(tree.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}


def _git(root: Path, *args: str, date: str = _DATES["base"]) -> str:
    env = {**os.environ, **_GIT_ENV, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)
    return subprocess.run(["git", *args], cwd=root, env=env, check=True,
                          capture_output=True, text=True).stdout


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel in _git(root, "ls-files").splitlines():
        if rel not in files:
            (root / rel).unlink()
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o644)


def build_fixture(dest: Path) -> Fixture:
    """A fresh git repository at *dest* (created; must be empty or absent)."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise FileExistsError(f"{dest} is not empty")
    _git(dest, "init", "-q", "-b", "main")
    _write_tree(dest, _tree_files(TREES / "base"))
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", BASE_MESSAGE, date=_DATES["base"])
    base = _git(dest, "rev-parse", "HEAD").strip()
    _write_tree(dest, _tree_files(TREES / "head"))
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", _head_message(), date=_DATES["head"])
    head = _git(dest, "rev-parse", "HEAD").strip()
    return Fixture(dest, base, head)


# ── reading the repository: probes, lines, truth ───────────────────────────

def _show(fx: Fixture, sha: str, path: str) -> Optional[str]:
    try:
        return _git(fx.root, "show", f"{sha}:{path}")
    except subprocess.CalledProcessError:
        return None


def _subst(text: str, fx: Fixture) -> str:
    return text.replace("{base7}", fx.base_sha[:7]).replace("{base}", fx.base_sha)


def _lines_of(text: str, needle: str) -> Optional[tuple[int, int]]:
    """1-based inclusive span of the first occurrence of *needle* in *text*."""
    i = text.find(needle)
    if i < 0 or not needle:
        return None
    start = text.count("\n", 0, i) + 1
    body = needle[:-1] if needle.endswith("\n") else needle
    return start, start + body.count("\n")


def _symbol_span(text: str, qualname: str) -> Optional[tuple[int, int]]:
    """The definition span of ``f`` or ``Class.method`` in *text*."""
    parts = qualname.split(".")
    nodes = ast.parse(text).body
    found = None
    for part in parts:
        found = next((n for n in nodes if isinstance(n, (ast.FunctionDef, ast.ClassDef))
                      and n.name == part), None)
        if found is None:
            return None
        nodes = found.body
    return found.lineno, found.end_lineno


def _truth_at(row: dict, fx: Fixture, sha: str) -> Optional[bool]:
    if row["kind"] == "world":
        return row["world"]
    if row["gone"] and sha == fx.head_sha:
        path, text = row["gone"]
        source = _show(fx, sha, path) or ""
        if text not in source:
            return None
    if row["git"]:
        args, text = row["git"]
        out = _git(fx.root, *(_subst(a, fx) for a in args))
        code = _subst(text, fx) in out
    else:
        path, text = row["probe"]
        source = _show(fx, sha, path)
        code = source is not None and bool(text) and text in source
    return code and row["world"] if row["kind"] == "mixed" else code


def _how(row: dict, fx: Fixture) -> str:
    """The reading that settles the claim, with the deciding lines."""
    if row["how"]:
        return row["how"]
    if row["git"]:
        args, _ = row["git"]
        return "git " + " ".join(_subst(a, fx) for a in args)
    path, text = row["probe"]
    symbol = row["anchors"][1] if len(row["anchors"]) > 1 else ""
    parts = []
    for side, sha in (("base", fx.base_sha), ("head", fx.head_sha)):
        source = _show(fx, sha, path)
        if source is None:
            parts.append(f"{side}: no file {path}")
            continue
        span = _lines_of(source, text)
        if span is None and symbol and path.endswith(".py"):
            span = _symbol_span(source, symbol)
            if span is None:
                parts.append(f"{side}: no `{symbol}` in {path}")
                continue
        if span is None:
            parts.append(f"{side}: read {path} (the text is absent)")
        else:
            parts.append(f"{side}: read {path}:{span[0]}-{span[1]}")
    if row["kind"] == "mixed":
        parts.append("plus the world part: " + ("true" if row["world"] else "false"))
    return "; ".join(parts)


def facts(fx: Fixture) -> list[dict]:
    """fixture_facts.json: every seeded behaviour, its file, symbol and lines."""
    out = []
    for row in SPEC:
        if not row["fact"]:
            continue
        path, text = row["probe"]
        sha = fx.head_sha if row["expect"] == "new" else fx.base_sha
        span = _lines_of(_show(fx, sha, path), text)
        out.append({"id": row["id"], "kind": row["expect"], "what": row["fact"],
                    "file": path, "symbol": row["anchors"][1],
                    "at": "head" if row["expect"] == "new" else "base",
                    "text": text, "lines": list(span)})
    return out


def claims(fx: Fixture) -> list[dict]:
    out = []
    for row in SPEC:
        tb, th = _truth_at(row, fx, fx.base_sha), _truth_at(row, fx, fx.head_sha)
        out.append({
            "id": row["id"], "claim": _subst(row["claim"], fx), "kind": row["kind"],
            "truth": tb, "truth_base": tb, "truth_head": th,
            "anchors": [_subst(a, fx) for a in row["anchors"]],
            "expect": row["expect"], "how": _how(row, fx),
        })
    return out


def render(fx: Fixture) -> tuple[str, str]:
    """The two JSON files, as text."""
    shas = {"base_sha": fx.base_sha, "head_sha": fx.head_sha}
    fact_doc = {**shas, "facts": facts(fx)}
    claim_doc = {"set": "fixture", **shas,
                 "note": "truth_base/truth_head computed by make_fixture.py from the probes; "
                         "`truth` repeats truth_base for scripts/claim_vote.py",
                 "claims": claims(fx)}
    dump = lambda d: json.dumps(d, ensure_ascii=False, indent=1) + "\n"  # noqa: E731
    return dump(fact_doc), dump(claim_doc)


def main(argv: Optional[list] = None) -> int:
    import tempfile
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true", help="regenerate fixture_facts.json and claims_fixture.json")
    g.add_argument("--check", action="store_true", help="exit 1 when the committed files are stale")
    g.add_argument("--build", type=Path, metavar="DIR", help="build the repository in DIR and print the shas")
    args = ap.parse_args(argv)
    if args.build:
        fx = build_fixture(args.build)
        print(f"{fx.root} base={fx.base_sha} head={fx.head_sha}")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        fact_text, claim_text = render(build_fixture(Path(tmp) / "fx"))
    if args.write:
        FACTS_FILE.write_text(fact_text, encoding="utf-8")
        CLAIMS_FILE.write_text(claim_text, encoding="utf-8")
        print(f"wrote {FACTS_FILE.name}, {CLAIMS_FILE.name}")
        return 0
    stale = [f.name for f, t in ((FACTS_FILE, fact_text), (CLAIMS_FILE, claim_text))
             if not f.exists() or f.read_text(encoding="utf-8") != t]
    if stale:
        print("stale: " + ", ".join(stale) + " (run make_fixture.py --write)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
