# Goofish native remote MCP

## Status and boundary

This is an application-native endpoint, not a stdio-to-HTTP bridge or an additional container. It is disabled by default. The legacy read-only stdio adapter and existing browser/REST authentication remain available.

Local implementation and verification do not mean publication, deployment or ASS registration. Production rollout requires separate approval. Do not run the mutation smoke against production or import real login states/LLM credentials into test fixtures.

The local acceptance record and remaining delivery prerequisites are in `native-mcp-verification.md`. It records passed local regression/SDK checks and independent reviews separately from the unexecuted Linux image, registry and production gates.

## Transport and authentication

Endpoint: `POST /api/mcp`; stateless Streamable HTTP with JSON responses, no SSE subscriptions. Authenticated GET/DELETE return 405. Negotiated protocol versions: `2025-06-18` and `2025-03-26`. POST clients must accept `application/json, text/event-stream` and send UTF-8 JSON. Origin and Host are checked; browser cookies never grant MCP authority.

Runtime settings (set through the protected production configuration workflow, never commit real values):

| Setting | Meaning |
| --- | --- |
| `GOOFISH_MCP_ENABLED` | Explicit `true` enables the endpoint; default false. |
| `GOOFISH_MCP_READ_TOKEN` | Existing read-only machine token; native use requires at least 32 ASCII Bearer-token characters. |
| `GOOFISH_MCP_READ_SUBJECT` | Stable audit identity for the read token. |
| `GOOFISH_MCP_WRITE_TOKEN` | Separate random ASCII Bearer token of at least 32 characters; must not equal the read token. |
| `GOOFISH_MCP_WRITE_SUBJECT` | Stable writer identity, default `ass-mcp`. |
| `GOOFISH_MCP_WRITE_SCOPES` | Comma-separated explicit grants; absent/unknown scopes fail closed. Include `system:read` for own receipt lookups. |
| `GOOFISH_MCP_ALLOWED_HOSTS` | Exact allowed hostnames, not a public wildcard; defaults to localhost/loopback. Configure the verified ASS-to-Goofish internal destination. |
| `GOOFISH_MCP_ALLOWED_ORIGINS` | Explicit origins if a browser client is deliberately approved; absent Origin is valid for server-to-server use, unapproved Origin is denied. |
| `GOOFISH_MCP_STATE_DIR` | Durable write receipts and mutation lock; default `data/mcp` inside the existing persisted data mount. |

Read token scopes remain `tasks:read,results:read,system:read`; do not upgrade that token to writer authority. Optional settings/account/prompt access is granted explicitly through the separate writer identity. Register the upstream in ASS and grant the exact current selected tool names, not all future tools. Host checking is not a replacement for a public-ingress deny rule or a network ACL; review public `/api/mcp` exposure before activation.

## Capability acceptance list

The complete expected names are asserted by `tests/unit/test_mcp_catalog.py`. The source currently declares 49 tools, including the write receipt query.

- Tasks: `list_tasks`, `get_task`, `create_task`, `generate_task`, `get_generation_job`, `clone_task`, `update_task`, `set_task_schedule`, `set_task_enabled`, `set_task_criteria`, `start_task`, `stop_task`, `get_task_runtime_status`, `validate_task_config`, `preview_task_change`, `preview_task_deletion`, `delete_task`.
- Prompts: `list_prompts`, `get_prompt`, `create_prompt`, `update_prompt`, `preview_prompt_deletion`, `delete_prompt`.
- Results: `list_result_sets`, `query_results`, `get_result`, `get_price_insights`, `set_result_status`, `get_blacklist_rules`, `update_blacklist_rules`, `export_results`, `preview_result_cleanup`, `delete_result_set`.
- Diagnostics: `get_health`, `get_dashboard_summary`, `get_system_status`, `get_task_logs`, `get_account_source_status`, `get_failure_guard_status`, `list_account_metadata`.
- Administration: `get_ai_settings`, `update_ai_settings`, `test_ai_connection`, `get_notification_settings`, `update_notification_settings`, `test_notification`, `get_rotation_settings`, `update_rotation_settings`.
- Receipts: `get_invocation`.

Available scopes: `tasks:read`, `tasks:write`, `tasks:run`, `tasks:delete`, `prompts:read`, `prompts:write`, `prompts:delete`, `results:read`, `results:write`, `results:delete`, `system:read`, `accounts:read`, `settings:read`, `settings:write`, `settings:test`, `notifications:send`.

Task creation/clone defaults to disabled. Starting a task, AI generation, AI connection testing and notification testing are real operations, not previews. No MCP tool accepts raw cookies, account login-state content, API keys, an arbitrary new model endpoint, arbitrary notification destination, shell command or filesystem path.

## Mutation protocol

Every mutation requires a canonical lowercase UUID `request_id`. The same subject/request ID with unchanged tool and arguments returns the stored result; changed reuse is rejected. A durable claim is written before business execution. Interrupted/unknown operations are never automatically reclaimed. Query `get_invocation` using the original ID and inspect real target state; do not choose a fresh ID merely because a response timed out.

Responses include `_receipt` with invocation ID, request ID and status. Receipts are subject-isolated; only hashes of input arguments are persisted. Outputs are bounded and scrubbed before they are returned or stored. The separate SQLite journal is not an automatic retry queue and must be included in normal backups. Do not delete it to resolve an uncertain operation.

Explicit handler failure/uncertainty preserves its business evidence through HTTP, journal, receipt queries and replay. A notification timeout can mean the receiver accepted the request: it is `unknown`, not a successful invocation or proof of no delivery, and is not automatically retried. A confirmed channel response means `accepted`, not delivered/read; generic Webhook confirmation is HTTP-level only. Existing cached responses are scrubbed again before replay without rewriting historical journal rows. This output protection does not claim to erase old on-disk secrets if an earlier experimental build stored them.

Configuration/content edits require the revision returned by the relevant read/preview. Deletion defaults to preserving monitoring history; purging historical data is an independent destructive decision. Inspect the tool's live schema and preview rather than assuming all delete operations share the same semantics. Prompt paths are restricted and task criteria use isolated names. These guarantees are tested separately from transport idempotency.

Task/asset consistency and its migration boundaries are specified in `task-prompt-consistency.md`: task instance identity and configuration versions are durable, asset/reference writers share a directory lock, runtime consumes controlled prompt references, and generation receipts preserve immutable task snapshots. Both REST and MCP task deletion now preserve results/history/logs/prompts; `purge=true` is rejected rather than silently expanding deletion. Disable/remove-schedule operations do not depend on intact unused AI assets.

This introduces additive SQLite fields/metadata and durable generation receipts. Before activation, back up SQLite consistently with WAL state plus prompts, stop all old application/crawler writers, and verify that every process uses the same database and prompt root. Never mix old and new writers. Rolling back to old code requires a reviewed restore of the consistent pre-upgrade state; simply switching image tags does not restore the old concurrency contract.

## Settings persistence prerequisite

Settings writes use a shared `.env.lock` and atomic replacement of `.env` in a writable parent directory. Do not unlink the lock while a writer runs; do not mix old uncoordinated configuration writers with this version. `.env-write-*` crash leftovers are private runtime material, excluded from Git and image context. Effective probe configuration is captured as one immutable snapshot; `.env` interpolation is disabled, so `${VAR}` is treated literally.

The observed production service mounts `/app/.env` as a **read-only single-file bind**. Local writable-file tests do not establish production write capability. Even a writable single-file bind is not a suitable atomic-replacement target. Keep `settings:write` ungranted until a separately approved persistence design is implemented and tested (for example a dedicated writable configuration directory, or a separate non-secret override store that preserves read-only credentials). Do not silently remount the file, fall back to truncating it in place, copy credentials into a new store, or change the ASS credential source to make tests pass. Read/test capabilities have separate scopes and do not authorize configuration changes.

## Verification

Hermetic backend regression:

```
uv run --no-project --python 3.11 --with-requirements requirements.txt pytest -m 'not live' -q
```

Frontend:

```
cd web-ui
npm ci --no-fund
npm run build
```

Real socket CRUD is part of `tests/integration/test_mcp_transport_crud.py`: temporary SQLite, local uvicorn, no actual crawling/LLM/notifications. It verifies discovery, scope denial, disabled keyword-mode creation, same-ID replay, readback, update, stale-revision rejection, deletion and persistent receipt.

CI must also run `scripts/smoke_native_mcp.py` against the exact built image using fresh credentials and an empty disposable instance. It accepts only numeric loopback origins, disables proxies and rejects redirects; it refuses nonempty services and deletes only its own fixture. A passed Python test is not a passed Linux Docker image gate.

A real installed `mcp` SDK was also exercised locally against an isolated full `src.app` process, including initialize, tool discovery and CRUD. Preserve this check after transport contract changes.

## Production handoff (not executed)

1. Finish local regression and independent review. Obtain approval before commit/push/PR/publication.
2. Run the publish-only CI described in `ci-publish-only.md`. Require successful real Linux image smoke, private immutable GHCR publication, and exact validated manifest. Do not rebuild an untested replacement on NAS.
3. Review the live Compose baseline independently. A deployed service may use a separate Compose file and overrides, not automatically `infra/nas/compose.yaml`. Keep existing mounts, account-source key, bindings, scheduler behavior and browser access.
4. Back up application SQLite consistently, prompts, protected configuration and MCP journal if present. Record the previous registry digest and container state. Review whether scheduled/running monitors require a maintenance window.
5. Obtain separate approval for container replacement, protected MCP credentials/scopes and any public-ingress deny configuration. Never expose real credentials in a manifest or image build arguments.
6. After deployment, read back exact container image/revision/start state, normal health and monitoring configuration; register the private native endpoint in ASS and grant the exact reviewed current tool list. Verify through the real ASS client. Production write acceptance must use a separately approved harmless fixture; no notification/crawler test by default.
7. Roll back the Compose image to the recorded immutable digest and restore protected configuration if needed. Retain receipt history. Data/prompt restoration is a separate reviewed action if any production mutation was performed; reverting the container alone does not undo data changes.

Record the current source revision, immutable image digest and start state in protected deployment notes immediately before an approved rollout. Do not commit live runtime snapshots or protected production configuration to source control.
