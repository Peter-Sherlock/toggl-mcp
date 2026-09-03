"""Asynchronous Toggl 2.0 API client with no MCP dependency."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from toggl_mcp.config import TogglConfig
from toggl_mcp.exceptions import (
    TimerAlreadyRunningError,
    TogglAPIError,
    TogglAuthorizationError,
    TogglConflictError,
    TogglError,
    TogglNetworkError,
    TogglNotFoundError,
    TogglQuotaError,
    TogglRateLimitError,
    TogglRequestValidationError,
    TogglResponseFormatError,
    TogglServerError,
)
from toggl_mcp.models import (
    BulkEditOutcome,
    Client,
    Project,
    SummaryGroup,
    Tag,
    Task,
    TimeEntriesResult,
    TimeEntry,
    TimeSummary,
)

ModelT = TypeVar("ModelT", bound=BaseModel)
JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None

SummaryGrouping = Literal["project", "date", "tag"]

# Safety guard so a misbehaving upstream that keeps returning full pages cannot hang the
# client in an infinite pagination loop. With the default page size of 100 this allows
# far more entries than Toggl's documented 1000-entry range cap.
MAX_PAGES = 100


def summarize_entries(
    entries: Sequence[TimeEntry],
    *,
    group_by: SummaryGrouping,
    project_names: Mapping[int, str] | None = None,
    possibly_truncated: bool = False,
) -> TimeSummary:
    """Aggregate validated entries; the single source of summary semantics.

    Running entries (null or negative duration) are counted but excluded from all sums
    because their durations are not final. Tag grouping attributes one entry's full
    duration to each of its tags, so group sums can exceed `tracked_seconds`. Dates are
    the UTC calendar dates of entry starts.
    """

    names = project_names or {}
    totals: dict[str, list[int]] = {}
    group_project: dict[str, int | None] = {}

    def record(label: str, seconds: int, project_id: int | None) -> None:
        bucket = totals.setdefault(label, [0, 0])
        bucket[0] += seconds
        bucket[1] += 1
        group_project.setdefault(label, project_id)

    tracked_seconds = 0
    running_count = 0
    for entry in entries:
        if entry.duration is None or entry.duration < 0:
            running_count += 1
            continue
        tracked_seconds += entry.duration
        if group_by == "project":
            project_id = entry.project_id
            if project_id is None:
                record("(no project)", entry.duration, None)
            else:
                record(names.get(project_id, f"project {project_id}"), entry.duration, project_id)
        elif group_by == "date":
            start = entry.start if entry.start.tzinfo is None else entry.start.astimezone(UTC)
            record(start.date().isoformat(), entry.duration, None)
        else:
            if entry.tags:
                for tag in entry.tags:
                    record(tag.name, entry.duration, None)
            else:
                record("untagged", entry.duration, None)

    ordered = sorted(totals.items(), key=lambda item: (-item[1][0], item[0]))
    groups = [
        SummaryGroup(
            label=label,
            seconds=bucket[0],
            entry_count=bucket[1],
            project_id=group_project.get(label),
        )
        for label, bucket in ordered
    ]
    return TimeSummary(
        entry_count=len(entries),
        tracked_seconds=tracked_seconds,
        running_count=running_count,
        possibly_truncated=possibly_truncated,
        groups=groups,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TogglClient:
    """Small V1 client that owns one reusable `httpx.AsyncClient`."""

    def __init__(
        self,
        config: TogglConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._config = config
        self._clock = clock
        # Verified against the real API: projects, time entries, and tracking live under the
        # organization-scoped prefix, while clients, tags, and tasks are only exposed under
        # the plain workspace prefix.
        self._scope = (
            f"/organizations/{config.organization_id}/workspaces/{config.workspace_id}"
        )
        self._ws_scope = f"/workspaces/{config.workspace_id}"
        self._http = httpx.AsyncClient(
            base_url=config.base_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.api_key.get_secret_value()}",
                "User-Agent": "toggl-mcp/0.1.0",
            },
            timeout=httpx.Timeout(config.timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )

    async def __aenter__(self) -> TogglClient:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close pooled network connections."""

        await self._http.aclose()

    async def list_projects(self) -> list[Project]:
        """Return all projects belonging to the configured workspace.

        Toggl's page-number pagination is consumed internally. Projects from other workspaces are
        discarded so callers cannot accidentally mix workspace-scoped IDs.
        """

        projects: list[Project] = []
        page_number = 1
        fetched = 0
        total: int | None = None

        while True:
            payload = await self._request_json(
                "GET",
                f"{self._scope}/projects",
                params={
                    "page": page_number,
                    "per_page": self._config.page_size,
                    "only_me": "true",
                },
            )
            raw_projects, page_total = self._extract_page(payload, endpoint="projects")
            total = page_total if page_total is not None else total
            fetched += len(raw_projects)
            page = [self._validate(Project, item) for item in raw_projects]
            projects.extend(
                project
                for project in page
                if project.workspace_id == self._config.workspace_id
            )

            if len(raw_projects) < self._config.page_size or (
                total is not None and fetched >= total
            ):
                break
            if page_number >= MAX_PAGES:
                raise TogglResponseFormatError(
                    "Project pagination did not terminate within the safety page limit."
                )
            page_number += 1

        return projects

    async def get_current_timer(self) -> TimeEntry | None:
        """Read the running timer from Toggl, returning `None` when no timer exists."""

        try:
            payload = await self._request_json("GET", f"{self._scope}/tracking/current")
        except TogglNotFoundError:
            return None

        value = self._unwrap_data(payload)
        if value is None:
            return None
        return self._validate(TimeEntry, value).model_copy(update={"running": True})

    async def get_time_entries(
        self,
        start_date: datetime,
        end_date: datetime,
    ) -> TimeEntriesResult:
        """Return entries whose start time is within an aware RFC3339 range.

        `possibly_truncated` is True when Toggl's reported total exceeds the fetched
        entries (its range cap can hide the remainder) or when pagination hit the safety
        page limit without exhausting the reported total.
        """

        self._require_aware_datetime(start_date, name="start_date")
        self._require_aware_datetime(end_date, name="end_date")
        if start_date > end_date:
            raise ValueError("start_date must be before or equal to end_date")

        entries: list[TimeEntry] = []
        page_number = 1
        total: int | None = None
        hit_page_guard = False
        while True:
            payload = await self._request_json(
                "GET",
                f"{self._scope}/time-entries",
                params={
                    "date_from": self._format_rfc3339(start_date),
                    "date_to": self._format_rfc3339(end_date),
                    "page": page_number,
                    "per_page": self._config.page_size,
                    "include_taskless": "true",
                },
            )
            raw_entries, page_total = self._extract_page(payload, endpoint="time-entries")
            total = page_total if page_total is not None else total
            entries.extend(self._validate(TimeEntry, item) for item in raw_entries)
            if len(raw_entries) < self._config.page_size or (
                total is not None and len(entries) >= total
            ):
                break
            if page_number >= MAX_PAGES:
                hit_page_guard = True
                break
            page_number += 1

        return TimeEntriesResult(
            entries=entries,
            count=len(entries),
            possibly_truncated=(
                hit_page_guard or (total is not None and total > len(entries))
            ),
        )

    async def summarize_time(
        self,
        start_date: datetime,
        end_date: datetime,
        *,
        group_by: SummaryGrouping = "project",
    ) -> TimeSummary:
        """Aggregate tracked time over an aware range without counting running work.

        Project grouping resolves entry project IDs to names via one extra projects read
        and disambiguates duplicate project names with their IDs. The truncation signal
        of the underlying range query is propagated so callers can treat totals as
        lower bounds.
        """

        result = await self.get_time_entries(start_date, end_date)
        project_names: dict[int, str] | None = None
        if group_by == "project" and any(
            entry.project_id is not None for entry in result.entries
        ):
            projects = await self.list_projects()
            name_counts = Counter(project.name for project in projects)
            project_names = {
                project.id: (
                    f"{project.name} (project {project.id})"
                    if name_counts[project.name] > 1
                    else project.name
                )
                for project in projects
            }
        return summarize_entries(
            result.entries,
            group_by=group_by,
            project_names=project_names,
            possibly_truncated=result.possibly_truncated,
        )

    async def start_timer(
        self,
        description: str,
        project_id: int | None = None,
    ) -> TimeEntry:
        """Start a timer after a best-effort check that no timer is already running.

        The preflight and the start call are separate requests: a timer started in
        another client between them would still be replaced by Toggl's start endpoint.
        """

        clean_description = description.strip()
        if not clean_description:
            raise ValueError("description must not be empty")
        if project_id is not None and project_id <= 0:
            raise ValueError("project_id must be a positive integer")

        current = await self.get_current_timer()
        if current is not None:
            raise TimerAlreadyRunningError(current.id)

        start = self._clock()
        self._require_aware_datetime(start, name="clock result")
        body: dict[str, JsonValue] = {
            "description": clean_description,
            "start": self._format_rfc3339(start),
            "type": "activity",
        }
        if project_id is not None:
            body["project_id"] = project_id

        payload = await self._request_json(
            "POST",
            f"{self._scope}/tracking/start",
            json=body,
        )
        return self._validate(TimeEntry, self._unwrap_data(payload)).model_copy(
            update={"running": True}
        )

    async def stop_timer(self) -> TimeEntry | None:
        """Stop Toggl's current timer, using IDs read from the backend just beforehand."""

        current = await self.get_current_timer()
        if current is None:
            return None

        end = self._clock()
        self._require_aware_datetime(end, name="clock result")
        payload = await self._request_json(
            "POST",
            f"{self._scope}/tracking/stop",
            json={"end": self._format_rfc3339(end)},
        )
        # Stop semantics win even if the stopped entry still reports `duration: null`.
        return self._validate(TimeEntry, self._unwrap_data(payload)).model_copy(
            update={"running": False}
        )

    async def get_time_entry(self, entry_id: int) -> TimeEntry:
        """Read one time entry of the configured workspace by ID."""

        if entry_id <= 0:
            raise ValueError("entry_id must be a positive integer")
        payload = await self._request_json("GET", f"{self._scope}/time-entries/{entry_id}")
        return self._validate(TimeEntry, self._unwrap_data(payload))

    async def create_time_entry(
        self,
        description: str,
        start: datetime,
        duration_seconds: int,
        *,
        project_id: int | None = None,
        tags: list[str] | None = None,
        billable: bool = False,
    ) -> TimeEntry:
        """Create a stopped time entry directly, e.g. to backfill tracked work.

        Tag names are resolved to IDs against the workspace first: the upstream create
        endpoint silently ignores string `tags`, so `tag_ids` is the verified way to
        attach them.
        """

        clean_description = description.strip()
        if not clean_description:
            raise ValueError("description must not be empty")
        self._require_aware_datetime(start, name="start")
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be a positive integer")
        if project_id is not None and project_id <= 0:
            raise ValueError("project_id must be a positive integer")

        body: dict[str, JsonValue] = {
            "description": clean_description,
            "start": self._format_rfc3339(start),
            "duration": duration_seconds,
            "billable": billable,
            "created_with": "toggl-mcp",
            # Verified against the real API: the create endpoint rejects payloads
            # without a `type` field (validated as required upstream).
            "type": "activity",
        }
        if project_id is not None:
            body["project_id"] = project_id
        if tags:
            body["tag_ids"] = await self._resolve_tag_ids(tags)

        payload = await self._request_json("POST", f"{self._scope}/time-entries", json=body)
        return self._validate(TimeEntry, self._unwrap_data(payload))

    async def update_time_entry(
        self,
        entry_id: int,
        *,
        description: str | None = None,
        project_id: int | None = None,
        tags: list[str] | None = None,
        start: datetime | None = None,
        duration_seconds: int | None = None,
    ) -> TimeEntry:
        """Update fields of one time entry; fields left as None stay unchanged.

        The upstream PUT requires `start` and `type`, answers with an empty 204 instead
        of the updated entry, and preserves omitted optional fields. The client therefore
        reads the current entry, merges the requested changes, sends the full verified
        field set, and reads the entry back. Changing the duration of a running entry is
        rejected locally: stop the timer first.
        """

        if entry_id <= 0:
            raise ValueError("entry_id must be a positive integer")
        if (
            description is None
            and project_id is None
            and tags is None
            and start is None
            and duration_seconds is None
        ):
            raise ValueError("update_time_entry requires at least one field to change")
        if description is not None and not description.strip():
            raise ValueError("description must not be empty when provided")
        if project_id is not None and project_id <= 0:
            raise ValueError("project_id must be a positive integer")
        if start is not None:
            self._require_aware_datetime(start, name="start")
        if duration_seconds is not None and duration_seconds <= 0:
            raise ValueError("duration_seconds must be a positive integer")

        current = await self.get_time_entry(entry_id)
        if duration_seconds is not None and (current.duration is None or current.duration < 0):
            raise ValueError(
                "This entry is still running; stop the timer before changing its duration."
            )

        body = self._entry_put_body(
            current,
            description=description.strip() if description is not None else None,
            project_id=project_id,
            start=start,
            duration_seconds=duration_seconds,
            tag_ids=await self._resolve_tag_ids(tags) if tags is not None else None,
        )
        await self._request_ok(
            "PUT", f"{self._scope}/time-entries/{entry_id}", json=body
        )
        final = await self.get_time_entry(entry_id)
        if project_id is not None and final.project_id != project_id:
            # Verified: the reachable PUT route can silently ignore project changes.
            raise ValueError(
                "Upstream did not apply the project move; the target project may be "
                "inactive or unusable for tracking. Other field changes may have applied."
            )
        return final

    @staticmethod
    def _entry_put_body(
        current: TimeEntry,
        *,
        description: str | None = None,
        project_id: int | None = None,
        start: datetime | None = None,
        duration_seconds: int | None = None,
        tag_ids: Sequence[int] | None = None,
    ) -> dict[str, JsonValue]:
        """Build the full verified PUT body for one entry from its current state.

        The upstream PUT requires `start` and `type`, preserves omitted optional fields,
        and ignores absent `tag_ids`, so an explicit (possibly empty) `tag_ids` list is
        the only way to clear tags. A running entry carries no final duration, which is
        simply omitted.
        """

        body: dict[str, JsonValue] = {
            "type": current.entry_type or "activity",
            "billable": current.billable,
            "start": TogglClient._format_rfc3339(
                start if start is not None else current.start
            ),
        }
        final_description = description if description is not None else current.description
        if final_description is not None:
            body["description"] = final_description
        if duration_seconds is not None:
            body["duration"] = duration_seconds
        elif current.duration is not None:
            body["duration"] = current.duration
        body["project_id"] = project_id if project_id is not None else current.project_id
        if tag_ids is not None:
            body["tag_ids"] = list(tag_ids)
        elif current.tag_ids:
            body["tag_ids"] = list(current.tag_ids)
        return body

    async def bulk_edit_time_entries(
        self,
        entry_ids: Sequence[int],
        *,
        add_tags: Sequence[str] | None = None,
        remove_tags: Sequence[str] | None = None,
        project_id: int | None = None,
    ) -> list[BulkEditOutcome]:
        """Apply tag and project changes to many entries, one verified request pair each.

        The upstream bulk PATCH endpoint is not reachable under this base URL (verified),
        so the change set is applied per entry over the single-entry routes. Tag changes
        are applied and trusted; a project move is re-read and confirmed, because the
        upstream PUT silently ignores project changes on the reachable routes (verified
        across field-name variants). One failure never blocks the remaining entries, and
        a silently ignored move is reported as a failure, never as success.
        """

        clean_ids = list(dict.fromkeys(entry_ids))
        if not clean_ids:
            raise ValueError("entry_ids must not be empty")
        if any(entry_id <= 0 for entry_id in clean_ids):
            raise ValueError("entry_ids must all be positive integers")
        if not add_tags and not remove_tags and project_id is None:
            raise ValueError("bulk edit requires add_tags, remove_tags, or project_id")
        overlap = sorted(set(add_tags or ()) & set(remove_tags or ()))
        if overlap:
            raise ValueError(
                "Tag(s) cannot be both added and removed: " + ", ".join(overlap)
            )
        if project_id is not None and project_id <= 0:
            raise ValueError("project_id must be a positive integer")

        add_ids = await self._resolve_tag_ids(add_tags) if add_tags else []
        remove_ids = await self._resolve_tag_ids(remove_tags) if remove_tags else []

        outcomes: list[BulkEditOutcome] = []
        for entry_id in clean_ids:
            outcomes.append(
                await self._bulk_edit_one(
                    entry_id,
                    add_ids=add_ids,
                    remove_ids=remove_ids,
                    project_id=project_id,
                    move_requested=project_id is not None,
                )
            )
        return outcomes

    async def _bulk_edit_one(
        self,
        entry_id: int,
        *,
        add_ids: list[int],
        remove_ids: list[int],
        project_id: int | None,
        move_requested: bool,
    ) -> BulkEditOutcome:
        move_note = (
            "the project move was not applied by upstream; the target project may be "
            "inactive or unusable for tracking"
        )
        try:
            current = await self.get_time_entry(entry_id)
            merged_tag_ids = [t for t in current.tag_ids if t not in remove_ids]
            for tag_id in add_ids:
                if tag_id not in merged_tag_ids:
                    merged_tag_ids.append(tag_id)
            body = self._entry_put_body(
                current, project_id=project_id, tag_ids=merged_tag_ids
            )
            await self._request_ok(
                "PUT", f"{self._scope}/time-entries/{entry_id}", json=body
            )
            if move_requested:
                final = await self.get_time_entry(entry_id)
                if final.project_id != project_id:
                    return BulkEditOutcome(entry_id=entry_id, updated=False, error=move_note)
            return BulkEditOutcome(entry_id=entry_id, updated=True)
        except (TogglError, ValueError) as exc:
            return BulkEditOutcome(entry_id=entry_id, updated=False, error=str(exc))

    async def delete_time_entry(self, entry_id: int) -> None:
        """Permanently delete one time entry of the configured workspace."""

        if entry_id <= 0:
            raise ValueError("entry_id must be a positive integer")
        await self._request_ok("DELETE", f"{self._scope}/time-entries/{entry_id}")

    async def create_project(
        self,
        name: str,
        *,
        active: bool = True,
        client_id: int | None = None,
        color: str | None = None,
        is_private: bool = True,
    ) -> Project:
        """Create a project in the configured workspace."""

        clean_name = name.strip()
        if not clean_name:
            raise ValueError("name must not be empty")
        if client_id is not None and client_id <= 0:
            raise ValueError("client_id must be a positive integer")

        body: dict[str, JsonValue] = {
            "name": clean_name,
            "active": active,
            "is_private": is_private,
            "created_with": "toggl-mcp",
        }
        if client_id is not None:
            body["client_id"] = client_id
        if color is not None:
            body["color"] = color

        payload = await self._request_json("POST", f"{self._scope}/projects", json=body)
        return self._validate(Project, self._unwrap_data(payload))

    async def update_project(
        self,
        project_id: int,
        *,
        name: str | None = None,
        active: bool | None = None,
        client_id: int | None = None,
    ) -> Project:
        """Partially update a project; fields left as None stay unchanged."""

        if project_id <= 0:
            raise ValueError("project_id must be a positive integer")
        body: dict[str, JsonValue] = {}
        if name is not None:
            clean = name.strip()
            if not clean:
                raise ValueError("name must not be empty when provided")
            body["name"] = clean
        if active is not None:
            body["active"] = active
        if client_id is not None:
            if client_id <= 0:
                raise ValueError("client_id must be a positive integer")
            body["client_id"] = client_id
        if not body:
            raise ValueError("update_project requires at least one field to change")

        payload = await self._request_json(
            "PUT", f"{self._scope}/projects/{project_id}", json=body
        )
        return self._validate(Project, self._unwrap_data(payload))

    async def delete_project(self, project_id: int) -> None:
        """Permanently delete a project of the configured workspace."""

        if project_id <= 0:
            raise ValueError("project_id must be a positive integer")
        await self._request_ok("DELETE", f"{self._scope}/projects/{project_id}")

    async def list_clients(self) -> list[Client]:
        """Return all clients (customers) of the configured workspace."""

        return await self._list_workspace_entities(
            "clients", lambda item: self._validate(Client, item)
        )

    async def create_client(self, name: str) -> Client:
        """Create a client (customer) in the configured workspace."""

        clean_name = name.strip()
        if not clean_name:
            raise ValueError("name must not be empty")
        payload = await self._request_json(
            "POST", f"{self._ws_scope}/clients", json={"name": clean_name}
        )
        return self._validate(Client, self._unwrap_data(payload))

    async def list_tags(self) -> list[Tag]:
        """Return all tags of the configured workspace."""

        return await self._list_workspace_entities(
            "tags", lambda item: self._validate(Tag, item)
        )

    async def create_tag(self, name: str) -> Tag:
        """Create a tag in the configured workspace."""

        clean_name = name.strip()
        if not clean_name:
            raise ValueError("name must not be empty")
        payload = await self._request_json(
            "POST", f"{self._ws_scope}/tags", json={"name": clean_name}
        )
        return self._validate(Tag, self._unwrap_data(payload))

    async def list_tasks(self, project_id: int) -> list[Task]:
        """Return all tasks of one project.

        Toggl exposes tasks only on plans with the tasks feature; the API answers 404 on
        plans without it, which callers should surface as "not available", not as a bug.
        """

        if project_id <= 0:
            raise ValueError("project_id must be a positive integer")
        return await self._list_workspace_entities(
            f"tasks of project {project_id}",
            lambda item: self._validate(Task, item),
            path=f"{self._ws_scope}/projects/{project_id}/tasks",
        )

    async def _request_ok(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, JsonValue] | None = None,
    ) -> None:
        """Perform a request whose success body carries no useful JSON (e.g. deletes, PUT)."""

        try:
            response = await self._http.request(method, path, json=json)
        except httpx.TimeoutException as exc:
            raise TogglNetworkError("Timed out while contacting Toggl Track.") from exc
        except httpx.RequestError as exc:
            raise TogglNetworkError("Could not contact Toggl Track.") from exc
        if not response.is_success:
            raise self._api_error(response)

    async def _resolve_tag_ids(self, names: Sequence[str]) -> list[int]:
        """Resolve tag names to workspace tag IDs; tags must already exist.

        Verified against the real API: entry mutations attach tags only through
        `tag_ids`, so name-based input must be resolved here.
        """

        by_name: dict[str, int] = {
            tag.name: tag.id
            for tag in await self.list_tags()
            if tag.id is not None
        }
        missing = sorted({name for name in names if name not in by_name})
        if missing:
            raise ValueError(
                "Unknown tag(s): "
                + ", ".join(missing)
                + ". Create them first with create_tag."
            )
        return list(dict.fromkeys(by_name[name] for name in names))

    async def _list_workspace_entities(
        self,
        endpoint: str,
        validate_item: Callable[[Any], ModelT],
        *,
        path: str | None = None,
    ) -> list[ModelT]:
        """Consume a workspace list endpoint that answers with an envelope or a plain array.

        The real API returns `{data, page, per_page[, total]}` envelopes for these
        endpoints; a plain JSON array is accepted as a complete single response.
        """

        items: list[ModelT] = []
        page_number = 1
        total: int | None = None
        while True:
            payload = await self._request_json(
                "GET",
                path or f"{self._ws_scope}/{endpoint}",
                params={"page": page_number, "per_page": self._config.page_size},
            )
            if isinstance(payload, list):
                raw, total, last_page = payload, None, True
            else:
                raw, total = self._extract_page(payload, endpoint=endpoint)
                last_page = len(raw) < self._config.page_size or (
                    total is not None and len(items) + len(raw) >= total
                )
            items.extend(validate_item(item) for item in raw)
            if last_page:
                break
            if page_number >= MAX_PAGES:
                raise TogglResponseFormatError(
                    f"{endpoint} pagination did not terminate within the safety page limit."
                )
            page_number += 1
        return items

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json: dict[str, JsonValue] | None = None,
    ) -> JsonValue:
        try:
            response = await self._http.request(method, path, params=params, json=json)
        except httpx.TimeoutException as exc:
            raise TogglNetworkError("Timed out while contacting Toggl Track.") from exc
        except httpx.RequestError as exc:
            raise TogglNetworkError("Could not contact Toggl Track.") from exc

        if not response.is_success:
            raise self._api_error(response)
        if not response.content:
            return None

        try:
            value: JsonValue = response.json()
        except ValueError as exc:
            raise TogglResponseFormatError("Toggl returned a non-JSON success response.") from exc
        return value

    def _api_error(self, response: httpx.Response) -> TogglAPIError:
        status = response.status_code
        detail = self._safe_response_detail(response)
        retry_after = self._retry_after_seconds(response)

        if status in (400, 422):
            error_type: type[TogglAPIError] = TogglRequestValidationError
            message = "Toggl rejected the request parameters."
        elif status == 402:
            error_type = TogglQuotaError
            message = "Toggl API quota is exhausted."
        elif status in (401, 403):
            error_type = TogglAuthorizationError
            message = "Toggl authentication failed or access was denied."
        elif status == 404:
            error_type = TogglNotFoundError
            message = "The requested Toggl resource was not found."
        elif status == 409:
            error_type = TogglConflictError
            message = "The Toggl operation conflicts with the current resource state."
        elif status == 429:
            error_type = TogglRateLimitError
            message = "Toggl request rate limit was exceeded."
        elif status >= 500:
            error_type = TogglServerError
            message = "Toggl returned a server error."
        else:
            error_type = TogglAPIError
            message = f"Toggl returned HTTP {status}."

        return error_type(
            message,
            status_code=status,
            detail=detail,
            retry_after_seconds=retry_after,
        )

    def _safe_response_detail(self, response: httpx.Response) -> str | None:
        text = response.text.strip()
        if not text:
            return None
        api_key = self._config.api_key.get_secret_value()
        return text.replace(api_key, "[REDACTED]")[:500]

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> int | None:
        value = response.headers.get("X-Toggl-Quota-Resets-In") or response.headers.get(
            "Retry-After"
        )
        if value is None:
            return None
        try:
            return max(0, int(float(value)))
        except ValueError:
            return None

    @staticmethod
    def _unwrap_data(payload: JsonValue) -> JsonValue:
        if isinstance(payload, dict) and set(payload) == {"data"}:
            value: JsonValue = payload["data"]
            return value
        return payload

    @staticmethod
    def _extract_page(payload: JsonValue, *, endpoint: str) -> tuple[list[Any], int | None]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise TogglResponseFormatError(f"Expected a paginated JSON object from {endpoint}.")
        items: list[Any] = payload["data"]
        total_value = payload.get("total")
        total = total_value if isinstance(total_value, int) else None
        return items, total

    @staticmethod
    def _validate(model: type[ModelT], value: JsonValue) -> ModelT:
        try:
            return model.model_validate(value)
        except ValidationError as exc:
            raise TogglResponseFormatError(
                f"Toggl response did not match the expected {model.__name__} schema."
            ) from exc

    @staticmethod
    def _require_aware_datetime(value: datetime, *, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must include a timezone")

    @staticmethod
    def _format_rfc3339(value: datetime) -> str:
        utc_value = value.astimezone(UTC)
        return utc_value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
