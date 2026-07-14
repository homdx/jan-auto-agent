"""Toy module mirroring the real prompt_store.get_current guarded-access case.

`stack[-1]` below is GUARDED: an early return above it means the index is
never reached with an empty stack.
"""


def get_current(stack):
    """Return the top of the prompt stack, or None if it's empty."""
    if not stack:
        return None
    return stack[-1]
