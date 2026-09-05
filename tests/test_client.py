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
    TogglRateLimitError,
    TogglRequestValidationError,
)
from toggl_mcp.models import BulkDeleteOutcome, BulkEditOutcome

ORGANIZATION_ID = 321
WORKSPACE_ID = 123
NOW = datetime(2026, 8, 15, 19, 30, tzinfo=UTC)
SCOPE = f"/api/organizations/{ORGANIZATION_ID}/workspaces/{WORKSPACE_ID}"
WSCOPE = f"/api/workspaces/{WORKSPACE_ID}"


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
        (400, TogglRequestValidationError),
        (401, TogglAuthorizationError),
        (403, TogglAuthorizationError),
        (422, TogglRequestValidationError),
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

    assert captured.value.status_code == status
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


@pytest.mark.asyncio
async def test_validation_error_422_preserves_status_code_and_redacts_api_key() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            422,
            text='{"error": "invalid field", "key": "toggl_sk_test-key"}',
        )
    )
    async with TogglClient(config(), transport=transport) as client:
        with pytest.raises(TogglRequestValidationError) as captured:
            await client.get_current_timer()

    assert captured.value.status_code == 422
    assert captured.value.detail is not None
    assert "toggl_sk_test-key" not in captured.value.detail
    assert "[REDACTED]" in captured.value.detail


@pytest.mark.asyncio
async def test_get_time_entries_stops_at_reported_total_without_extra_page() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [stopped_entry(), stopped_entry(entry_id=11)],
                "total": 2,
            },
        )

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(page_size=2), transport=transport) as client:
        result = await client.get_time_entries(
            datetime(2026, 8, 15, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 15, 23, 59, 59, tzinfo=UTC),
        )

    assert result.count == 2
    assert result.possibly_truncated is False
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_get_time_entries_flags_truncation_when_total_exceeds_fetched() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(
                200,
                json={
                    "data": [
                        stopped_entry(entry_id=calls["n"]),
                        stopped_entry(entry_id=calls["n"] * 10),
                    ],
                    "total": 6,
                },
            )
        return httpx.Response(200, json={"data": [], "total": 6})

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(page_size=2), transport=transport) as client:
        result = await client.get_time_entries(
            datetime(2026, 8, 15, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 15, 23, 59, 59, tzinfo=UTC),
        )

    assert result.count == 4
    assert result.possibly_truncated is True
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_list_projects_uses_fetched_count_against_total() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = request.url.params["page"]
        if page == "1":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": 1, "name": "Mine", "workspace_id": WORKSPACE_ID},
                        {"id": 2, "name": "Foreign", "workspace_id": 999},
                    ],
                    "total": 3,
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [{"id": 3, "name": "Mine2", "workspace_id": WORKSPACE_ID}],
                "total": 3,
            },
        )

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(page_size=2), transport=transport) as client:
        projects = await client.list_projects()

    assert [project.name for project in projects] == ["Mine", "Mine2"]
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_get_time_entries_allows_equal_start_and_end() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"data": [], "total": 0})

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        result = await client.get_time_entries(NOW, NOW)

    assert result.count == 0
    assert captured[0].url.params["date_from"] == captured[0].url.params["date_to"]


def planned_entry_json(*, entry_id: int = 30) -> dict[str, object]:
    """Verified real-API shape: planned_start/planned_duration instead of start/duration."""

    return {
        "id": entry_id,
        "workspace_id": WORKSPACE_ID,
        "task_id": None,
        "project_id": None,
        "time_block_id": None,
        "type": "activity",
        "toggl_user_id": 7663892,
        "planned_start": "2026-08-18T01:30:00Z",
        "planned_duration": 3600,
        "description": "leetcode",
        "tag_ids": None,
        "tags": [],
        "billable": False,
        "billable_source": "task_default",
    }


@pytest.mark.asyncio
async def test_list_planned_entries_filters_tracked_and_keeps_planned_only() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        page = int(request.url.params["page"])
        if page == 1:
            rows = [
                planned_entry_json(entry_id=30),
                stopped_entry(entry_id=31),
                {"id": 32, "workspace_id": WORKSPACE_ID, "type": "activity"},
            ]
            return httpx.Response(200, json={"data": rows, "page": 1, "per_page": 3})
        return httpx.Response(
            200, json={"data": [planned_entry_json(entry_id=33)], "page": 2, "per_page": 3}
        )

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(page_size=3), transport=transport) as client:
        result = await client.list_planned_entries(
            datetime(2026, 8, 15, tzinfo=UTC), datetime(2026, 8, 20, tzinfo=UTC)
        )

    assert result.count == 2
    assert result.possibly_truncated is False
    assert [entry.id for entry in result.entries] == [30, 33]
    # A row without planned_start cannot validate and is skipped, not fatal.
    assert result.entries[0].planned_duration == 3600
    assert result.entries[0].tag_ids == []
    assert len(captured) == 2
    assert captured[0].url.params["date_from"] == "2026-08-15T00:00:00.000Z"
    assert captured[0].url.params["include_taskless"] == "true"


@pytest.mark.asyncio
async def test_list_planned_entries_rejects_naive_dates_before_network_call() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network should not be called")

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        with pytest.raises(ValueError, match="timezone"):
            await client.list_planned_entries(datetime(2026, 8, 15), NOW)
        with pytest.raises(ValueError, match="before or equal"):
            await client.list_planned_entries(NOW, datetime(2026, 8, 14, tzinfo=UTC))


@pytest.mark.asyncio
async def test_running_flag_uses_one_rule_across_all_read_paths() -> None:
    current_reads = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if request.url.path.endswith("/time-entries"):
                return httpx.Response(
                    200, json={"data": [running_entry(entry_id=77)], "total": 1}
                )
            current_reads["n"] += 1
            if current_reads["n"] < 3:
                return httpx.Response(204)
            return httpx.Response(200, json=running_entry(entry_id=77))
        return httpx.Response(200, json=running_entry(entry_id=77))

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport, clock=lambda: NOW) as client:
        current = await client.get_current_timer()
        ranged = await client.get_time_entries(NOW, NOW)
        started = await client.start_timer("MCP learning")
        stopped = await client.stop_timer()

    assert current is None
    assert ranged.entries[0].is_running is True
    assert started.is_running is True
    # Stop semantics win even though the stop response still reports `duration: null`.
    assert stopped is not None and stopped.is_running is False


@pytest.mark.asyncio
async def test_create_time_entry_posts_expected_body() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tags"):
            return httpx.Response(
                200,
                json=[{"id": 7, "name": "learning", "workspace_id": WORKSPACE_ID}],
            )
        assert request.url.path == f"{SCOPE}/time-entries"
        return httpx.Response(
            200,
            json={
                "id": 42,
                "workspace_id": WORKSPACE_ID,
                "project_id": 88,
                "description": "Backfilled work",
                "start": "2026-08-15T09:00:00Z",
                "duration": 3600,
                "tag_ids": [7],
                "tags": [{"id": 7, "name": "learning"}],
            },
        )

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        entry = await client.create_time_entry(
            "Backfilled work",
            datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
            3600,
            project_id=88,
            tags=["learning"],
            billable=True,
        )

    body = json.loads(requests[-1].content)
    assert body == {
        "description": "Backfilled work",
        "start": "2026-08-15T09:00:00.000Z",
        "duration": 3600,
        "billable": True,
        "created_with": "toggl-mcp",
        "type": "activity",
        "project_id": 88,
        "tag_ids": [7],
    }
    assert entry.id == 42
    assert entry.is_running is False
    assert entry.tags[0].name == "learning"


@pytest.mark.asyncio
async def test_update_time_entry_patches_changed_fields_only() -> None:
    requests: list[httpx.Request] = []

    patched = {"done": False}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            description = "Renamed work" if patched["done"] else "Old work"
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "workspace_id": WORKSPACE_ID,
                    "project_id": 88,
                    "description": description,
                    "start": "2026-08-15T09:00:00Z",
                    "duration": 3600,
                    "billable": True,
                    "type": "activity",
                    "tag_ids": [7],
                },
            )
        assert request.method == "PATCH"
        # The verified upstream PATCH answers 204 with an empty body.
        patched["done"] = True
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        entry = await client.update_time_entry(42, description="Renamed work")

    # PATCH is partial: only the changed field is sent.
    patch_request = next(r for r in requests if r.method == "PATCH")
    assert json.loads(patch_request.content) == {"description": "Renamed work"}
    assert entry.description == "Renamed work"


@pytest.mark.asyncio
async def test_update_time_entry_rejects_duration_change_while_running() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "workspace_id": WORKSPACE_ID,
                    "description": "Running work",
                    "start": "2026-08-15T09:00:00Z",
                    "type": "activity",
                },
            )
        raise AssertionError("PATCH must not happen for a running entry")

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        with pytest.raises(ValueError, match="stop the timer"):
            await client.update_time_entry(42, duration_seconds=120)


@pytest.mark.asyncio
async def test_tag_names_resolve_to_ids_and_unknown_names_fail() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/tags"):
            return httpx.Response(
                200,
                json=[{"id": 7, "name": "learning", "workspace_id": WORKSPACE_ID}],
            )
        return httpx.Response(
            200,
            json={
                "id": 42,
                "workspace_id": WORKSPACE_ID,
                "description": "work",
                "start": "2026-08-15T09:00:00Z",
                "duration": 60,
                "type": "activity",
            },
        )

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        with pytest.raises(ValueError, match="Unknown tag"):
            await client.create_time_entry(
                "work",
                datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
                60,
                tags=["nonexistent"],
            )
        entry = await client.update_time_entry(42, tags=["learning"])
        assert entry.duration == 60

    patch_request = next(call for call in calls if call.method == "PATCH")
    patch_body = json.loads(patch_request.content)
    assert patch_body["tag_ids"] == [7]


@pytest.mark.asyncio
async def test_update_time_entry_rejects_empty_change_set() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(500, text="must not be called")
    )
    async with TogglClient(config(), transport=transport) as client:
        with pytest.raises(ValueError, match="at least one field"):
            await client.update_time_entry(42)


@pytest.mark.asyncio
async def test_delete_time_entry_tolerates_non_json_ok_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == f"{SCOPE}/time-entries/42"
        return httpx.Response(200, text="OK")

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        await client.delete_time_entry(42)


@pytest.mark.asyncio
async def test_create_and_update_project_use_org_scoped_paths() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": 91,
                "name": "Renamed project",
                "workspace_id": WORKSPACE_ID,
                "active": False,
                "client_id": 5,
            },
        )

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        created = await client.create_project("New project", client_id=5)
        updated = await client.update_project(91, name="Renamed project", active=False)

    assert requests[0].url.path == f"{SCOPE}/projects"
    assert json.loads(requests[0].content) == {
        "name": "New project",
        "active": True,
        "is_private": True,
        "created_with": "toggl-mcp",
        "client_id": 5,
    }
    assert requests[1].url.path == f"{SCOPE}/projects/91"
    assert json.loads(requests[1].content) == {"name": "Renamed project", "active": False}
    assert created.client_id == 5
    assert updated.active is False


@pytest.mark.asyncio
async def test_list_clients_consumes_envelope_pagination_on_workspace_scope() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = request.url.params["page"]
        if page == "1":
            return httpx.Response(
                200,
                json={"data": [{"id": 5, "name": "ACME", "wid": WORKSPACE_ID}], "total": 2},
            )
        return httpx.Response(
            200,
            json={"data": [{"id": 6, "name": "Beta", "wid": WORKSPACE_ID}], "total": 2},
        )

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(page_size=1), transport=transport) as client:
        clients = await client.list_clients()

    assert [client.name for client in clients] == ["ACME", "Beta"]
    assert len(requests) == 2
    assert requests[0].url.path == WSCOPE + "/clients"


@pytest.mark.asyncio
async def test_list_tags_accepts_plain_array_response() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[{"id": 7, "name": "learning", "color": "#123456", "workspace_id": WORKSPACE_ID}],
        )

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        tags = await client.list_tags()

    assert [tag.name for tag in tags] == ["learning"]
    assert len(requests) == 1
    assert requests[0].url.path == WSCOPE + "/tags"


@pytest.mark.asyncio
async def test_create_client_and_tag_post_to_workspace_scope() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/clients"):
            return httpx.Response(200, json={"id": 9, "name": "ACME", "wid": WORKSPACE_ID})
        assert request.url.path == WSCOPE + "/tags"
        return httpx.Response(200, json={"id": 10, "name": "urgent"})

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        created_client = await client.create_client("ACME")
        created_tag = await client.create_tag("urgent")

    assert created_client.id == 9
    assert created_tag.name == "urgent"
    assert [request.method for request in requests] == ["POST", "POST"]


@pytest.mark.asyncio
async def test_list_tasks_uses_workspace_scoped_project_path() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [{"id": 13, "name": "Setup", "project_id": 88, "active": True}],
                "total": 1,
            },
        )

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        tasks = await client.list_tasks(88)

    assert tasks[0].name == "Setup"
    assert requests[0].url.path == WSCOPE + "/projects/88/tasks"


def _query_handler(
    requests: list[httpx.Request],
    *,
    rows: list[dict[str, object]],
    projects: list[dict[str, object]] | None = None,
    tags: list[dict[str, object]] | None = None,
    user_rows: list[dict[str, object]] | None = None,
    current: dict[str, object] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/query"):
            body = json.loads(request.content)
            grouping = body["groupings"][0]["property"]
            if grouping == "user_account_id":
                return httpx.Response(200, json={"data_json_row": user_rows or rows})
            return httpx.Response(200, json={"data_json_row": rows})
        if request.url.path.endswith("/projects") and projects is not None:
            return httpx.Response(200, json={"data": projects, "total": len(projects)})
        if request.url.path.endswith("/tags") and tags is not None:
            return httpx.Response(200, json=tags)
        if request.url.path.endswith("/tracking/current"):
            if current is None:
                return httpx.Response(204)
            return httpx.Response(200, json=current)
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_summarize_time_project_groups_resolve_names() -> None:
    requests: list[httpx.Request] = []
    transport = _query_handler(
        requests,
        rows=[
            {"count": 3, "project_id": 0, "sum_duration": 18697},
            {"count": 1, "project_id": 88, "sum_duration": 62},
        ],
        projects=[{"id": 88, "name": "Agent Learning", "workspace_id": WORKSPACE_ID}],
    )
    async with TogglClient(config(), transport=transport) as client:
        summary = await client.summarize_time(
            datetime(2026, 8, 15, tzinfo=UTC), datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
        )

    assert summary.tracked_seconds == 18759
    assert summary.entry_count == 4
    assert summary.running_count == 0
    assert summary.possibly_truncated is False
    assert [(g.label, g.seconds, g.entry_count) for g in summary.groups] == [
        ("(no project)", 18697, 3),
        ("Agent Learning", 62, 1),
    ]
    assert summary.groups[1].project_id == 88
    # 查询体应使用 RFC3339 时间戳边界
    query_body = json.loads(requests[0].content)
    assert query_body["period"] == {
        "from": "2026-08-15T00:00:00.000Z",
        "to": "2026-08-16T12:00:00.000Z",
    }
    assert query_body["groupings"] == [{"property": "project_id"}]


@pytest.mark.asyncio
async def test_summarize_time_skips_project_lookup_without_project_ids() -> None:
    requests: list[httpx.Request] = []
    transport = _query_handler(
        requests,
        rows=[{"count": 2, "project_id": 0, "sum_duration": 120}],
    )
    async with TogglClient(config(), transport=transport) as client:
        summary = await client.summarize_time(
            datetime(2026, 8, 15, tzinfo=UTC), datetime(2026, 8, 16, tzinfo=UTC)
        )

    # one report query + one running-timer check; no /projects read.
    paths = [r.url.path for r in requests]
    assert paths == [
        f"/api/reports/workspaces/{WORKSPACE_ID}/query",
        f"/api/organizations/{ORGANIZATION_ID}/workspaces/{WORKSPACE_ID}/tracking/current",
    ]
    assert summary.groups[0].label == "(no project)"


@pytest.mark.asyncio
async def test_summarize_time_disambiguates_duplicate_project_names() -> None:
    requests: list[httpx.Request] = []
    transport = _query_handler(
        requests,
        rows=[
            {"count": 1, "project_id": 1, "sum_duration": 30},
            {"count": 1, "project_id": 2, "sum_duration": 70},
        ],
        projects=[
            {"id": 1, "name": "Same", "workspace_id": WORKSPACE_ID},
            {"id": 2, "name": "Same", "workspace_id": WORKSPACE_ID},
        ],
    )
    async with TogglClient(config(), transport=transport) as client:
        summary = await client.summarize_time(
            datetime(2026, 8, 15, tzinfo=UTC), datetime(2026, 8, 16, tzinfo=UTC)
        )

    assert [(g.label, g.seconds) for g in summary.groups] == [
        ("Same (project 2)", 70),
        ("Same (project 1)", 30),
    ]


@pytest.mark.asyncio
async def test_summarize_time_date_groups_use_server_buckets() -> None:
    requests: list[httpx.Request] = []
    transport = _query_handler(
        requests,
        rows=[
            {"count": 1, "start_date": "2026-08-17", "sum_duration": 16879},
            {"count": 3, "start_date": "2026-09-02", "sum_duration": 1861},
        ],
    )
    async with TogglClient(config(), transport=transport) as client:
        summary = await client.summarize_time(
            datetime(2026, 8, 15, tzinfo=UTC),
            datetime(2026, 9, 5, tzinfo=UTC),
            group_by="date",
        )

    assert [(g.label, g.seconds) for g in summary.groups] == [
        ("2026-08-17", 16879),
        ("2026-09-02", 1861),
    ]
    query_body = json.loads(requests[0].content)
    assert query_body["groupings"] == [{"property": "start_date"}]


@pytest.mark.asyncio
async def test_summarize_time_tag_groups_resolve_names_and_exact_totals() -> None:
    requests: list[httpx.Request] = []
    transport = _query_handler(
        requests,
        rows=[
            {"count": 1, "tag_ids": [7], "sum_duration": 600},
            {"count": 2, "tag_ids": [], "sum_duration": 1858},
        ],
        tags=[{"id": 7, "name": "learning", "workspace_id": WORKSPACE_ID}],
        user_rows=[{"count": 3, "user_account_id": 7663892, "sum_duration": 2458}],
    )
    async with TogglClient(config(), transport=transport) as client:
        summary = await client.summarize_time(
            datetime(2026, 8, 15, tzinfo=UTC),
            datetime(2026, 9, 5, tzinfo=UTC),
            group_by="tag",
        )

    # exact totals come from the ungrouped query (tag rows double-count)
    assert summary.tracked_seconds == 2458
    assert summary.entry_count == 3
    assert [(g.label, g.seconds) for g in summary.groups] == [
        ("untagged", 1858),
        ("learning", 600),
    ]


@pytest.mark.asyncio
async def test_summarize_time_tolerates_empty_result_object() -> None:
    """Verified: the query endpoint answers `{}` (no data_json_row) when empty."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/query"):
            # Verified: the real endpoint answers {} when nothing matches.
            return httpx.Response(200, json={})
        if request.url.path.endswith("/tracking/current"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)

    async with TogglClient(config(), transport=transport) as client:
        summary = await client.summarize_time(
            datetime(2026, 8, 15, tzinfo=UTC), datetime(2026, 8, 16, tzinfo=UTC)
        )

    assert summary.groups == []
    assert summary.tracked_seconds == 0
    assert summary.entry_count == 0


@pytest.mark.asyncio
async def test_summarize_time_reports_running_timer_in_range() -> None:
    requests: list[httpx.Request] = []
    transport = _query_handler(
        requests,
        rows=[{"count": 1, "project_id": 0, "sum_duration": 0}],
        current={
            "id": 77,
            "workspace_id": WORKSPACE_ID,
            "description": "running",
            "start": "2026-08-15T10:00:00Z",
            "type": "activity",
        },
    )
    async with TogglClient(config(), transport=transport) as client:
        summary = await client.summarize_time(
            datetime(2026, 8, 15, tzinfo=UTC), datetime(2026, 8, 16, tzinfo=UTC)
        )

    assert summary.running_count == 1
    assert summary.tracked_seconds == 0


def _entry_json(entry_id: int, *, tag_ids: list[int], project_id: int) -> dict[str, object]:
    return {
        "id": entry_id,
        "workspace_id": WORKSPACE_ID,
        "project_id": project_id,
        "description": "bulk work",
        "start": "2026-08-15T09:00:00Z",
        "duration": 600,
        "type": "activity",
        "tag_ids": tag_ids,
    }


@pytest.mark.asyncio
async def test_bulk_edit_adds_tags_and_moves_projects_via_bulk_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tags"):
            return httpx.Response(
                200,
                json=[{"id": 7, "name": "learn", "workspace_id": WORKSPACE_ID}],
            )
        if request.method == "GET" and request.url.path.endswith("/batch"):
            requested = request.url.params["ids"]
            entries = [
                _entry_json(int(raw_id), tag_ids=[], project_id=99)
                for raw_id in requested.split(",")
            ]
            return httpx.Response(200, json=entries)
        if request.method == "PATCH" and request.url.path.endswith("/bulk-edit"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        outcomes = await client.bulk_edit_time_entries(
            [11, 12], add_tags=["learn"], project_id=99
        )

    assert outcomes == [
        BulkEditOutcome(entry_id=11, updated=True, error=None),
        BulkEditOutcome(entry_id=12, updated=True, error=None),
    ]
    # One batch read, then ONE grouped bulk-edit call (both entries share the
    # same resulting tag set).
    bulk_calls = [
        json.loads(r.content) for r in requests if str(r.url.path).endswith("/bulk-edit")
    ]
    assert len(bulk_calls) == 1
    assert bulk_calls[0] == {
        "ids": [11, 12],
        "changes": {"project_id": 99, "tag_ids": [7]},
    }


@pytest.mark.asyncio
async def test_bulk_edit_groups_entries_by_resulting_tag_set() -> None:
    requests: list[httpx.Request] = []

    moved = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tags"):
            return httpx.Response(
                200,
                json=[{"id": 7, "name": "learn", "workspace_id": WORKSPACE_ID}],
            )
        if request.method == "GET" and request.url.path.endswith("/batch"):
            project = 99 if moved["n"] else 88
            entries = [
                _entry_json(11, tag_ids=[8], project_id=project),
                _entry_json(12, tag_ids=[], project_id=project),
            ]
            return httpx.Response(200, json=entries)
        if request.method == "PATCH" and request.url.path.endswith("/bulk-edit"):
            moved["n"] += 1
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        outcomes = await client.bulk_edit_time_entries(
            [11, 12], add_tags=["learn"], project_id=99
        )

    bulk_calls = [
        json.loads(r.content) for r in requests if str(r.url.path).endswith("/bulk-edit")
    ]
    # Entry 11 keeps tag 8 and gains 7; entry 12 only gains 7 → two distinct groups.
    assert len(bulk_calls) == 2
    by_ids = {tuple(call["ids"]): call["changes"]["tag_ids"] for call in bulk_calls}
    assert by_ids == {(11,): [8, 7], (12,): [7]}
    assert [o.entry_id for o in outcomes] == [11, 12]
    assert all(o.updated for o in outcomes)


@pytest.mark.asyncio
async def test_bulk_edit_removes_tags_down_to_explicit_empty() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tags"):
            return httpx.Response(
                200,
                json=[{"id": 7, "name": "learn", "workspace_id": WORKSPACE_ID}],
            )
        if request.method == "GET" and request.url.path.endswith("/batch"):
            return httpx.Response(200, json=[_entry_json(11, tag_ids=[7], project_id=88)])
        assert request.method == "PATCH" and request.url.path.endswith("/bulk-edit")
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        outcomes = await client.bulk_edit_time_entries([11], remove_tags=["learn"])

    assert outcomes[0].updated is True
    bulk_call = json.loads(requests[-1].content)
    # Explicit empty list is the tri-state way to clear tags upstream.
    assert bulk_call["changes"] == {"tag_ids": []}


@pytest.mark.asyncio
async def test_bulk_edit_reports_missing_entries_and_ignored_moves() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/batch"):
            # Entry 99 is missing (not found/inaccessible); the other keeps the OLD
            # project after the edit: the move is silently ignored upstream.
            return httpx.Response(200, json=[_entry_json(11, tag_ids=[], project_id=88)])
        assert request.method == "PATCH" and request.url.path.endswith("/bulk-edit")
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        outcomes = await client.bulk_edit_time_entries([99, 11], project_id=99)

    assert outcomes[0].updated is False
    assert "not found or not accessible" in (outcomes[0].error or "")
    assert outcomes[1].updated is False
    assert "was not applied" in (outcomes[1].error or "")


@pytest.mark.asyncio
async def test_bulk_edit_surfaces_upstream_bulk_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tags"):
            return httpx.Response(
                200,
                json=[{"id": 7, "name": "learn", "workspace_id": WORKSPACE_ID}],
            )
        if request.method == "GET" and request.url.path.endswith("/batch"):
            return httpx.Response(200, json=[_entry_json(11, tag_ids=[], project_id=88)])
        assert request.method == "PATCH" and request.url.path.endswith("/bulk-edit")
        return httpx.Response(429, json={"error": "rate limited"})

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        outcomes = await client.bulk_edit_time_entries([11], add_tags=["learn"])

    assert outcomes == [
        BulkEditOutcome(entry_id=11, updated=False, error="Toggl request rate limit was exceeded.")
    ]


@pytest.mark.asyncio
async def test_bulk_edit_validates_before_touching_any_entry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tags"):
            return httpx.Response(
                200,
                json=[{"id": 7, "name": "learn", "workspace_id": WORKSPACE_ID}],
            )
        raise AssertionError("no entry request should happen for invalid input")

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        with pytest.raises(ValueError, match="empty"):
            await client.bulk_edit_time_entries([])
        with pytest.raises(ValueError, match="requires"):
            await client.bulk_edit_time_entries([11])
        with pytest.raises(ValueError, match="both added and removed"):
            await client.bulk_edit_time_entries([11], add_tags=["learn"], remove_tags=["learn"])
        with pytest.raises(ValueError, match="Unknown tag"):
            await client.bulk_edit_time_entries([11], add_tags=["nonexistent"])


def _bulk_delete_handler(
    requests: list[httpx.Request],
    store: set[int],
    *,
    apply_deletion: bool = True,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/batch"):
            requested = [int(raw_id) for raw_id in request.url.params["ids"].split(",")]
            return httpx.Response(
                200,
                json=[
                    _entry_json(entry_id, tag_ids=[], project_id=88)
                    for entry_id in requested
                    if entry_id in store
                ],
            )
        assert request.method == "DELETE"
        assert request.url.path.endswith("/time-entries/bulk")
        if apply_deletion:
            store.difference_update(
                int(raw_id) for raw_id in request.url.params["ids"].split(",")
            )
        return httpx.Response(204)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_bulk_delete_confirms_deletions_and_reports_unknown_ids() -> None:
    requests: list[httpx.Request] = []
    store = {11, 12, 13}
    transport = _bulk_delete_handler(requests, store)

    async with TogglClient(config(), transport=transport) as client:
        outcomes = await client.bulk_delete_time_entries([11, 99, 12])

    # 99 is unknown and fails individually; 11 and 12 are confirmed deleted.
    assert outcomes == [
        BulkDeleteOutcome(entry_id=11, deleted=True, error=None),
        BulkDeleteOutcome(
            entry_id=99,
            deleted=False,
            error="Entry not found or not accessible in this workspace.",
        ),
        BulkDeleteOutcome(entry_id=12, deleted=True, error=None),
    ]
    delete_calls = [r for r in requests if r.method == "DELETE"]
    assert [r.url.params["ids"] for r in delete_calls] == ["11,12"]
    assert store == {13}


@pytest.mark.asyncio
async def test_bulk_delete_chunks_ids_across_requests() -> None:
    requests: list[httpx.Request] = []
    store = set(range(1, 102))  # 101 IDs: one full chunk plus one remainder.
    transport = _bulk_delete_handler(requests, store)

    async with TogglClient(config(), transport=transport) as client:
        outcomes = await client.bulk_delete_time_entries(sorted(store))

    delete_calls = [r for r in requests if r.method == "DELETE"]
    assert len(delete_calls) == 2
    assert delete_calls[0].url.params["ids"] == ",".join(str(i) for i in range(1, 101))
    assert delete_calls[1].url.params["ids"] == "101"
    assert all(outcome.deleted for outcome in outcomes)
    assert store == set()


@pytest.mark.asyncio
async def test_bulk_delete_reports_unconfirmed_deletion_when_read_fails() -> None:
    requests: list[httpx.Request] = []
    reads = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/batch"):
            reads["n"] += 1
            if reads["n"] == 1:
                # Pre-read sees the entry; the confirmation read after the delete fails.
                return httpx.Response(200, json=[_entry_json(11, tag_ids=[], project_id=88)])
            return httpx.Response(500, json={"error": "server error"})
        assert request.method == "DELETE"
        return httpx.Response(204)

    async with TogglClient(config(), transport=httpx.MockTransport(handler)) as client:
        outcomes = await client.bulk_delete_time_entries([11])

    assert outcomes[0].deleted is False
    message = outcomes[0].error or ""
    assert "Delete was accepted" in message
    assert "confirmation read failed" in message


@pytest.mark.asyncio
async def test_bulk_delete_reports_entries_upstream_kept() -> None:
    requests: list[httpx.Request] = []
    store = {11}
    transport = _bulk_delete_handler(requests, store, apply_deletion=False)

    async with TogglClient(config(), transport=transport) as client:
        outcomes = await client.bulk_delete_time_entries([11])

    assert outcomes[0].deleted is False
    assert "still exists" in (outcomes[0].error or "")


@pytest.mark.asyncio
async def test_bulk_delete_surfaces_rejected_delete_calls() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/batch"):
            return httpx.Response(200, json=[_entry_json(11, tag_ids=[], project_id=88)])
        assert request.method == "DELETE"
        return httpx.Response(429, json={"error": "rate limited"})

    async with TogglClient(config(), transport=httpx.MockTransport(handler)) as client:
        outcomes = await client.bulk_delete_time_entries([11])

    assert outcomes == [
        BulkDeleteOutcome(
            entry_id=11,
            deleted=False,
            error="Toggl request rate limit was exceeded.",
        )
    ]


@pytest.mark.asyncio
async def test_bulk_delete_validates_before_touching_any_entry() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should happen for invalid input")

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        with pytest.raises(ValueError, match="empty"):
            await client.bulk_delete_time_entries([])
        with pytest.raises(ValueError, match="positive integers"):
            await client.bulk_delete_time_entries([11, 0])


@pytest.mark.asyncio
async def test_get_me_settings_returns_orienting_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/users/me/settings"
        return httpx.Response(
            200,
            json={
                "current_workspace_id": WORKSPACE_ID,
                "date_format": "MM/DD/YYYY",
                "duration_format": "improved",
                "timeofday_format": "H:mm",
                "timezone": "Asia/Shanghai",
                "focus_mode_count_up": False,
            },
        )

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        settings = await client.get_me_settings()

    assert settings.current_workspace_id == WORKSPACE_ID
    assert settings.duration_format == "improved"
    assert settings.timezone == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_list_workspace_members_flattens_workspace_membership() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/organizations/{ORGANIZATION_ID}/users"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "name": "Peter",
                    "email": "peter@example.com",
                    "owner": True,
                    "is_admin": True,
                    "active": True,
                    "joined": True,
                    "workspaces": [{"id": WORKSPACE_ID, "name": "Main"}],
                },
                {
                    "id": 2,
                    "name": "Invitee",
                    "owner": False,
                    "is_admin": False,
                    "active": False,
                    "joined": False,
                    "workspaces": [],
                },
            ],
        )

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        members = await client.list_workspace_members()

    assert members[0].workspace_ids == [WORKSPACE_ID]
    assert members[0].owner is True and members[0].admin is True
    assert members[1].joined is False and members[1].workspace_ids == []
