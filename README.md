<div align="center">

# toggl-mcp

**Let MCP agents operate your entire Toggl Track workspace — timers, entries,
projects, reports, and cleanup — through 35 verified tools.**

[![CI](https://github.com/Peter-Sherlock/toggl-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Peter-Sherlock/toggl-mcp/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-Official%20Python%20SDK-8A2BE2)
![License](https://img.shields.io/badge/License-MIT-3DA639)

**[English](./README.md)** | **[简体中文](./README.zh-CN.md)**

</div>

---

`toggl-mcp` is a local [Model Context Protocol](https://modelcontextprotocol.io) server
that turns Toggl Track into a clean, agent-friendly tool surface. It hides pagination,
IDs, timestamps, and the quirks of Toggl's newer Focus API behind structured outputs
with explicit success/failure semantics — so an agent can plan and correct itself
without human glue code.

Every tool listed below has been exercised against a real Toggl account, and the
upstream behaviors that differ from the public documentation are verified and
documented (see [Verified upstream behavior](#-verified-upstream-behavior)).

## ✨ Features

**35 tools** — 13 read, 22 write. Write tools only exist when explicitly enabled
(fail-closed, see [Configuration](#%EF%B8%8F-configuration)).

### ⏱ Time tracking

| Tool | Description |
| --- | --- |
| `start_timer(description, project_id=None)` | Start a real timer; refuses instead of replacing one that is already running |
| `continue_timer(description)` | Start a timer from a recent description, letting upstream restore its project/tags context |
| `stop_timer()` | Stop the running timer; a successful no-op when none is running |
| `get_current_timer()` | The timer currently running, if any |

### 📝 Time entries

| Tool | Description |
| --- | --- |
| `get_time_entries(start, end)` | All entries in a timezone-aware interval, paginated internally, with a `possibly_truncated` flag |
| `get_time_entry(entry_id)` | One entry by exact ID |
| `create_time_entry(description, start, duration_seconds, ...)` | Backfill a finished entry with optional project, tags, and billable flag |
| `update_time_entry(entry_id, ...)` | Partial update — description, project, tags, start, duration |
| `delete_time_entry(entry_id)` | Delete one entry (soft delete upstream) |
| `restore_time_entry(entry_id)` | Restore a soft-deleted entry |

### 📦 Bulk operations

| Tool | Description |
| --- | --- |
| `bulk_edit_time_entries(entry_ids, add_tags, remove_tags, project_id)` | Retag and/or move many entries in one call; per-entry outcomes |
| `bulk_delete_time_entries(entry_ids)` | Delete many entries chunked and confirmed per entry; one failure never blocks the rest |
| `log_planned_entry(entry_id)` | Convert a calendar plan into tracked time in place |

### 📁 Projects

| Tool | Description |
| --- | --- |
| `list_projects()` | All workspace projects, paginated internally |
| `get_project(project_id)` | Full detail: estimate, dates, archived/completed state, tracked-time totals |
| `create_project(name, ...)` | With client, color, description, privacy, billable flag, and estimate |
| `update_project(project_id, ...)` | Partial update — name, client, color, description, privacy, billable, estimate |
| `set_project_archived(project_id, archived)` | Archive or unarchive |
| `set_project_completed(project_id, completed)` | Mark complete or reopen |
| `duplicate_project(project_id, name=None)` | Duplicate, optionally under an explicit name |
| `delete_project(project_id)` | Delete a project (entries survive, unassigned) |

### 👥 Clients & tags

| Tool | Description |
| --- | --- |
| `list_clients()` / `create_client(name)` | List and create clients |
| `update_client(client_id, name)` | Rename a client |
| `delete_client(client_id)` | Delete a client |
| `list_tags()` / `create_tag(name)` | List and create tags |
| `update_tag(tag_id, name=None, color=None)` | Rename or recolor a tag |
| `delete_tag(tag_id)` | Delete a tag |

### 📊 Reports & search

| Tool | Description |
| --- | --- |
| `summarize_time(start, end, group_by, project_id=None, user_account_id=None)` | Native Toggl report engine: totals grouped by project, UTC date, ISO week, or tag — optionally filtered to one project and/or one member |
| `search(keyword, per_group=5)` | Unified workspace search across time entries, tasks, and projects |
| `list_planned_entries(start, end)` | Calendar-scheduled entries that do not carry tracked time yet |

### 🏢 Workspace

| Tool | Description |
| --- | --- |
| `get_me()` | The authenticated user's settings, including the workspace selected in Toggl |
| `list_workspace_members()` | Organization members with their workspace membership |
| `list_tasks(project_id)` | Project tasks — requires a Toggl plan with the tasks feature (other plans answer 404 cleanly) |

## 🚀 Quick start

Prerequisites: [uv](https://docs.astral.sh/uv/), Python 3.11+ (managed by uv), a Toggl
Track account with an API key.

```bash
git clone https://github.com/Peter-Sherlock/toggl-mcp.git
cd toggl-mcp
uv sync
copy .env.example .env        # PowerShell; `cp` on macOS/Linux
# edit .env — see next section
```

Smoke-test your credentials (read-only, never modifies your account):

```bash
uv run --env-file .env python scripts/verify_account.py
```

Run the server over stdio:

```bash
uv run --env-file .env toggl-mcp
```

## ⚙️ Configuration

The server is configured entirely through environment variables
([`.env.example`](./.env.example)):

| Variable | Required | Default | Description |
| --- | :---: | --- | --- |
| `TOGGL_API_KEY` | ✅ | — | Toggl 2.0 API key from **Track → Profile → API Token**. Starts with `toggl_sk_`. |
| `TOGGL_ORGANIZATION_ID` | ✅ | — | Your organization ID (integer). |
| `TOGGL_WORKSPACE_ID` | ✅ | — | Your workspace ID (integer). |
| `TOGGL_ENABLE_WRITE_TOOLS` | — | `false` | **Fail-closed write gate.** When `false`, all 22 write tools are not even registered — they never appear in `tools/list`. |
| `TOGGL_TIMEOUT_SECONDS` | — | `10` | HTTP timeout per request (1–60). |

Finding your IDs: open Toggl Track in a browser and enter your workspace — the
workspace ID and organization ID are the integers in the page URLs
(`track.toggl.com/...workspaces/<workspace_id>...`,
`.../organizations/<organization_id>/...`). If either is wrong,
`scripts/verify_account.py` fails fast with an authentication error.

> ⚠️ **Security notes.** The API key is only ever sent to the pinned upstream host
> (`focus.toggl.com`), which is deliberately not configurable. Never commit `.env`;
> error messages redact the key automatically. Starting with write tools disabled and
> enabling them per client is the recommended setup.

## 🔌 Connecting an MCP client

### Generic stdio client (Claude Desktop, ZCode, …)

```json
{
  "mcpServers": {
    "toggl-track": {
      "command": "uv",
      "args": ["run", "--env-file", ".env", "python", "-m", "toggl_mcp.server"],
      "cwd": "/path/to/toggl-mcp"
    }
  }
}
```

### Codex

A project-scoped `.codex/config.toml` next to your workspace root:

```toml
[mcp_servers.toggl_track]
command = 'uv'
args = ["run", "--frozen", "--env-file", ".env", "python", "-m", "toggl_mcp.server"]
cwd = '/path/to/toggl-mcp'
startup_timeout_sec = 30
tool_timeout_sec = 60

[mcp_servers.toggl_track.env]
TOGGL_ENABLE_WRITE_TOOLS = "true"
```

`enabled_tools` can pin an exact allowlist; this repository ships a drift test
(`tests/test_codex_config.py`) that fails when the allowlist no longer matches the
registered tool surface.

## ✅ Verification

```bash
uv run pytest          # 96 offline tests (httpx.MockTransport — never touches Toggl)
uv run ruff check .    # lint
uv run mypy            # strict type check
```

CI runs all three on every push and pull request. The protocol tests exercise the
server through the real MCP boundary (`Client` ↔ `MCPServer` over structured content),
and scripts/verify_mcp.py re-verifies the full 35-tool surface over a real stdio
process.

## 🔍 Verified upstream behavior

Toggl's newer Focus API (`focus.toggl.com/api` — the only host that accepts
`toggl_sk_` keys) deviates from its public documentation in ways this client
handles for you. Highlights, each verified against a live account:

<details>
<summary><strong>Time entries & tracking</strong></summary>

- Create requires a `type` field; tags attach only through `tag_ids` (string `tags`
  are silently ignored), so name-based tool input is resolved against the workspace
  first.
- The single-entry PUT route is a full replace that answers an empty 204 and silently
  ignores project changes — updates and bulk edits use the partial `PATCH` routes and
  re-read to confirm.
- `PATCH /time-entries/bulk-edit` accepts `{ids, changes}` where `changes.tag_ids` is
  tri-state (absent = untouched, list = set, `[]` = clear).
- `DELETE /time-entries/bulk?ids=<csv>` answers an empty 204 and the batch read
  silently omits deleted IDs — bulk deletes are chunked (100 IDs per request) and
  confirmed per entry.
- Single deletes are soft: `PATCH /time-entries/{id}/restore` brings the entry back.
- `POST /tracking/start-from-description` requires `{name, extension_source, type}`
  (discovered through upstream validation errors).
- Planned (calendar) entries have no dedicated route reachable with a workspace token;
  they arrive from the shared range endpoint with `planned_start`/`planned_duration`
  and no `start`. Requesting `per_page` above 100 makes that endpoint silently return
  an empty page.

</details>

<details>
<summary><strong>Projects, clients & tags</strong></summary>

- The project `active` flag is read-only legacy noise — false even for projects in
  normal use — so agents see the meaningful `archived` state (from `archived_at`)
  instead.
- Project create takes `private` (not `is_private`) and has no `active` field; project
  PUT is a full replace that resets omitted fields (verified), so updates use the
  partial PATCH route.
- `POST .../complete` answers a `{project}` envelope, `POST .../uncomplete` answers
  the bare project; `POST .../duplicate` honors an explicit name and assigns a fresh
  color.
- `.../clients/{id}` and `.../tags/{id}` answer PATCH with 405 — PUT is the verb.
  `PUT clients/{id}` requires `name` and silently ignores archive flags, so
  `update_client` offers renaming only. Client `archived` is derived from upstream
  `active`.

</details>

<details>
<summary><strong>Reports & search</strong></summary>

- `POST /reports/workspaces/{wid}/query` groups by `project_id`, `start_date`,
  `tag_ids`, `user_account_id` and aggregates `sum(duration)` in seconds; an empty
  result is `{}` with no `data_json_row` key. Running entries contribute 0 seconds;
  planned entries are excluded.
- Filters must use the `"="` operator; `project_id` and `user_account_id` filters are
  verified, while tag filtering is rejected upstream in every workable shape — so it
  is not offered (`group_by="tag"` covers the dimension).
- The native `week` grouping reports bare week numbers without a year; weekly summaries
  are therefore bucketed client-side into unambiguous ISO `YYYY-Www` labels
  (cross-checked against native rows).
- Grouped rows are paginated explicitly with a safety page limit; the explicit `count`
  aggregation is rejected upstream (counts arrive free on every row).
- Search returns suggestion-style groups: project hits carry IDs, time-entry hits are
  deduplicated descriptions without IDs — resolve exact entries with
  `get_time_entries`.

</details>

## 🗺 Roadmap

- [ ] End-to-end agent validation: scripted real-client sessions, tool-description
      tuning from observed agent behavior
- [ ] Project bulk archive/restore, pinning
- [ ] Optional PyPI publishing for `uvx` installs

Deliberately out of scope: task CRUD (paid plans only), billing rates and
profitability, timesheet approvals, admin surfaces, CSV import, calendar integrations,
and webhooks.

## License

[MIT](./LICENSE) © Peter Wang
