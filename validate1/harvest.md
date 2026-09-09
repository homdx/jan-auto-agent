# Validation harvest

5 reviewers · 96 distinct findings · 261 judgements

Reviewers: agnes-2-5-flash, dots-3-note, laguna-s-2-1, sensenova-6-8-flash-lite, step-3-7-flash

**Noise floor: 88%** of findings were dismissed by every reviewer who looked at them.


## Act on these (2)

### `tools/search_agent.py::_DEFAULT_SKIP_DIRS` — LOW · **NEW**

*SearchAgent aliases the module-level skip-dir default, so one instance's append poisons every later instance*

- **votes:** sensenova-6-8-flash-lite=confirmed
- **impact:** latent — a caller appending to one SearchAgent.skip_dirs silently adds that directory to the exclusion set of every SearchAgent() constructed later, so file discovery prunes dirs the operator never configured and the agent misses files
- **evidence:** _DEFAULT_SKIP_DIRS = [;. .git, __pycache__, venv, .venv, .tox,; . node_modules, dist, build, .mypy_cache, .pytest_cache,; ]
- **disproof:** The disproof that would make this a non-issue: a defensive copy at assignment. It is not present — line 94 is 'self.skip_dirs = _DEFAULT_SKIP_DIRS if skip_dirs is None else skip_dirs', a reference bind with no list(...) wrapper. Proven by identity check: SearchAgent().skip_dirs is _DEFAULT_SKIP_DIRS -> True, and a second instance is the same object too, so one append reaches both. State IS used: t
- **repro:** python3 -c "from tools.search_agent import SearchAgent; a=SearchAgent(); a.skip_dirs.append('secret_dir'); b=SearchAgent(); print('secret_dir' in b.skip_dirs)" -> True. Verified live in this repo.

### `tools/auto/arch_probe.py::ArchProbe.last_by_op` — LOW · **NEW**

*last_by_op hands out a shallow copy that still shares its hit lists*

- **votes:** sensenova-6-8-flash-lite=confirmed
- **impact:** A caller that mutates the returned dict's nested [hits, misses] list changes ArchProbe's own tally for the current round, so last_by_op_str() reports a wrong count. That string is the by_op trace parameter written into the probe_result event (architect.py:1937, :2009) and into the unresolved-decline text (architect.py:1688), so the trace can show facts=999/1 for a round that actually resolved 0 hi
- **evidence:** @property / def last_by_op(self) -> dict: / return dict(self._last_by_op)  # tools/auto/arch_probe.py:581-584, with the nested values allocated as mutable lists at :865 via _tally = self._last_by_op.setdefault(op.op, [0, 0])
- **disproof:** Ran the exact disproof step the calibration used for the _progress false positive: does the container hold nested mutable values? Yes - every value is a two-element list built by setdefault(op.op, [0, 0]) at :865, so dict() is not a complete copy here. Verified: the outer dict is a new object but the inner list is the same object, and writing through it changed the owner. Checked the three sibling
- **repro:** Build ArchProbe with a stub bridge over a temp dir, call execute([ProbeOp('facts','f')]), then d = probe.last_by_op; d['facts'][0] = 999. Verified live: probe._last_by_op becomes {'facts': [999, 1]} and probe.last_by_op_str() becomes 'facts=999/1' where it was 'facts=0/1' before.


## Disputed — a human decides (5)

### `tools/metrics_collector.py::MetricsCollector._load_all_cached` — MEDIUM

*MetricsCollector._load_all_cached returns the live cache list, but its only consumer already copies*

- **votes:** agnes-2-5-flash=false, dots-3-note=false, laguna-s-2-1=false, sensenova-6-8-flash-lite=confirmed, step-3-7-flash=confirmed
- **impact:** latent — a caller that invokes the private accessor and appends or re-sorts the returned list poisons self._cache, and the next record() then persists that poisoned list into metrics.json, silently destroying or fabricating telemetry history
- **evidence:** return self._cache
- **disproof:** Reachability check that would make this a non-issue: whether any caller other than record() touches the cache. Grep of _load_all_cached across the whole repo returns exactly two hits — the definition at metrics_collector.py:45 and the call at :65 inside record(). The single consumer immediately neutralises the leak: :66 'records = list(records)  # don't mutate the cached list in place', an explici
- **repro:** mc = MetricsCollector(tmp/'m.json'); mc.record(run1); cached = mc._load_all_cached(); cached.append({'fabricated': True}); mc.record(run2); the persisted metrics.json now holds run1, the fabricated entry, and run2

### `tools/auto/controller.py::AutoController.config` — MEDIUM

*AutoController.config exposes live ConfigParser, no defensive copy*

- **votes:** agnes-2-5-flash=false, dots-3-note=false, laguna-s-2-1=confirmed, step-3-7-flash=false
- **impact:** self.config is a plain mutable configparser.ConfigParser (line 346, no @property). Any external caller can call config.set/remove_section/read_string to silently change task_mode, gate order, probe settings, or limits mid-run. Config state drives decisions: task_mode normalization (line 384), resolve_gate_order, RunLimits.from_config (line 416), probe config (lines 402-403).
- **evidence:** self.config = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))  # line 346 — plain attribute, no @property/defensive-copy
- **disproof:** Disproving fact 1: self.config is a @property returning a copy. grep for 'def config' in controller.py: no match on AutoController (only RunLimits properties at lines 136-149). Line 346 is a plain assignment → defect holds. Disproving fact 2 (for HIGH/CRITICAL): an external caller corrupts state today via controller.config.set(). grep for config.set/cfg.set/config.remove in tools/auto/: zero match
- **repro:** orc = AutoController(...); orc.config.set('auto','task_mode','bogus') changes the value read at line 384 with no validation.

### `tools/auto/state.py::StateStore.resume_info` — MEDIUM

*resume_info returns detached task dicts but lists may be live*

- **votes:** agnes-2-5-flash=confirmed, dots-3-note=false, sensenova-6-8-flash-lite=false, step-3-7-flash=false
- **impact:** resume_info()['pending'] and ['in_progress'] are fresh lists but contain detached dicts; however the lists themselves are newly created each call
- **evidence:** in_progress = [self._detached(t) for t in tasks if t['status'] == STATUS_IN_PROGRESS]  # line 393; pending = [self._detached(t) for t in tasks if ...]  # line 394
- **disproof:** tested: two calls to resume_info() return distinct list objects. The lists are newly created each call via list comprehensions.

### `tools/search_agent.py::SearchAgent.skip_dirs` — MEDIUM

*SearchAgent.skip_dirs is a public mutable list, aliases shared _DEFAULT_SKIP_DIRS*

- **votes:** dots-3-note=false, laguna-s-2-1=confirmed
- **impact:** self.skip_dirs is a public mutable list (line 94). When skip_dirs is None (default), it aliases the shared module-level _DEFAULT_SKIP_DIRS list, so any mutation would corrupt the exclusion set for ALL SearchAgent instances. It is used at line 257: list_source_files(base_dir, skip_dirs=self.skip_dirs) — mutating it would change which directories are skipped during file collection, a real decision.
- **evidence:** self.skip_dirs: List[str] = _DEFAULT_SKIP_DIRS if skip_dirs is None else skip_dirs  # search_agent.py:94 — plain mutable list, aliases shared constant when default
- **disproof:** The disproving fact: self.skip_dirs is NOT mutable (tuple/frozenset), NOT reachable from outside, or NOT used in decisions. Check: (1) type is List[str] (line 80 param, line 94 assignment) — mutable, not immutable. (2) It is a public attribute with no @property or setter — directly reachable as agent.skip_dirs. (3) Used at line 257: list_source_files(base_dir, skip_dirs=self.skip_dirs) → file filt
- **repro:** sa = SearchAgent(); sa.skip_dirs.append('secret_dir'); _DEFAULT_SKIP_DIRS now contains 'secret_dir' — all future SearchAgent instances skip it too

### `tools/search_agent.py::SearchAgent.__init__` — MEDIUM · **NEW**

*SearchAgent aliases the module-level _DEFAULT_SKIP_DIRS list*

- **votes:** dots-3-note=confirmed, step-3-7-flash=false
- **impact:** Any SearchAgent built with skip_dirs=None shares the module-level _DEFAULT_SKIP_DIRS list; a mutation of one agent's skip_dirs (append/remove) silently changes every other default agent's exclusion set, and list_source_files() then walks a different tree
- **evidence:** tools/search_agent.py:94 self.skip_dirs: List[str] = _DEFAULT_SKIP_DIRS if skip_dirs is None else skip_dirs — no copy, so the default path stores a reference to the module-level list at :70
- **disproof:** Would be a false positive only if _DEFAULT_SKIP_DIRS were immutable or copied on assignment; it is a plain module-level list and the assignment is a direct reference copy, so the alias is real
- **repro:** Build SearchAgent() (skip_dirs=None) twice; mutate a.skip_dirs.append('secret_dir'); assert b.skip_dirs == ['secret_dir', ...] — b's list is the same object as a's


## Already fixed (5)

### `tools/auto/state.py::StateStore.get_task` — NONE

*get_task/all_tasks/resume_info already return deep copies via _detached (M1 fix in place)*

- **votes:** agnes-2-5-flash=false, dots-3-note=false, laguna-s-2-1=false, sensenova-6-8-flash-lite=already, step-3-7-flash=already
- **impact:** none — deepcopy means nested target_files/cited_locations/dependencies are all detached
- **evidence:** return self._detached(t)  # state.py:411 get_task — comment line 405: 'returns a copy, not the live object'; all_tasks line 418: [self._detached(t) ...] — comment 416-417: M1 fixed shallow copy
- **disproof:** The entry's premise is that 'the existing suite in tests/test_auto_2.py never asserts the detachment contract'. Half is right and half is wrong: tests/test_auto_2.py indeed has no detachment assertion (only 'is not None' checks at :167 and :326), but the contract IS pinned, in the right tier — tests_bugfix/test_bugfix_m1_get_task_reference_leak.py contains 10 tests covering exactly the scenario th
- **repro:** none — no nested alias exists

### `tools/auto/state.py::StateStore._detached` — NONE

*Accessor copy-on-read contract already enforced by tests_bugfix*

- **votes:** agnes-2-5-flash=false, dots-3-note=already, sensenova-6-8-flash-lite=already, sensenova-6-8-flash-lite=false, step-3-7-flash=false
- **impact:** None. The leak this test was written to guard was real pre-FIX-3 and is now closed: get_task, all_tasks and resume_info all route through _detached(), which returns copy.deepcopy(task), so no caller mutation can reach plan.json. Adding the requested test to tests/test_auto_1.py would duplicate coverage that already exists.
- **evidence:** tools/auto/state.py:362-379 _detached() returns copy.deepcopy(task); :409-411 get_task returns self._detached(t); :418 all_tasks returns [self._detached(t) for t in ...]; :393-395 resume_info in_progress/pending are [self._detached(t) ...]
- **disproof:** Disproving fact for the residual claim on get_progress: a shallow dict() copy leaks only if the dict holds nested mutable values. Every write to _progress (lines 714-727, 866-868, 1040-1042) is a str or int (status, updated_at, stop_reason, done_count, pending_count), so dict(self._progress) at :427 is a complete copy. get_progress() also has exactly one caller, progress_display.py:287, which only
- **repro:** None. Pre-fix repro from the commit message (t = store.get_task('T1'); t['status']=12345; store.increment_task_counters('T1', round_delta=1)) now leaves plan.json untouched.

### `tools/auto/state.py::StateStore.all_tasks` — NONE

*StateStore accessors already return copies*

- **votes:** agnes-2-5-flash=false, step-3-7-flash=already
- **impact:** none — accessors already return copies via _detached()/deepcopy
- **evidence:** return [self._detached(t) for t in self._plan.get('tasks', [])]  # line 418
- **disproof:** All StateStore read accessors already return deep copies. The proposal acknowledges this. Task is about adding regression tests, not fixing code.

### `tests/test_auto_1.py::test_auto_creates_agent_dir` — NONE

*StateStore accessors already hand back detached copies*

- **votes:** dots-3-note=false, step-3-7-flash=already
- **impact:** none — the accessors are already detached; the proposed test would pass as written
- **evidence:** tools/auto/state.py:362-379 _detached() returns copy.deepcopy(task); get_task (:409-411), all_tasks (:418), resume_info (:393-395) all route through it; get_progress returns dict(self._progress) of scalars
- **disproof:** The premise of this entry — 'the accessors used by that path do not hand back detached copies' — is false. Every read accessor is already routed through _detached() (deep copy), as established in AUTO-T23. A test that mutates the returned objects and re-reads the JSON files would pass, because there is nothing to catch. This is a test-coverage suggestion for an already-correct contract, not a defe
- **repro:** none — no defect to reproduce

### `tests_bugfix/test_bugfix_m1_get_task_reference_leak.py::TestAccessorsReturnDetachedCopies` — NONE

*Detachment contract is already pinned by the regression suite*

- **votes:** dots-3-note=already
- **impact:** none — the contract is already tested; all five proposed tests exist
- **evidence:** tests_bugfix/test_bugfix_m1_get_task_reference_leak.py:62-108 — test_mutating_a_get_task_result_cannot_reach_disk, test_mutating_an_all_tasks_element_cannot_reach_disk, test_mutating_a_resume_info_task_cannot_reach_disk, test_accessors_do_not_return_the_live_object, test_two_reads_are_independent_of_each_other, test_nested_values_are_copied_too
- **disproof:** The proposed test class already exists under a different name in tests_bugfix/test_bugfix_m1_get_task_reference_leak.py: every one of the five named tests is covered — get_task mutation (:62), all_tasks mutation (:69), resume_info mutation (:77), identity/non-live (:88), two-read independence (:95), and the deep-copy-of-nested-lists guard (:103, which appends to target_files and asserts on-disk un
- **repro:** none — tests already pass


## Dismissed (84)

| finding | reviewers |
|---|---|
| `tools/auto/state.py::StateStore.get_progress` | 6 |
| `tools/auto/backlog_prioritiser.py::BacklogPrioritiser.build` | 5 |
| `tools/auto/executor.py::Executor.run` | 5 |
| `tools/auto/ticket_store.py::TicketStore.list_all` | 5 |
| `tools/prompt_optimizer.py::PromptOptimizer.generate_candidate` | 5 |
| `tools/file_reader.py::list_source_files` | 5 |
| `scripts/check_runbook.py::Report.failed` | 5 |
| `tests/fixtures/collect_mini_repo_java/com/example/Point.java::Point` | 5 |
| `tools/auto/gate1_filter.py::Gate1Filter._check_existence` | 5 |
| `tools/collect/registries.py::fail_open_locations` | 5 |
| `tools/collect/model.py::FunctionRecord.with_llm_summary` | 5 |
| `tools/collect/verifier.py::citation_check` | 5 |
| `tools/auto/continuity_validator.py::ContinuityValidator.check` | 5 |
| `tools/auto/inner_loop.py::_parse_verdict_soft` | 5 |
| `tools/auto/gate_registry.py::resolve_gate_order` | 5 |
| `tools/auto/gate_registry.py::run_gates` | 5 |
| `tools/llm_stream.py::strip_json_fence` | 5 |
| `tests_bugfix/test_llm_stream_reasoning_field_fallback.py::_RecordingHandler` | 4 |
| `tools/auto/executor.py::Executor._prepare_workspace` | 4 |
| `tests/fixtures/collect_mini_repo_java/com/example/Greeter.java::Greeter` | 4 |
| `tests/fixtures/collect_mini_repo_java/com/example/Greeter.java::Greeter.getPrefix` | 4 |
| `tools/collect/cli.py::read_collect_settings` | 4 |
| `tools/auto/coder.py::Coder._creative_is_edit` | 4 |
| `tools/backoff.py::load_state` | 4 |
| `tools/collect/config_map.py::_read_ini` | 4 |
| `tools/collect/loader.py::CollectModel.module` | 4 |
| `tools/improvement_agent.py::ImprovementAgent.__init__` | 4 |
| `tools/collect/model.py::ExceptSite` | 4 |
| `tools/collect/verifier.py::verify_module` | 4 |
| `tools/auto/architect.py::CandidateTask` | 3 |
| `tools/auto/inner_loop.py::InnerLoopResult` | 3 |
| `tools/auto/repo_ingest.py::RepoCluster` | 3 |
| `scripts/check_runbook.py::Report` | 3 |
| `tests/test_auto_c4.py::FakeInnerLoop` | 3 |
| `tools/auto/context_broker.py::ContextBroker._resolved_cache` | 3 |
| `tools/collect/loader.py::CollectModel.contracts_for` | 3 |
| `tools/collect/loader.py::CollectModel.gates_for` | 3 |
| `tools/collect/loader.py::CollectModel.config_map_for` | 3 |
| `tools/auto/coder.py::Coder._BLOCKED_ALWAYS` | 3 |
| `tools/collect/model.py::ConfigRead` | 3 |
| `tools/search_agent.py::SearchAgent` | 2 |
| `tools/auto/coder.py::Coder` | 2 |
| `tools/collect/scanner.py::scan_module` | 2 |
| `tools/collect/loader.py::CollectModel.is_safe` | 2 |
| `tools/auto/architect.py::ClusterReviewer._parse_candidates` | 2 |
| `tools/ui.py::Spinner` | 2 |
| `tools/auto/architect.py::CandidateTask.target_files` | 2 |
| `tests_bugfix/test_llm_stream_reasoning_field_fallback.py::_reset_reasoning_cache` | 2 |
| `tools/auto/inner_loop.py::InnerLoopResult.records` | 2 |
| `tools/auto/repo_ingest.py::RepoCluster.files` | 2 |
| `tools/auto/theme_validator.py::ThemeValidator.guidelines` | 2 |
| `scripts/check_runbook.py::Report.findings` | 2 |
| `tests/fixtures/collect_mini_repo_java/com/example/Greeter.java::Greeter.prefix` | 2 |
| `tests/test_auto_c4.py::FakeInnerLoop.seen_prior` | 2 |
| `tools/auto/theme_validator.py::ThemeValidator.__init__` | 2 |
| `tools/auto/architect.py::ClusterReviewer._parse_candidates_ex` | 2 |
| `tools/llm_stream.py::llm_stream_mod._REASONING_UNSUPPORTED_KEYS` | 1 |
| `tools/auto/theme_validator.py::ThemeValidator` | 1 |
| `tools/improvement_agent.py::ImprovementAgent` | 1 |
| `tools/collect/ast_facts.py::extract_except_sites` | 1 |
| `tools/collect/model.py::LLMSummary` | 1 |
| `tests_bugfix/test_bugfix_upsert_task_merge.py::TestAccessorsReturnDetachedCopies` | 1 |
| `tools/collect/loader.py::module` | 1 |
| `tools/ui.py::Spinner.frames` | 1 |
| `tools/llm_stream.py::_REASONING_UNSUPPORTED_KEYS` | 1 |
| `tools/auto/state.py::StateStore` | 1 |
| `tools/collect/cli.py::CollectSettings` | 1 |
| `tools/auto/state.py::resume_info` | 1 |
| `tools/auto/state.py::get_task` | 1 |
| `tools/auto/state.py::get_progress` | 1 |
| `tools/auto/context_broker.py::resolve` | 1 |
| `tools/auto/coder.py::_creative_is_edit` | 1 |
| `tools/auto/architect.py::_parse_candidates` | 1 |
| `tools/auto/controller.py::AutoController.__init__` | 1 |
| `tools/llm_stream.py::llm_stream._REASONING_UNSUPPORTED_KEYS` | 1 |
| `tests_bugfix/test_llm_stream_reasoning_field_fallback.py::_serve_script` | 1 |
| `tools/agent_trace.py::AgentTracer._sanitize` | 1 |
| `tools/ui.py::Spinner.__init__` | 1 |
| `tools/collect/model.py::ModuleRecord` | 1 |
| `tools/auto/context_broker.py::ContextBroker` | 1 |
| `tools/collect/model.py::CollectModel.module` | 1 |
| `tools/collect/model.py::ContractRecord` | 1 |
| `tools/collect/gates.py::GateEntry` | 1 |
| `tools/collect/config_map.py::ConfigMapEntry` | 1 |


## Solo findings (2 discoveries, 1 unshared judgements)

Found or judged by exactly one reviewer. A single vote is weak evidence about the finding and strong evidence about the reviewer: whoever brings a solo find that verifies is doing something the others are not.

### Discoveries — not on the list at all

These are the reason to run several reviewers. Check each one by hand: a solo NEW is either the best result of the run or a hallucination.

| finding | reviewer | verdict | severity | truth | title |
|---|---|---|---|---|---|
| `tools/search_agent.py::_DEFAULT_SKIP_DIRS` | sensenova-6-8-flash-lite | CONFIRMED | LOW | REAL ✅ | SearchAgent aliases the module-level skip-dir default, so one instance |
| `tools/auto/arch_probe.py::ArchProbe.last_by_op` | sensenova-6-8-flash-lite | CONFIRMED | LOW | REAL ✅ | last_by_op hands out a shallow copy that still shares its hit lists |

### Unshared judgements — nobody else reached this entry

Usually a coverage gap rather than insight. Worth re-running another reviewer over these ids before trusting a lone verdict.

| finding | reviewer | verdict | severity | truth | title |
|---|---|---|---|---|---|
| `tests_bugfix/test_bugfix_m1_get_task_reference_leak.py::TestAccessorsReturnDetachedCopies` | dots-3-note | ALREADY_FIXED | NONE | _unchecked_ | Detachment contract is already pinned by the regression suite |


## Reviewer scorecard

| reviewer | rows | confirmed | fixed | dismissed | NEW | solo | skepticism | accuracy | no-evidence | no-disproof |
|---|---|---|---|---|---|---|---|---|---|---|
| laguna-s-2-1 | 46 | 2 | 0 | 44 | 0 | 9 | 96% | 75% (3/4) | 0 | 0 |
| sensenova-6-8-flash-lite | 55 | 3 | 2 | 50 | 2 | 7 | 91% | 71% (5/7) | 0 | 0 |
| dots-3-note | 54 | 1 | 2 | 51 | 1 | 3 | 94% | 50% (3/6) | 0 | 0 |
| step-3-7-flash | 53 | 1 | 3 | 49 | 0 | 6 | 92% | 50% (3/6) | 0 | 0 |
| agnes-2-5-flash | 53 | 1 | 0 | 52 | 0 | 6 | 98% | 40% (2/5) | 0 | 0 |

**skepticism** = share of findings this reviewer rejected. Near 100% with no NEW-* rows means it dismissed the list without reading the code; near 0% means it agreed with everything. The reviewers worth keeping reject most of a noisy list *and* still bring findings of their own.

**accuracy** is scored only against findings listed in `--truth`, so it measures the checked subset and cannot be gained by guessing. Rank on accuracy first, then on NEW — a reviewer that is right about a noisy list and still discovers something is the one to keep.

**solo** counts findings only this reviewer recorded — discoveries and coverage gaps together; see the solo table for the split.

**no-evidence / no-disproof** should both be 0 — the helper refuses rows without them, so anything above 0 is a file written by hand.
