"""Agent-facing structured output models for the V1 MCP tools."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from toggl_mcp.models import Project, TimeEntry


class ToolOutput(BaseModel):
    """Immutable base model for values returned through MCP structuredContent."""

    model_config = ConfigDict(frozen=True)


class ProjectSummary(ToolOutput):
    """Project fields an agent needs when choosing a `project_id`."""

    id: int = Field(description="Toggl project ID accepted by start_timer.")
    name: str = Field(description="Human-readable project name.")
    active: bool = Field(description="Whether the project is active.")
    description: str | None = Field(default=None, description="Optional project description.")

    @classmethod
    def from_project(cls, project: Project) -> ProjectSummary:
        return cls(
            id=project.id,
            name=project.name,
            active=project.active,
            description=project.description,
        )


class TimeEntrySummary(ToolOutput):
    """Stable subset of a Toggl time entry for agent reasoning."""

    id: int = Field(description="Toggl time-entry ID.")
    description: str | None = Field(description="What was tracked.")
    project_id: int | None = Field(description="Associated project ID, if any.")
    task_id: int | None = Field(description="Associated task ID, if any.")
    start: datetime = Field(description="Timezone-aware timer start timestamp.")
    duration_seconds: int | None = Field(
        description="Recorded duration in seconds; may be absent while a timer is running."
    )
    is_running: bool = Field(description="Whether Toggl currently reports this timer as running.")
    entry_type: str = Field(description="Toggl entry type, normally activity.")

    @classmethod
    def from_time_entry(cls, entry: TimeEntry) -> TimeEntrySummary:
        return cls(
            id=entry.id,
            description=entry.description,
            project_id=entry.project_id,
            task_id=entry.task_id,
            start=entry.start,
            duration_seconds=entry.duration,
            is_running=entry.is_running,
            entry_type=entry.entry_type,
        )


class ListProjectsOutput(ToolOutput):
    """Result of listing projects in the configured workspace."""

    count: int = Field(description="Number of projects returned.")
    projects: list[ProjectSummary]


class CurrentTimerOutput(ToolOutput):
    """Current backend timer state."""

    running: bool = Field(description="True when Toggl has a running timer.")
    timer: TimeEntrySummary | None = Field(description="Running timer details, otherwise null.")


class TimeEntriesOutput(ToolOutput):
    """Time entries whose starts fall in the requested interval."""

    start_date: datetime
    end_date: datetime
    count: int = Field(description="Number of entries returned after internal pagination.")
    entries: list[TimeEntrySummary]


class StartTimerOutput(ToolOutput):
    """Confirmation and backend representation of a newly started timer."""

    started: bool = Field(default=True)
    timer: TimeEntrySummary


class StopTimerOutput(ToolOutput):
    """Idempotent stop result."""

    stopped: bool = Field(description="True only when a running timer was stopped.")
    timer: TimeEntrySummary | None = Field(
        description="Stopped timer details, or null if there was no running timer."
    )
    reason: str | None = Field(
        description="Machine-readable no-op reason; null after a successful stop."
    )
