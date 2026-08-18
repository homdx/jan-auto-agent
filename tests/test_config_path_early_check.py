"""tests/test_config_path_early_check.py — main.py fails loudly on a
missing/misspelled --config path, instead of silently falling back to an
empty ConfigParser and only failing several steps later inside the
pipeline with a confusing NoSectionError far from the actual cause.

Real-world trigger this guards against: `--config agents_128k.in` (typo,
missing trailing 'k') used to run all the way to the architect stage
before failing with `configparser.NoSectionError: No section: 'api_local'`
— a traceback pointing at architect.py, nowhere near the actual mistake.

  AC-1  --auto with a nonexistent --config path exits 1 with a clear
        message before run_auto is ever called.
  AC-2  --validate-plan with a nonexistent --config path exits 1 with a
        clear message before run_validate is ever called.
  AC-3  --auto with an EXISTING --config path is unaffected — run_auto is
        still called normally.
  AC-4  --validate-plan with an EXISTING --config path is unaffected.
  AC-5  AutoController itself is UNCHANGED — constructing it directly with
        a nonexistent config_path (the "none.ini" convention used
        throughout the rest of this test suite as shorthand for "use
        coded defaults") still works with no error, since this check
        lives only at the CLI entry point in main.py, not inside
        AutoController.__init__.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.auto.controller import AutoController


class TestConfigPathEarlyCheckAuto:

    def test_missing_config_exits_1_before_run_auto(self, tmp_path: Path, capsys) -> None:
        import main as main_mod

        argv = [
            "main.py", "--auto", "improve error handling", "--dry-run",
            "--base", str(tmp_path), "--config", "definitely_missing_128k.ini",
        ]
        with patch("tools.auto.controller.run_auto") as mock_run_auto:
            with patch.object(sys, "argv", argv):
                with pytest.raises(SystemExit) as exc_info:
                    main_mod.main()

        assert exc_info.value.code == 1
        mock_run_auto.assert_not_called()
        err = capsys.readouterr().err
        assert "definitely_missing_128k.ini" in err
        assert "not found" in err.lower()

    def test_typo_extension_produces_helpful_hint(self, tmp_path: Path, capsys) -> None:
        """The exact real-world typo this check exists for: '.in' instead
        of '.ini'."""
        import main as main_mod

        argv = [
            "main.py", "--auto", "improve error handling", "--dry-run",
            "--base", str(tmp_path), "--config", "agents_128k.in",
        ]
        with patch("tools.auto.controller.run_auto") as mock_run_auto:
            with patch.object(sys, "argv", argv):
                with pytest.raises(SystemExit) as exc_info:
                    main_mod.main()

        assert exc_info.value.code == 1
        mock_run_auto.assert_not_called()
        err = capsys.readouterr().err
        assert "agents_128k.in" in err

    def test_existing_config_is_unaffected(self, tmp_path: Path) -> None:
        import main as main_mod

        ini = tmp_path / "real.ini"
        ini.write_text("[api]\nactive = local\n[api_local]\nbase_url = http://x\napi_key = k\nmodel = m\n", encoding="utf-8")

        argv = [
            "main.py", "--auto", "improve error handling", "--dry-run",
            "--base", str(tmp_path), "--config", str(ini),
        ]
        with patch("tools.auto.controller.run_auto", return_value=0) as mock_run_auto:
            with patch.object(sys, "argv", argv):
                with pytest.raises(SystemExit) as exc_info:
                    main_mod.main()

        assert exc_info.value.code == 0
        mock_run_auto.assert_called_once()
        assert mock_run_auto.call_args.kwargs["config_path"] == str(ini)


class TestConfigPathEarlyCheckValidatePlan:

    def test_missing_config_exits_1_before_run_validate(self, tmp_path: Path, capsys) -> None:
        import main as main_mod

        argv = [
            "main.py", "--validate-plan",
            "--base", str(tmp_path), "--config", "definitely_missing.ini",
        ]
        with patch("tools.auto.plan_validator.run_validate") as mock_run_validate:
            with patch.object(sys, "argv", argv):
                with pytest.raises(SystemExit) as exc_info:
                    main_mod.main()

        assert exc_info.value.code == 1
        mock_run_validate.assert_not_called()
        err = capsys.readouterr().err
        assert "definitely_missing.ini" in err
        assert "not found" in err.lower()

    def test_existing_config_is_unaffected(self, tmp_path: Path) -> None:
        import main as main_mod

        ini = tmp_path / "real.ini"
        ini.write_text("[api]\nactive = local\n[api_local]\nbase_url = http://x\napi_key = k\nmodel = m\n", encoding="utf-8")

        argv = [
            "main.py", "--validate-plan",
            "--base", str(tmp_path), "--config", str(ini),
        ]
        with patch("tools.auto.plan_validator.run_validate", return_value=0) as mock_run_validate:
            with patch.object(sys, "argv", argv):
                with pytest.raises(SystemExit) as exc_info:
                    main_mod.main()

        assert exc_info.value.code == 0
        mock_run_validate.assert_called_once()
        assert mock_run_validate.call_args.kwargs["config_path"] == str(ini)


class TestAutoControllerDirectConstructionUnaffected:
    """AUTO-CONFIG-CHECK-1 is deliberately scoped to main.py's CLI entry
    point, NOT to AutoController itself — the rest of this test suite
    relies on constructing AutoController directly with a nonexistent
    config_path (conventionally "none.ini") as shorthand for "use coded
    defaults, no ini overrides". This must keep working unchanged."""

    def test_nonexistent_config_path_still_works_via_direct_construction(
        self, tmp_path: Path
    ) -> None:
        ctrl = AutoController(
            goal="improve things",
            base_dir=tmp_path,
            config_path="none.ini",
        )
        assert ctrl.config.sections() == []
