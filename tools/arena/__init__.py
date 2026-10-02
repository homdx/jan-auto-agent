"""tools/arena — EPIC AR: `arena <object> <verb>`, one short command for the contest flow.

AR-1 is the skeleton only: the parser (`cli.py`), the output and masking rules
(`output.py`) and the entry points. Every later ticket adds one `<object> <verb>`
to `cli.OBJECTS`; nothing here reaches into `tools/contest/`.
"""
