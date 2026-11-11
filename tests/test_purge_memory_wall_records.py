"""scripts/purge_memory_wall_records.py drops only the records Kilo's own `Compaction exhausted` wrote."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import purge_memory_wall_records as purge

WALL_MESSAGE = ("Compaction exhausted: context still exceeds model limits "
                "after 3 attempts")
PROVIDER_MESSAGE = "the request exceeds the model's maximum context length"
NOW = 1_791_000_000.0


def _record(agent="apertus-70b-instruct", round_="157", at=NOW, **over) -> dict:
    base = {"at": at, "round": round_, "agent": agent, "provider": "publicai",
            "model": "swiss-ai/apertus-70b-instruct", "limit": None, "last_ok": 22_561,
            "prompt": None, "output": None, "grew": 473}
    base.update(over)
    return base


def _events(contest_out: Path, round_, agent, *messages_done_at) -> None:
    """The agent's `events.jsonl`: one assistant message per (text, completed_at)."""
    folder = contest_out / str(round_) / agent
    folder.mkdir(parents=True, exist_ok=True)
    lines = []
    for text, done in messages_done_at:
        lines.append(json.dumps({"info": {"role": "assistant",
                                          "time": {"created": int((done - 8) * 1000),
                                                   "completed": int(done * 1000)},
                                          "error": {"name": "ContextOverflowError",
                                                    "data": {"message": text}}}}))
    (folder / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _memory(tmp_path, records) -> Path:
    path = tmp_path / "contest-out" / "context-memory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    return path


def test_a_record_a_compaction_exhausted_of_its_own_agent_wrote_is_dropped(tmp_path):
    out = tmp_path / "contest-out"
    _events(out, "157", "apertus-70b-instruct", (WALL_MESSAGE, NOW - 9))
    keep, drop = purge.split([_record()], out)
    assert (keep, len(drop)) == ([], 1)


def test_a_providers_own_overflow_is_never_dropped(tmp_path):
    """The provider's words in the events, or a record that names a number."""
    out = tmp_path / "contest-out"
    _events(out, "157", "apertus-70b-instruct", (PROVIDER_MESSAGE, NOW - 9))
    named = _record(agent="other", limit=262_144, output=32_000)
    _events(out, "157", "other", (WALL_MESSAGE, NOW - 9))      # the wall, but a named limit
    wordless = _record()
    keep, drop = purge.split([wordless, named], out)
    assert drop == [] and keep == [wordless, named]


def test_a_wall_that_is_not_just_before_the_record_does_not_count(tmp_path):
    out = tmp_path / "contest-out"
    _events(out, "157", "apertus-70b-instruct", (WALL_MESSAGE, NOW - 600),   # long before
            (WALL_MESSAGE, NOW + 30))                                         # after it
    keep, drop = purge.split([_record()], out)
    assert drop == [] and len(keep) == 1


def test_a_record_whose_round_folder_is_gone_stays(tmp_path):
    keep, drop = purge.split([_record()], tmp_path / "contest-out")
    assert drop == [] and len(keep) == 1


def test_a_record_that_is_no_record_stays(tmp_path):
    out = tmp_path / "contest-out"
    junk = ["x", None, {"at": "soon"}, {"at": True, "agent": "a", "round": "1"}]
    keep, drop = purge.split(junk, out)
    assert drop == [] and keep == junk


def test_the_default_is_a_dry_run_and_apply_keeps_the_old_file(tmp_path, capsys):
    out = tmp_path / "contest-out"
    _events(out, "157", "apertus-70b-instruct", (WALL_MESSAGE, NOW - 9))
    kept = _record(agent="glm", limit=131_072, last_ok=98_777, round_="157")
    memory = _memory(tmp_path, [_record(), kept])
    before = memory.read_text(encoding="utf-8")

    assert purge.main(["--repo", str(tmp_path)]) == 0
    assert "keep 1, drop 1" in capsys.readouterr().out
    assert memory.read_text(encoding="utf-8") == before            # nothing written

    assert purge.main(["--repo", str(tmp_path), "--apply"]) == 0
    assert json.loads(memory.read_text(encoding="utf-8")) == [kept]
    backup = memory.with_name("context-memory.json" + purge.BACKUP_SUFFIX)
    assert backup.read_text(encoding="utf-8") == before

    # nothing left to drop: a second run writes nothing and touches no backup
    backup.write_text("sentinel", encoding="utf-8")
    assert purge.main(["--repo", str(tmp_path), "--apply"]) == 0
    assert backup.read_text(encoding="utf-8") == "sentinel"


def test_a_missing_or_broken_memory_file_is_one_line_and_exit_one(tmp_path, capsys):
    assert purge.main(["--repo", str(tmp_path)]) == 1
    assert "context-memory.json" in capsys.readouterr().err
    memory = _memory(tmp_path, [])
    memory.write_text("{not json", encoding="utf-8")
    assert purge.main(["--repo", str(tmp_path)]) == 1
    memory.write_text('{"a": 1}', encoding="utf-8")
    assert purge.main(["--repo", str(tmp_path)]) == 1
    assert "not a list" in capsys.readouterr().err


def test_a_second_apply_with_a_new_record_refuses_to_overwrite_the_backup(tmp_path, capsys):
    out = tmp_path / "contest-out"
    first = _record(agent="apertus-70b-instruct", round_="157")
    second = _record(agent="glm", round_="158")
    _events(out, "157", "apertus-70b-instruct", (WALL_MESSAGE, NOW - 9))    # 9 s before
    _events(out, "158", "glm", (WALL_MESSAGE, NOW - 45))                   # 45 s before
    memory = _memory(tmp_path, [first, second])
    original = memory.read_text(encoding="utf-8")
    backup = memory.with_name("context-memory.json" + purge.BACKUP_SUFFIX)

    # a narrow window drops only the first record: a normal first purge
    assert purge.main(["--repo", str(tmp_path), "--apply", "--window", "30"]) == 0
    capsys.readouterr()
    assert json.loads(memory.read_text(encoding="utf-8")) == [second]
    assert backup.read_text(encoding="utf-8") == original

    # a wider window wants to drop the second one: the backup is already here
    assert purge.main(["--repo", str(tmp_path), "--apply", "--window", "60"]) == 1
    err = capsys.readouterr().err
    assert "refusing" in err and "already exists" in err
    assert json.loads(memory.read_text(encoding="utf-8")) == [second]       # nothing written
    assert backup.read_text(encoding="utf-8") == original                  # the first one kept

    # a rerun with nothing new to drop still succeeds and touches no backup
    assert purge.main(["--repo", str(tmp_path), "--apply", "--window", "30"]) == 0
    capsys.readouterr()
    assert json.loads(memory.read_text(encoding="utf-8")) == [second]
    assert backup.read_text(encoding="utf-8") == original


# ── beyond the first/second purge: backups that are not a plain file, and what must still work ──
def _wall_memory(tmp_path, *agents):
    out = tmp_path / "contest-out"
    for agent in agents:
        _events(out, "157", agent, (WALL_MESSAGE, NOW - 9))
    return _memory(tmp_path, [_record(agent=a) for a in agents])


def test_an_existing_backup_that_is_a_directory_is_refused_not_copied_into(tmp_path, capsys):
    memory = _wall_memory(tmp_path, "a")
    backup = memory.with_name("context-memory.json" + purge.BACKUP_SUFFIX)
    backup.mkdir()
    before = memory.read_text(encoding="utf-8")
    assert purge.main(["--repo", str(tmp_path), "--apply"]) == 1
    assert memory.read_text(encoding="utf-8") == before and list(backup.iterdir()) == []


def test_a_backup_that_is_a_symlink_never_writes_through_to_its_target(tmp_path, capsys):
    memory = _wall_memory(tmp_path, "a")
    backup = memory.with_name("context-memory.json" + purge.BACKUP_SUFFIX)
    precious = tmp_path / "precious.txt"
    precious.write_text("do not touch\n", encoding="utf-8")
    backup.symlink_to(precious)
    assert purge.main(["--repo", str(tmp_path), "--apply"]) == 1
    assert precious.read_text(encoding="utf-8") == "do not touch\n"
    precious.unlink()                      # now the link dangles: `exists()` is False, the link is still there
    before = memory.read_text(encoding="utf-8")
    assert purge.main(["--repo", str(tmp_path), "--apply"]) == 1
    assert not precious.exists() and memory.read_text(encoding="utf-8") == before


def test_an_empty_leftover_backup_is_still_somebodys_backup(tmp_path, capsys):
    memory = _wall_memory(tmp_path, "a")
    backup = memory.with_name("context-memory.json" + purge.BACKUP_SUFFIX)
    backup.write_text("", encoding="utf-8")
    assert purge.main(["--repo", str(tmp_path), "--apply"]) == 1
    assert backup.read_text(encoding="utf-8") == ""


def test_a_dry_run_with_a_backup_present_writes_nothing_and_succeeds(tmp_path, capsys):
    memory = _wall_memory(tmp_path, "a")
    backup = memory.with_name("context-memory.json" + purge.BACKUP_SUFFIX)
    backup.write_text("older\n", encoding="utf-8")
    before = memory.read_text(encoding="utf-8")
    assert purge.main(["--repo", str(tmp_path)]) == 0
    assert memory.read_text(encoding="utf-8") == before and backup.read_text(encoding="utf-8") == "older\n"


def test_a_run_with_nothing_to_drop_makes_no_backup(tmp_path, capsys):
    memory = _memory(tmp_path, [_record(limit=262_144)])
    assert purge.main(["--repo", str(tmp_path), "--apply"]) == 0
    assert not memory.with_name("context-memory.json" + purge.BACKUP_SUFFIX).exists()
