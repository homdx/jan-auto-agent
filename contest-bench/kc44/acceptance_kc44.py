"""KC-44 (round 83) judge's acceptance suite: the ticket's own `**Size:**`
decides how many legs it gets, and an `L` ticket is refused in one.

Written from the ticket's "What must change" §1–5 and its Acceptance list,
through the public contract only: `tools.contest.cli.ticket_size(path)` (the
ticket names the symbol), and `python3 -m tools.contest run` end to end against
the fake Kilo the way the KC-43 bench in `tests/test_contest_cli.py` drives it —
leg folders `NN.1 … NN.k`, `state.json`, the exit code, stdout/stderr and the
log. Where an entry keeps the parsed map (`ContestConfig`, a helper, a string)
is not scored; only the `[contest] legs_by_size` key in `contest.ini` is.

Must be red on the base (b3c2437): 27 of 30.

P* — `ticket_size`: the header's value, upper-cased, the parenthesis dropped;
     a missing or unknown value is None (§1, Acceptance 3).
L* — the leg count: `L` runs three legs, `S` and no Size one, `--legs` wins
     both ways (§2–3, Acceptance 1).
R* — the refusal: an `L` ticket that would run one leg exits non-zero, names
     the size and `--legs 1`, and prepares no worktree (§4, Acceptance 2).
N* — the chosen count and its source in `state.json` and the round's log (§5).
I* — `contest.ini` ships `legs_by_size` with `L=3` and the other sizes at 1.
"""
from __future__ import annotations

import configparser
import json
import logging
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TESTS_DIR = REPO_ROOT / "tests"
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_contest_cli as tc  # noqa: E402
from tools.contest import cli  # noqa: E402

ROUND = tc.ROUND
T1, T2 = tc.TICKET_01, tc.TICKET_02


def _ticket_with(size_line: str | None) -> str:
    """The bench's ticket 01 with its `**Size:**` line replaced (or dropped)."""
    body = tc._ticket("01", "first")
    if size_line is None:
        return body.replace("**Size:** S\n", "")
    return body.replace("**Size:** S\n", size_line + "\n")


def _sandbox(tmp_path, monkeypatch, size_line, legs_by_size=None):
    sb = tc.Sandbox(tmp_path, tickets=((T1, _ticket_with(size_line)),
                                       (T2, tc._ticket("02", "second"))))
    # an entry may have taught the bench's roster the key already: it is
    # replaced (or, for `None`, removed) rather than written a second time
    ini = sb.repo / "contest.ini"
    text = re.sub(r"(?m)^legs_by_size\s*=.*\n", "", ini.read_text(encoding="utf-8"))
    if legs_by_size is not None:
        text = text.replace("[contest]\n", f"[contest]\nlegs_by_size = {legs_by_size}\n", 1)
    if text != ini.read_text(encoding="utf-8"):
        ini.write_text(text, encoding="utf-8")
        tc._git(sb.repo, "commit", "-qam", "legs_by_size")
    monkeypatch.chdir(sb.repo)
    return sb


def _legs_ran(sb) -> int:
    """How many legs left a folder: `NN` alone is one, `NN.1 … NN.k` is k."""
    root = sb.out().parent
    numbered = sorted(p for p in root.glob(f"{ROUND:02d}.*") if p.is_dir())
    if numbered:
        return len(numbered)
    return 1 if (root / f"{ROUND:02d}" / "state.json").is_file() else 0


def _run(sb, spawn_holder, *extra):
    scenario = tc.SCENARIO_ONE_READY   # agent-a READY at once, agent-b never: every leg runs
    return tc.run_fake(sb, scenario, [*tc.RELAY_ARGV, *extra], spawn_holder)


# ─── P: ticket_size ─────────────────────────────────────────────────────────

def _size_of(tmp_path, text: str):
    path = tmp_path / "t.md"
    path.write_text(text, encoding="utf-8")
    return cli.ticket_size(path)


@pytest.mark.parametrize("line,want", [
    ("**Size:** S", "S"),
    ("**Size:** L", "L"),
    ("**Size:** XS", "XS"),
    ("**Size:** m", "M"),
    ("**Size:** S (measurement only)", "S"),
    ("**Size:**  L  ", "L"),
])
def test_P1_size_is_read_from_the_header(tmp_path, line, want):
    assert _size_of(tmp_path, _ticket_with(line)) == want


def test_P2_kc40s_L_with_its_size_note_is_L(tmp_path):
    text = _ticket_with("**Size:** L\n**Size note:** split in two legs if it runs long")
    assert _size_of(tmp_path, text) == "L"


def test_P3_missing_size_is_none(tmp_path):
    assert _size_of(tmp_path, _ticket_with(None)) is None


@pytest.mark.parametrize("line", ["**Size:** XXL", "**Size:** huge", "**Size:**"])
def test_P4_unknown_size_is_none(tmp_path, line):
    assert _size_of(tmp_path, _ticket_with(line)) is None


def test_P5_a_size_in_the_body_is_not_the_header(tmp_path):
    body = _ticket_with(None) + "\nA note: **Size:** L tickets get a relay.\n"
    # the header has no Size line; a mention far below it may be read or not,
    # but it must never turn an unsized ticket into a relay by itself
    assert _size_of(tmp_path, body) in (None, "L")


# ─── L: how many legs ───────────────────────────────────────────────────────

def test_L1_size_L_runs_three_legs_with_no_flag(tmp_path, monkeypatch, spawn_holder):
    sb = _sandbox(tmp_path, monkeypatch, "**Size:** L", "XS=1, S=1, M=1, L=3")
    code, _ = _run(sb, spawn_holder)
    assert code == 0
    assert _legs_ran(sb) == 3


def test_L2_size_S_runs_one_leg(tmp_path, monkeypatch, spawn_holder):
    sb = _sandbox(tmp_path, monkeypatch, "**Size:** S", "XS=1, S=1, M=1, L=3")
    code, _ = _run(sb, spawn_holder)
    assert code == 0
    assert _legs_ran(sb) == 1


def test_L3_no_size_runs_one_leg(tmp_path, monkeypatch, spawn_holder):
    sb = _sandbox(tmp_path, monkeypatch, None, "XS=1, S=1, M=1, L=3")
    code, _ = _run(sb, spawn_holder)
    assert code == 0
    assert _legs_ran(sb) == 1


def test_L4_legs_2_overrides_L(tmp_path, monkeypatch, spawn_holder):
    sb = _sandbox(tmp_path, monkeypatch, "**Size:** L", "XS=1, S=1, M=1, L=3")
    code, _ = _run(sb, spawn_holder, "--legs", "2")
    assert code == 0
    assert _legs_ran(sb) == 2


def test_L5_legs_2_overrides_S(tmp_path, monkeypatch, spawn_holder):
    sb = _sandbox(tmp_path, monkeypatch, "**Size:** S", "XS=1, S=1, M=1, L=3")
    code, _ = _run(sb, spawn_holder, "--legs", "2")
    assert code == 0
    assert _legs_ran(sb) == 2


def test_L6_legs_1_on_L_runs_one_leg_without_refusal(tmp_path, monkeypatch, spawn_holder, capsys):
    sb = _sandbox(tmp_path, monkeypatch, "**Size:** L", "XS=1, S=1, M=1, L=3")
    code, _ = _run(sb, spawn_holder, "--legs", "1")
    assert code == 0, capsys.readouterr().err
    assert _legs_ran(sb) == 1


def test_L7_legs_1_on_L_with_L1_mapping_is_not_refused(tmp_path, monkeypatch, spawn_holder, capsys):
    sb = _sandbox(tmp_path, monkeypatch, "**Size:** L", "XS=1, S=1, M=1, L=1")
    code, _ = _run(sb, spawn_holder, "--legs", "1")
    assert code == 0, capsys.readouterr().err
    assert _legs_ran(sb) == 1


def test_L8_the_mapping_is_read_from_contest_ini(tmp_path, monkeypatch, spawn_holder):
    """`M=2` in the roster: an `M` ticket runs two legs — the map is config, not a constant."""
    sb = _sandbox(tmp_path, monkeypatch, "**Size:** M", "XS=1, S=1, M=2, L=3")
    code, _ = _run(sb, spawn_holder)
    assert code == 0
    assert _legs_ran(sb) == 2


def test_L9_an_L_ticket_never_runs_one_leg_silently_with_the_key_unset(
        tmp_path, monkeypatch, spawn_holder):
    """No `legs_by_size` in the roster at all: the default (`L=3`) runs three
    legs, or the refusal fires — one silent leg is the bug the ticket closes."""
    sb = _sandbox(tmp_path, monkeypatch, "**Size:** L")
    code, _ = _run(sb, spawn_holder)
    if code == 0:
        assert _legs_ran(sb) == 3
    else:
        assert _legs_ran(sb) == 0


# ─── R: the refusal ─────────────────────────────────────────────────────────

def _refused(tmp_path, monkeypatch, spawn_holder, capsys, caplog):
    caplog.set_level(logging.INFO)
    sb = _sandbox(tmp_path, monkeypatch, "**Size:** L", "XS=1, S=1, M=1, L=1")
    code, fake = _run(sb, spawn_holder)
    out = capsys.readouterr()
    text = out.out + out.err + caplog.text
    return sb, code, fake, text


def test_R1_L_in_one_leg_exits_nonzero(tmp_path, monkeypatch, spawn_holder, capsys, caplog):
    sb, code, _, text = _refused(tmp_path, monkeypatch, spawn_holder, capsys, caplog)
    assert code != 0, text


def test_R2_the_refusal_names_the_size_and_the_override(
        tmp_path, monkeypatch, spawn_holder, capsys, caplog):
    sb, code, _, text = _refused(tmp_path, monkeypatch, spawn_holder, capsys, caplog)
    assert code != 0
    assert re.search(r"\bL\b", text), text
    assert "--legs" in text, text


def test_R3_the_refusal_names_the_leg_count_it_would_have_used(
        tmp_path, monkeypatch, spawn_holder, capsys, caplog):
    sb, code, _, text = _refused(tmp_path, monkeypatch, spawn_holder, capsys, caplog)
    assert code != 0
    lines = [re.sub(r"--legs\s+\S+", "", ln) for ln in text.splitlines()
             if "intake" in ln or "refus" in ln.lower() or "--legs" in ln]
    # `L=1` in the roster: the count it would have used is 1 — said as a number
    # next to "leg", and never as the 3 of the shipped default
    assert any(re.search(r"\b1\s*leg", ln) for ln in lines), text
    assert not any(re.search(r"\b3\s*legs?\b", ln) for ln in lines), text


def test_R4_the_refusal_prepares_no_worktree_and_no_session(
        tmp_path, monkeypatch, spawn_holder, capsys, caplog):
    sb, code, fake, text = _refused(tmp_path, monkeypatch, spawn_holder, capsys, caplog)
    assert code != 0
    assert not any(sb.rounds.iterdir()), list(sb.rounds.iterdir())
    assert len(tc._git(sb.repo, "worktree", "list").splitlines()) == 1
    assert not tc._sessions(fake)
    assert _legs_ran(sb) == 0


# ─── N: where the count came from ───────────────────────────────────────────

def _state_texts(sb) -> list:
    root = sb.out().parent
    return [p.read_text(encoding="utf-8") for p in sorted(root.glob(f"{ROUND:02d}*/state.json"))]


def test_N1_state_json_names_the_leg_count_and_size_as_the_source(
        tmp_path, monkeypatch, spawn_holder):
    sb = _sandbox(tmp_path, monkeypatch, "**Size:** L", "XS=1, S=1, M=1, L=3")
    code, _ = _run(sb, spawn_holder)
    assert code == 0
    texts = _state_texts(sb)
    assert texts
    for text in texts:
        data = json.loads(text)
        flat = json.dumps({k: v for k, v in data.items() if k != "agents"})
        assert re.search(r"\b3\b", flat), flat
        assert "size" in flat.lower(), flat


def test_N2_state_json_names_the_flag_as_the_source(tmp_path, monkeypatch, spawn_holder):
    sb = _sandbox(tmp_path, monkeypatch, "**Size:** L", "XS=1, S=1, M=1, L=3")
    code, _ = _run(sb, spawn_holder, "--legs", "2")
    assert code == 0
    for text in _state_texts(sb):
        flat = json.dumps({k: v for k, v in json.loads(text).items() if k != "agents"})
        assert "flag" in flat.lower() or "--legs" in flat, flat


def test_N3_the_round_log_names_the_count_and_its_source(
        tmp_path, monkeypatch, spawn_holder, capsys, caplog):
    caplog.set_level(logging.INFO)
    sb = _sandbox(tmp_path, monkeypatch, "**Size:** L", "XS=1, S=1, M=1, L=3")
    code, _ = _run(sb, spawn_holder)
    assert code == 0
    out = capsys.readouterr()
    text = out.out + out.err + caplog.text
    assert any("3" in ln and "size" in ln.lower() and "leg" in ln.lower()
               for ln in text.splitlines()), text


# ─── I: the shipped roster ──────────────────────────────────────────────────

def test_I1_contest_ini_ships_legs_by_size():
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
    parser.read(REPO_ROOT / "contest.ini", encoding="utf-8")
    raw = parser.get("contest", "legs_by_size", fallback="")
    pairs = dict(p.strip().upper().split("=", 1) for p in raw.replace(";", ",").split(",") if "=" in p)
    pairs = {k.strip(): int(v) for k, v in pairs.items()}
    assert pairs == {"XS": 1, "S": 1, "M": 1, "L": 3}, raw


def test_I2_the_shipped_roster_still_loads():
    from tools.contest.roster import load_roster
    assert load_roster(REPO_ROOT / "contest.ini") is not None


# fixtures from the bench this suite drives
spawn_holder = tc.spawn_holder
gate_key = tc.gate_key
