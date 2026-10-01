# KC-76 bench — `--target REPO_PATH`

Offline acceptance is `tests/test_contest_target.py` and the `--target` tests in
`tests/test_contest_cli.py`. The live check is KC-77's `2legs/run_2legs.sh`: it runs
two rounds with `--target $TARGET` from the jan-auto-agent checkout (11/11 on both
machines, 2026-09-29).
