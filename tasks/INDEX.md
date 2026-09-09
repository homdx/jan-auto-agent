# Task index

5 confirmed defect(s), highest severity first.

| # | severity | finding | ticket |
|---|---|---|---|
| 1 | MEDIUM | `tools/auto/controller.py::AutoController.config` | [01-autocontroller-config.md](01-autocontroller-config.md) |
| 2 | MEDIUM | `tools/file_reader.py::list_py_files` | [02-list-py-files.md](02-list-py-files.md) |
| 3 | MEDIUM | `tools/search_agent.py::_DEFAULT_SKIP_DIRS` | [03-default-skip-dirs.md](03-default-skip-dirs.md) |
| 4 | NONE | `tools/auto/architect.py::_serialise_candidates` | [04-serialise-candidates.md](04-serialise-candidates.md) |
| 5 | MEDIUM | `tools/auto/arch_probe.py::ArchProbe.last_by_op` | [05-archprobe-last-by-op.md](05-archprobe-last-by-op.md) |

## Working these

One ticket per commit. Verify before fixing — the pipeline that produced these measured an 88% noise floor on its own proposals, and adjudication narrowed that but does not replace reading the code.