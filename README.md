# Toggl Track MCP Server

This project is being built in verified phases. The server exposes the core Toggl Track
data surface — time entries, projects, clients, tags, and tasks — as agent-oriented tools
over the official MCP Python SDK, on top of the independently tested asynchronous Toggl 2.0
API client.

## Tools

Read tools are exposed by default:

- `list_projects()`
- `get_current_timer()`
- `get_time_entries(start_date, end_date)`
- `get_time_entry(entry_id)`
- `list_clients()`
- `list_tags()`
- `list_tasks(project_id)` — requires a Toggl plan with the tasks feature; other plans
  answer 404, which surfaces as a clean "not found" tool error.
- `summarize_time(start_date, end_date, group_by="project"|"date"|"tag")`

### Time summary semantics

`summarize_time` aggregates the raw range query client-side, so its output is always
consistent with `get_time_entries` and the truncation signal carries over:

- Still-running entries (null or negative duration) are counted in `running_count` but
  excluded from `tracked_seconds` because their durations are not final.
- `possibly_truncated` is propagated from the underlying query; when true, every total is
  a lower bound and agents should narrow the interval before reporting.
- Tag grouping attributes one entry's full duration to each of its tags, so group sums
  can exceed `tracked_seconds`.
- Date grouping uses the UTC calendar date of each entry's start.
- Project grouping resolves project IDs to names via one extra projects read; duplicate
  project names are disambiguated with their IDs.

Toggl's dedicated Reports API (`/reports/api/v3/...`) was not reachable under any tested
host and auth scheme, so summary aggregation is computed from the verified time-entries
endpoint instead.

Write tools are exposed only when `TOGGL_ENABLE_WRITE_TOOLS=true`:

- `start_timer(description, project_id=None)`
- `stop_timer()`
- `create_time_entry(description, start, duration_seconds, project_id=None, tags=None, billable=False)`
- `update_time_entry(entry_id, description=None, project_id=None, tags=None, start=None, duration_seconds=None)`
- `bulk_edit_time_entries(entry_ids, add_tags=None, remove_tags=None, project_id=None)`
- `delete_time_entry(entry_id)`
- `create_project(name, active=True, client_id=None, color=None, is_private=True)`
- `update_project(project_id, name=None, active=None, client_id=None)`
- `delete_project(project_id)`
- `create_client(name)`
- `create_tag(name)`

The server hides organization/workspace IDs, pagination, API timestamps for mutations, and raw
Toggl response fields from the agent-facing schemas. Tool results use explicit Pydantic output
models so MCP clients receive `structuredContent` and a matching output schema.

## Verified upstream routing

The real Toggl API serves a mixed route layout, confirmed against a live account:

- Projects, time entries, and tracking (`start`/`stop`/`current`) live under
  `/api/organizations/{oid}/workspaces/{wid}/...`.
- Clients, tags, and project tasks live under `/api/workspaces/{wid}/...` and answer with
  `{data, page, per_page[, total]}` envelopes; plain JSON arrays are also accepted.
- The `/me` endpoint is not reachable under either prefix on this base URL and is not
  exposed as a tool yet.

## Verified write semantics

The time-entry write lifecycle (create → update → delete) is verified against a live
account:

- The create endpoint rejects payloads without a `type` field; the client always sends
  `"type": "activity"`.
- Tags attach only through `tag_ids` (integer IDs); string `tags` are silently ignored.
  Name-based tool input is resolved against `list_tags`, and unknown names fail with a
  clear error instead of silently dropping the tag.
- `tag_ids: null` in responses is normalized to an empty list, like `tags`.
- The PUT endpoint requires `start` and `type`, preserves omitted optional fields
  (verified for `project_id`), and answers with an empty 204 instead of the updated
  entry. The client therefore reads the current entry, merges the requested changes,
  sends the full verified field set, and reads the entry back.
- Changing the duration of a still-running entry is rejected locally; stop the timer
  first.

### Bulk edit semantics

Toggl's documented bulk PATCH endpoint (`/workspaces/{wid}/time_entries/{ids}`) and the
canonical `api.track.toggl.com/api/v9` host are **not usable with this server's
configuration**: the ws-scoped routes return 404 on this base URL, and the canonical host
rejects `toggl_sk_` credentials (401 Bearer / 403 Basic). `bulk_edit_time_entries`
therefore applies changes per entry over the verified single-entry routes:

- Tag changes work and are trusted (verified live: add via `tag_ids` merge, remove down
  to an explicit empty list). Unknown tag names abort the whole call before any entry is
  touched, so there is never a partial tag set.
- A project move is re-read and confirmed after the PUT. Verified: the reachable PUT
  route **silently ignores project changes** (across `project_id`, legacy `pid`,
  `task_id: null`, `workspace_id`, and stop-inclusive variants), so an ignored move is
  reported as a per-entry failure — never as success. The likely cause is moving to an
  inactive project (all projects creatable via this API come back `active=false`, and
  `active` cannot be flipped to true).
- Every entry reports its own outcome (`updated` / `error`); one failure never blocks
  the rest.

Project, client, and tag mutations are covered by offline protocol tests but have not
been exercised against the live account.

Write operations against the live account are intentionally not exercised by automated
verification; they change real Toggl data.

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

The configuration allowlists exactly the seventeen registered tools. Its `writes` approval
policy allows the read-only tools without a write approval and asks for approval before any
write tool runs. A drift test (`tests/test_codex_config.py`) fails when `enabled_tools`
no longer matches the registered tool surface. Run `/mcp` in Codex to inspect the connected
`toggl_track` server and its tools.

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
  Both endpoints stop at the upstream-reported `total`, and a safety page limit guards
  against a misbehaving upstream.
- `get_time_entries` reports `possibly_truncated` when Toggl's reported total exceeds the
  fetched entries or pagination hit the safety page limit. The signal reaches agents through
  the tool result's `possibly_truncated` field.
- `is_running` follows one rule everywhere: explicit backend state where the endpoint
  provides it (current timer, start, stop), otherwise the documented convention that a
  still-running entry reports `duration: null`.
- A 204 from the current-tracking endpoint is normalized to `None`.
- Starting a timer first checks for an existing timer and raises a conflict instead of
  implicitly stopping it. That preflight is best-effort: the check and the start call are
  separate requests, so a timer started in another client between them can still be
  replaced by Toggl's start endpoint.
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
