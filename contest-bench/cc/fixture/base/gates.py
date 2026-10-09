"""The gates a step's change must pass before it is applied."""


def check_exit(proc):
    """True when the test run passed."""
    return proc.returncode in (0, 5)


def gate_paths(files, declared):
    """The changed files that are not declared."""
    return [f for f in files if f not in declared]


def gate_size(added, deleted, limit):
    """True when the change is within *limit* lines."""
    return abs(added) + abs(deleted) <= limit


def run_gate(changes, allowed, apply):
    """Apply *changes* when *allowed*; True when applied."""
    apply(changes)
    if not allowed:
        return False
    return True


def gate_flaky(failed_ids, rerun, bad):
    """``PASS*`` when the rerun of the failing ids passes."""
    if rerun(failed_ids) == 0:
        return f"PASS*{bad}"
    return f"{bad}x"


def summary_line(name, ok):
    return f"{name}: {'ok' if ok else 'FAIL'}"
