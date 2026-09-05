"""Asynchronous Toggl 2.0 API client with no MCP dependency."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
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
    BulkDeleteOutcome,
    BulkEditOutcome,
    Client,
    PlannedEntriesResult,
    PlannedTimeEntry,
    Project,
    SearchResults,
    SummaryGroup,
    Tag,
    Task,
    TimeEntriesResult,
    TimeEntry,
    TimeSummary,
    UserSettings,
    WorkspaceMember,
)

ModelT = TypeVar("ModelT", bound=BaseModel)
JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None

SummaryGrouping = Literal["project", "date", "week", "tag"]

# Safety guards so a misbehaving upstream that keeps returning full pages cannot hang the
# client in an infinite pagination loop. With the default page size of 100 this allows
# far more entries than Toggl's documented 1000-entry range cap.
MAX_PAGES = 100

# The report query endpoint paginates its grouped rows the same way (verified: the
# `pagination` body field caps `data_json_row`), so those pages are followed explicitly.
REPORT_PAGE_SIZE = 100
MAX_REPORT_PAGES = 50

# IDs travel in the bulk endpoints' query string, so large batches are chunked to stay
# within URL length limits.
BULK_CHUNK = 100


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
            # The range endpoint also returns planned entries (calendar plans), which
            # carry `planned_start` instead of `start`. They are not tracked time and
            # cannot validate against the entry schema, so they are skipped here; the
            # pagination math keeps counting them because they consume page slots.
            tracked = [
                item
                for item in raw_entries
                if isinstance(item, dict) and item.get("start") is not None
            ]
            entries.extend(self._validate(TimeEntry, item) for item in tracked)
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

    async def list_planned_entries(
        self,
        start_date: datetime,
        end_date: datetime,
    ) -> PlannedEntriesResult:
        """Return planned (calendar) entries whose planned_start is within an aware range.

        Verified against the Focus API: there is no dedicated planned-entries route
        reachable with a workspace token (the documented backoffice route 404s here), so
        planned entries are collected from the same range endpoint as tracked entries.
        Narrow windows filter them by `planned_start`. Entries that also carry tracked
        time (`start`) are excluded — they belong to `get_time_entries`. The range
        envelope reports no total, so `possibly_truncated` is only set when pagination
        hits the safety page limit.
        """

        self._require_aware_datetime(start_date, name="start_date")
        self._require_aware_datetime(end_date, name="end_date")
        if start_date > end_date:
            raise ValueError("start_date must be before or equal to end_date")

        entries: list[PlannedTimeEntry] = []
        page_number = 1
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
            raw_entries, _total = self._extract_page(payload, endpoint="time-entries")
            planned = [
                item
                for item in raw_entries
                if isinstance(item, dict)
                and item.get("start") is None
                and item.get("planned_start") is not None
            ]
            entries.extend(self._validate(PlannedTimeEntry, item) for item in planned)
            if len(raw_entries) < self._config.page_size:
                break
            if page_number >= MAX_PAGES:
                hit_page_guard = True
                break
            page_number += 1

        return PlannedEntriesResult(
            entries=entries,
            count=len(entries),
            possibly_truncated=hit_page_guard,
        )

    async def log_planned_entry(self, entry_id: int) -> TimeEntry:
        """Convert a planned (calendar) entry into a logged entry, in place.

        Verified against the Focus API: `POST /time-entries/{id}/log` with an empty
        payload answers 200 with the same entry, whose `planned_start`/
        `planned_duration` became `start`/`duration`. The logged entry stops
        appearing in `list_planned_entries` and starts appearing in
        `get_time_entries`.
        """

        if entry_id <= 0:
            raise ValueError("entry_id must be a positive integer")
        payload = await self._request_json(
            "POST", f"{self._scope}/time-entries/{entry_id}/log", json={}
        )
        return self._validate(TimeEntry, self._unwrap_data(payload))

    async def summarize_time(
        self,
        start_date: datetime,
        end_date: datetime,
        *,
        group_by: SummaryGrouping = "project",
        project_id: int | None = None,
        user_account_id: int | None = None,
    ) -> TimeSummary:
        """Aggregate tracked time over an aware range via Toggl's native report engine.

        Verified against the Focus API: `POST /reports/workspaces/{wid}/query` groups by
        `project_id`, `start_date`, or `tag_ids` and answers `sum(duration)` in seconds.
        Running entries contribute 0 seconds, planned (calendar) entries are excluded,
        and one tag-grouped row appears per tag id, so tag group sums can exceed the
        total for multi-tag entries — the exact entry total for tag grouping therefore
        comes from one extra per-user query. Project names are resolved via one extra
        projects read (duplicate names disambiguated with IDs), tag names via the tags
        read; a running timer inside the range is reported through `running_count`.

        Optional filters combine with AND and use the upstream `"="` operator (the only
        equality operator it accepts; verified). Tag filtering is deliberately not
        offered: the upstream rejects every workable shape for `tag_ids` filters
        (verified). Weekly grouping is bucketed client-side from the daily rows into
        ISO weeks, because the native `week` grouping reports bare week numbers
        without a year.
        """

        self._require_aware_datetime(start_date, name="start_date")
        self._require_aware_datetime(end_date, name="end_date")
        if start_date > end_date:
            raise ValueError("start_date must be before or equal to end_date")
        filters = self._report_filters(project_id=project_id, user_account_id=user_account_id)

        if group_by == "week":
            rows = await self._query_report(start_date, end_date, "date", filters=filters)
            groups, tracked_seconds, entry_count = self._week_groups(rows)
        else:
            rows = await self._query_report(start_date, end_date, group_by, filters=filters)
            if group_by == "project":
                groups, tracked_seconds, entry_count = await self._project_groups(rows)
            elif group_by == "date":
                groups, tracked_seconds, entry_count = self._date_groups(rows)
            else:
                groups, tracked_seconds, entry_count = await self._tag_groups(
                    start_date, end_date, rows, filters=filters
                )

        running_count = 0
        current = await self.get_current_timer()
        if current is not None and start_date <= current.start <= end_date:
            running_count = 1

        return TimeSummary(
            entry_count=entry_count,
            tracked_seconds=tracked_seconds,
            running_count=running_count,
            possibly_truncated=False,
            groups=groups,
        )

    @staticmethod
    def _report_filters(
        *,
        project_id: int | None,
        user_account_id: int | None,
    ) -> list[dict[str, JsonValue]]:
        """Build the verified `"=" filter list; empty means unfiltered."""

        filters: list[dict[str, JsonValue]] = []
        if project_id is not None:
            if project_id <= 0:
                raise ValueError("project_id must be a positive integer")
            filters.append({"property": "project_id", "operator": "=", "value": project_id})
        if user_account_id is not None:
            if user_account_id <= 0:
                raise ValueError("user_account_id must be a positive integer")
            filters.append(
                {"property": "user_account_id", "operator": "=", "value": user_account_id}
            )
        return filters

    async def _query_report(
        self,
        start_date: datetime,
        end_date: datetime,
        group_by: SummaryGrouping | Literal["user"],
        *,
        filters: list[dict[str, JsonValue]] | None = None,
    ) -> list[dict[str, Any]]:
        """Run one grouped report query and return its raw `data_json_row` rows.

        Grouped rows are paginated (verified: the `pagination` body field caps the
        rows), so pages are followed until a short page; the safety page limit guards
        against a misbehaving upstream.
        """

        property_name = {
            "project": "project_id",
            "date": "start_date",
            "tag": "tag_ids",
            "user": "user_account_id",
        }[group_by]
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            body: dict[str, JsonValue] = {
                "period": {
                    "from": self._format_rfc3339(start_date),
                    "to": self._format_rfc3339(end_date),
                },
                "aggregations": [{"function": "sum", "property": "duration"}],
                "groupings": [{"property": property_name}],
                "pagination": {"page": page, "per_page": REPORT_PAGE_SIZE},
            }
            if filters:
                body["filters"] = filters
            payload = await self._request_json(
                "POST",
                f"/reports/workspaces/{self._config.workspace_id}/query",
                json=body,
            )
            # Verified against the real API: an empty result is `{}` — the data_json_row
            # key is absent entirely rather than an empty array.
            if not isinstance(payload, dict):
                raise TogglResponseFormatError(
                    "Expected a JSON object from the report query endpoint."
                )
            page_rows = payload.get("data_json_row") or []
            if not isinstance(page_rows, list):
                raise TogglResponseFormatError(
                    "Expected a data_json_row array from the report query endpoint."
                )
            rows.extend(row for row in page_rows if isinstance(row, dict))
            if len(page_rows) < REPORT_PAGE_SIZE:
                return rows
            if page >= MAX_REPORT_PAGES:
                raise TogglResponseFormatError(
                    "Report pagination did not terminate within the safety page limit."
                )
            page += 1

    @staticmethod
    def _row_seconds_count(row: dict[str, Any]) -> tuple[int, int]:
        return int(row.get("sum_duration") or 0), int(row.get("count") or 0)

    async def _project_groups(
        self, rows: list[dict[str, Any]]
    ) -> tuple[list[SummaryGroup], int, int]:
        project_ids = {
            row["project_id"]
            for row in rows
            if isinstance(row.get("project_id"), int) and row["project_id"] > 0
        }
        names: dict[int, str] = {}
        if project_ids:
            projects = await self.list_projects()
            name_counts = Counter(project.name for project in projects)
            names = {
                project.id: (
                    f"{project.name} (project {project.id})"
                    if name_counts[project.name] > 1
                    else project.name
                )
                for project in projects
            }

        totals: dict[str, list[int]] = {}
        group_project: dict[str, int] = {}
        tracked_seconds = 0
        entry_count = 0
        for row in rows:
            seconds, count = self._row_seconds_count(row)
            tracked_seconds += seconds
            entry_count += count
            raw_id = row.get("project_id")
            project_id = raw_id if isinstance(raw_id, int) and raw_id > 0 else None
            label = (
                "(no project)"
                if project_id is None
                else names.get(project_id, f"project {project_id}")
            )
            bucket = totals.setdefault(label, [0, 0])
            bucket[0] += seconds
            bucket[1] += count
            if project_id is not None:
                group_project.setdefault(label, project_id)

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
        return groups, tracked_seconds, entry_count

    @staticmethod
    def _date_groups(
        rows: list[dict[str, Any]],
    ) -> tuple[list[SummaryGroup], int, int]:
        totals: dict[str, list[int]] = {}
        tracked_seconds = 0
        entry_count = 0
        for row in rows:
            seconds, count = TogglClient._row_seconds_count(row)
            tracked_seconds += seconds
            entry_count += count
            label = str(row.get("start_date") or "unknown")
            bucket = totals.setdefault(label, [0, 0])
            bucket[0] += seconds
            bucket[1] += count
        ordered = sorted(totals.items(), key=lambda item: (-item[1][0], item[0]))
        groups = [
            SummaryGroup(label=label, seconds=bucket[0], entry_count=bucket[1])
            for label, bucket in ordered
        ]
        return groups, tracked_seconds, entry_count

    @staticmethod
    def _week_groups(
        rows: list[dict[str, Any]],
    ) -> tuple[list[SummaryGroup], int, int]:
        """Bucket daily report rows into ISO weeks, labeled with the ISO year.

        The engine has a native `week` grouping, verified to answer bare week numbers
        without a year, which is ambiguous across year boundaries — weekly totals
        therefore come from the unambiguous daily rows instead.
        """

        totals: dict[str, list[int]] = {}
        tracked_seconds = 0
        entry_count = 0
        for row in rows:
            seconds, count = TogglClient._row_seconds_count(row)
            tracked_seconds += seconds
            entry_count += count
            raw_date = row.get("start_date")
            try:
                iso = date.fromisoformat(str(raw_date)).isocalendar()
                label = f"{iso.year}-W{iso.week:02d}"
            except ValueError:
                label = "unknown"
            bucket = totals.setdefault(label, [0, 0])
            bucket[0] += seconds
            bucket[1] += count
        ordered = sorted(totals.items(), key=lambda item: (-item[1][0], item[0]))
        groups = [
            SummaryGroup(label=label, seconds=bucket[0], entry_count=bucket[1])
            for label, bucket in ordered
        ]
        return groups, tracked_seconds, entry_count

    async def _tag_groups(
        self,
        start_date: datetime,
        end_date: datetime,
        rows: list[dict[str, Any]],
        *,
        filters: list[dict[str, JsonValue]] | None = None,
    ) -> tuple[list[SummaryGroup], int, int]:
        # Tag rows double-count multi-tag entries, so the exact totals come from one
        # extra per-user query: every entry has exactly one owner, so summing those
        # rows counts each entry once. The same filters must apply, or the exact
        # totals would not match the filtered groups.
        user_rows = await self._query_report(start_date, end_date, "user", filters=filters)
        tracked_seconds = sum(self._row_seconds_count(row)[0] for row in user_rows)
        entry_count = sum(self._row_seconds_count(row)[1] for row in user_rows)

        tag_ids = {
            tag_id
            for row in rows
            for tag_id in (row.get("tag_ids") or [])
            if isinstance(tag_id, int)
        }
        tag_names: dict[int, str] = {}
        if tag_ids:
            tag_names = {
                tag.id: tag.name for tag in await self.list_tags() if tag.id is not None
            }

        totals: dict[str, list[int]] = {}
        for row in rows:
            seconds, count = self._row_seconds_count(row)
            row_tag_ids = row.get("tag_ids") or []
            labels = (
                [
                    tag_names.get(tag_id, f"tag {tag_id}")
                    for tag_id in row_tag_ids
                    if isinstance(tag_id, int)
                ]
                or ["untagged"]
            )
            for label in labels:
                bucket = totals.setdefault(label, [0, 0])
                bucket[0] += seconds
                bucket[1] += count
        ordered = sorted(totals.items(), key=lambda item: (-item[1][0], item[0]))
        groups = [
            SummaryGroup(label=label, seconds=bucket[0], entry_count=bucket[1])
            for label, bucket in ordered
        ]
        return groups, tracked_seconds, entry_count

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

    async def continue_timer(self, description: str) -> TimeEntry:
        """Start a timer for a description, restoring the context of recent matches.

        Verified against the Focus API: `POST /tracking/start-from-description` takes
        `{name, extension_source, type}` and starts a timer for the description — a
        fresh description works too, while a description matching recent entries lets
        upstream restore that context. The response is a `{time_entry, task?}`
        envelope; only the started entry is returned. The caller checks for a running
        timer first, like `start_timer`.
        """

        clean_description = description.strip()
        if not clean_description:
            raise ValueError("description must not be empty")

        current = await self.get_current_timer()
        if current is not None:
            raise TimerAlreadyRunningError(current.id)

        payload = await self._request_json(
            "POST",
            f"{self._scope}/tracking/start-from-description",
            json={
                "name": clean_description,
                # Verified against the real API: all three fields are required; the
                # extension source identifies the calling integration.
                "extension_source": "toggl-mcp",
                "type": "activity",
            },
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("time_entry"), dict):
            raise TogglResponseFormatError(
                "Expected a time_entry object from the start-from-description endpoint."
            )
        entry = self._validate(TimeEntry, payload["time_entry"])
        return entry.model_copy(update={"running": True})

    async def get_time_entry(self, entry_id: int) -> TimeEntry:
        """Read one time entry of the configured workspace by ID."""

        if entry_id <= 0:
            raise ValueError("entry_id must be a positive integer")
        payload = await self._request_json("GET", f"{self._scope}/time-entries/{entry_id}")
        return self._validate(TimeEntry, self._unwrap_data(payload))

    async def search(self, keyword: str, *, per_group: int = 5) -> SearchResults:
        """Search the workspace across time entries, tasks, and projects.

        Verified against the Focus API: `GET /search?keyword=` answers three
        suggestion groups. Time-entry hits are deduplicated suggestion rows that
        carry no entry IDs — resolve exact entries with `get_time_entries`
        afterwards. Upstream requires at least 3 characters (also enforced here).
        """

        clean_keyword = keyword.strip()
        if len(clean_keyword) < 3:
            raise ValueError("search keyword must be at least 3 characters")
        if not 1 <= per_group <= 10:
            raise ValueError("per_group must be between 1 and 10")
        payload = await self._request_json(
            "GET",
            f"{self._scope}/search",
            params={"keyword": clean_keyword, "per_group": per_group},
        )
        return self._validate(SearchResults, self._unwrap_data(payload))

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

        Verified against the Focus API: partial updates go through `PATCH`, whose
        payload treats absent fields as untouched and supports `project_id` — unlike
        the PUT route, which silently ignores project changes. The endpoint answers
        with an empty 204, so the entry is re-read afterwards and a project move is
        confirmed against the fresh state. Changing the duration of a still-running
        entry is rejected locally: stop the timer first.
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
        if duration_seconds is not None:
            if duration_seconds <= 0:
                raise ValueError("duration_seconds must be a positive integer")
            current = await self.get_time_entry(entry_id)
            if current.duration is None or current.duration < 0:
                raise ValueError(
                    "This entry is still running; stop the timer before changing its "
                    "duration."
                )

        body: dict[str, JsonValue] = {}
        if description is not None:
            body["description"] = description.strip()
        if project_id is not None:
            body["project_id"] = project_id
        if tags is not None:
            body["tag_ids"] = await self._resolve_tag_ids(tags)
        if start is not None:
            body["start"] = self._format_rfc3339(start)
        if duration_seconds is not None:
            body["duration"] = duration_seconds

        await self._request_ok(
            "PATCH", f"{self._scope}/time-entries/{entry_id}", json=body
        )
        final = await self.get_time_entry(entry_id)
        if project_id is not None and final.project_id != project_id:
            raise ValueError(
                "Upstream did not apply the project move; the target project may be "
                "inactive or unusable for tracking. Other field changes may have applied."
            )
        return final

    async def bulk_edit_time_entries(
        self,
        entry_ids: Sequence[int],
        *,
        add_tags: Sequence[str] | None = None,
        remove_tags: Sequence[str] | None = None,
        project_id: int | None = None,
    ) -> list[BulkEditOutcome]:
        """Apply tag and project changes to many entries through the bulk-edit endpoint.

        Verified against the Focus API: `PATCH /time-entries/bulk-edit` accepts
        `{ids, changes}` where `changes.tag_ids` is tri-state (absent leaves tags
        untouched, a list sets them) and `changes.project_id` moves entries — the
        single-entry PUT route cannot do project moves. The current state of every
        requested entry is read in one batch call, entries are grouped by their
        resulting tag set, and one bulk-edit call is issued per group. The upstream
        answers with an empty 204, so outcomes are derived client-side: entries missing
        from the batch read fail, and a project move is confirmed by re-reading the
        affected entries.
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

        batch = await self._get_entries_by_ids(clean_ids)
        by_id = {entry.id: entry for entry in batch}
        groups: dict[tuple[int, ...] | None, list[int]] = {}
        outcomes: list[BulkEditOutcome] = []
        for entry_id in clean_ids:
            entry = by_id.get(entry_id)
            if entry is None:
                outcomes.append(
                    BulkEditOutcome(
                        entry_id=entry_id,
                        updated=False,
                        error="Entry not found or not accessible in this workspace.",
                    )
                )
                continue
            merged = [t for t in entry.tag_ids if t not in remove_ids]
            for tag_id in add_ids:
                if tag_id not in merged:
                    merged.append(tag_id)
            key = tuple(merged) if (add_tags or remove_tags) else None
            groups.setdefault(key, []).append(entry_id)

        move_note = (
            "the project move was not applied by upstream; the target project may be "
            "inactive or unusable for tracking"
        )
        for key, group_ids in groups.items():
            changes: dict[str, JsonValue] = {}
            if project_id is not None:
                changes["project_id"] = project_id
            if key is not None:
                changes["tag_ids"] = list(key)
            try:
                await self._request_ok(
                    "PATCH",
                    f"{self._scope}/time-entries/bulk-edit",
                    json={"ids": group_ids, "changes": changes},
                )
            except TogglError as exc:
                for entry_id in group_ids:
                    outcomes.append(
                        BulkEditOutcome(entry_id=entry_id, updated=False, error=str(exc))
                    )
                continue
            if project_id is None:
                for entry_id in group_ids:
                    outcomes.append(BulkEditOutcome(entry_id=entry_id, updated=True))
                continue
            after = {entry.id: entry for entry in await self._get_entries_by_ids(group_ids)}
            for entry_id in group_ids:
                final = after.get(entry_id)
                if final is None or final.project_id != project_id:
                    outcomes.append(
                        BulkEditOutcome(entry_id=entry_id, updated=False, error=move_note)
                    )
                else:
                    outcomes.append(BulkEditOutcome(entry_id=entry_id, updated=True))

        order = {entry_id: index for index, entry_id in enumerate(clean_ids)}
        outcomes.sort(key=lambda outcome: order.get(outcome.entry_id, len(order)))
        return outcomes

    async def _get_entries_by_ids(self, entry_ids: Sequence[int]) -> list[TimeEntry]:
        """Read the current state of specific entries in one batch call."""

        payload = await self._request_json(
            "GET",
            f"{self._scope}/time-entries/batch",
            params={"ids": ",".join(str(entry_id) for entry_id in entry_ids)},
        )
        if not isinstance(payload, list):
            raise TogglResponseFormatError("Expected a JSON array from the batch endpoint.")
        return [self._validate(TimeEntry, item) for item in payload]

    async def bulk_delete_time_entries(
        self,
        entry_ids: Sequence[int],
    ) -> list[BulkDeleteOutcome]:
        """Permanently delete many time entries through the bulk endpoint.

        Verified against the Focus API: `DELETE /time-entries/bulk?ids=<csv>` answers an
        empty 204 and the batch endpoint silently omits IDs that no longer exist, so
        every deletion is confirmed by re-reading the requested IDs afterwards. The
        current state is read first so unknown IDs fail individually instead of letting
        one bad ID reject the whole delete request. IDs are deleted in chunks to stay
        within URL length limits; one chunk's failure never blocks the others.
        """

        clean_ids = list(dict.fromkeys(entry_ids))
        if not clean_ids:
            raise ValueError("entry_ids must not be empty")
        if any(entry_id <= 0 for entry_id in clean_ids):
            raise ValueError("entry_ids must all be positive integers")

        missing_note = "Entry not found or not accessible in this workspace."
        outcomes: list[BulkDeleteOutcome] = []
        deletable: list[int] = []
        batch = await self._get_entries_by_ids(clean_ids)
        by_id = {entry.id: entry for entry in batch}
        for entry_id in clean_ids:
            if entry_id in by_id:
                deletable.append(entry_id)
            else:
                outcomes.append(
                    BulkDeleteOutcome(entry_id=entry_id, deleted=False, error=missing_note)
                )

        for chunk_start in range(0, len(deletable), BULK_CHUNK):
            chunk = deletable[chunk_start : chunk_start + BULK_CHUNK]
            try:
                await self._request_ok(
                    "DELETE",
                    f"{self._scope}/time-entries/bulk",
                    params={"ids": ",".join(str(entry_id) for entry_id in chunk)},
                )
            except TogglError as exc:
                for entry_id in chunk:
                    outcomes.append(
                        BulkDeleteOutcome(entry_id=entry_id, deleted=False, error=str(exc))
                    )
                continue
            try:
                survivors = await self._get_entries_by_ids(chunk)
            except TogglNotFoundError:
                # The batch read 404s when none of the IDs exist anymore, which means
                # every deletion in this chunk was applied.
                survivors = []
            except TogglError as exc:
                note = f"Delete was accepted but the confirmation read failed: {exc}"
                for entry_id in chunk:
                    outcomes.append(
                        BulkDeleteOutcome(entry_id=entry_id, deleted=False, error=note)
                    )
                continue
            survivor_ids = {entry.id for entry in survivors}
            for entry_id in chunk:
                if entry_id in survivor_ids:
                    outcomes.append(
                        BulkDeleteOutcome(
                            entry_id=entry_id,
                            deleted=False,
                            error="Upstream accepted the delete but this entry still exists.",
                        )
                    )
                else:
                    outcomes.append(BulkDeleteOutcome(entry_id=entry_id, deleted=True))

        order = {entry_id: index for index, entry_id in enumerate(clean_ids)}
        outcomes.sort(key=lambda outcome: order.get(outcome.entry_id, len(order)))
        return outcomes

    async def get_me_settings(self) -> UserSettings:
        """Read the authenticated user's Focus settings."""

        payload = await self._request_json("GET", "/users/me/settings")
        return self._validate(UserSettings, self._unwrap_data(payload))

    async def list_workspace_members(self) -> list[WorkspaceMember]:
        """List the members of the configured organization with their workspaces."""

        payload = await self._request_json(
            "GET", f"/organizations/{self._config.organization_id}/users"
        )
        if not isinstance(payload, list):
            raise TogglResponseFormatError("Expected a JSON array of organization users.")
        return [self._validate(WorkspaceMember, item) for item in payload]

    async def delete_time_entry(self, entry_id: int) -> None:
        """Permanently delete one time entry of the configured workspace."""

        if entry_id <= 0:
            raise ValueError("entry_id must be a positive integer")
        await self._request_ok("DELETE", f"{self._scope}/time-entries/{entry_id}")

    async def restore_time_entry(self, entry_id: int) -> TimeEntry:
        """Restore a soft-deleted time entry of the configured workspace.

        Verified against the Focus API: single deletes are soft; `PATCH
        /time-entries/{id}/restore` answers an empty 204 and the entry becomes
        readable again. The restored entry is re-read and returned.
        """

        if entry_id <= 0:
            raise ValueError("entry_id must be a positive integer")
        await self._request_ok(
            "PATCH", f"{self._scope}/time-entries/{entry_id}/restore", json={}
        )
        return await self.get_time_entry(entry_id)

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

    async def update_client(self, client_id: int, *, name: str) -> Client:
        """Rename a client of the configured workspace.

        Verified against the Focus API: the route answers only to PUT (PATCH is a
        405), the PUT payload requires `name`, and the answer is an empty 204 — so
        the client is re-read afterwards. Archive state is deliberately not offered:
        the upstream PUT accepts `name` only and silently ignores both `archived`
        and `active` (verified live).
        """

        if client_id <= 0:
            raise ValueError("client_id must be a positive integer")
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("name must not be empty")
        await self._request_ok(
            "PUT", f"{self._ws_scope}/clients/{client_id}", json={"name": clean_name}
        )
        return await self.get_client(client_id)

    async def get_client(self, client_id: int) -> Client:
        """Read one client (customer) of the configured workspace by ID."""

        if client_id <= 0:
            raise ValueError("client_id must be a positive integer")
        payload = await self._request_json("GET", f"{self._ws_scope}/clients/{client_id}")
        return self._validate(Client, self._unwrap_data(payload))

    async def delete_client(self, client_id: int) -> None:
        """Permanently delete a client of the configured workspace."""

        if client_id <= 0:
            raise ValueError("client_id must be a positive integer")
        await self._request_ok("DELETE", f"{self._ws_scope}/clients/{client_id}")

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

    async def update_tag(
        self,
        tag_id: int,
        *,
        name: str | None = None,
        color: str | None = None,
    ) -> Tag:
        """Update a tag; fields left as None stay unchanged.

        Verified against the Focus API: the route answers only to PUT (PATCH is a
        405) and PUT accepts `{name, color}`, answering with the updated tag. The
        current state is read first so omitted fields keep their values.
        """

        if tag_id <= 0:
            raise ValueError("tag_id must be a positive integer")
        if name is None and color is None:
            raise ValueError("update_tag requires at least one field to change")
        if name is not None and not name.strip():
            raise ValueError("name must not be empty when provided")
        current = await self.get_tag(tag_id)
        body: dict[str, JsonValue] = {
            "name": (name or current.name).strip(),
            "color": color if color is not None else current.color,
        }
        payload = await self._request_json(
            "PUT", f"{self._ws_scope}/tags/{tag_id}", json=body
        )
        return self._validate(Tag, self._unwrap_data(payload))

    async def get_tag(self, tag_id: int) -> Tag:
        """Read one tag of the configured workspace by ID."""

        if tag_id <= 0:
            raise ValueError("tag_id must be a positive integer")
        payload = await self._request_json("GET", f"{self._ws_scope}/tags/{tag_id}")
        return self._validate(Tag, self._unwrap_data(payload))

    async def delete_tag(self, tag_id: int) -> None:
        """Permanently delete a tag of the configured workspace."""

        if tag_id <= 0:
            raise ValueError("tag_id must be a positive integer")
        await self._request_ok("DELETE", f"{self._ws_scope}/tags/{tag_id}")

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
        params: Mapping[str, str | int] | None = None,
    ) -> None:
        """Perform a request whose success body carries no useful JSON (e.g. deletes, PUT)."""

        try:
            response = await self._http.request(method, path, json=json, params=params)
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
