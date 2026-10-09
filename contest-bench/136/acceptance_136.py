"""Judge's acceptance suite for round 136 (AR-1), written from the ticket alone.

Drives only the public contract the ticket names: the `arena` launcher and
`python3 -m tools.arena` end to end, `tools.arena.cli.main` / `OBJECTS`, and
`tools.arena.output.emit/mask/scrub/refuse`. The ticket leaves the shape of a
verb registration open ("the fake verb handler used by the test"), so
`_install_fake` adapts to the three shapes the entries chose: a
`register_verb()` function, `OBJECTS[obj].verbs[verb].handler`, or a
`HANDLERS[(obj, verb)]` dict.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/136/acceptance_136.py -n 8 -q
Red on the base (aadbe49): there is no tools/arena/.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE = "aadbe49"


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd or ROOT, capture_output=True, text=True, timeout=60
    )


def _arena_m(*argv: str) -> subprocess.CompletedProcess:
    return _run([sys.executable, "-m", "tools.arena", *argv])


def _one_usage_line(cp: subprocess.CompletedProcess) -> None:
    lines = cp.stderr.splitlines()
    assert cp.returncode == 2, (cp.returncode, cp.stderr)
    assert len(lines) == 1, cp.stderr
    assert lines[0].startswith("arena: "), cp.stderr
    assert "usage:" not in cp.stderr.lower()


# ── launcher and package ─────────────────────────────────────────────────────

def test_help_lists_four_objects():
    cp = _arena_m("--help")
    assert cp.returncode == 0, cp.stderr
    for obj in ("profile", "issue", "run", "entry"):
        assert obj in cp.stdout


@pytest.mark.parametrize("obj", ["profile", "issue", "run", "entry"])
def test_object_help_exits_zero(obj):
    cp = _arena_m(obj, "--help")
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip()


def test_launcher_mode_100755_in_git():
    out = _run(["git", "ls-files", "-s", "arena"]).stdout
    assert out.startswith("100755 "), out
    assert os.access(ROOT / "arena", os.X_OK)


def test_launcher_help_from_another_cwd(tmp_path):
    cp = _run([str(ROOT / "arena"), "--help"], cwd=tmp_path)
    assert cp.returncode == 0, cp.stderr
    assert "profile" in cp.stdout and "entry" in cp.stdout


def test_launcher_runs_this_checkout_not_cwd_tools(tmp_path):
    # A `tools` package in the caller's cwd must not shadow the checkout's.
    (tmp_path / "tools" / "arena").mkdir(parents=True)
    (tmp_path / "tools" / "__init__.py").write_text("")
    (tmp_path / "tools" / "arena" / "__init__.py").write_text("raise SystemExit(9)\n")
    cp = _run([str(ROOT / "arena"), "--help"], cwd=tmp_path)
    assert cp.returncode == 0, cp.stderr


def test_gitignore_has_dot_arena():
    lines = (ROOT / ".gitignore").read_text().splitlines()
    assert any(l.strip() in (".arena/", "/.arena/") for l in lines)


def test_tools_contest_unchanged():
    cp = _run(["git", "diff", "--stat", BASE, "--", "tools/contest"])
    assert cp.returncode == 0 and cp.stdout.strip() == "", cp.stdout


def test_feature_tests_exist_and_tiered():
    assert (ROOT / "tests" / "test_arena_cli.py").is_file()
    link = ROOT / ".smoke_tests" / "test_arena_cli.py"
    reg = ROOT / ".regression_tests" / "test_arena_cli.py"
    assert link.is_symlink() or reg.is_symlink()
    cp = _run([sys.executable, "scripts/sync_test_tiers.py", "--check"])
    assert cp.returncode == 0, cp.stdout + cp.stderr


# ── usage errors: one line, exit 2 ───────────────────────────────────────────

def test_unknown_object_one_line():
    _one_usage_line(_arena_m("bogus"))


def test_missing_object_is_usage_error():
    _one_usage_line(_arena_m())


def test_missing_verb_is_usage_error():
    _one_usage_line(_arena_m("issue"))


def test_bad_output_format_is_usage_error():
    _one_usage_line(_arena_m("-o", "xml", "issue", "list"))


def test_unknown_global_flag_is_usage_error():
    _one_usage_line(_arena_m("--bogus", "issue", "list"))


def test_usage_error_does_not_echo_a_secret():
    cp = _arena_m("--api_key=sekrit42", "issue", "list")
    assert cp.returncode == 2
    assert "sekrit42" not in cp.stdout + cp.stderr


# The ticket names no verb list; `run start 5` is the one it spells out.
@pytest.mark.parametrize("obj,verb", [("run", "start")])
def test_unimplemented_verb_names_its_ticket(obj, verb):
    cp = _arena_m(obj, verb, "5")
    _one_usage_line(cp)
    import re
    assert re.fullmatch(
        rf"arena: {obj} {verb} is not implemented yet \(AR-(\d+|N)\)", cp.stderr.strip()
    ), cp.stderr


# ── passthrough through a fake verb handler ──────────────────────────────────

def _ticket_of(args):
    """The ticket wherever the entry's own seam put it."""
    for name in ("ticket",):
        if getattr(args, name, None) is not None:
            return str(getattr(args, name))
    for name in ("verb_args", "rest", "verb"):
        v = getattr(args, name, None)
        if isinstance(v, list):
            words = [w for w in v if w not in ("start",)]
            if words:
                return str(words[0])
    return None


def _install_fake(monkeypatch, obj: str, verb: str, seen: dict):
    """Install a recording `obj verb` handler through the seam the entry built.

    The ticket leaves the seam open ("the fake verb handler used by the test"),
    so each entry's own test-3 seam is used: register_verb (two signatures),
    a `.verbs` map, OBJECTS[obj][verb] / OBJECTS[obj]["verbs"][verb],
    VERBS / VERB_HANDLERS / HANDLERS tables, a main(registry=|verbs=) kwarg, or
    a module-level _fake_verb_handler."""
    import functools
    import inspect
    from tools.arena import cli

    def handler(args, *_rest) -> int:
        seen.update(
            ticket=_ticket_of(args),
            passthrough=list(args.passthrough),
            profile=getattr(args, "profile", "<missing>"),
            yes=getattr(args, "yes", "<missing>"),
            output=getattr(args, "output", "<missing>"),
        )
        return 0

    def add(p: argparse.ArgumentParser) -> None:
        p.add_argument("ticket")

    params = inspect.signature(cli.main).parameters
    if "registry" in params:  # main(argv, registry={obj: {verb: {...}}})
        reg = {obj: {verb: {"help": "fake", "args": [("ticket", {})], "func": handler}}}
        monkeypatch.setattr(cli, "main", functools.partial(cli.main, registry=reg))
        return
    if "verbs" in params:  # main(argv, verbs={obj: {verb: handler}})
        monkeypatch.setattr(cli, "main", functools.partial(cli.main, verbs={obj: {verb: handler}}))
        return
    if hasattr(cli, "_fake_verb_handler"):
        monkeypatch.setattr(cli, "_fake_verb_handler", handler)
        return
    if hasattr(cli, "register_verb"):
        sig = list(inspect.signature(cli.register_verb).parameters)
        if hasattr(cli, "VerbConfig"):  # register_verb(obj, verb, config, handler)
            for t in ("VERB_REGISTRY", "VERB_HANDLERS"):
                monkeypatch.setattr(cli, t, dict(getattr(cli, t)))
            cli.register_verb(obj, verb, cli.VerbConfig(positional_args=[("ticket", "t")]), handler)
            return
        if len(sig) == 2 and hasattr(cli, "Verb"):  # register_verb(obj, Verb(...))
            monkeypatch.setattr(cli.OBJECTS[obj], "verbs", dict(cli.OBJECTS[obj].verbs))
            cli.register_verb(obj, cli.Verb(verb, handler, add))
            return
        reg = getattr(cli, "_VERB_REGISTRY", None)
        if reg is not None:
            monkeypatch.setitem(reg, obj, copy.copy(reg[obj]))
        cli.register_verb(obj, verb, handler, add)
        return
    if hasattr(cli, "VERBS") and hasattr(cli, "Verb"):  # VERBS[obj][verb] = Verb(ticket, handler, positionals)
        monkeypatch.setitem(cli.VERBS[obj], verb, cli.Verb("AR-x", handler, {"ticket": {}}))
        return
    if hasattr(cli, "VERB_HANDLERS"):
        monkeypatch.setitem(cli.VERB_HANDLERS.setdefault(obj, {}), verb, handler)
        return
    entry = cli.OBJECTS[obj]
    if isinstance(entry, dict) and isinstance(entry.get("verbs"), dict):
        monkeypatch.setitem(entry["verbs"], verb, handler)
        return
    if isinstance(entry, dict) and verb in entry:
        monkeypatch.setitem(entry, verb, handler)
        return
    if hasattr(entry, "verbs"):
        v = entry.verbs[verb]
        monkeypatch.setattr(v, "handler", handler)
        if hasattr(v, "add_arguments"):
            monkeypatch.setattr(v, "add_arguments", add)
        return
    monkeypatch.setitem(cli.HANDLERS, (obj, verb), handler)


def _main(argv: list[str]) -> int:
    from tools.arena import cli
    try:
        return cli.main(argv)
    except SystemExit as exc:  # tolerated: some mains exit instead of return
        return exc.code if isinstance(exc.code, int) else 2


def test_passthrough_after_double_dash(monkeypatch):
    seen: dict = {}
    _install_fake(monkeypatch, "run", "start", seen)
    rc = _main(["run", "start", "5", "--", "--no-gate", "--max-parallel", "4"])
    assert rc == 0
    assert seen["ticket"] == "5"
    assert seen["passthrough"] == ["--no-gate", "--max-parallel", "4"]


def test_no_double_dash_gives_empty_passthrough(monkeypatch):
    seen: dict = {}
    _install_fake(monkeypatch, "run", "start", seen)
    assert _main(["run", "start", "5"]) == 0
    assert seen["passthrough"] == []
    assert seen["ticket"] == "5"


def test_passthrough_verbatim_second_double_dash(monkeypatch):
    seen: dict = {}
    _install_fake(monkeypatch, "run", "start", seen)
    assert _main(["run", "start", "7", "--", "-x", "--", "a b", "--k=v"]) == 0
    assert seen["passthrough"] == ["-x", "--", "a b", "--k=v"]


def test_global_flags_retained(monkeypatch):
    seen: dict = {}
    _install_fake(monkeypatch, "run", "start", seen)
    assert _main(["-p", "fast", "-o", "json", "-y", "run", "start", "5"]) == 0
    assert seen["profile"] == "fast"
    assert seen["yes"] is True
    assert seen["output"] == "json"


def test_global_flag_defaults(monkeypatch):
    seen: dict = {}
    _install_fake(monkeypatch, "run", "start", seen)
    assert _main(["run", "start", "5"]) == 0
    assert seen["profile"] is None
    assert seen["yes"] is False
    assert seen["output"] == "table"


# ── mask ─────────────────────────────────────────────────────────────────────

def test_mask_secret_keys():
    from tools.arena.output import mask
    src = {
        "api_key": "a1", "API_KEY": "a2", "apiKey": "a3", "gate_token": "a4",
        "client-secret": "a5", "password": "a6", "passwd": "a7", "key": "a8",
        "token": "a9", "apikey": "a10", "Secret": "a11", "db.password": "a12",
    }
    out = mask(src)
    assert all(v == "***" for v in out.values()), out


def test_mask_leaves_lookalikes():
    from tools.arena.output import mask
    src = {"tokens": 1234, "max_tokens": 8192, "monkey": "m", "base_url": "http://h",
           "model": "x", "keyboard": "k"}
    assert mask(src) == src


def test_mask_recurses_and_copies():
    from tools.arena.output import mask
    src = {"profiles": [{"name": "a", "api_key": "s1"}, {"nested": {"token": "s2"}}],
           "gate": {"apiKey": "s3", "tokens": 5}}
    before = copy.deepcopy(src)
    out = mask(src)
    assert src == before
    assert out["profiles"][0] == {"name": "a", "api_key": "***"}
    assert out["profiles"][1]["nested"]["token"] == "***"
    assert out["gate"] == {"apiKey": "***", "tokens": 5}


# ── scrub / refuse ───────────────────────────────────────────────────────────

def test_scrub_url_and_query():
    from tools.arena.output import scrub
    s = scrub("https://u:p@host/v1?api_key=abc&x=1")
    assert "u:p" not in s and "abc" not in s
    assert "***" in s and "host/v1" in s and "x=1" in s


@pytest.mark.parametrize("text,secret", [
    ("token=xyz", "xyz"), ("key=k1", "k1"), ("secret=s3c", "s3c"),
    ("password=hunter2", "hunter2"), ("a=1&token=tt9&b=2", "tt9"),
    ("TOKEN=UP99", "UP99"),
])
def test_scrub_form_values(text, secret):
    from tools.arena.output import scrub
    assert secret not in scrub(text)


def test_scrub_leaves_plain_text():
    from tools.arena.output import scrub
    assert scrub("monkey=banana tokens=12") == "monkey=banana tokens=12"


def test_refuse_one_scrubbed_line(capsys):
    from tools.arena.output import refuse
    rc = refuse("cannot reach https://u:p@host/v1?api_key=abc&x=1 with token=xyz")
    err = capsys.readouterr().err
    assert rc == 2
    assert len(err.splitlines()) == 1 and err.startswith("arena: ")
    for s in ("u:p", "abc", "xyz"):
        assert s not in err


def test_refuse_multiline_message_stays_one_line(capsys):
    from tools.arena.output import refuse
    assert refuse("first\nsecond") == 2
    assert len(capsys.readouterr().err.splitlines()) == 1


# ── emit ─────────────────────────────────────────────────────────────────────

ROWS = [
    {"name": "fast", "api_key": "sk-123", "tokens": 4096,
     "base_url": "https://u:p@host/v1?api_key=abc", "gate": {"token": "g1", "url": "https://a:b@g"}},
]


def test_emit_json_prints_masked_parseable(capsys):
    from tools.arena.output import emit
    emit(ROWS, ["name", "api_key", "tokens"], "json")
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data[0]["api_key"] == "***"
    assert data[0]["tokens"] == 4096
    for s in ("sk-123", "u:p", "abc", "g1", "a:b"):
        assert s not in out


def test_emit_table_prints_columns(capsys):
    from tools.arena.output import emit
    emit(ROWS, ["name", "api_key", "tokens"], "table")
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines[0].split() == ["name", "api_key", "tokens"]
    assert any(l.split()[:3] == ["fast", "***", "4096"] for l in lines[1:]), out
    assert "sk-123" not in out
    # left-aligned: the value column starts where its header does
    assert lines[0].index("api_key") == next(
        l for l in lines[1:] if "fast" in l).index("***")


def test_emit_does_not_mutate_rows(capsys):
    from tools.arena.output import emit
    rows = copy.deepcopy(ROWS)
    emit(rows, ["name"], "json")
    assert rows == ROWS
