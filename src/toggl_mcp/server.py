"""Official MCP Python SDK server exposing agent-oriented Toggl tools over stdio."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, TypeVar

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from toggl_mcp.client import TogglClient, _utc_now
from toggl_mcp.config import TogglConfig
from toggl_mcp.exceptions import (
    TimerAlreadyRunningError,
    TogglAuthorizationError,
    TogglConfigError,
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
from toggl_mcp.tool_models import (
    BulkDeleteOutcomeSummary,
    BulkDeleteTimeEntriesOutput,
    BulkEditOutcomeSummary,
    BulkEditTimeEntriesOutput,
    ClientSummary,
    CreateProjectOutput,
    CreateTimeEntryOutput,
    CurrentTimerOutput,
    DeletedEntityOutput,
    ListClientsOutput,
    ListPlannedEntriesOutput,
    ListProjectsOutput,
    ListTagsOutput,
    ListTasksOutput,
    ListWorkspaceMembersOutput,
    LogPlannedEntryOutput,
    MemberSummary,
    MeSettingsOutput,
    PlannedEntrySummary,
    ProjectSummary,
    RestoreTimeEntryOutput,
    SearchOutput,
    SearchProjectSummary,
    SearchTaskSummary,
    SearchTimeEntrySummary,
    StartTimerOutput,
    StopTimerOutput,
    SummarizeTimeOutput,
    SummaryGroupOutput,
    TagSummary,
    TaskSummary,
    TimeEntriesOutput,
    TimeEntrySummary,
    UpdateProjectOutput,
    UpdateTimeEntryOutput,
)

logger = logging.getLogger(__name__)
ResultT = TypeVar("ResultT")
ConfigLoader = Callable[[], TogglConfig]


@dataclass(frozen=True)
class ServerState:
    """Process-lifetime infrastructure, not timer business state."""

    client: TogglClient


def _write_tools_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Fail closed unless the operator explicitly enables side-effecting tools."""

    values = os.environ if environ is None else environ
    raw_value = values.get("TOGGL_ENABLE_WRITE_TOOLS", "false").strip().lower()
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value not in {"", "0", "false", "no", "off"}:
        logger.warning("Invalid TOGGL_ENABLE_WRITE_TOOLS value; write tools remain disabled.")
    return False


def _safe_tool_message(error: Exception) -> str:
    """Translate expected failures without exposing headers, raw bodies, or stack traces."""

    if isinstance(error, TimerAlreadyRunningError):
        return "A timer is already running. Stop it before starting another timer."
    if isinstance(error, TogglAuthorizationError):
        return "Toggl authentication failed or this token cannot access the configured workspace."
    if isinstance(error, TogglQuotaError):
        return "The Toggl account API quota is exhausted. Try again after the quota resets."
    if isinstance(error, TogglRateLimitError):
        retry = error.retry_after_seconds
        suffix = f" Retry after about {retry} seconds." if retry is not None else ""
        return f"Toggl rate-limited this request.{suffix}"
    if isinstance(error, TogglRequestValidationError):
        return "Toggl rejected the request. Check the supplied values and project ID."
    if isinstance(error, TogglNotFoundError):
        return "The requested Toggl resource was not found in the configured workspace."
    if isinstance(error, TogglConflictError):
        return "Toggl rejected the operation because its current state has changed."
    if isinstance(error, TogglNetworkError):
        return "The MCP server could not reach Toggl. Try again shortly."
    if isinstance(error, TogglServerError):
        return "Toggl returned a server error. Try again shortly."
    if isinstance(error, TogglResponseFormatError):
        return "Toggl returned an unexpected response format."
    if isinstance(error, TogglConfigError):
        return "The Toggl MCP server is not configured correctly. Check its environment variables."
    if isinstance(error, ValueError):
        return str(error)
    if isinstance(error, TogglError):
        return "The Toggl operation failed."
    return "The Toggl MCP server encountered an unexpected internal error."


async def _execute(operation_name: str, operation: Callable[[], Awaitable[ResultT]]) -> ResultT:
    try:
        return await operation()
    except (TogglError, ValueError) as error:
        raise ToolError(_safe_tool_message(error)) from None
    except Exception as error:
        logger.exception("Unexpected failure while executing %s", operation_name)
        raise ToolError(_safe_tool_message(error)) from None


def _state(context: Context[ServerState, Any]) -> ServerState:
    return context.request_context.lifespan_context


def create_server(
    *,
    config_loader: ConfigLoader = TogglConfig.from_env,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], datetime] = _utc_now,
    enable_write_tools: bool | None = None,
) -> MCPServer[ServerState]:
    """Build the server; injectable dependencies keep protocol tests offline."""

    @asynccontextmanager
    async def lifespan(_server: MCPServer[ServerState]) -> AsyncIterator[ServerState]:
        async with TogglClient(config_loader(), transport=transport, clock=clock) as client:
            yield ServerState(client=client)

    server = MCPServer[ServerState](
        name="toggl-track",
        title="Toggl Track",
        description="Operate one configured Toggl Track workspace through agent-oriented tools.",
        instructions=(
            "Use list_projects to resolve a project name before start_timer. "
            "Read tools query Toggl directly; write tools may be disabled by the operator."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    read_annotations = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )

    @server.tool(
        annotations=read_annotations,
        structured_output=True,
        description=(
            "List projects in the configured Toggl workspace. Use this to resolve a human "
            "project name to the exact project_id required by start_timer."
        ),
    )
    async def list_projects(context: Context[ServerState, Any]) -> ListProjectsOutput:
        projects = await _execute("list_projects", _state(context).client.list_projects)
        summaries = [ProjectSummary.from_project(project) for project in projects]
        return ListProjectsOutput(count=len(summaries), projects=summaries)

    @server.tool(
        annotations=read_annotations,
        structured_output=True,
        description=(
            "Check Toggl for the timer that is currently running. Returns running=false and "
            "timer=null when there is no active timer."
        ),
    )
    async def get_current_timer(context: Context[ServerState, Any]) -> CurrentTimerOutput:
        timer = await _execute("get_current_timer", _state(context).client.get_current_timer)
        return CurrentTimerOutput(
            running=timer is not None,
            timer=TimeEntrySummary.from_time_entry(timer) if timer is not None else None,
        )

    @server.tool(
        annotations=read_annotations,
        structured_output=True,
        description=(
            "Get all Toggl time entries whose start timestamps fall within an inclusive, "
            "timezone-aware interval. Pagination and workspace IDs are handled internally."
        ),
    )
    async def get_time_entries(
        start_date: Annotated[
            datetime,
            Field(description="Interval start as an ISO 8601 timestamp with timezone."),
        ],
        end_date: Annotated[
            datetime,
            Field(description="Interval end as an ISO 8601 timestamp with timezone."),
        ],
        context: Context[ServerState, Any],
    ) -> TimeEntriesOutput:
        result = await _execute(
            "get_time_entries",
            lambda: _state(context).client.get_time_entries(start_date, end_date),
        )
        entries = [TimeEntrySummary.from_time_entry(entry) for entry in result.entries]
        return TimeEntriesOutput(
            start_date=start_date,
            end_date=end_date,
            count=len(entries),
            possibly_truncated=result.possibly_truncated,
            entries=entries,
        )

    @server.tool(
        annotations=read_annotations,
        structured_output=True,
        description=(
            "Read one time entry by its exact Toggl ID, e.g. before updating or deleting it."
        ),
    )
    async def get_time_entry(
        entry_id: Annotated[int, Field(gt=0, description="Exact Toggl time-entry ID.")],
        context: Context[ServerState, Any],
    ) -> TimeEntrySummary:
        timer = await _execute(
            "get_time_entry",
            lambda: _state(context).client.get_time_entry(entry_id),
        )
        return TimeEntrySummary.from_time_entry(timer)

    @server.tool(
        annotations=read_annotations,
        structured_output=True,
        description=(
            "List planned (calendar-scheduled) entries whose planned start falls within a "
            "timezone-aware interval. Entries that already carry tracked time are excluded; "
            "use get_time_entries for those."
        ),
    )
    async def list_planned_entries(
        start_date: Annotated[
            datetime,
            Field(description="Interval start as an ISO 8601 timestamp with timezone."),
        ],
        end_date: Annotated[
            datetime,
            Field(description="Interval end as an ISO 8601 timestamp with timezone."),
        ],
        context: Context[ServerState, Any],
    ) -> ListPlannedEntriesOutput:
        result = await _execute(
            "list_planned_entries",
            lambda: _state(context).client.list_planned_entries(start_date, end_date),
        )
        entries = [
            PlannedEntrySummary.from_planned_entry(entry) for entry in result.entries
        ]
        return ListPlannedEntriesOutput(
            start_date=start_date,
            end_date=end_date,
            count=len(entries),
            possibly_truncated=result.possibly_truncated,
            entries=entries,
        )

    @server.tool(
        annotations=read_annotations,
        structured_output=True,
        description=(
            "Search the workspace across time entries, tasks, and projects by keyword "
            "(at least 3 characters). Time-entry hits are deduplicated suggestions "
            "without entry IDs; use get_time_entries over the surrounding interval to "
            "resolve exact entry IDs."
        ),
    )
    async def search(
        keyword: Annotated[
            str,
            Field(min_length=3, description="Search term, at least 3 characters."),
        ],
        per_group: Annotated[
            int,
            Field(ge=1, le=10, description="Max results per group."),
        ] = 5,
        *,
        context: Context[ServerState, Any],
    ) -> SearchOutput:
        results = await _execute(
            "search",
            lambda: _state(context).client.search(keyword, per_group=per_group),
        )
        return SearchOutput(
            keyword=keyword.strip(),
            time_entries=[
                SearchTimeEntrySummary(
                    description=hit.description,
                    project_id=hit.project_id,
                    project_name=hit.project_name,
                    task_name=hit.task_name,
                    client_name=hit.client_name,
                    last_tracked_at=hit.last_tracked_at,
                    matched_terms=hit.matched_terms,
                )
                for hit in results.time_entries
            ],
            tasks=[
                SearchTaskSummary(
                    id=hit.id,
                    name=hit.name,
                    project_name=hit.project_name,
                    client_name=hit.client_name,
                    matched_terms=hit.matched_terms,
                )
                for hit in results.tasks
            ],
            projects=[
                SearchProjectSummary(
                    id=hit.id,
                    name=hit.name,
                    color=hit.color,
                    client_name=hit.client_name,
                    matched_terms=hit.matched_terms,
                )
                for hit in results.projects
            ],
        )

    @server.tool(
        annotations=read_annotations,
        structured_output=True,
        description=(
            "List clients (customers) of the configured Toggl workspace. Client IDs are "
            "accepted by create_project."
        ),
    )
    async def list_clients(context: Context[ServerState, Any]) -> ListClientsOutput:
        clients = await _execute("list_clients", _state(context).client.list_clients)
        summaries = [ClientSummary.from_client(client) for client in clients]
        return ListClientsOutput(count=len(summaries), clients=summaries)

    @server.tool(
        annotations=read_annotations,
        structured_output=True,
        description="List tags of the configured Toggl workspace.",
    )
    async def list_tags(context: Context[ServerState, Any]) -> ListTagsOutput:
        tags = await _execute("list_tags", _state(context).client.list_tags)
        summaries = [TagSummary.from_tag(tag) for tag in tags]
        return ListTagsOutput(count=len(summaries), tags=summaries)

    @server.tool(
        annotations=read_annotations,
        structured_output=True,
        description=(
            "List tasks of one project. Toggl only offers tasks on plans with the tasks "
            "feature; on other plans this fails with a not-found error, which is expected."
        ),
    )
    async def list_tasks(
        project_id: Annotated[int, Field(gt=0, description="Exact Toggl project ID.")],
        context: Context[ServerState, Any],
    ) -> ListTasksOutput:
        tasks = await _execute(
            "list_tasks", lambda: _state(context).client.list_tasks(project_id)
        )
        summaries = [TaskSummary.from_task(task) for task in tasks]
        return ListTasksOutput(count=len(summaries), tasks=summaries)

    @server.tool(
        annotations=read_annotations,
        structured_output=True,
        description=(
            "Summarize tracked time in a timezone-aware interval, grouped by project, "
            "UTC date, ISO week, or tag — optionally filtered to one project and/or one "
            "member. Computed by Toggl's report engine; when possibly_truncated is true, "
            "treat every total as a lower bound and narrow the interval before reporting."
        ),
    )
    async def summarize_time(
        start_date: Annotated[
            datetime,
            Field(description="Interval start as an ISO 8601 timestamp with timezone."),
        ],
        end_date: Annotated[
            datetime,
            Field(description="Interval end as an ISO 8601 timestamp with timezone."),
        ],
        group_by: Annotated[
            Literal["project", "date", "week", "tag"],
            Field(description="Aggregation bucket for the returned groups."),
        ] = "project",
        project_id: Annotated[
            int | None,
            Field(gt=0, description="Only count time on this project (from list_projects)."),
        ] = None,
        user_account_id: Annotated[
            int | None,
            Field(
                gt=0,
                description=(
                    "Only count time tracked by this member (organization user ID from "
                    "list_workspace_members)."
                ),
            ),
        ] = None,
        *,
        context: Context[ServerState, Any],
    ) -> SummarizeTimeOutput:
        summary = await _execute(
            "summarize_time",
            lambda: _state(context).client.summarize_time(
                start_date,
                end_date,
                group_by=group_by,
                project_id=project_id,
                user_account_id=user_account_id,
            ),
        )
        return SummarizeTimeOutput(
            start_date=start_date,
            end_date=end_date,
            group_by=group_by,
            entry_count=summary.entry_count,
            tracked_seconds=summary.tracked_seconds,
            running_count=summary.running_count,
            possibly_truncated=summary.possibly_truncated,
            groups=[
                SummaryGroupOutput(
                    label=group.label,
                    seconds=group.seconds,
                    entry_count=group.entry_count,
                    project_id=group.project_id,
                )
                for group in summary.groups
            ],
        )

    @server.tool(
        annotations=read_annotations,
        structured_output=True,
        description=(
            "Read the authenticated Toggl user's settings, including the workspace they "
            "currently have selected. Use it to confirm this server's configured "
            "workspace matches the user's expectation."
        ),
    )
    async def get_me(context: Context[ServerState, Any]) -> MeSettingsOutput:
        settings = await _execute(
            "get_me", _state(context).client.get_me_settings
        )
        return MeSettingsOutput.from_settings(settings)

    @server.tool(
        annotations=read_annotations,
        structured_output=True,
        description=(
            "List the members of this Toggl organization with the workspaces each "
            "member belongs to. Filter by workspace_ids to answer 'who is in my "
            "workspace'."
        ),
    )
    async def list_workspace_members(
        context: Context[ServerState, Any],
    ) -> ListWorkspaceMembersOutput:
        members = await _execute(
            "list_workspace_members", _state(context).client.list_workspace_members
        )
        summaries = [MemberSummary.from_member(member) for member in members]
        return ListWorkspaceMembersOutput(count=len(summaries), members=summaries)

    writes_enabled = _write_tools_enabled() if enable_write_tools is None else enable_write_tools
    if writes_enabled:
        start_annotations = ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        )
        stop_annotations = ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=True,
        )
        create_annotations = ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        )
        update_annotations = ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        )
        delete_annotations = ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        )

        @server.tool(
            annotations=start_annotations,
            structured_output=True,
            description=(
                "Start a real Toggl timer with a non-empty description and optional exact "
                "project_id. Checks for an already-running timer first and fails instead of "
                "replacing it; that check is best-effort, so a timer started in another "
                "client moments earlier can still be replaced."
            ),
        )
        async def start_timer(
            description: Annotated[
                str,
                Field(min_length=1, description="Short description of the work being timed."),
            ],
            project_id: Annotated[
                int | None,
                Field(gt=0, description="Exact Toggl project ID from list_projects, if needed."),
            ] = None,
            *,
            context: Context[ServerState, Any],
        ) -> StartTimerOutput:
            timer = await _execute(
                "start_timer",
                lambda: _state(context).client.start_timer(description, project_id),
            )
            return StartTimerOutput(timer=TimeEntrySummary.from_time_entry(timer))

        @server.tool(
            annotations=start_annotations,
            structured_output=True,
            description=(
                "Start a timer for a description, letting Toggl restore the context "
                "(project, tags) of recent entries with the same description — the "
                "'continue timer' behavior. A brand-new description starts a plain "
                "timer. Checks for an already-running timer first; that check is "
                "best-effort. To control the project explicitly, use start_timer."
            ),
        )
        async def continue_timer(
            description: Annotated[
                str,
                Field(min_length=1, description="Description of the timer to continue."),
            ],
            *,
            context: Context[ServerState, Any],
        ) -> StartTimerOutput:
            timer = await _execute(
                "continue_timer",
                lambda: _state(context).client.continue_timer(description),
            )
            return StartTimerOutput(timer=TimeEntrySummary.from_time_entry(timer))

        @server.tool(
            annotations=stop_annotations,
            structured_output=True,
            description=(
                "Stop the timer currently running in Toggl. This changes real Toggl data. "
                "If no timer is running, returns a successful no-op result."
            ),
        )
        async def stop_timer(context: Context[ServerState, Any]) -> StopTimerOutput:
            timer = await _execute("stop_timer", _state(context).client.stop_timer)
            if timer is None:
                return StopTimerOutput(
                    stopped=False,
                    timer=None,
                    reason="no_running_timer",
                )
            return StopTimerOutput(
                stopped=True,
                timer=TimeEntrySummary.from_time_entry(timer),
                reason=None,
            )

        @server.tool(
            annotations=create_annotations,
            structured_output=True,
            description=(
                "Create a stopped time entry directly (backfill), with an explicit start "
                "timestamp and positive duration. This changes real Toggl data."
            ),
        )
        async def create_time_entry(
            description: Annotated[
                str,
                Field(min_length=1, description="What was tracked."),
            ],
            start: Annotated[
                datetime,
                Field(description="Entry start as an ISO 8601 timestamp with timezone."),
            ],
            duration_seconds: Annotated[
                int, Field(gt=0, description="Tracked duration in seconds.")
            ],
            project_id: Annotated[
                int | None, Field(gt=0, description="Exact Toggl project ID, if any.")
            ] = None,
            tags: Annotated[
                list[str] | None, Field(description="Tag names to attach to the entry.")
            ] = None,
            billable: Annotated[bool, Field(description="Mark the entry as billable.")] = False,
            *,
            context: Context[ServerState, Any],
        ) -> CreateTimeEntryOutput:
            timer = await _execute(
                "create_time_entry",
                lambda: _state(context).client.create_time_entry(
                    description,
                    start,
                    duration_seconds,
                    project_id=project_id,
                    tags=tags,
                    billable=billable,
                ),
            )
            return CreateTimeEntryOutput(
                time_entry=TimeEntrySummary.from_time_entry(timer)
            )

        @server.tool(
            annotations=update_annotations,
            structured_output=True,
            description=(
                "Update fields of one time entry by ID. Omitted fields stay unchanged; "
                "clearing a project or task is not supported. This changes real Toggl data."
            ),
        )
        async def update_time_entry(
            entry_id: Annotated[int, Field(gt=0, description="Exact Toggl time-entry ID.")],
            description: Annotated[str | None, Field(min_length=1)] = None,
            project_id: Annotated[int | None, Field(gt=0)] = None,
            tags: Annotated[list[str] | None, Field()] = None,
            start: Annotated[
                datetime | None,
                Field(description="New entry start as an ISO 8601 timestamp with timezone."),
            ] = None,
            duration_seconds: Annotated[int | None, Field(gt=0)] = None,
            *,
            context: Context[ServerState, Any],
        ) -> UpdateTimeEntryOutput:
            timer = await _execute(
                "update_time_entry",
                lambda: _state(context).client.update_time_entry(
                    entry_id,
                    description=description,
                    project_id=project_id,
                    tags=tags,
                    start=start,
                    duration_seconds=duration_seconds,
                ),
            )
            return UpdateTimeEntryOutput(time_entry=TimeEntrySummary.from_time_entry(timer))

        @server.tool(
            annotations=update_annotations,
            structured_output=True,
            description=(
                "Apply the same tag and project changes to many time entries at once: add "
                "tags by name, remove tags by name, and/or move all entries to one "
                "project. Every entry is edited and reported individually; one failure "
                "never blocks the rest. Tags must already exist (create_tag first). This "
                "changes real Toggl data."
            ),
        )
        async def bulk_edit_time_entries(
            entry_ids: Annotated[
                list[int],
                Field(min_length=1, description="Time-entry IDs to edit, without duplicates."),
            ],
            add_tags: Annotated[
                list[str] | None,
                Field(description="Tag names to attach to every listed entry."),
            ] = None,
            remove_tags: Annotated[
                list[str] | None,
                Field(description="Tag names to detach from every listed entry."),
            ] = None,
            project_id: Annotated[
                int | None,
                Field(gt=0, description="Move every listed entry to this project."),
            ] = None,
            *,
            context: Context[ServerState, Any],
        ) -> BulkEditTimeEntriesOutput:
            outcomes = await _execute(
                "bulk_edit_time_entries",
                lambda: _state(context).client.bulk_edit_time_entries(
                    entry_ids,
                    add_tags=add_tags,
                    remove_tags=remove_tags,
                    project_id=project_id,
                ),
            )
            return BulkEditTimeEntriesOutput(
                updated_count=sum(1 for outcome in outcomes if outcome.updated),
                failed_count=sum(1 for outcome in outcomes if not outcome.updated),
                outcomes=[
                    BulkEditOutcomeSummary(
                        entry_id=outcome.entry_id,
                        updated=outcome.updated,
                        error=outcome.error,
                    )
                    for outcome in outcomes
                ],
            )

        @server.tool(
            annotations=delete_annotations,
            structured_output=True,
            description=(
                "Permanently delete many time entries by ID in one call. Every entry is "
                "reported individually; one failure never blocks the rest. This is "
                "destructive and cannot be undone through Toggl."
            ),
        )
        async def bulk_delete_time_entries(
            entry_ids: Annotated[
                list[int],
                Field(min_length=1, description="Time-entry IDs to delete, without duplicates."),
            ],
            *,
            context: Context[ServerState, Any],
        ) -> BulkDeleteTimeEntriesOutput:
            outcomes = await _execute(
                "bulk_delete_time_entries",
                lambda: _state(context).client.bulk_delete_time_entries(entry_ids),
            )
            return BulkDeleteTimeEntriesOutput(
                deleted_count=sum(1 for outcome in outcomes if outcome.deleted),
                failed_count=sum(1 for outcome in outcomes if not outcome.deleted),
                outcomes=[
                    BulkDeleteOutcomeSummary(
                        entry_id=outcome.entry_id,
                        deleted=outcome.deleted,
                        error=outcome.error,
                    )
                    for outcome in outcomes
                ],
            )

        @server.tool(
            annotations=create_annotations,
            structured_output=True,
            description=(
                "Restore a soft-deleted time entry by ID, returning its current state. "
                "This changes real Toggl data."
            ),
        )
        async def restore_time_entry(
            entry_id: Annotated[int, Field(gt=0, description="Exact Toggl time-entry ID.")],
            *,
            context: Context[ServerState, Any],
        ) -> RestoreTimeEntryOutput:
            timer = await _execute(
                "restore_time_entry",
                lambda: _state(context).client.restore_time_entry(entry_id),
            )
            return RestoreTimeEntryOutput(
                restored=True,
                time_entry=TimeEntrySummary.from_time_entry(timer),
            )

        @server.tool(
            annotations=create_annotations,
            structured_output=True,
            description=(
                "Log a planned (calendar) entry as tracked time: its planned start and "
                "duration become the real start and duration. The entry stops appearing "
                "in list_planned_entries. This changes real Toggl data."
            ),
        )
        async def log_planned_entry(
            entry_id: Annotated[
                int,
                Field(gt=0, description="Exact planned-entry ID from list_planned_entries."),
            ],
            *,
            context: Context[ServerState, Any],
        ) -> LogPlannedEntryOutput:
            timer = await _execute(
                "log_planned_entry",
                lambda: _state(context).client.log_planned_entry(entry_id),
            )
            return LogPlannedEntryOutput(
                logged=True,
                time_entry=TimeEntrySummary.from_time_entry(timer),
            )

        @server.tool(
            annotations=delete_annotations,
            structured_output=True,
            description=(
                "Permanently delete one time entry by ID. This is destructive and cannot be "
                "undone through Toggl."
            ),
        )
        async def delete_time_entry(
            entry_id: Annotated[int, Field(gt=0, description="Exact Toggl time-entry ID.")],
            *,
            context: Context[ServerState, Any],
        ) -> DeletedEntityOutput:
            await _execute(
                "delete_time_entry",
                lambda: _state(context).client.delete_time_entry(entry_id),
            )
            return DeletedEntityOutput(deleted=True, entity_id=entry_id)

        @server.tool(
            annotations=create_annotations,
            structured_output=True,
            description=(
                "Create a project in the configured Toggl workspace. Use list_clients to "
                "resolve a client_id. This changes real Toggl data."
            ),
        )
        async def create_project(
            name: Annotated[str, Field(min_length=1, description="New project name.")],
            active: Annotated[bool, Field(description="Whether the project is active.")] = True,
            client_id: Annotated[int | None, Field(gt=0)] = None,
            color: Annotated[str | None, Field(description="Hex color for the project.")] = None,
            is_private: Annotated[bool, Field(description="Restrict the project.")] = True,
            *,
            context: Context[ServerState, Any],
        ) -> CreateProjectOutput:
            project = await _execute(
                "create_project",
                lambda: _state(context).client.create_project(
                    name,
                    active=active,
                    client_id=client_id,
                    color=color,
                    is_private=is_private,
                ),
            )
            return CreateProjectOutput(project=ProjectSummary.from_project(project))

        @server.tool(
            annotations=update_annotations,
            structured_output=True,
            description=(
                "Update a project by ID. Omitted fields stay unchanged. This changes real "
                "Toggl data."
            ),
        )
        async def update_project(
            project_id: Annotated[int, Field(gt=0, description="Exact Toggl project ID.")],
            name: Annotated[str | None, Field(min_length=1)] = None,
            active: Annotated[bool | None, Field()] = None,
            client_id: Annotated[int | None, Field(gt=0)] = None,
            *,
            context: Context[ServerState, Any],
        ) -> UpdateProjectOutput:
            project = await _execute(
                "update_project",
                lambda: _state(context).client.update_project(
                    project_id, name=name, active=active, client_id=client_id
                ),
            )
            return UpdateProjectOutput(project=ProjectSummary.from_project(project))

        @server.tool(
            annotations=delete_annotations,
            structured_output=True,
            description=(
                "Permanently delete a project by ID. Time entries keep existing but lose "
                "their project assignment. This is destructive."
            ),
        )
        async def delete_project(
            project_id: Annotated[int, Field(gt=0, description="Exact Toggl project ID.")],
            *,
            context: Context[ServerState, Any],
        ) -> DeletedEntityOutput:
            await _execute(
                "delete_project",
                lambda: _state(context).client.delete_project(project_id),
            )
            return DeletedEntityOutput(deleted=True, entity_id=project_id)

        @server.tool(
            annotations=create_annotations,
            structured_output=True,
            description=(
                "Create a client (customer) in the configured Toggl workspace. This changes "
                "real Toggl data."
            ),
        )
        async def create_client(
            name: Annotated[str, Field(min_length=1, description="New client name.")],
            *,
            context: Context[ServerState, Any],
        ) -> ClientSummary:
            client = await _execute(
                "create_client",
                lambda: _state(context).client.create_client(name),
            )
            return ClientSummary.from_client(client)

        @server.tool(
            annotations=update_annotations,
            structured_output=True,
            description=(
                "Rename a client by ID. The upstream route offers renaming only — "
                "archive state changes are silently ignored by Toggl, so they are not "
                "offered. This changes real Toggl data."
            ),
        )
        async def update_client(
            client_id: Annotated[int, Field(gt=0, description="Exact Toggl client ID.")],
            name: Annotated[str, Field(min_length=1, description="New client name.")],
            *,
            context: Context[ServerState, Any],
        ) -> ClientSummary:
            client = await _execute(
                "update_client",
                lambda: _state(context).client.update_client(client_id, name=name),
            )
            return ClientSummary.from_client(client)

        @server.tool(
            annotations=delete_annotations,
            structured_output=True,
            description=(
                "Permanently delete a client by ID. This is destructive."
            ),
        )
        async def delete_client(
            client_id: Annotated[int, Field(gt=0, description="Exact Toggl client ID.")],
            *,
            context: Context[ServerState, Any],
        ) -> DeletedEntityOutput:
            await _execute(
                "delete_client",
                lambda: _state(context).client.delete_client(client_id),
            )
            return DeletedEntityOutput(deleted=True, entity_id=client_id)

        @server.tool(
            annotations=create_annotations,
            structured_output=True,
            description=(
                "Create a tag in the configured Toggl workspace. This changes real Toggl data."
            ),
        )
        async def create_tag(
            name: Annotated[str, Field(min_length=1, description="New tag name.")],
            *,
            context: Context[ServerState, Any],
        ) -> TagSummary:
            tag = await _execute(
                "create_tag",
                lambda: _state(context).client.create_tag(name),
            )
            return TagSummary.from_tag(tag)

        @server.tool(
            annotations=update_annotations,
            structured_output=True,
            description=(
                "Update a tag by ID: rename it or recolor it. Omitted fields stay "
                "unchanged. This changes real Toggl data."
            ),
        )
        async def update_tag(
            tag_id: Annotated[int, Field(gt=0, description="Exact Toggl tag ID.")],
            name: Annotated[str | None, Field(min_length=1)] = None,
            color: Annotated[
                str | None, Field(description="Hex color for the tag.")
            ] = None,
            *,
            context: Context[ServerState, Any],
        ) -> TagSummary:
            tag = await _execute(
                "update_tag",
                lambda: _state(context).client.update_tag(tag_id, name=name, color=color),
            )
            return TagSummary.from_tag(tag)

        @server.tool(
            annotations=delete_annotations,
            structured_output=True,
            description=(
                "Permanently delete a tag by ID. Entries keep existing but lose this "
                "tag. This is destructive."
            ),
        )
        async def delete_tag(
            tag_id: Annotated[int, Field(gt=0, description="Exact Toggl tag ID.")],
            *,
            context: Context[ServerState, Any],
        ) -> DeletedEntityOutput:
            await _execute(
                "delete_tag",
                lambda: _state(context).client.delete_tag(tag_id),
            )
            return DeletedEntityOutput(deleted=True, entity_id=tag_id)

    return server


mcp = create_server()


def main() -> None:
    """Run the local server using MCP's stdio transport."""

    mcp.run("stdio")


if __name__ == "__main__":
    main()
