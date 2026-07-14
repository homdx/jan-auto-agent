"""Toy module mirroring the real view_trace.find_trace_file guarded-access case.

`candidates[-1]` below is GUARDED by a `len(...) == 0` early return — a
different guard *shape* than prompt_store.get_current's `if not stack`, so
the dataflow check (COLLECT-7) has more than one guard pattern to prove it
recognizes.
"""


def find_trace_file(candidates):
    """Return the most recent candidate trace file, or None if there are none."""
    if len(candidates) == 0:
        return None
    return candidates[-1]
