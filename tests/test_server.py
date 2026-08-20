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

    assert [tool.name for tool in result.tools] == [
        "list_projects",
        "get_current_timer",
        "get_time_entries",
    ]
    assert all(tool.output_schema is not None for tool in result.tools)
    assert result.tools[0].input_schema["properties"] == {}
    assert result.tools[0].annotations is not None
    assert result.tools[0].annotations.read_only_hint is True


@pytest.mark.asyncio
async def test_tools_list_exposes_exact_v1_surface_when_writes_are_enabled() -> None:
    server = create_server(
        config_loader=config,
        transport=httpx.MockTransport(lambda _request: httpx.Response(204)),
        enable_write_tools=True,
    )

    async with Client(server) as client:
        result = await client.list_tools(cache_mode="bypass")

    assert [tool.name for tool in result.tools] == [
        "list_projects",
        "get_current_timer",
        "get_time_entries",
        "start_timer",
        "stop_timer",
    ]
    start = result.tools[3]
    stop = result.tools[4]
    assert set(start.input_schema["properties"]) == {"description", "project_id"}
    assert start.annotations is not None
    assert start.annotations.idempotent_hint is False
    assert stop.annotations is not None
    assert stop.annotations.destructive_hint is True
    assert stop.annotations.idempotent_hint is True


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
