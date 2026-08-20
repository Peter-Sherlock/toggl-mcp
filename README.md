# Toggl Track MCP Server

This project is being built in verified phases. Phase 4 adds an official MCP Python SDK v2 server
over stdio on top of the independently tested asynchronous Toggl 2.0 API client.

## V1 tools

Read tools are exposed by default:

- `list_projects()`
- `get_current_timer()`
- `get_time_entries(start_date, end_date)`

Write tools are exposed only when `TOGGL_ENABLE_WRITE_TOOLS=true`:

- `start_timer(description, project_id=None)`
- `stop_timer()`

The server hides organization/workspace IDs, pagination, API timestamps for mutations, and raw
Toggl response fields from the agent-facing schemas. Tool results use explicit Pydantic output
models so MCP clients receive `structuredContent` and a matching output schema.

## Setup

The project requires Python 3.11 or newer. `uv` installs and manages the project interpreter and
dependencies independently of the system Python.

```powershell
Copy-Item .env.example .env
# Edit .env locally. Do not commit it.
uv sync --dev
```

Required environment variables:

```text
TOGGL_API_KEY
TOGGL_ORGANIZATION_ID
TOGGL_WORKSPACE_ID
```

`TOGGL_API_KEY` is generated in Toggl 2.0 settings and begins with `toggl_sk_`. For compatibility
with an early Phase 2 `.env`, `TOGGL_API_TOKEN` is also accepted as a legacy variable name.

The optional write gate defaults to false:

```text
TOGGL_ENABLE_WRITE_TOOLS=false
```

This is an enforcement boundary, not just an MCP annotation. When false, `start_timer` and
`stop_timer` are not registered and therefore do not appear in `tools/list`.

## Run over stdio

The console entry point uses stdio by default:

```powershell
uv run --env-file .env toggl-mcp
```

Do not print application output to stdout while the server is running; stdout carries the MCP
protocol.

## Codex integration

The repository's parent workspace contains a project-scoped Codex configuration at
`../.codex/config.toml`. When Codex opens the trusted `Toggl_track_mcp` workspace, it starts this
server over stdio with the locked uv environment and loads secrets from this project's `.env`.
The API key is not copied into Codex configuration.

The configuration allowlists exactly the five V1 tools. Its `writes` approval policy allows the
three read-only tools without a write approval and asks for approval before `start_timer` or
`stop_timer`. Run `/mcp` in Codex to inspect the connected `toggl_track` server and its tools.

This configuration is intentionally project-scoped. It does not expose the real Toggl account to
unrelated Codex workspaces.

## Offline verification

The client and MCP protocol tests use `httpx.MockTransport`; they never call Toggl or modify a real
account.

```powershell
uv run pytest
uv run ruff check .
uv run mypy
```

## Real-account read-only verification

After creating `.env`, run:

```powershell
uv run --env-file .env python scripts/verify_account.py
```

This script only lists projects, checks the current timer, and fetches recent time entries. It
never starts or stops a timer. Real write verification is intentionally performed separately and
only after explicit confirmation because it changes Toggl data.

## Client behavior

- Dates passed to `get_time_entries` must be timezone-aware `datetime` values.
- Project and time-entry pagination are consumed internally; page numbers are not exposed.
- A 204 from the current-tracking endpoint is normalized to `None`.
- Starting a timer first checks for an existing timer and raises a conflict instead of implicitly
  stopping it, even though the upstream start endpoint would replace the running timer.
- Stopping a timer first confirms that the configured workspace has a running entry, then calls
  the workspace-scoped tracking endpoint with an explicit end timestamp.
- Rate/quota failures are surfaced with retry timing metadata. The client does not sleep for long
  periods or automatically retry side-effecting requests.

## Server behavior

- One reusable async HTTP client lives for the MCP process lifetime; the server stores no current
  timer as business state.
- Every timer read, start preflight, and stop preflight asks the Toggl backend for current state.
- Expected client/API errors become stable tool errors without raw response bodies, credentials,
  or Python stack traces in the tool result.
- Stopping when no timer exists is an idempotent no-op with
  `{"stopped": false, "reason": "no_running_timer"}`.
