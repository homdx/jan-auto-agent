"""tests_bugfix/test_bugfix_collect_config_check.py — FIX-2 #4: --collect
fails loudly on a missing/misspelled EXPLICIT --config path, the same way
--auto and --validate-plan already do (see test_config_path_early_check.py),
while still tolerating a missing *default* agents.ini so --collect keeps
working out of the box from any directory.

Before the fix, `--collect` built its ConfigParser with
`if os.path.exists(args.config): config.read(...)` — a missing/typo'd
--config was silently swallowed and the whole collect pass ran against an
empty, defaults-only config (wrong thresholds, Pass B possibly skipped)
while still reporting success.

Regression this guards against specifically: an earlier attempt at this
fix ("Mistral 3.5 fix") made the check unconditionally fatal — matching
--auto/--validate-plan literally, but also erroring out on the *default*
"agents.ini" when it doesn't exist, which broke `--collect` runs that
relied on built-in defaults (e.g. invoked from outside the repo root with
no config file at all). That version was reverted in favour of the
current one, which is fatal only when the user *explicitly* named a
--config file that doesn't exist.

  AC-1  --collect with a nonexistent, EXPLICITLY-passed --config exits 1
        with a clear "config file not found" message before collect_run
        is ever called.
  AC-2  --collect with an EXISTING --config is unaffected.
  AC-3  --collect with NO --config at all, and no agents.ini on disk,
        does NOT exit 1 — it proceeds with built-in defaults (the
        default-path exception; this is the behaviour the earlier,
        always-fatal fix attempt regressed).
  AC-4  The typo'd-extension case (the original real-world trigger,
        e.g. 'agents_128k.in' instead of '.ini') is covered too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def _run_collect(argv, cwd):
    """Run `main.main()` with `argv` from *cwd*, mocking collect_run so we
    never touch a real repo. Returns (systemexit_code_or_None, stderr)."""
    import main as main_mod

    old_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        with patch("tools.collect.cli.run") as mock_run:
            mock_run.return_value.action = "collect"
            mock_run.return_value.message = "built 0 file(s)"
            with patch.object(sys, "argv", argv):
                try:
                    main_mod.main()
                    return None, mock_run
                except SystemExit as exc:
                    return exc.code, mock_run
    finally:
        os.chdir(old_cwd)


class TestCollectExplicitMissingConfigFailsLoud:

    def test_missing_explicit_config_exits_1(self, tmp_path: Path, capsys) -> None:
        argv = [
            "main.py", "--collect",
            "--base", str(tmp_path),
            "--config", "definitely_missing_128k.ini",
        ]
        code, mock_run = _run_collect(argv, tmp_path)

        assert code == 1
        mock_run.assert_not_called()
        err = capsys.readouterr().err
        assert "definitely_missing_128k.ini" in err
        assert "not found" in err.lower()

    def test_typo_extension_produces_helpful_hint(self, tmp_path: Path, capsys) -> None:
        """The real-world trigger this check exists for: '.in' instead of
        '.ini'."""
        argv = [
            "main.py", "--collect",
            "--base", str(tmp_path),
            "--config", "agents_128k.in",
        ]
        code, mock_run = _run_collect(argv, tmp_path)

        assert code == 1
        mock_run.assert_not_called()
        assert "agents_128k.in" in capsys.readouterr().err

    def test_existing_explicit_config_is_unaffected(self, tmp_path: Path) -> None:
        ini = tmp_path / "real.ini"
        ini.write_text("[collect]\ndir = .collect\n", encoding="utf-8")

        argv = [
            "main.py", "--collect",
            "--base", str(tmp_path),
            "--config", str(ini),
        ]
        code, mock_run = _run_collect(argv, tmp_path)

        assert code == 0
        mock_run.assert_called_once()


class TestCollectDefaultMissingConfigIsTolerated:
    """AC-3: the default-path exception. This is the exact behaviour the
    earlier "always fatal" fix attempt broke — locking it in here so a
    future "just mirror --auto" refactor can't silently regress it again."""

    def test_no_config_flag_and_no_agents_ini_on_disk_still_runs(
        self, tmp_path: Path, capsys
    ) -> None:
        # No --config passed at all -> args.config defaults to "agents.ini",
        # which does not exist anywhere under tmp_path (our cwd for the run).
        argv = ["main.py", "--collect", "--base", str(tmp_path)]
        code, mock_run = _run_collect(argv, tmp_path)

        assert code is None or code == 0
        mock_run.assert_called_once()
        # A missing default is worth a warning, but must not be fatal.
        err = capsys.readouterr().err
        assert "not found" not in err.lower()
