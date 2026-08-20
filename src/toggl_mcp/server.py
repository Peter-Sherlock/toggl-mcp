"""Official MCP Python SDK server exposing agent-oriented Toggl tools over stdio."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, TypeVar

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
    CurrentTimerOutput,
    ListProjectsOutput,
    ProjectSummary,
    StartTimerOutput,
    StopTimerOutput,
    TimeEntriesOutput,
    TimeEntrySummary,
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
            entries=entries,
        )

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

        @server.tool(
            annotations=start_annotations,
            structured_output=True,
            description=(
                "Start a real Toggl timer with a non-empty description and optional exact "
                "project_id. Fails safely if another timer is already running."
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

    return server


mcp = create_server()


def main() -> None:
    """Run the local server using MCP's stdio transport."""

    mcp.run("stdio")


if __name__ == "__main__":
    main()
