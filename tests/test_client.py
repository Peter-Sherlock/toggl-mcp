from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from toggl_mcp.client import TogglClient, summarize_entries
from toggl_mcp.config import TogglConfig
from toggl_mcp.exceptions import (
    TimerAlreadyRunningError,
    TogglAPIError,
    TogglAuthorizationError,
    TogglRateLimitError,
    TogglRequestValidationError,
)
from toggl_mcp.models import BulkEditOutcome, TimeEntry

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
async def test_update_time_entry_merges_into_full_put_and_rereads() -> None:
    requests: list[httpx.Request] = []
    reads = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            reads["n"] += 1
            description = "Old work" if reads["n"] == 1 else "Renamed work"
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
        assert request.method == "PUT"
        # The verified upstream PUT answers 204 with an empty body.
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        entry = await client.update_time_entry(42, description="Renamed work")

    assert json.loads(requests[1].content) == {
        "type": "activity",
        "billable": True,
        "start": "2026-08-15T09:00:00.000Z",
        "description": "Renamed work",
        "duration": 3600,
        "project_id": 88,
        "tag_ids": [7],
    }
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
        raise AssertionError("PUT must not happen for a running entry")

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

    put_request = next(call for call in calls if call.method == "PUT")
    put_body = json.loads(put_request.content)
    assert put_body["tag_ids"] == [7]


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


def _entry(**overrides: object) -> TimeEntry:
    fields: dict[str, object] = {
        "id": 1,
        "workspace_id": WORKSPACE_ID,
        "start": datetime(2026, 8, 15, 18, 0, tzinfo=UTC),
        "duration": 3600,
    }
    fields.update(overrides)
    return TimeEntry(**fields)  # type: ignore[arg-type]


def test_summarize_entries_groups_by_project_and_excludes_running() -> None:
    entries = [
        _entry(id=1, duration=3600, project_id=1),
        _entry(id=2, duration=1800, project_id=1),
        _entry(id=3, duration=None, project_id=2),
        _entry(id=4, duration=-3600, project_id=2),
        _entry(id=5, duration=60),
    ]

    summary = summarize_entries(entries, group_by="project", project_names={1: "Alpha"})

    assert summary.entry_count == 5
    assert summary.tracked_seconds == 5460
    assert summary.running_count == 2
    assert [(g.label, g.seconds, g.entry_count) for g in summary.groups] == [
        ("Alpha", 5400, 2),
        ("(no project)", 60, 1),
    ]
    assert summary.groups[0].project_id == 1
    assert summary.groups[1].project_id is None


def test_summarize_entries_unknown_projects_fall_back_to_id_labels() -> None:
    summary = summarize_entries([_entry(duration=120, project_id=77)], group_by="project")

    assert summary.groups[0].label == "project 77"
    assert summary.groups[0].project_id == 77


def test_summarize_entries_by_date_uses_utc_dates_and_descending_seconds() -> None:
    entries = [
        _entry(id=1, duration=600, start=datetime(2026, 8, 15, 23, 30, tzinfo=UTC)),
        _entry(id=2, duration=3600, start=datetime(2026, 8, 14, 22, 0, tzinfo=UTC)),
    ]

    summary = summarize_entries(entries, group_by="date")

    assert [(g.label, g.seconds) for g in summary.groups] == [
        ("2026-08-14", 3600),
        ("2026-08-15", 600),
    ]


def test_summarize_entries_tag_grouping_counts_entry_under_each_tag() -> None:
    entries = [
        _entry(id=1, duration=100, tags=[{"name": "a"}, {"name": "b"}]),
        _entry(id=2, duration=50),
    ]

    summary = summarize_entries(entries, group_by="tag")

    assert summary.tracked_seconds == 150
    assert [(g.label, g.seconds) for g in summary.groups] == [
        ("a", 100),
        ("b", 100),
        ("untagged", 50),
    ]


@pytest.mark.asyncio
async def test_summarize_time_resolves_names_and_propagates_truncation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
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
        assert request.url.path.endswith("/time-entries")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": 1,
                        "workspace_id": WORKSPACE_ID,
                        "project_id": 88,
                        "description": "work",
                        "start": "2026-08-15T09:00:00Z",
                        "duration": 3600,
                        "type": "activity",
                    }
                ],
                "total": 6,
            },
        )

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        summary = await client.summarize_time(
            datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)
        )

    assert summary.possibly_truncated is True
    assert summary.tracked_seconds == 3600
    assert summary.groups[0].label == "Agent Learning"
    assert len(requests) == 2  # one range query plus one projects read for names


@pytest.mark.asyncio
async def test_summarize_time_skips_project_lookup_without_project_ids() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path.endswith("/time-entries")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": 1,
                        "workspace_id": WORKSPACE_ID,
                        "description": "work",
                        "start": "2026-08-15T09:00:00Z",
                        "duration": 60,
                        "type": "activity",
                    }
                ],
                "total": 1,
            },
        )

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        summary = await client.summarize_time(
            datetime(2026, 8, 15, tzinfo=UTC), datetime(2026, 8, 16, tzinfo=UTC)
        )

    assert len(requests) == 1
    assert summary.groups[0].label == "(no project)"


@pytest.mark.asyncio
async def test_summarize_time_disambiguates_duplicate_project_names() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/projects"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": 1, "name": "Same", "workspace_id": WORKSPACE_ID},
                        {"id": 2, "name": "Same", "workspace_id": WORKSPACE_ID},
                    ],
                    "total": 2,
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": 1,
                        "workspace_id": WORKSPACE_ID,
                        "project_id": 1,
                        "description": "work",
                        "start": "2026-08-15T09:00:00Z",
                        "duration": 30,
                        "type": "activity",
                    },
                    {
                        "id": 2,
                        "workspace_id": WORKSPACE_ID,
                        "project_id": 2,
                        "description": "work",
                        "start": "2026-08-15T09:00:00Z",
                        "duration": 70,
                        "type": "activity",
                    },
                ],
                "total": 2,
            },
        )

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        summary = await client.summarize_time(
            datetime(2026, 8, 15, tzinfo=UTC), datetime(2026, 8, 16, tzinfo=UTC)
        )

    labels = [group.label for group in summary.groups]
    assert labels == ["Same (project 2)", "Same (project 1)"]
    assert [group.project_id for group in summary.groups] == [2, 1]


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
async def test_bulk_edit_adds_tags_and_moves_projects_per_entry() -> None:
    requests: list[httpx.Request] = []
    moved: set[int] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tags"):
            return httpx.Response(
                200,
                json=[{"id": 7, "name": "learn", "workspace_id": WORKSPACE_ID}],
            )
        entry_id = int(request.url.path.rsplit("/", 1)[-1])
        if request.method == "GET":
            existing = [8] if entry_id == 11 else []
            project = 99 if entry_id in moved else 88
            body = _entry_json(entry_id, tag_ids=existing, project_id=project)
            return httpx.Response(200, json=body)
        assert request.method == "PUT"
        moved.add(entry_id)
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        outcomes = await client.bulk_edit_time_entries(
            [11, 12], add_tags=["learn"], project_id=99
        )

    assert outcomes == [
        BulkEditOutcome(entry_id=11, updated=True, error=None),
        BulkEditOutcome(entry_id=12, updated=True, error=None),
    ]
    put_bodies = [json.loads(r.content) for r in requests if r.method == "PUT"]
    # entry 11 keeps its pre-existing tag 8 and gains 7; entry 12 gains 7.
    assert put_bodies[0]["tag_ids"] == [8, 7]
    assert put_bodies[1]["tag_ids"] == [7]
    assert all(body["project_id"] == 99 for body in put_bodies)


@pytest.mark.asyncio
async def test_bulk_edit_reports_silently_ignored_project_moves() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        entry_id = int(request.url.path.rsplit("/", 1)[-1])
        if request.method == "GET":
            # Upstream always reports the OLD project: the move is silently ignored.
            return httpx.Response(200, json=_entry_json(entry_id, tag_ids=[], project_id=88))
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        outcomes = await client.bulk_edit_time_entries([11, 12], project_id=99)

    assert all(outcome.updated is False for outcome in outcomes)
    assert all("was not applied" in (outcome.error or "") for outcome in outcomes)


@pytest.mark.asyncio
async def test_bulk_edit_removes_tags_down_to_empty() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tags"):
            return httpx.Response(
                200,
                json=[{"id": 7, "name": "learn", "workspace_id": WORKSPACE_ID}],
            )
        if request.method == "GET":
            return httpx.Response(200, json=_entry_json(11, tag_ids=[7], project_id=88))
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        outcomes = await client.bulk_edit_time_entries([11], remove_tags=["learn"])

    assert outcomes[0].updated is True
    put_body = json.loads(requests[-1].content)
    # Explicit empty list is the only verified way to clear tags upstream.
    assert put_body["tag_ids"] == []


@pytest.mark.asyncio
async def test_bulk_edit_reports_per_entry_failures_without_blocking() -> None:
    requests: list[httpx.Request] = []

    moved: set[int] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        entry_id = int(request.url.path.rsplit("/", 1)[-1])
        if request.method == "GET":
            if entry_id == 99:
                return httpx.Response(404, json={"error": "gone"})
            project = 99 if entry_id in moved else 88
            return httpx.Response(200, json=_entry_json(entry_id, tag_ids=[], project_id=project))
        moved.add(entry_id)
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        outcomes = await client.bulk_edit_time_entries([99, 11], project_id=99)

    assert outcomes[0].updated is False
    assert outcomes[0].error is not None
    assert outcomes[1].updated is True


@pytest.mark.asyncio
async def test_update_time_entry_raises_when_move_is_silently_ignored() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            # Always the old project: the move is silently ignored upstream.
            return httpx.Response(200, json=_entry_json(42, tag_ids=[], project_id=88))
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    async with TogglClient(config(), transport=transport) as client:
        with pytest.raises(ValueError, match="did not apply"):
            await client.update_time_entry(42, project_id=99)


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
