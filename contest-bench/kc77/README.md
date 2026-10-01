# KC-77 bench — two-leg relay on an external repo

The live run is `2legs/run_2legs.sh` (see `2legs/HOW-WE-RUN-2LEGS.md`); its checker
is `2legs/check_2legs.py`. Results stay in `$TARGET/contest-out/`, never in git.

`leg_record.py` writes a leg record by hand for step 5 of the runbook (a new Kilo
session continuing an agent that did not finish). With `--legs 2` the round writes
`contest-out/NN.1/<agent>.leg.md` itself (KC-43/KC-74).
