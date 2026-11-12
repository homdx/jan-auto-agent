#!/usr/bin/env python3
"""contest-bench/slow1/check_slow1.py — SLOW-1: what does an entry's commit fix?

The mechanical half of judging round 119 against REFERENCE.md — no LLM, no
network, no timing (time the suite yourself, on one quiet machine):

* cause 1 — the runner harness sets ``error_retry_backoff_sec=0``;
* cause 2 — ``tests/test_contest_runner.py`` has no file-wide
  ``port_bound_http_servers`` mark;
* cause 3 (bonus) — the two collect test files build their index once;
* no test file the commit touches lost an ``assert``;
* the commit message names the base sha and seconds.

The base is the ticket commit (``SLOW-1: ticket``) below COMMIT.

Usage:  python3 contest-bench/slow1/check_slow1.py <TARGET> <COMMIT>
Exit code 0 when the regression (causes 1 and 2) is gone and the hard rules
hold, 1 otherwise. Cause 3 is reported, never required.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

RUNNER_TESTS = "tests/test_contest_runner.py"
COLLECT_TESTS = ("tests/test_collect_already_safe_query.py", "tests/test_collect_gates.py")
TICKET_SUBJECT = "SLOW-1: ticket"
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _git(target: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=target, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _show(target: Path, rev: str, path: str) -> str:
    return _git(target, "show", f"{rev}:{path}")


def backoff_zero(src: str) -> bool:
    """make_config returns an object whose `error_retry_backoff_sec` is the number 0 — in code, not in a comment or a string.

    The function is found in the parsed file (a decorator on the next function is no part of it). What counts
    is the setting of the *returned* object: a keyword of the returned call, a dict key of the returned literal,
    or — for a returned name — the call or dict it was bound to, then any attribute or key assigned to it later
    (plain or annotated), an `.update(...)` or a `setattr` on it.
    A setting on an object that is never returned is ignored. A return of another shape falls back to the last
    setting written anywhere in the function.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    func = next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "make_config"), None)
    if func is None:
        return False
    name = "error_retry_backoff_sec"

    def zero(node) -> bool:
        return (isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
                and not isinstance(node.value, bool) and node.value == 0)

    def own_nodes():
        # the function's own statements, not those of a nested function, lambda or class
        stack = list(func.body)
        while stack:
            n = stack.pop()
            if isinstance(n, _SCOPES):
                continue
            yield n
            if isinstance(n, ast.If) and isinstance(n.test, ast.Constant):
                stack.extend(n.body if n.test.value else n.orelse)
            elif isinstance(n, ast.While) and isinstance(n.test, ast.Constant) and not n.test.value:
                stack.extend(n.orelse)
            else:
                stack.extend(ast.iter_child_nodes(n))

    nodes = list(own_nodes())

    def event(value_node):
        return (value_node.lineno, value_node.col_offset, zero(value_node))

    def of_value(node, before, depth):
        """Settings (line, col, is_zero) of the object *node* builds or names."""
        if isinstance(node, ast.Call):
            own = [event(k.value) for k in node.keywords if k.arg == name]
            if own or depth > 2:
                return own
            names = [a for a in (*node.args, *(k.value for k in node.keywords)) if isinstance(a, ast.Name)]
            return [e for a in names for e in of_value(a, node.lineno, depth + 1)]
        if isinstance(node, ast.Dict):
            return [event(v) for k, v in zip(node.keys, node.values)
                    if isinstance(k, ast.Constant) and k.value == name]
        if isinstance(node, ast.Name) and depth <= 2:
            return of_name(node.id, before, depth)
        return []

    def is_ident(node, ident):
        return isinstance(node, ast.Name) and node.id == ident

    def sets_name(target, ident):
        """`ident.<name>` or `ident["<name>"]` as an assignment target."""
        return (isinstance(target, ast.Attribute) and target.attr == name and is_ident(target.value, ident)) or (
            isinstance(target, ast.Subscript) and is_ident(target.value, ident)
            and isinstance(target.slice, ast.Constant) and target.slice.value == name)

    def of_update(call, ident, depth):
        """What `ident.update(...)` or `setattr(ident, "<name>", v)` writes: keywords, a dict, a dict call, a name."""
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == "update" and is_ident(func.value, ident):
            # Resolve the mapping's final value before moving its event to
            # the call. Otherwise tied positions let True win over False.
            values = [e for arg in call.args for e in of_value(arg, call.lineno, depth + 1)]
            for keyword in call.keywords:
                if keyword.arg == name:
                    values = [event(keyword.value)]
                elif keyword.arg is None:
                    expanded = of_value(keyword.value, call.lineno, depth + 1)
                    if expanded:
                        values = expanded
            return [(call.lineno, call.col_offset, max(values)[2])] if values else []
        if (isinstance(func, ast.Name) and func.id == "setattr" and len(call.args) == 3
                and is_ident(call.args[0], ident) and isinstance(call.args[1], ast.Constant)
                and call.args[1].value == name):
            return [event(call.args[2])]
        return []

    def of_name(ident, before, depth):
        def binds_ident(n):
            if isinstance(n, ast.Assign):
                return any(is_ident(t, ident) for t in n.targets)
            return isinstance(n, ast.AnnAssign) and n.value is not None and is_ident(n.target, ident)

        binds = [n for n in nodes if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.lineno < before and binds_ident(n)]
        if not binds:
            return []
        bind = max(binds, key=lambda n: (n.lineno, n.col_offset))
        found = of_value(bind.value, bind.lineno, depth + 1)
        for n in nodes:
            if not bind.lineno < getattr(n, "lineno", 0) < before:
                continue
            if isinstance(n, ast.Assign) and any(sets_name(t, ident) for t in n.targets):
                found.append(event(n.value))
            elif isinstance(n, ast.AnnAssign) and n.value is not None and sets_name(n.target, ident):
                found.append(event(n.value))
            elif isinstance(n, ast.Call):
                found += of_update(n, ident, depth)
        return found

    verdicts = []
    for ret in (n for n in nodes if isinstance(n, ast.Return) and n.value is not None):
        found = of_value(ret.value, ret.lineno, 0)
        if found:
            verdicts.append(max(found)[2])
    if verdicts:
        return all(verdicts)

    # a return of another shape: the last setting written anywhere in the function
    sets: list = []     # a keyword or an assignment
    keys: list = []     # a dict key
    for node in nodes:
        if isinstance(node, ast.keyword) and node.arg == name:
            sets.append(event(node.value))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any((isinstance(t, ast.Name) and t.id == name) or (isinstance(t, ast.Attribute) and t.attr == name)
                   for t in targets):
                sets.append(event(node.value))
        elif isinstance(node, ast.Dict):
            keys.extend(event(v) for k, v in zip(node.keys, node.values)
                        if isinstance(k, ast.Constant) and k.value == name)
    found = sets or keys
    return bool(found) and max(found)[2]


def runner_off_port_group(src: str) -> bool:
    return re.search(r"^pytestmark\s*=.*port_bound_http_servers", src, re.M) is None


def collect_shared(target: Path, rev: str) -> bool:
    safe = _show(target, rev, COLLECT_TESTS[0])
    # the per-test rebuild is `index = _real_repo_index()` inside a test body
    return bool(safe) and "index = _real_repo_index()" not in safe


def _asserts(src: str) -> int:
    return len(re.findall(r"^\s*assert\b", src, re.M))


def _base(target: Path, commit: str) -> str:
    for line in _git(target, "log", "--format=%H %s", commit).splitlines():
        sha, _, subject = line.partition(" ")
        if subject == TICKET_SUBJECT:
            return sha
    return ""


def checks(target: Path, commit: str) -> list[tuple[bool, bool, str]]:
    """(ok, required, message) per check."""
    res: list[tuple[bool, bool, str]] = []
    sha = _git(target, "rev-parse", "--verify", f"{commit}^{{commit}}").strip()
    if not sha:
        return [(False, True, f"{commit} is not a commit in {target}")]
    base = _base(target, sha)
    if not base:
        return [(False, True, f"no '{TICKET_SUBJECT}' commit below {sha[:12]}")]

    runner = _show(target, sha, RUNNER_TESTS)
    res.append((backoff_zero(runner), True,
                "cause 1: make_config sets error_retry_backoff_sec=0"))
    res.append((runner_off_port_group(runner), True,
                f"cause 2: {RUNNER_TESTS} has no file-wide port_bound_http_servers mark"))
    res.append((collect_shared(target, sha), False,
                "cause 3 (bonus): the collect index is built once, not per test"))

    touched = [p for p in _git(target, "diff", "--name-only", base, sha).splitlines()
               if p.startswith(("tests/", "tests_bugfix/")) and p.endswith(".py")]
    lost = []
    for p in touched:
        before, after = _show(target, base, p), _show(target, sha, p)
        if before and _asserts(after) < _asserts(before):
            lost.append(f"{p} {_asserts(before)}->{_asserts(after)}")
    res.append((not lost, True, "no touched test file lost an assert"
                + (f" — {', '.join(lost)}" if lost else "")))

    msg = _git(target, "log", "--format=%B", f"{base}..{sha}")
    res.append((base[:7] in msg, True, f"the message names the base {base[:7]}"))
    res.append((re.search(r"\d+(\.\d+)?\s*s\b", msg) is not None, True,
                "the message gives times in seconds"))
    return res


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.split("Usage:")[1].split("\n")[0].strip(), file=sys.stderr)
        return 2
    results = checks(Path(argv[0]).resolve(), argv[1])
    for ok, required, msg in results:
        print(f"{'PASS' if ok else ('FAIL' if required else 'MISS')}  {msg}")
    failed = sum(required and not ok for ok, required, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
