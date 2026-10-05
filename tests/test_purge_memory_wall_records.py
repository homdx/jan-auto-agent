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
