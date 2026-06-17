import sys
import time
import threading


class Spinner:
    """
    Braille-dot terminal spinner for blocking LLM wait states.

    Usage as a context manager (preferred):
        with Spinner("Calling validator"):
            result = blocking_llm_call()

    Usage manual:
        s = Spinner("Thinking")
        s.start()
        result = blocking_llm_call()
        s.stop()
    """

    def __init__(self, message: str = "Processing"):
        self.message = message
        self.frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self.idx = 0
        self.running = False
        self._thread: threading.Thread | None = None

    def spin(self):
        """Advance one frame (used internally by the background thread)."""
        sys.stdout.write(f"\r{self.frames[self.idx % len(self.frames)]} {self.message}...")
        sys.stdout.flush()
        self.idx += 1

    def _run(self):
        while self.running:
            self.spin()
            time.sleep(0.08)

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        if self._thread is not None:
            self._thread.join(timeout=0.5)
            self._thread = None
        # Clear the spinner line so subsequent output starts cleanly.
        sys.stdout.write("\r" + " " * (len(self.message) + 6) + "\r")
        sys.stdout.flush()

    # ── context-manager support ──────────────────────────────────────────
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()


def stream_tracker():
    """
    Returns (on_token, stats) for wrapping request_completion's on_token kwarg.

    on_token(t): write token t to stdout and update counters.
    stats(): return a formatted string like "(42.3 tok/s, 156 tok)"
             to print after streaming ends. Returns "" if no tokens received.

    Usage::
        on_tok, tok_stats = stream_tracker()
        result = request_completion(..., stream=True, on_token=on_tok)
        print()
        if s := tok_stats():
            print(f"[{ts()}] {s}")
    """
    state: dict = {"n": 0, "t0": None}

    def on_token(t: str) -> None:
        if state["t0"] is None:
            state["t0"] = time.time()
        state["n"] += 1
        sys.stdout.write(t)
        sys.stdout.flush()

    def stats() -> str:
        n = state["n"]
        t0 = state["t0"]
        if n == 0 or t0 is None:
            return ""
        elapsed = time.time() - t0
        if elapsed < 0.05:
            return f"({n} tok)"
        return f"({n / elapsed:.1f} tok/s, {n} tok)"

    return on_token, stats
