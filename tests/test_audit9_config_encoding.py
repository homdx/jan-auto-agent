"""AUDIT-9 config decoding regressions."""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

import main
from tools.auto.controller import AutoController


def test_utf8_bom_is_accepted_by_controller_and_main_loader(tmp_path):
    config = tmp_path / "agents.ini"
    config.write_bytes(b"\xef\xbb\xbf[auto]\nmax_tasks_per_run = 3\n")
    controller = AutoController("goal", tmp_path, str(config))
    assert controller.config.getint("auto", "max_tasks_per_run") == 3

    orchestrator = main.Orchestrator.__new__(main.Orchestrator)
    orchestrator.config = configparser.ConfigParser()
    orchestrator.load_config(str(config))
    assert orchestrator.config.getint("auto", "max_tasks_per_run") == 3


def test_invalid_utf8_is_path_aware_and_main_exits_two(tmp_path, capsys):
    config = tmp_path / "bad.ini"
    config.write_bytes(b"[auto]\nmax_tasks_per_run = \xff\n")
    with pytest.raises(ValueError, match="bad.ini.*UTF-8"):
        AutoController("goal", tmp_path, str(config))

    with pytest.raises(SystemExit) as exc:
        main._validate_typed_config_values(str(config), str(tmp_path))
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "bad.ini" in captured.err and "UTF-8" in captured.err


def test_malformed_ini_still_falls_back_in_controller(tmp_path, caplog):
    config = tmp_path / "bad.ini"
    config.write_text("max_tasks_per_run = 3\n", encoding="utf-8")
    controller = AutoController("goal", tmp_path, str(config))
    assert not controller.config.has_section("auto")
    assert "malformed" in caplog.text
