"""Agent-facing structured output models for the V1 MCP tools."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from toggl_mcp.models import Client, Project, Tag, Task, TimeEntry


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
    possibly_truncated: bool = Field(
        description=(
            "True when the result may be incomplete: Toggl's reported total exceeded the "
            "fetched entries, or pagination hit its safety page limit. Do not treat count "
            "as a complete summary of the interval when this is true."
        )
    )
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


class ClientSummary(ToolOutput):
    """Toggl client (customer) fields an agent needs for project assignment."""

    id: int = Field(description="Toggl client ID.")
    name: str = Field(description="Human-readable client name.")
    archived: bool = Field(description="Whether the client is archived.")

    @classmethod
    def from_client(cls, client: Client) -> ClientSummary:
        return cls(id=client.id, name=client.name, archived=client.archived)


class TagSummary(ToolOutput):
    """Toggl tag fields an agent needs when tagging time entries."""

    id: int = Field(description="Toggl tag ID.")
    name: str = Field(description="Human-readable tag name.")

    @classmethod
    def from_tag(cls, tag: Tag) -> TagSummary:
        if tag.id is None:
            raise ValueError("Workspace list endpoints always return hydrated tag IDs")
        return cls(id=tag.id, name=tag.name)


class TaskSummary(ToolOutput):
    """Task fields an agent needs when tracking against a project task."""

    id: int = Field(description="Toggl task ID.")
    name: str = Field(description="Human-readable task name.")
    project_id: int = Field(description="Project the task belongs to.")
    active: bool = Field(description="Whether the task is active.")

    @classmethod
    def from_task(cls, task: Task) -> TaskSummary:
        return cls(id=task.id, name=task.name, project_id=task.project_id, active=task.active)


class ListClientsOutput(ToolOutput):
    """Result of listing clients in the configured workspace."""

    count: int = Field(description="Number of clients returned.")
    clients: list[ClientSummary]


class ListTagsOutput(ToolOutput):
    """Result of listing tags in the configured workspace."""

    count: int = Field(description="Number of tags returned.")
    tags: list[TagSummary]


class ListTasksOutput(ToolOutput):
    """Result of listing tasks of one project."""

    count: int = Field(description="Number of tasks returned.")
    tasks: list[TaskSummary]


class CreateTimeEntryOutput(ToolOutput):
    """Confirmation and backend representation of a created time entry."""

    created: bool = Field(default=True)
    time_entry: TimeEntrySummary


class UpdateTimeEntryOutput(ToolOutput):
    """Confirmation and backend representation of an updated time entry."""

    updated: bool = Field(default=True)
    time_entry: TimeEntrySummary


class DeletedEntityOutput(ToolOutput):
    """Confirmation that an entity was deleted."""

    deleted: bool = Field(default=True)
    entity_id: int = Field(description="ID of the deleted entity.")


class CreateProjectOutput(ToolOutput):
    """Confirmation and backend representation of a created project."""

    created: bool = Field(default=True)
    project: ProjectSummary


class UpdateProjectOutput(ToolOutput):
    """Confirmation and backend representation of an updated project."""

    updated: bool = Field(default=True)
    project: ProjectSummary


class BulkEditOutcomeSummary(ToolOutput):
    """Per-entry result of a bulk edit."""

    entry_id: int = Field(description="Time-entry ID this outcome belongs to.")
    updated: bool = Field(description="Whether the edit was applied to this entry.")
    error: str | None = Field(
        description="Stable failure reason for this entry; null when updated is true."
    )


class BulkEditTimeEntriesOutput(ToolOutput):
    """Aggregated per-entry outcomes of one bulk edit."""

    updated_count: int = Field(description="Number of entries successfully edited.")
    failed_count: int = Field(description="Number of entries whose edit failed.")
    outcomes: list[BulkEditOutcomeSummary]


class SummaryGroupOutput(ToolOutput):
    """One aggregation bucket of a time summary."""

    label: str = Field(
        description="Project name, UTC date (YYYY-MM-DD), or tag name for this bucket."
    )
    seconds: int = Field(description="Tracked seconds aggregated under this label.")
    entry_count: int = Field(description="Number of finished entries under this label.")
    project_id: int | None = Field(
        description="Project ID when grouping by project; null for other groupings."
    )


class SummarizeTimeOutput(ToolOutput):
    """Aggregated tracked time for an interval, safe for statistics and reporting."""

    start_date: datetime
    end_date: datetime
    group_by: str = Field(description="Aggregation bucket used for groups.")
    entry_count: int = Field(
        description="All entries in the interval, including still-running ones."
    )
    tracked_seconds: int = Field(
        description=(
            "Sum of finished durations in seconds. Still-running entries are excluded "
            "because their durations are not final."
        )
    )
    running_count: int = Field(
        description="Entries still running; excluded from tracked_seconds."
    )
    possibly_truncated: bool = Field(
        description=(
            "True when the interval may contain more entries than were returned. Treat "
            "every total here as a lower bound and narrow the interval before reporting."
        )
    )
    groups: list[SummaryGroupOutput]
