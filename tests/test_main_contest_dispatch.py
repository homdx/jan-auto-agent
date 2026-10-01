"""tests/test_main_contest_dispatch.py — KC-33.

`main.py`'s REPL already dispatches `/auto`, `/collect`, `/faq` into their
`tools.*` entry points, but the contest CLI (KC-6 `tools/contest/runner.py`,
KC-16 `tools/contest/cli.py`) had no entry from inside the shell: an operator
running the pipeline from `main.py` had to Ctrl-C, re-invoke
`python3 -m tools.contest run --ticket NN` from a second terminal, then restart
`main.py`. This adds `/contest` — every word after the command forwarded
verbatim to `tools.contest.cli.main`, `["--help"]` for a bare `/contest` — and
a `--contest "<args>"` one-shot flag mirroring `--auto`.

The load-bearing assertion is the `SystemExit` handling: argparse's own `--help`
action and its own usage-error path (a bare `/contest run` missing the required
`--ticket`) call `sys.exit()` directly rather than returning an int. `SystemExit`
is not an `Exception` subclass and is not caught by the REPL's
`except (KeyboardInterrupt, EOFError):`, so an unguarded dispatch would propagate
out of `while True:` and kill the whole `main.py` process — exactly the failure
mode `/collect`'s own dispatch was built to avoid. Two tests below therefore use
the REAL `tools.contest.cli.main` so argparse itself raises the real
`SystemExit(0)` / `SystemExit(2)`; a monkeypatch that returns 0 or 2 directly
cannot catch a regression here.
"""

from __future__ import annotations

import contextlib
import sys
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# harness
# ─────────────────────────────────────────────────────────────────────────────

class _FakeOrch:
    """Minimal stand-in for `Orchestrator`: the REPL needs only its key strings
    and a `run_pipeline` to detect a missed dispatch."""

    def __init__(self, config_path=None):
        self.config_path = config_path
        self.exit_key = "/exit"
        self.new_chat_key = "/new"
        self._direct_chat_history = []
        self.config = MagicMock()
        self.pipeline_calls: list = []

    def run_pipeline(self, user_input, base_dir, resume_state=None):
        self.pipeline_calls.append(user_input)


def _contest_recorder(return_code=0):
    """A stand-in for `tools.contest.cli.main(argv)` that records every argv."""
    calls: list = []

    def _fake(argv):
        calls.append(argv)
        return return_code

    _fake.calls = calls
    return _fake


def _run_repl(lines, base_dir, contest_main=None, *, real_contest=False):
    """Drive one REPL session through `main.main()`.

    Returns the fake orchestrator, so the caller can inspect `pipeline_calls`.
    *lines* are the successive `input()` results — the last must be the exit key
    so the loop ends. `Orchestrator` is replaced so the test never reads
    `agents.ini`, never builds an agent and never touches a live endpoint;
    `backoff.load_state` is stubbed so a saved checkpoint does not prompt on
    stdin.

    With *real_contest* the real `tools.contest.cli.main` runs, so argparse
    itself raises `SystemExit`; otherwise *contest_main* stands in for it.
    """
    import main as main_mod

    orch = _FakeOrch()

    # a config path that does not exist: `_validate_typed_config_values` bails
    # out immediately instead of scanning the real agents.ini
    argv = ["main.py", "--base", str(base_dir),
            "--config", str(base_dir / "absent.ini")]

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(main_mod, "Orchestrator",
                                         lambda config_path=None: orch))
        stack.enter_context(patch.object(main_mod.backoff, "load_state",
                                         return_value=None))
        stack.enter_context(patch("builtins.input", side_effect=list(lines)))
        stack.enter_context(patch.object(sys, "argv", argv))
        if not real_contest:
            stack.enter_context(patch("tools.contest.cli.main", contest_main))
        main_mod.main()

    return orch


def _repl(base_dir, line, *, real_contest=False, contest_main=None):
    """One line through the REPL, then `/exit`; returns the fake orchestrator.

    Callers read `capsys.readouterr()` afterwards — done here the buffer would
    already be drained.
    """
    return _run_repl([line, "/exit"], base_dir, contest_main, real_contest=real_contest)


# ─────────────────────────────────────────────────────────────────────────────
# /contest in the REPL
# ─────────────────────────────────────────────────────────────────────────────

class TestReplContestDispatch:

    def test_forwards_argv_verbatim_and_does_not_run_pipeline(self, tmp_path, capsys):
        contest = _contest_recorder(0)
        orch = _repl(tmp_path, "/contest run --ticket 63 --no-tests", contest_main=contest)
        out = capsys.readouterr().out
        assert contest.calls == [["run", "--ticket", "63", "--no-tests"]]
        # must not fall through to run_pipeline — a missed dispatch would parse
        # the whole line as a prompt and try to chat or edit a file
        assert orch.pipeline_calls == []
        assert "Unknown command" not in out

    def test_forwards_flags_as_given(self, tmp_path, capsys):
        contest = _contest_recorder(0)
        _repl(tmp_path, "/contest run --ticket 63 --models agnes-2-5-flash:free,hy3:free "
                       "--max-parallel 4", contest_main=contest)
        assert contest.calls == [["run", "--ticket", "63", "--models",
                                  "agnes-2-5-flash:free,hy3:free",
                                  "--max-parallel", "4"]]

    def test_bare_contest_passes_help(self, tmp_path, capsys):
        """A bare `/contest` (or one with trailing spaces) passes `["--help"]`."""
        contest = _contest_recorder(0)
        for line in ("/contest", "/contest   ", "/contest\t"):
            contest.calls.clear()
            _repl(tmp_path, line, contest_main=contest)
            assert contest.calls == [["--help"]], line


class TestReplContestSystemExit:
    """The REPL must survive argparse's own `sys.exit()` calls.

    These use the REAL `tools.contest.cli.main`: argparse's `--help` action and
    its usage-error path raise `SystemExit` instead of returning an int, so a
    recorder that returns 0 or 2 cannot exercise this path at all.
    """

    def test_bare_contest_survives_argparse_help(self, tmp_path, capsys):
        """Bare `/contest` → real argparse `--help` → `SystemExit(0)`, no crash."""
        orch = _run_repl(["/contest", "/exit"], tmp_path, real_contest=True)
        assert orch.pipeline_calls == []
        out = capsys.readouterr().out
        # argparse printed the contest's own usage, and the loop kept running
        assert "tools.contest" in out
        assert "Unknown command" not in out

    def test_missing_ticket_survives_argparse_usage_error(self, tmp_path, capsys):
        """`/contest run` with no `--ticket` → `SystemExit(2)`, warning printed.

        `--ticket` is `required=True` in `tools/contest/cli.py`, so this is
        argparse's own usage-error path. Left unguarded it propagates out of the
        REPL loop and kills the process.
        """
        orch = _run_repl(["/contest run", "/exit"], tmp_path, real_contest=True)
        err = capsys.readouterr().err
        assert "--ticket" in err
        assert orch.pipeline_calls == []

    def test_nonzero_return_prints_warning(self, tmp_path, capsys):
        contest = _contest_recorder(2)
        _repl(tmp_path, "/contest run --ticket 63", contest_main=contest)
        out = capsys.readouterr().out
        assert "exit code 2" in out
        assert "contest" in out

    def test_zero_return_prints_no_warning(self, tmp_path, capsys):
        contest = _contest_recorder(0)
        _repl(tmp_path, "/contest run --ticket 63", contest_main=contest)
        out = capsys.readouterr().out
        assert "exit code" not in out

    def test_repl_prompts_again_after_a_contest(self, tmp_path, capsys):
        """A failing `/contest` must not end the session: the next line is read."""
        contest = _contest_recorder(1)
        orch = _run_repl(["/contest run --ticket 63", "/exit"], tmp_path,
                         contest_main=contest)
        assert contest.calls == [["run", "--ticket", "63"]]
        out = capsys.readouterr().out
        assert "exit code 1" in out


# ─────────────────────────────────────────────────────────────────────────────
# --contest one-shot flag
# ─────────────────────────────────────────────────────────────────────────────

class TestContestOneShot:

    def test_parse_args_defaults_to_none(self):
        import main as main_mod
        with patch.object(sys, "argv", ["main.py"]):
            args = main_mod._parse_args()
        assert args.contest is None

    def test_parse_args_keeps_the_argument_string_intact(self):
        """Quotes and commas must survive; the value is shlex.split on use."""
        import main as main_mod
        with patch.object(sys, "argv",
                          ["main.py", "--contest", "run --ticket 63 --models a:free,b:free"]):
            args = main_mod._parse_args()
        assert args.contest == "run --ticket 63 --models a:free,b:free"

    def test_parse_args_help_lists_the_contest_flag(self, capsys):
        """`python main.py --help` must advertise `--contest`."""
        import main as main_mod
        with patch.object(sys, "argv", ["main.py", "--help"]):
            with pytest.raises(SystemExit) as exc_info:
                main_mod._parse_args()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "--contest" in out

    def test_main_dispatches_and_exits_with_the_return_code(self, tmp_path):
        import main as main_mod
        contest = _contest_recorder(3)
        with patch("tools.contest.cli.main", contest):
            with patch.object(sys, "argv",
                              ["main.py", "--contest", "run --ticket 63",
                               "--base", str(tmp_path)]):
                with pytest.raises(SystemExit) as exc_info:
                    main_mod.main()
        assert exc_info.value.code == 3
        assert contest.calls == [["run", "--ticket", "63"]]

    def test_main_splits_the_argument_string(self, tmp_path):
        import main as main_mod
        contest = _contest_recorder(0)
        with patch("tools.contest.cli.main", contest):
            with patch.object(sys, "argv",
                              ["main.py", "--contest",
                               "run --ticket 63 --models agnes-2-5-flash:free,hy3:free "
                               "--max-parallel 8",
                               "--base", str(tmp_path)]):
                with pytest.raises(SystemExit) as exc_info:
                    main_mod.main()
        assert exc_info.value.code == 0
        assert contest.calls == [["run", "--ticket", "63", "--models",
                                  "agnes-2-5-flash:free,hy3:free",
                                  "--max-parallel", "8"]]

    def test_main_skips_the_orchestrator(self, tmp_path):
        """`--contest` must not build the agents — it needs no API config."""
        import main as main_mod
        contest = _contest_recorder(0)

        built: list = []

        class _Boom:
            def __init__(self, config_path=None):
                built.append(config_path)

        with patch("tools.contest.cli.main", contest):
            with patch.object(main_mod, "Orchestrator", _Boom):
                with patch.object(sys, "argv",
                                  ["main.py", "--contest", "run --ticket 63",
                                   "--base", str(tmp_path)]):
                    with pytest.raises(SystemExit) as exc_info:
                        main_mod.main()
        assert contest.calls == [["run", "--ticket", "63"]]
        assert built == []
        assert exc_info.value.code == 0

    def test_faq_one_shot_still_builds_the_orchestrator(self, tmp_path):
        """Regression guard: `--contest` was inserted next to `--faq`, and the
        FAQ one-shot keeps its own `Orchestrator` construction (it is the only
        way `orchestrator.faq_agent` below resolves)."""
        import main as main_mod

        answers = []

        class _FakeOrch:
            def __init__(self, config_path=None):
                answers.append(config_path)
                self.faq_agent = MagicMock()
                self.faq_agent.answer.return_value = "the answer"
                self.faq_agent.NOT_FOUND = "NOT FOUND"
                self.faq_agent.llm_call_count = 1

        with patch.object(main_mod, "Orchestrator", _FakeOrch):
            with patch.object(sys, "argv",
                              ["main.py", "--faq", "what?",
                               "--base", str(tmp_path),
                               "--config", str(tmp_path / "absent.ini")]):
                with pytest.raises(SystemExit) as exc_info:
                    main_mod.main()
        assert answers, "--faq must construct the Orchestrator"
        assert exc_info.value.code == 0


# ─────────────────────────────────────────────────────────────────────────────
# slash-command guard
# ─────────────────────────────────────────────────────────────────────────────

class TestContestGuardAndHelp:

    def test_contest_is_exempt_from_the_unrecognized_slash_guard(self, tmp_path, capsys):
        contest = _contest_recorder(0)
        orch = _repl(tmp_path, "/contest run --ticket 63", contest_main=contest)
        out = capsys.readouterr().out
        assert "Unknown command" not in out
        assert orch.pipeline_calls == []

    def test_other_slash_commands_still_report_unknown(self, tmp_path, capsys):
        """The exemption is `/contest` only — an unrelated slash command is
        still flagged, and no contest dispatch happens."""
        contest = _contest_recorder(0)
        orch = _repl(tmp_path, "/nope", contest_main=contest)
        out = capsys.readouterr().out
        assert "Unknown command" in out
        assert contest.calls == []
        assert orch.pipeline_calls == []

    def test_help_text_lists_contest(self):
        import main as main_mod
        assert "/contest run --ticket" in main_mod.HELP_TEXT

    def test_help_text_documents_the_forwarded_flags(self):
        import main as main_mod
        text = main_mod.HELP_TEXT
        assert "--models" in text
        assert "tools.contest run" in text
        assert "/contest --help" in text

    def test_cli_epilog_gives_a_contest_example(self, capsys):
        """The argparse epilog lists every other one-shot flag; `--contest`
        should too, so it is discoverable from `python main.py --help`."""
        import main as main_mod
        with patch.object(sys, "argv", ["main.py", "--help"]):
            with pytest.raises(SystemExit):
                main_mod._parse_args()
        out = capsys.readouterr().out
        assert "--contest \"run --ticket 63 --models hy3:free\"" in out