"""TicketStore.create must not let two racing writers both succeed for one id."""
import threading

from tools.auto.ticket_store import TicketAlreadyExists, TicketStore, make_ticket


def _ticket(title):
    return make_ticket("T-1", "bug", "AUTO-T1", title, "body")


def test_racing_creates_of_one_id_yield_exactly_one_winner(tmp_path, monkeypatch):
    store = TicketStore(tmp_path)
    # Hold both threads between the exists() check and the write, so each has
    # already seen "no such ticket" — the window the old code left open.
    barrier = threading.Barrier(2, timeout=10)
    real = TicketStore._ensure_dir

    def gated(self):
        real(self)
        barrier.wait()

    monkeypatch.setattr(TicketStore, "_ensure_dir", gated)
    results = {}

    def run(name):
        try:
            store.create(_ticket(name))
            results[name] = "ok"
        except TicketAlreadyExists:
            results[name] = "exists"

    ts = [threading.Thread(target=run, args=(n,)) for n in ("a", "b")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results.values()) == ["exists", "ok"], results
    winner = next(n for n, r in results.items() if r == "ok")
    assert store.get("T-1")["title"] == winner


def test_create_leaves_no_temp_file_when_the_id_exists(tmp_path):
    store = TicketStore(tmp_path)
    store.create(_ticket("a"))
    try:
        store.create(_ticket("b"))
    except TicketAlreadyExists:
        pass
    assert [p.name for p in tmp_path.iterdir()] == ["T-1.json"]
