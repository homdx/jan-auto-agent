"""177: `scripts/split_epic_tickets.py` never overwrites tickets (and their **Status:** lines) already in --out."""

from __future__ import annotations

import pytest

from scripts import split_epic_tickets as split


def _first_ticket(out):
    return next(p for p in sorted(out.glob("[0-9]*-*.md")))


def test_a_fresh_folder_is_written(tmp_path, capsys):
    out = tmp_path / "tasks"
    assert split.main(["--out", str(out)]) == 0
    assert (out / "INDEX.md").is_file() and _first_ticket(out).is_file()


def test_a_folder_that_holds_a_ticket_is_refused_and_untouched(tmp_path, capsys):
    out = tmp_path / "tasks"
    assert split.main(["--out", str(out)]) == 0
    ticket = _first_ticket(out)
    landed = ticket.read_text(encoding="utf-8").replace(
        "**Severity:**", "**Status:** landed — `8212df1`  \n**Severity:**", 1)
    ticket.write_text(landed, encoding="utf-8")
    index = (out / "INDEX.md")
    index.write_text("| 1 | `L2` | landed `8212df1` |\n", encoding="utf-8")
    capsys.readouterr()

    assert split.main(["--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "refusing" in err and "--force" in err and ticket.name in err
    assert ticket.read_text(encoding="utf-8") == landed
    assert index.read_text(encoding="utf-8") == "| 1 | `L2` | landed `8212df1` |\n"


def test_a_lone_index_is_enough_to_refuse(tmp_path):
    out = tmp_path / "tasks"
    out.mkdir()
    (out / "INDEX.md").write_text("keep me\n", encoding="utf-8")
    assert split.main(["--out", str(out)]) == 1
    assert (out / "INDEX.md").read_text(encoding="utf-8") == "keep me\n"
    assert not list(out.glob("[0-9]*-*.md"))


def test_force_overwrites(tmp_path):
    out = tmp_path / "tasks"
    assert split.main(["--out", str(out)]) == 0
    ticket = _first_ticket(out)
    ticket.write_text("edited\n", encoding="utf-8")
    assert split.main(["--out", str(out), "--force"]) == 0
    assert ticket.read_text(encoding="utf-8") != "edited\n"
