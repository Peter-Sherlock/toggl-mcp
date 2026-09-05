"""Typed, intentionally small models for Toggl objects used by V1."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Tag(BaseModel):
    """Small hydrated tag representation returned by Toggl 2.0."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int | None = None
    name: str
    color: str | None = None


class Project(BaseModel):
    """Project fields useful to an agent; unrelated billing metadata is ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    name: str
    workspace_id: int
    active: bool = True
    color: str | None = None
    description: str | None = None
    client_id: int | None = None
    pinned: bool = False
    private: bool = False


class Client(BaseModel):
    """Toggl client (customer) as returned by the workspace clients endpoint.

    The Focus API reports client state as `active`; agents reason about the inverse
    `archived` flag, so it is derived here and stays the single source of truth.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    name: str
    archived: bool = False

    @model_validator(mode="before")
    @classmethod
    def derive_archived_from_active(cls, data: object) -> object:
        if isinstance(data, dict) and "archived" not in data and "active" in data:
            data = {**data, "archived": not data["active"]}
        return data


class Task(BaseModel):
    """Task belonging to a project; requires a Toggl plan with tasks enabled."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    name: str
    project_id: int
    active: bool = True


class TimeEntry(BaseModel):
    """Time-entry fields required by the V1 client and future MCP tool results."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    workspace_id: int
    project_id: int | None = None
    task_id: int | None = None
    description: str | None = None
    start: datetime
    duration: int | None = None
    tags: list[Tag] = Field(default_factory=list)
    tag_ids: list[int] = Field(default_factory=list)
    billable: bool = False
    entry_type: str = Field(default="activity", validation_alias="type")
    running: bool | None = Field(default=None, exclude=True, repr=False)

    @field_validator("tags", "tag_ids", mode="before")
    @classmethod
    def normalize_null_lists(cls, value: object) -> object:
        """The API documents `null` for missing tag lists; callers receive stable lists."""

        if value is None:
            return []
        if isinstance(value, list):
            return [{"name": item} if isinstance(item, str) else item for item in value]
        return value

    @property
    def is_running(self) -> bool:
        """One unified rule for every read path.

        Call sites that know the backend state explicitly (current-timer reads, starts,
        stops) set `running`; all other entries derive it from the documented convention
        that a still-running entry reports `duration: null`.
        """

        if self.running is not None:
            return self.running
        return self.duration is None


class TimeEntriesResult(BaseModel):
    """Range-query result with an explicit truncation signal.

    `possibly_truncated` is True when Toggl's reported total exceeds the fetched entries
    (its range cap can hide the remainder) or when pagination hit the client's safety
    page limit.
    """

    model_config = ConfigDict(frozen=True)

    entries: list[TimeEntry]
    count: int
    possibly_truncated: bool


class PlannedTimeEntry(BaseModel):
    """Planned (calendar-scheduled) entry, which does not carry tracked time yet.

    Verified against the real API: planned entries arrive from the same range endpoint
    as tracked entries but carry `planned_start`/`planned_duration` instead of
    `start`/`duration`, and report `null` for missing tag lists.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    workspace_id: int
    project_id: int | None = None
    task_id: int | None = None
    description: str | None = None
    planned_start: datetime
    planned_duration: int | None = None
    entry_type: str = Field(default="activity", validation_alias="type")
    tag_ids: list[int] = Field(default_factory=list)
    billable: bool = False

    @field_validator("tag_ids", mode="before")
    @classmethod
    def normalize_null_lists(cls, value: object) -> object:
        """The API documents `null` for missing tag lists; callers receive stable lists."""

        if value is None:
            return []
        return value


class PlannedEntriesResult(BaseModel):
    """Range-query result for planned entries, with the same truncation signal."""

    model_config = ConfigDict(frozen=True)

    entries: list[PlannedTimeEntry]
    count: int
    possibly_truncated: bool


class SummaryGroup(BaseModel):
    """One aggregation bucket of a time summary."""

    model_config = ConfigDict(frozen=True)

    label: str
    seconds: int
    entry_count: int
    project_id: int | None = None


class BulkEditOutcome(BaseModel):
    """Per-entry result of a bulk edit; one failure never blocks the others."""

    model_config = ConfigDict(frozen=True)

    entry_id: int
    updated: bool
    error: str | None = None


class BulkDeleteOutcome(BaseModel):
    """Per-entry result of a bulk delete; one failure never blocks the others."""

    model_config = ConfigDict(frozen=True)

    entry_id: int
    deleted: bool
    error: str | None = None


class UserSettings(BaseModel):
    """The authenticated user's Focus settings; unrelated UI preferences are ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    current_workspace_id: int | None = None
    date_format: str | None = None
    duration_format: str | None = None
    timeofday_format: str | None = None
    timezone: str | None = None


class WorkspaceMember(BaseModel):
    """One member of the organization, with the workspaces they belong to."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    name: str
    email: str | None = None
    owner: bool = False
    admin: bool = Field(default=False, validation_alias="is_admin")
    active: bool = True
    joined: bool = False
    workspace_ids: list[int] = Field(
        default_factory=list, validation_alias="workspaces"
    )

    @field_validator("workspace_ids", mode="before")
    @classmethod
    def extract_workspace_ids(cls, value: object) -> object:
        """The API returns workspace membership as objects; keep only their IDs."""

        if isinstance(value, list):
            return [
                item.get("id")
                for item in value
                if isinstance(item, dict) and isinstance(item.get("id"), int)
            ]
        return value


class TimeSummary(BaseModel):
    """Aggregated tracked time for a range, safe for agent reasoning.

    `tracked_seconds` excludes still-running entries, whose durations are not final.
    With tag grouping, one entry contributes its duration to every tag it carries, so
    group sums can exceed `tracked_seconds`.
    """

    model_config = ConfigDict(frozen=True)

    entry_count: int
    tracked_seconds: int
    running_count: int
    possibly_truncated: bool
    groups: list[SummaryGroup]


class SearchTimeEntryResult(BaseModel):
    """Suggestion-style search hit for time entries; carries no entry ID.

    Verified against the real API: the search endpoint deduplicates time-entry hits
    into suggestion rows, so exact entries must be resolved through the range
    endpoint afterwards.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    description: str | None = None
    project_id: int | None = None
    project_name: str | None = None
    task_id: int | None = None
    task_name: str | None = None
    client_name: str | None = None
    tag_ids: list[int] = Field(default_factory=list)
    last_tracked_at: datetime | None = None
    matched_terms: int = 0

    @field_validator("tag_ids", mode="before")
    @classmethod
    def normalize_null_lists(cls, value: object) -> object:
        if value is None:
            return []
        return value


class SearchProjectResult(BaseModel):
    """One project hit of a workspace search."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    name: str
    color: str | None = None
    client_name: str | None = None
    matched_terms: int = 0


class SearchTaskResult(BaseModel):
    """One task hit of a workspace search."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    name: str
    project_id: int | None = None
    project_name: str | None = None
    client_name: str | None = None
    matched_terms: int = 0


class SearchResults(BaseModel):
    """The three suggestion groups of a workspace search."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    time_entries: list[SearchTimeEntryResult] = Field(default_factory=list)
    tasks: list[SearchTaskResult] = Field(default_factory=list)
    projects: list[SearchProjectResult] = Field(default_factory=list)
