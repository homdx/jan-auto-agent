"""AR-1: the `arena <object> <verb>` skeleton, passthrough, exit codes and masking."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tools.arena import cli, output

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_help_lists_objects_and_object_help_exits_zero(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for name in ("profile", "issue", "run", "entry"):
        assert name in out
    assert cli.main(["issue", "--help"]) == 0


def test_unknown_object_is_one_stderr_line_exit_two(capsys):
    assert cli.main(["bogus"]) == 2
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("arena:")
    assert "usage:" not in err


@pytest.mark.parametrize(
    "argv", [[], ["issue"], ["-o", "xml", "issue", "list"], ["--bogus", "run", "list"]]
)
def test_missing_or_malformed_arguments_are_one_line_usage_errors(argv, capsys):
    # A missing object or verb, a bad --output, an unknown global flag: each
    # is a usage error, never a help page with exit 0.
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and err.startswith("arena: ")
    assert "usage:" not in err


def test_unimplemented_verb_names_its_ticket(capsys):
    assert cli.main(["run", "start", "5"]) == 2
    assert capsys.readouterr().err.strip() == (
        "arena: run start is not implemented yet (AR-3)"
    )


def test_passthrough_after_double_dash_is_kept_verbatim(monkeypatch):
    seen = []

    def add_arguments(p):
        p.add_argument("ticket", type=int)

    def handler(args):
        seen.append((args.ticket, args.passthrough))
        return 0

    verbs = dict(cli.OBJECTS["run"].verbs)
    verbs["start"] = cli.Verb("start", "AR-3", add_arguments, handler)
    monkeypatch.setitem(cli.OBJECTS, "run", cli.Object("rounds", verbs))

    assert cli.main(["run", "start", "5", "--", "--no-gate", "--max-parallel", "4"]) == 0
    assert cli.main(["run", "start", "5"]) == 0
    assert seen == [(5, ["--no-gate", "--max-parallel", "4"]), (5, [])]


def test_fake_verb_handler_gets_the_global_flags_and_their_defaults(monkeypatch):
    # From sensenova-6-7-var1's entry: the globals reach the verb handler, and
    # only the first standalone `--` splits — a later one is passed verbatim.
    seen = []

    def add_arguments(p):
        p.add_argument("ticket")

    def handler(args):
        seen.append((args.profile, args.output, args.yes, args.passthrough))
        return 0

    verbs = dict(cli.OBJECTS["run"].verbs)
    verbs["start"] = cli.Verb("start", "AR-3", add_arguments, handler)
    monkeypatch.setitem(cli.OBJECTS, "run", cli.Object("rounds", verbs))

    assert cli.main(["-p", "p1", "-o", "json", "-y", "run", "start", "5",
                     "--", "-x", "--", "a b"]) == 0
    assert cli.main(["run", "start", "5"]) == 0
    assert seen == [("p1", "json", True, ["-x", "--", "a b"]),
                    (None, "table", False, [])]


def test_usage_error_does_not_echo_a_secret(capsys):
    assert cli.main(["--api_key=sekrit42", "issue", "list"]) == 2
    captured = capsys.readouterr()
    assert "sekrit42" not in captured.out + captured.err


def test_refuse_keeps_a_multiline_message_on_one_line(capsys):
    assert output.refuse("first\nsecond token=xyz") == 2
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and err.startswith("arena: ")
    assert "xyz" not in err


def test_mask_whole_words_recursive_without_mutating():
    data = {
        "api_key": "k1",
        "API_KEY": "k2",
        "apiKey": "k3",
        "gate_token": "t",
        "client-secret": "s",
        "model": "m",
        "base_url": "https://host/v1",
        "tokens": 1234,
        "max_tokens": 10,
        "monkey": "banana",
        "nested": {"password": "p", "ok": "v"},
        "items": [{"token": "x"}, "plain"],
    }
    masked = output.mask(data)
    for k in ("api_key", "API_KEY", "apiKey", "gate_token", "client-secret"):
        assert masked[k] == "***"
    assert masked["nested"] == {"password": "***", "ok": "v"}
    assert masked["items"] == [{"token": "***"}, "plain"]
    assert masked["model"] == "m"
    assert masked["base_url"] == "https://host/v1"
    assert masked["tokens"] == 1234
    assert masked["max_tokens"] == 10
    assert masked["monkey"] == "banana"
    assert data["api_key"] == "k1"
    assert data["nested"]["password"] == "p"


def test_emit_json_parses_and_masks(capsys):
    output.emit([{"name": "a", "api_key": "sekrit", "tokens": 7}], ["name"], "json")
    out = capsys.readouterr().out
    rows = json.loads(out)
    assert rows == [{"name": "a", "api_key": "***", "tokens": 7}]
    assert "sekrit" not in out


def test_scrub_and_refuse_hide_credentials(capsys):
    assert output.refuse("bad url https://u:p@host/v1?api_key=abc&x=1 and token=xyz") == 2
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1
    assert err.startswith("arena: ")
    assert "u:p@" not in err and "abc" not in err and "xyz" not in err
    assert "https://***@host/v1?api_key=***&x=1" in err
    assert "token=***" in err


def test_scrub_hides_apikey_and_passwd_values_too():
    s = output.scrub("apikey=a1 passwd=p2 monkey=banana tokens=12")
    assert "a1" not in s and "p2" not in s
    assert "monkey=banana tokens=12" in s


def test_launcher_is_executable_and_runs_from_another_cwd(tmp_path):
    launcher = REPO_ROOT / "arena"
    assert launcher.stat().st_mode & stat.S_IXUSR
    staged = subprocess.run(
        ["git", "ls-files", "-s", "arena"], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert staged.stdout.startswith("100755 "), staged.stdout
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, str(launcher), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "profile" in proc.stdout
    direct = subprocess.run([str(launcher), "--help"], cwd=tmp_path, env=env, capture_output=True)
    assert direct.returncode == 0
