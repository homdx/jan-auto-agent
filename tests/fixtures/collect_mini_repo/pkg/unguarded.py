"""Toy module: a deliberately UNGUARDED indexed access (COLLECT-7 negative case).

`items[-1]` below has no preceding guard of any kind — this is the control
case that dataflow analysis must still flag as UNGUARDED.
"""


def last_item(items):
    """Return the last item. Will raise IndexError on an empty list."""
    return items[-1]
