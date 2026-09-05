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
- `list_planned_entries(start_date, end_date)` — calendar-scheduled entries that do not
  carry tracked time yet.
- `list_clients()`
- `list_tags()`
- `list_tasks(project_id)` — requires a Toggl plan with the tasks feature; other plans
  answer 404, which surfaces as a clean "not found" tool error.
- `summarize_time(start_date, end_date, group_by="project"|"date"|"tag")`
- `get_me()` — the authenticated user's settings, including the workspace they have
  selected in Toggl.
- `list_workspace_members()` — the organization's members with their workspace
  membership.

### Time summary semantics

`summarize_time` is powered by the Focus API's native report engine
(`POST /reports/workspaces/{wid}/query`), discovered in the official OpenAPI spec. All
aggregation happens server-side; the client resolves labels and exact entry totals:

- Verified grouping properties: `project_id`, `start_date`, `tag_ids`; aggregation
  `sum(duration)` in seconds. `period` accepts full RFC3339 timestamps, so interval
  bounds stay timestamp-precise.
- Running entries contribute 0 seconds (verified live) and planned (calendar) entries
  are excluded — matching `get_time_entries`, which also skips planned entries (they
  carry `planned_start` instead of `start`).
- Tag grouping emits one row per tag id, so group sums can exceed `tracked_seconds` for
  multi-tag entries; the exact totals come from one extra per-user query (each entry has
  exactly one owner).
- Project names resolve via one extra projects read (duplicates disambiguated with IDs);
  tag names via the tags read; a running timer inside the range is reported through
  `running_count`.
- `possibly_truncated` is always false here: aggregation is server-side, so the range
  endpoint's cap does not apply. Row-cap behavior of the query endpoint is undocumented —
  pending external verification for very large result sets.

### Planned entries semantics

- The official spec lists a backoffice route (`GET /backoffice/users/{uid}/time-entries`
  with `view=planned`), but that scope is not served on `focus.toggl.com/api` — it 404s
  at the proxy level. Planned entries are therefore collected from the same range
  endpoint as tracked entries, where they appear with `planned_start`/`planned_duration`
  instead of `start`/`duration`.
- Narrow windows filter planned entries by `planned_start` (verified live). Entries that
  also carry tracked time are excluded from `list_planned_entries` and returned by
  `get_time_entries` instead.
- The range envelope carries no `total`, so `possibly_truncated` is only set when
  pagination hits the safety page limit.
- Verified quirk: requesting `per_page` above 100 makes the range endpoint silently
  answer with an empty page. The client caps `page_size` at 100, so this cannot be
  triggered through configuration.

Write tools are exposed only when `TOGGL_ENABLE_WRITE_TOOLS=true`:

- `start_timer(description, project_id=None)`
- `stop_timer()`
- `create_time_entry(description, start, duration_seconds, project_id=None, tags=None, billable=False)`
- `update_time_entry(entry_id, description=None, project_id=None, tags=None, start=None, duration_seconds=None)`
- `bulk_edit_time_entries(entry_ids, add_tags=None, remove_tags=None, project_id=None)`
- `bulk_delete_time_entries(entry_ids)`
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

The Focus API (`focus.toggl.com/api`, the only host that accepts `toggl_sk_` keys) has
its own OpenAPI spec at engineering.toggl.com, which documents the real bulk surface:
`PATCH /organizations/{oid}/workspaces/{wid}/time-entries/bulk-edit` with
`{ids, changes}`. Verified live:

- `changes.project_id` **does** move entries there — project changes must go through
  PATCH. The single-entry PUT route silently ignores `project_id` (and its variants),
  which is why updates and bulk edits both use PATCH now.
- `changes.tag_ids` is tri-state: absent leaves tags untouched, a list sets them, an
  empty list clears them. Tag names are resolved once up front; unknown names abort
  before any entry is touched.
- `update_time_entry` uses the single-entry `PATCH` (partial: absent fields stay
  unchanged) and re-reads the entry, confirming a project move against fresh state.
- `bulk_edit_time_entries` reads all requested entries in one batch call
  (`GET /time-entries/batch?ids=...`), groups them by resulting tag set, issues one
  bulk-edit call per group, and confirms moves by re-reading. The upstream answers with
  an empty 204, so outcomes are derived client-side: entries missing from the batch read
  fail individually, and an unapplied move is reported as a failure — never as success.
- `GET /users/me/settings` and `GET /organizations/{oid}/users` back the `get_me` and
  `list_workspace_members` tools.

### Bulk delete semantics

`DELETE /organizations/{oid}/workspaces/{wid}/time-entries/bulk?ids=<csv>` (documented in
the Focus spec, verified live) answers an empty 204, so `bulk_delete_time_entries`
derives outcomes client-side:

- A batch read happens first, so unknown or inaccessible IDs fail individually instead
  of letting one bad ID reject the whole delete request.
- IDs are deleted in chunks of 100 to stay within URL length limits; one chunk's failure
  never blocks the others.
- Every chunk is confirmed by re-reading its IDs: the batch endpoint silently omits IDs
  that no longer exist, and a 404 on that read means every deletion in the chunk was
  applied. An entry that upstream still returns after a 204 is reported as failed —
  never as success. A failed confirmation read is also reported as failed ("delete was
  accepted but the confirmation read failed"), because claiming success without
  confirmation is unacceptable for a destructive operation.

The canonical `api.track.toggl.com/api/v9` host rejects `toggl_sk_` credentials (401
Bearer / 403 Basic); those keys are Focus API tokens and only work against
`focus.toggl.com/api`.

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

This is an enforcement boundary, not just an MCP annotation. When false, the write tools
are not registered and therefore do not appear in `tools/list`.

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

The configuration allowlists exactly the twenty-three registered tools. Its `writes` approval
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
