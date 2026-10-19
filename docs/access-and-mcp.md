# API access and local MCP adapter

## Security model

The service separates browser administration from machine read access.

- `/health` remains anonymous for health checks.
- Every `/api/*` request is denied unless it has an authenticated principal.
- Browser login at `POST /auth/status` validates `WEB_USERNAME` and `WEB_PASSWORD`, then sets a signed, `HttpOnly`, `Secure`, `SameSite=Strict` `goofish_session` cookie. The browser session is administrator-scoped and expires after eight hours.
- The machine token in `GOOFISH_MCP_READ_TOKEN` is a distinct service-account credential. It only allows `GET`/`HEAD` requests to the explicit read allowlist: task queries, dashboard summary, result queries, task logs, and settings status. It cannot create, edit, start, stop, delete, upload, or alter configuration.
- Authorization decisions emit structured access-audit records to the service log with principal, method, path, and outcome. No token value is logged.

`GOOFISH_SESSION_SECRET` and `GOOFISH_MCP_READ_TOKEN` are required for protected production access. Store them in protected deployment secrets only; never commit them, put them in CI logs, or reuse the administrator password as a token.

## ASS account-source metadata

Task create/update/generate now accept `account_strategy: "ass"`. Only authenticated administrators can select or change it; it does not grant an ASS credential or a new machine write scope. Task responses and readonly MCP may show this strategy, never the ASS Key or session payload. ASS connection/target references are server-injected and the Key is a protected file, not a task field or an agent value-reading tool. Existing task strategies remain unchanged; partial edits do not switch ASS tasks back to local accounts. See [ASS account source](ass-account-source.md) for the separate consumer credential and deployment/validation boundaries.

## Local stdio MCP adapter

The adapter lives in `src/mcp/goofish_readonly.py`. It is designed to run on the Hermes host and is not a NAS daemon.

It exposes only these read-only MCP tools:

- `goofish_health`
- `goofish_list_tasks`
- `goofish_get_task`
- `goofish_dashboard_summary`
- `goofish_list_result_files`
- `goofish_get_result_records`
- `goofish_get_task_logs`

Its required environment is:

```text
GOOFISH_MCP_BASE_URL=https://<protected-goofish-endpoint>
GOOFISH_MCP_READ_TOKEN=[REDACTED]
```

Start it with:

```text
python -m src.mcp.goofish_readonly
```

The process communicates using newline-delimited JSON-RPC over stdin/stdout. It never writes to the Goofish API. A future write-capable adapter must use a separate service account, separate scopes, explicit server-side endpoints, audit coverage, and Hermes-side per-action confirmation.

## Deployment order

1. Review and merge the access-control code.
2. Generate a unique session secret and read-only service token in a protected deployment secret file.
3. Add those values to the NAS deployment environment without printing their values.
4. Publish the exact tested image through CI/CD, then deploy its manifest-pinned digest through the approved NAS channel. Automatic NAS deployment requires separate onboarding; the [first ASS delivery](records/ass-account-source-20261019.md) used an explicitly authorized manual deployment, not the skipped automatic deploy job.
5. Verify `/health` anonymously, then verify no-token API denial and allowed read-token access.
6. Configure Hermes to launch the local stdio adapter and perform read-only integration tests.

No existing monitor task, Cookie, result data, or service state is modified by this code alone.
