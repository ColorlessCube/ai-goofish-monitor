# Task / prompt consistency boundary

## Invariants

- SQLite task writers use `BEGIN IMMEDIATE`, partial-field merging, and conditional `UPDATE`/`DELETE` against `instance_id` + `config_version`. MCP additionally checks its asset-aware revision inside that transaction. Runtime-only status updates do not advance configuration versions or overwrite configuration snapshots.
- Task IDs retain a persistent high-water mark. Every instance receives a persistent random identity. Existing ID 0 is retained on upgrade; deleted IDs are not reused. Old previews cannot authorize a replacement instance.
- HTTP, MCP, generation, and task-reference persistence share a POSIX `flock` on the prompt directory. Lock order is prompt directory then SQLite. Preliminary async scans are advisory; final content revision/reference checks happen in the locked synchronous transaction. No lock spans an LLM call or process stop.
- Controlled prompt references accept a flat `.txt` filename or `prompts/name.txt`. Safe bare names are explicitly persisted as `prompts/name.txt`; traversal, absolute paths, backslashes, and symlinks are rejected, not normalized into valid tokens. Runtime crawler reads and generation reference reads open each directory component and leaf with `O_NOFOLLOW` and read regular-file descriptors only.
- HTTP prompt list/get/put and native MCP resolve the same root: app override `mcp_prompts_dir`, otherwise `GOOFISH_PROMPTS_DIR`, otherwise `prompts` in the working directory. No import-time REST default overrides the configured directory.
- Clone checks the source instance, configuration version, and asset-aware revision inside the directory lock and `BEGIN IMMEDIATE`; criteria sampling/copy and destination insertion commit within that same critical section. Advisory prechecks do not authorize the final clone.
- ProcessService dispatches `--task-id`, `--task-instance`, and `--expected-config-version`; the crawler checks all three against the loaded snapshot before reading prompts or starting work. New bound process launches fail if the version binding is absent. Old explicit CLI ID/instance and unambiguous-name invocations remain compatible; new duplicate names are rejected transactionally.
- Disable/remove-cron use independent conditional field patches and do not require intact AI assets. Enabling, adding/changing cron, switching to AI, and native start validate relevant AI assets. Explicit patches clearing either required AI prompt reference are rejected, even if enablement/schedule is unchanged. Keyword clones never read unused criteria and clear that reference.
- Native and REST generation receipts persist in `mcp_generation_jobs`. The created immutable task snapshot and instance are written in the **same transaction** as task creation. Scheduler failure is `partial_success` / `scheduler_sync_required` with `task_committed=true`, not a side-effect-free failure. Polling never resolves historical jobs through a reusable live task ID.
- Terminal generation receipts commit **before** public in-memory completion. Polling prioritizes durable status and immutable snapshot over advisory progress. Once completed is observed it cannot regress after worker cancellation/restart.
- A failed post-COMMIT unlock is reconciled from the same-transaction receipt, never inferred from a local return variable. Unreadable receipts yield `unknown`, `retry_safe=false`; cleanup is not attempted without positive evidence of non-commit, and referenced assets are always protected by a fresh locked reference check.
- Worker ownership is tracked by job ID and actual thread/async-task lifetime, not by the continued presence of an in-memory job. Cancelled/abnormally exited workers converge to `partial_success` with `task_committed=true`, `scheduler_sync_required` and snapshot when committed, otherwise `unknown`; both forbid automatic retry. In-flight creation writes settle before cancellation reconciliation. Missing historical records also return unknown. No automatic re-execution is implemented.
- Synchronous REST keyword generation retains its existing enabled-by-default behavior. Scheduler failure after creation returns the committed task/instance with `partial_success`, `task_committed=true`, `retry_safe=false`, and `error=scheduler_sync_required`, rather than a task-less HTTP 500.
- Both task delete entry points preserve results, price history, logs, and prompt files. `purge=true` is rejected. Cleanup of an uncommitted newly generated asset rechecks all references under the same shared lock.

## Migration / operational limits

`init_schema` adds `tasks.instance_id`, `tasks.config_version`, an identity index, ID high-water metadata, and import/high-water triggers. It backfills each old task identity once, including ID 0; it does not rewrite unsafe legacy prompt strings or rename duplicate legacy tasks. Generation receipts add one table lazily.

Concurrent first opens retry only pre-transaction WAL initialization `SQLITE_BUSY`/`SQLITE_LOCKED`, bounded by the connection busy timeout. I/O failures and mutation transactions are never blindly replayed.

Back up SQLite consistently (including WAL state) and prompts before first rollout. Stop old application/crawler writers during migration; old binaries do not implement this shared lock/CAS contract. All service processes must use the same SQLite database and prompt root (`GOOFISH_PROMPTS_DIR` when configured). The implementation targets POSIX/macOS/Linux local storage with working directory flock; Windows and network-filesystem lock behavior were not exercised. Never replace the mounted prompt-directory inode while services are running.

The migration is additive, not a destructive data rewrite. Nevertheless, running the old application against a database already written by the new version can reintroduce unsafe snapshot writes/ID reuse. A rollback should stop all writers and restore the pre-upgrade consistent backup, not delete the new columns from a live database. Existing absolute/unsafe prompt references now fail closed at runtime and need explicit administrator correction. Interrupted generation can leave unreferenced assets for manual review; there is deliberately no broad purge or automatic job replay.

The file lock coordinates application writers, not arbitrary administrator edits that ignore the lock. Descriptor no-follow checks protect reads against symlink swaps, but do not claim to defend against a privileged OS user replacing application binaries or the entire storage mount.

## Portable regression evidence

The independent 13 failing cases were copied to `tests/unit/test_task_prompt_regressions.py` and run RED before production changes. They now pass without hardcoded checkout paths. Additional suites cover old-schema ID-0 migration, stale saves, concurrent partial updates, cross-process asset-lock blocking/revalidation, symlink swaps and real crawler entry-point consumption (crawler stubbed), generation restart receipts, REST shared criteria, and post-commit scheduler failure.

```sh
uv run --offline --no-project --python 3.11 --with-requirements requirements.txt pytest -q \
  tests/unit/test_task_prompt_regressions.py \
  tests/unit/test_task_cas_storage.py \
  tests/unit/test_prompt_asset_lock.py \
  tests/unit/test_task_runtime_boundary.py \
  tests/unit/test_generation_receipts.py \
  tests/unit/test_rest_task_asset_boundary.py \
  tests/unit/test_mcp_task_tools.py tests/unit/test_mcp_prompt_tools.py \
  tests/unit/test_prompt_utils.py tests/unit/test_process_service.py \
  tests/integration/test_api_tasks.py tests/integration/test_cli_spider.py
```

Tests use pytest temporary SQLite/files; LLM, crawler, scheduler failure and subprocess spawn are fixture-controlled. The cross-process lock test launches only a local fixture SQLite writer, not a crawler or network service. No NAS, deployment, commit, or push is part of this verification.

### Second review cycle

Portable independent probes live in `test_independent_cas_review.py`, `test_runtime_receipt_review.py`, and `test_runtime_receipt_positive.py`. Their original RED run was 10 failures / 9 passes. The completion race is tested at both boundaries: no completed status while its durable write is blocked; once completed is observed, cancellation and restart preserve it. The simulated process-spawn test forwards the version binding and asserts spawn was actually exercised, so a stale mock signature cannot produce a false pass.

`test_generation_outcome_edges.py` additionally covers cancelled in-flight writes, unreadable post-commit receipts without asset cleanup, failed terminal persistence, and completion durability after cancellation for both REST/MCP. `test_task_sqlite_initialization.py` reproduces the transient first-open WAL contention discovered by the multi-process migration probe. All assertions are local-only; this implementation pass does not constitute independent final-review approval.
