from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from toggl_mcp.client import TogglClient
from toggl_mcp.config import TogglConfig
from toggl_mcp.exceptions import (
    TimerAlreadyRunningError,
    TogglAPIError,
    TogglAuthorizationError,
    TogglQuotaError,
    TogglRateLimitError,
)

ORGANIZATION_ID = 321
WORKSPACE_ID = 123
NOW = datetime(2026, 8, 15, 19, 30, tzinfo=UTC)
SCOPE = f"/api/organizations/{ORGANIZATION_ID}/workspaces/{WORKSPACE_ID}"


def config(*, page_size: int = 100) -> TogglConfig:
    return TogglConfig(
        api_key=SecretStr("toggl_sk_test-key"),
        organization_id=ORGANIZATION_ID,
        workspace_id=WORKSPACE_ID,
        page_size=page_size,
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
        "tags": [{"id": 7, "name": "learning", "color": "#123456"}],
    }


def stopped_entry(*, entry_id: int = 10) -> dict[str, object]:
    return {
        **running_entry(entry_id=entry_id),
        "duration": 5400,
    }


@pytest.mark.asyncio
async def test_list_projects_consumes_pagination_and_uses_bearer_auth() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = request.url.params["page"]
        if page == "1":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": 1, "name": "Agent Learning", "workspace_id": WORKSPACE_ID},
                        {"id": 2, "name": "Other", "workspace_id": 999},
                    ],
                    "page": 1,
                    "per_page": 2,
                    "total": 3,
                },
            )
        assert page == "2"
        return httpx.Response(
            200,
            json={
                "data": [{"id": 3, "name": "MCP", "workspace_id": WORKSPACE_ID}],
                "page": 2,
                "per_page": 2,
                "total": 3,
            },
        )

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(page_size=2), transport=transport) as client:
        projects = await client.list_projects()

    assert [project.name for project in projects] == ["Agent Learning", "MCP"]
    assert len(requests) == 2
    assert requests[0].url.path == f"{SCOPE}/projects"
    assert requests[0].url.params["only_me"] == "true"
    assert requests[0].headers["Authorization"] == "Bearer toggl_sk_test-key"


@pytest.mark.asyncio
async def test_get_current_timer_normalizes_204_to_none() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(204))
    async with TogglClient(config(), transport=transport) as client:
        assert await client.get_current_timer() is None


@pytest.mark.asyncio
async def test_get_time_entries_consumes_pages_and_uses_rfc3339_boundaries() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        page = int(request.url.params["page"])
        entries = [stopped_entry(entry_id=page * 10 + offset) for offset in range(2)]
        if page == 2:
            entries = entries[:1]
        return httpx.Response(200, json={"data": entries, "page": page, "per_page": 2})

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(page_size=2), transport=transport) as client:
        result = await client.get_time_entries(
            datetime(2026, 8, 15, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 15, 23, 59, 59, 999000, tzinfo=UTC),
        )

    assert result.count == 3
    assert result.possibly_truncated is False
    assert result.entries[0].tags[0].name == "learning"
    assert len(captured) == 2
    assert captured[0].url.params["date_from"] == "2026-08-15T00:00:00.000Z"
    assert captured[0].url.params["date_to"] == "2026-08-15T23:59:59.999Z"
    assert captured[0].url.params["include_taskless"] == "true"


@pytest.mark.asyncio
async def test_get_time_entries_marks_null_duration_as_running() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"data": [running_entry()]})
    )
    async with TogglClient(config(), transport=transport) as client:
        result = await client.get_time_entries(
            datetime(2026, 8, 15, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 15, 23, 59, 59, tzinfo=UTC),
        )

    assert result.entries[0].duration is None
    assert result.entries[0].is_running is True


@pytest.mark.asyncio
async def test_get_time_entries_rejects_naive_dates_before_network_call() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network should not be called")

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        with pytest.raises(ValueError, match="timezone"):
            await client.get_time_entries(datetime(2026, 8, 15), NOW)


@pytest.mark.asyncio
async def test_start_timer_preflights_and_posts_minimal_body() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tracking/current"):
            return httpx.Response(204)
        assert request.url.path == f"{SCOPE}/tracking/start"
        body = json.loads(request.content)
        assert body == {
            "description": "MCP learning",
            "start": "2026-08-15T19:30:00.000Z",
            "type": "activity",
            "project_id": 88,
        }
        return httpx.Response(200, json=running_entry())

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport, clock=lambda: NOW) as client:
        started = await client.start_timer("  MCP learning  ", project_id=88)

    assert started.is_running is True
    assert [request.method for request in requests] == ["GET", "POST"]


@pytest.mark.asyncio
async def test_start_timer_does_not_write_when_one_is_running() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=running_entry(entry_id=77))

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        with pytest.raises(TimerAlreadyRunningError) as captured:
            await client.start_timer("New work")

    assert captured.value.current_entry_id == 77
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
async def test_stop_timer_uses_scoped_endpoint_and_clock() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=running_entry(entry_id=77))
        assert request.url.path == f"{SCOPE}/tracking/stop"
        assert json.loads(request.content) == {"end": "2026-08-15T19:30:00.000Z"}
        return httpx.Response(200, json=stopped_entry(entry_id=77))

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport, clock=lambda: NOW) as client:
        stopped = await client.stop_timer()

    assert stopped is not None
    assert stopped.is_running is False
    assert [request.method for request in requests] == ["GET", "POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, TogglAuthorizationError),
        (403, TogglAuthorizationError),
        (429, TogglRateLimitError),
    ],
)
async def test_http_errors_are_classified(
    status: int,
    error_type: type[TogglAPIError],
) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            status,
            headers={"Retry-After": "42"},
            json={"error": "safe detail"},
        )
    )
    async with TogglClient(config(), transport=transport) as client:
        with pytest.raises(error_type) as captured:
            await client.get_current_timer()

    assert captured.value.retry_after_seconds == 42


@pytest.mark.asyncio
async def test_error_detail_redacts_api_key() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(401, text="bad credential toggl_sk_test-key")
    )
    async with TogglClient(config(), transport=transport) as client:
        with pytest.raises(TogglAuthorizationError) as captured:
            await client.list_projects()

    assert captured.value.detail == "bad credential [REDACTED]"
