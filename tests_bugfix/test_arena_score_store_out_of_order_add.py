"""ScoreStore.add: a record whose `at` is older than one already stored is kept, not read as future-dated."""
from tools.arena.models import ScoreStore


def _rec(model, at):
    return {"provider": "kp", "model": model, "via": "kilo", "score": 15, "max": 15,
            "error": "", "at": at}


def test_older_at_added_after_a_newer_one_keeps_both(tmp_path):
    store = ScoreStore(tmp_path / "scores.json", 30)
    now = 1_800_000_000.0
    # two worker threads stamp their records a moment apart; the later stamp lands first
    store.add(_rec("m1", now + 0.002), now + 0.002)
    store.add(_rec("m0", now), now)
    models = sorted(r["model"] for r in store.load(now + 1))
    assert models == ["m0", "m1"]


def test_many_out_of_order_adds_lose_nothing(tmp_path):
    store = ScoreStore(tmp_path / "scores.json", 30)
    now = 1_800_000_000.0
    for i in reversed(range(9)):
        store.add(_rec(f"m{i}", now + i * 0.001), now + i * 0.001)
    assert len(store.load(now + 1)) == 9
