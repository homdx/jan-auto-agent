"""Terminal output helpers: a spinner and streamed-token writers.

Everything here is *cosmetic* output. None of it may ever raise into the
caller: the LLM reply it accompanies has already been paid for and, once the
process's stdout is closed (a pipe to ``head``, a cron job with no terminal,
``| tail -1``), ``sys.stdout.write`` raises ``BrokenPipeError`` on every
subsequent call. A raised write inside a call's ``try`` therefore turns a
received answer into a failed call — see :func:`safe_stdout_write`.
"""

import sys
import time
import threading

__all__ = ["Spinner", "stream_tracker", "safe_stdout_write", "safe_print"]


def safe_stdout_write(t: str) -> None:
    """Write *t* to stdout, swallowing a closed/broken stdout.

    When stdout is piped to a command that closes early (``| head``) or the
    process is run without a terminal, ``sys.stdout.write`` raises
    ``BrokenPipeError`` — and stays broken, so the second write raises too.
    Streaming output is cosmetic: the token has already been received and is
    appended to the caller's own buffer, so the only correct behaviour is to
    drop the write and carry on.

    Shared by every streaming call site (validator, improvement, architect,
    coder) so a closed stdout can never be classified as an LLM failure.
    """
    try:
        sys.stdout.write(t)
        sys.stdout.flush()
    except UnicodeEncodeError:
        # A stdout that is not UTF-8 (a Windows console redirected to a file, a latin-1 locale) gets the
        # characters it can hold; the 🧠 of a banner is not a failed call.
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            sys.stdout.write(t.encode(encoding, "replace").decode(encoding, "replace"))
            sys.stdout.flush()
        except (OSError, ValueError, LookupError):
            pass
    except (OSError, ValueError):   # BrokenPipeError is an OSError, a closed file a ValueError
        pass


def safe_print(line: str = "") -> None:
    """``print(line)`` that cannot raise on a closed stdout — the same
    contract as :func:`safe_stdout_write` for a whole line."""
    safe_stdout_write(line + "\n")


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
        try:
            sys.stdout.write(f"\r{self.frames[self.idx % len(self.frames)]} {self.message}...")
            sys.stdout.flush()
        except UnicodeEncodeError:
            # AUDIT3-18: a stdout that cannot hold the Braille frames gets ASCII ones.
            self.frames = ["|", "/", "-", "\\"]
            safe_stdout_write(f"\r{self.frames[self.idx % len(self.frames)]} {self.message}...")
        except (BrokenPipeError, OSError):
            # Stdout closed early (e.g. | head): stop the thread instead of spinning on a dead pipe.
            self.running = False
            return
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
        try:
            sys.stdout.write("\r" + " " * (len(self.message) + 6) + "\r")
            sys.stdout.flush()
        except (BrokenPipeError, OSError, UnicodeEncodeError):
            pass

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
        try:
            sys.stdout.write(t)
            sys.stdout.flush()
        except UnicodeEncodeError:
            # AUDIT3-18: a non-UTF-8 stdout must not kill a paid streaming call.
            safe_stdout_write(t)
        except (BrokenPipeError, OSError):
            # Stdout closed early: streaming output is cosmetic, so keep tracking tokens and do not crash.
            pass

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
