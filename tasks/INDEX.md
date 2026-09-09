# Task index

2 confirmed defect(s), highest severity first.

| # | severity | finding | ticket |
|---|---|---|---|
| 1 | LOW | `tools/search_agent.py::_DEFAULT_SKIP_DIRS` | [01-default-skip-dirs.md](01-default-skip-dirs.md) |
| 2 | LOW | `tools/auto/arch_probe.py::ArchProbe.last_by_op` | [02-archprobe-last-by-op.md](02-archprobe-last-by-op.md) |

## Working these

One ticket per commit. Verify before fixing — the pipeline that produced these measured an 88% noise floor on its own proposals, and adjudication narrowed that but does not replace reading the code.