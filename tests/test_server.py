from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from mcp.client import Client
from pydantic import SecretStr

from toggl_mcp.config import TogglConfig
from toggl_mcp.server import create_server

ORGANIZATION_ID = 321
WORKSPACE_ID = 123
NOW = datetime(2026, 8, 15, 19, 30, tzinfo=UTC)
SCOPE = f"/api/organizations/{ORGANIZATION_ID}/workspaces/{WORKSPACE_ID}"


def config() -> TogglConfig:
    return TogglConfig(
        api_key=SecretStr("toggl_sk_protocol-test"),
        organization_id=ORGANIZATION_ID,
        workspace_id=WORKSPACE_ID,
    )


def running_entry(*, entry_id: int = 10) -> dict[str, object]:
    return {
        "id": entry_id,
        "workspace_id": WORKSPACE_ID,
        "project_id": 88,
        "task_id": None,
        "description": "MCP learning",
        "start": "2026-08-15T18:00:00Z",
        "type": "activity",
        "tags": None,
    }


def stopped_entry(*, entry_id: int = 10) -> dict[str, object]:
    return {**running_entry(entry_id=entry_id), "duration": 5400}


@pytest.mark.asyncio
async def test_tools_list_hides_write_tools_by_default() -> None:
    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(lambda _request: httpx.Response(204)),
        enable_write_tools=False,
    )

    async with Client(server) as client:
        result = await client.list_tools(cache_mode="bypass")

    assert [tool.name for tool in result.tools] == READ_TOOL_NAMES
    assert all(tool.output_schema is not None for tool in result.tools)
    assert result.tools[0].input_schema["properties"] == {}
    assert result.tools[0].annotations is not None
    assert result.tools[0].annotations.read_only_hint is True


READ_TOOL_NAMES = [
    "list_projects",
    "get_current_timer",
    "get_time_entries",
    "get_time_entry",
    "list_planned_entries",
    "search",
    "list_clients",
    "list_tags",
    "list_tasks",
    "summarize_time",
    "get_me",
    "list_workspace_members",
]
WRITE_TOOL_NAMES = [
    "start_timer",
    "continue_timer",
    "stop_timer",
    "create_time_entry",
    "update_time_entry",
    "bulk_edit_time_entries",
    "bulk_delete_time_entries",
    "restore_time_entry",
    "log_planned_entry",
    "delete_time_entry",
    "create_project",
    "update_project",
    "delete_project",
    "create_client",
    "update_client",
    "delete_client",
    "create_tag",
    "update_tag",
    "delete_tag",
]


@pytest.mark.asyncio
async def test_tools_list_exposes_exact_v1_surface_when_writes_are_enabled() -> None:
    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(lambda _request: httpx.Response(204)),
        enable_write_tools=True,
    )

    async with Client(server) as client:
        result = await client.list_tools(cache_mode="bypass")

    assert [tool.name for tool in result.tools] == READ_TOOL_NAMES + WRITE_TOOL_NAMES
    by_name = {tool.name: tool for tool in result.tools}
    start = by_name["start_timer"]
    stop = by_name["stop_timer"]
    delete_entry = by_name["delete_time_entry"]
    create_entry = by_name["create_time_entry"]
    update_entry = by_name["update_time_entry"]
    assert set(start.input_schema["properties"]) == {"description", "project_id"}
    assert start.annotations is not None
    assert start.annotations.idempotent_hint is False
    assert stop.annotations is not None
    assert stop.annotations.destructive_hint is True
    assert stop.annotations.idempotent_hint is True
    assert create_entry.annotations is not None
    assert create_entry.annotations.destructive_hint is False
    assert create_entry.annotations.idempotent_hint is False
    assert update_entry.annotations is not None
    assert update_entry.annotations.idempotent_hint is True
    assert delete_entry.annotations is not None
    assert delete_entry.annotations.destructive_hint is True
    assert delete_entry.annotations.idempotent_hint is False


@pytest.mark.asyncio
async def test_read_tools_return_structured_agent_facing_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": 88,
                            "name": "Agent Learning",
                            "workspace_id": WORKSPACE_ID,
                            "active": True,
                        }
                    ],
                    "total": 1,
                },
            )
        if request.url.path.endswith("/tracking/current"):
            return httpx.Response(200, json=running_entry())
        assert request.url.path.endswith("/time-entries")
        return httpx.Response(200, json={"data": [stopped_entry()], "total": 1})

    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(handler),
        enable_write_tools=False,
    )

    async with Client(server) as client:
        projects = await client.call_tool("list_projects")
        current = await client.call_tool("get_current_timer")
        entries = await client.call_tool(
            "get_time_entries",
            {
                "start_date": "2026-08-15T00:00:00+00:00",
                "end_date": "2026-08-15T23:59:59+00:00",
            },
        )

    assert projects.is_error is False
    assert projects.structured_content == {
        "count": 1,
        "projects": [
            {
                "id": 88,
                "name": "Agent Learning",
                "active": True,
                "description": None,
            }
        ],
    }
    assert current.structured_content is not None
    assert current.structured_content["running"] is True
    assert current.structured_content["timer"]["is_running"] is True
    assert entries.structured_content is not None
    assert entries.structured_content["count"] == 1
    assert entries.structured_content["entries"][0]["duration_seconds"] == 5400


@pytest.mark.asyncio
async def test_write_tools_use_backend_state_and_return_structured_results() -> None:
    requests: list[httpx.Request] = []
    current_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal current_reads
        requests.append(request)
        if request.url.path.endswith("/tracking/current"):
            current_reads += 1
            if current_reads == 1:
                return httpx.Response(204)
            return httpx.Response(200, json=running_entry(entry_id=77))
        if request.url.path.endswith("/tracking/start"):
            assert json.loads(request.content) == {
                "description": "MCP learning",
                "start": "2026-08-15T19:30:00.000Z",
                "type": "activity",
                "project_id": 88,
            }
            return httpx.Response(200, json=running_entry(entry_id=77))
        assert request.url.path == f"{SCOPE}/tracking/stop"
        return httpx.Response(200, json=stopped_entry(entry_id=77))

    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
        enable_write_tools=True,
    )

    async with Client(server) as client:
        started = await client.call_tool(
            "start_timer",
            {"description": "MCP learning", "project_id": 88},
        )
        stopped = await client.call_tool("stop_timer")

    assert started.is_error is False
    assert started.structured_content is not None
    assert started.structured_content["started"] is True
    assert stopped.is_error is False
    assert stopped.structured_content is not None
    assert stopped.structured_content["stopped"] is True
    assert [request.method for request in requests] == ["GET", "POST", "GET", "POST"]


@pytest.mark.asyncio
async def test_no_running_timer_is_an_idempotent_stop_no_op() -> None:
    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(lambda _request: httpx.Response(204)),
        enable_write_tools=True,
    )

    async with Client(server) as client:
        result = await client.call_tool("stop_timer")

    assert result.is_error is False
    assert result.structured_content == {
        "stopped": False,
        "timer": None,
        "reason": "no_running_timer",
    }


@pytest.mark.asyncio
async def test_tool_error_redacts_api_key_and_raw_response() -> None:
    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                401,
                text="bad credential toggl_sk_protocol-test and private upstream detail",
            )
        ),
        enable_write_tools=False,
    )

    async with Client(server) as client:
        result = await client.call_tool("list_projects")

    rendered = " ".join(getattr(item, "text", "") for item in result.content)
    assert result.is_error is True
    assert "authentication failed" in rendered
    assert "toggl_sk_protocol-test" not in rendered
    assert "private upstream detail" not in rendered


@pytest.mark.asyncio
async def test_tool_error_handles_422_validation_error() -> None:
    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                422,
                text="unprocessable entity toggl_sk_protocol-test invalid payload",
            )
        ),
        enable_write_tools=False,
    )

    async with Client(server) as client:
        result = await client.call_tool("list_projects")

    rendered = " ".join(getattr(item, "text", "") for item in result.content)
    assert result.is_error is True
    assert "Toggl rejected the request. Check the supplied values and project ID." in rendered
    assert "toggl_sk_protocol-test" not in rendered
    assert "unprocessable entity" not in rendered
    assert "Traceback" not in rendered


@pytest.mark.asyncio
async def test_time_entry_lifecycle_through_protocol() -> None:
    requests: list[httpx.Request] = []

    gets = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "workspace_id": WORKSPACE_ID,
                    "project_id": 88,
                    "description": "Backfilled work",
                    "start": "2026-08-15T09:00:00Z",
                    "duration": 3600,
                    "type": "activity",
                },
            )
        if request.method == "PATCH":
            return httpx.Response(204)
        if request.method == "DELETE":
            return httpx.Response(200, text="OK")
        gets["n"] += 1
        description = "Backfilled work" if gets["n"] == 1 else "Renamed work"
        return httpx.Response(
            200,
            json={
                "id": 42,
                "workspace_id": WORKSPACE_ID,
                "project_id": 88,
                "description": description,
                "start": "2026-08-15T09:00:00Z",
                "duration": 7200,
                "type": "activity",
            },
        )

    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(handler),
        enable_write_tools=True,
    )

    async with Client(server) as client:
        created = await client.call_tool(
            "create_time_entry",
            {
                "description": "Backfilled work",
                "start": "2026-08-15T09:00:00+00:00",
                "duration_seconds": 3600,
                "project_id": 88,
            },
        )
        updated = await client.call_tool(
            "update_time_entry",
            {"entry_id": 42, "description": "Renamed work", "duration_seconds": 7200},
        )
        deleted = await client.call_tool("delete_time_entry", {"entry_id": 42})

    assert created.is_error is False
    assert created.structured_content is not None
    assert created.structured_content["created"] is True
    assert created.structured_content["time_entry"]["duration_seconds"] == 3600
    assert updated.is_error is False
    assert updated.structured_content is not None
    assert updated.structured_content["time_entry"]["description"] == "Renamed work"
    assert deleted.is_error is False
    assert deleted.structured_content is not None
    assert deleted.structured_content == {"deleted": True, "entity_id": 42}
    # update = duration change reads current state first, then PATCH, then re-read.
    assert [request.method for request in requests] == [
        "POST", "GET", "PATCH", "GET", "DELETE",
    ]


@pytest.mark.asyncio
async def test_workspace_list_tools_return_structured_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/clients"):
            return httpx.Response(
                200, json={"data": [{"id": 5, "name": "ACME", "wid": WORKSPACE_ID}]}
            )
        if request.url.path.endswith("/tags"):
            return httpx.Response(
                200, json=[{"id": 7, "name": "learning", "workspace_id": WORKSPACE_ID}]
            )
        assert request.url.path.endswith("/projects/88/tasks")
        return httpx.Response(
            200, json={"data": [{"id": 13, "name": "Setup", "project_id": 88}]}
        )

    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(handler),
        enable_write_tools=False,
    )

    async with Client(server) as client:
        clients = await client.call_tool("list_clients")
        tags = await client.call_tool("list_tags")
        tasks = await client.call_tool("list_tasks", {"project_id": 88})

    assert clients.is_error is False
    assert clients.structured_content == {
        "count": 1,
        "clients": [{"id": 5, "name": "ACME", "archived": False}],
    }
    assert tags.is_error is False
    assert tags.structured_content == {"count": 1, "tags": [{"id": 7, "name": "learning"}]}
    assert tasks.is_error is False
    assert tasks.structured_content is not None
    assert tasks.structured_content["count"] == 1
    assert tasks.structured_content["tasks"][0]["name"] == "Setup"


@pytest.mark.asyncio
async def test_list_planned_entries_returns_structured_results() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        assert request.url.path.endswith("/time-entries")
        return httpx.Response(
            200,
            json={
                "data": [
                    stopped_entry(),
                    {
                        "id": 30,
                        "workspace_id": WORKSPACE_ID,
                        "task_id": None,
                        "project_id": None,
                        "type": "activity",
                        "planned_start": "2026-08-18T01:30:00Z",
                        "planned_duration": 3600,
                        "description": "leetcode",
                        "tag_ids": None,
                        "tags": [],
                        "billable": False,
                    },
                ],
                "page": 1,
                "per_page": 100,
            },
        )

    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(handler),
        enable_write_tools=False,
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "list_planned_entries",
            {
                "start_date": "2026-08-15T00:00:00+00:00",
                "end_date": "2026-08-20T00:00:00+00:00",
            },
        )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["count"] == 1
    assert result.structured_content["possibly_truncated"] is False
    planned = result.structured_content["entries"][0]
    assert planned["id"] == 30
    assert planned["description"] == "leetcode"
    assert planned["project_id"] is None
    assert planned["planned_duration_seconds"] == 3600
    assert planned["entry_type"] == "activity"
    assert planned["planned_start"].startswith("2026-08-18T01:30:00")
    assert captured[0].url.params["date_from"] == "2026-08-15T00:00:00.000Z"


@pytest.mark.asyncio
async def test_bulk_delete_time_entries_returns_per_entry_outcomes() -> None:
    requests: list[httpx.Request] = []
    reads = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/batch"):
            reads["n"] += 1
            if reads["n"] == 1:
                # Pre-read: both entries exist.
                return httpx.Response(
                    200,
                    json=[stopped_entry(entry_id=11), stopped_entry(entry_id=12)],
                )
            # Confirmation read: only 12 still exists.
            return httpx.Response(200, json=[stopped_entry(entry_id=12)])
        assert request.method == "DELETE"
        assert request.url.path.endswith("/time-entries/bulk")
        assert request.url.params["ids"] == "11,12"
        return httpx.Response(204)

    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(handler),
        enable_write_tools=True,
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "bulk_delete_time_entries",
            {"entry_ids": [11, 12]},
        )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["deleted_count"] == 1
    assert result.structured_content["failed_count"] == 1
    outcomes = result.structured_content["outcomes"]
    assert outcomes[0] == {
        "entry_id": 11,
        "deleted": True,
        "error": None,
    }
    assert outcomes[1]["deleted"] is False
    assert "still exists" in (outcomes[1]["error"] or "")
    assert [request.method for request in requests] == ["GET", "DELETE", "GET"]


@pytest.mark.asyncio
async def test_search_returns_structured_suggestions() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        assert request.url.path.endswith("/search")
        return httpx.Response(
            200,
            json={
                "time_entries": [
                    {
                        "description": "leetcode practice",
                        "project_id": 88,
                        "project_name": "Agent Learning",
                        "tag_ids": None,
                        "last_tracked_at": "2026-08-18T03:30:00Z",
                        "matched_terms": 1,
                    }
                ],
                "tasks": [],
                "projects": [{"id": 88, "name": "Agent Learning", "matched_terms": 1}],
            },
        )

    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(handler),
        enable_write_tools=False,
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "search", {"keyword": "leetcode", "per_group": 3}
        )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["keyword"] == "leetcode"
    assert result.structured_content["projects"][0]["id"] == 88
    hit = result.structured_content["time_entries"][0]
    assert hit["description"] == "leetcode practice"
    assert hit["project_name"] == "Agent Learning"
    assert captured[0].url.params["keyword"] == "leetcode"


@pytest.mark.asyncio
async def test_continue_timer_starts_via_native_route() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tracking/current"):
            return httpx.Response(204)
        assert request.url.path.endswith("/tracking/start-from-description")
        return httpx.Response(
            200,
            json={"time_entry": running_entry(entry_id=91), "task": None},
        )

    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(handler),
        enable_write_tools=True,
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "continue_timer", {"description": "MCP learning"}
        )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["started"] is True
    assert result.structured_content["timer"]["is_running"] is True
    body = json.loads(requests[-1].content)
    assert body == {
        "name": "MCP learning",
        "extension_source": "toggl-mcp",
        "type": "activity",
    }


@pytest.mark.asyncio
async def test_update_client_and_delete_tag_round_trip() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/clients/5"):
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "id": 5,
                        "workspace_id": WORKSPACE_ID,
                        "name": "Renamed",
                        "active": True,
                    },
                )
            assert request.method == "PUT"
            return httpx.Response(204)
        assert request.method == "DELETE"
        assert request.url.path.endswith("/tags/7")
        return httpx.Response(204)

    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(handler),
        enable_write_tools=True,
    )

    async with Client(server) as client:
        updated = await client.call_tool(
            "update_client", {"client_id": 5, "name": "Renamed"}
        )
        deleted = await client.call_tool("delete_tag", {"tag_id": 7})

    assert updated.is_error is False
    assert updated.structured_content == {
        "id": 5,
        "name": "Renamed",
        "archived": False,
    }
    assert deleted.structured_content == {"deleted": True, "entity_id": 7}
    put_body = json.loads(requests[0].content)
    assert put_body == {"name": "Renamed"}


@pytest.mark.asyncio
async def test_summarize_time_returns_structured_groups() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/query"):
            body = json.loads(request.content)
            if "groupings" not in body:
                ungrouped = {"data_json_row": [{"count": 2, "sum_duration": 3600}]}
                return httpx.Response(200, json=ungrouped)
            return httpx.Response(
                200,
                json={
                    "data_json_row": [
                        {"count": 1, "project_id": 88, "sum_duration": 3600},
                        {"count": 1, "project_id": 0, "sum_duration": 0},
                    ]
                },
            )
        if request.url.path.endswith("/projects"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": 88, "name": "Agent Learning", "workspace_id": WORKSPACE_ID}
                    ],
                    "total": 1,
                },
            )
        if request.url.path.endswith("/tracking/current"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(handler),
        enable_write_tools=False,
    )

    async with Client(server) as client:
        result = await client.call_tool(
            "summarize_time",
            {
                "start_date": "2026-08-15T00:00:00+00:00",
                "end_date": "2026-08-15T23:59:59+00:00",
                "group_by": "project",
            },
        )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["tracked_seconds"] == 3600
    assert result.structured_content["running_count"] == 0
    assert result.structured_content["entry_count"] == 2
    assert result.structured_content["possibly_truncated"] is False
    assert result.structured_content["groups"] == [
        {
            "label": "Agent Learning",
            "seconds": 3600,
            "entry_count": 1,
            "project_id": 88,
        },
        {"label": "(no project)", "seconds": 0, "entry_count": 1, "project_id": None},
    ]
