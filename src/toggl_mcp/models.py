"""Typed, intentionally small models for Toggl objects used by V1."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    pinned: bool = False
    private: bool = False


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
    entry_type: str = Field(default="activity", validation_alias="type")
    running: bool = Field(default=False, exclude=True, repr=False)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_null_tags(cls, value: object) -> object:
        """The API documents `null` for entries without tags; callers receive a stable list."""

        if value is None:
            return []
        if isinstance(value, list):
            return [{"name": item} if isinstance(item, str) else item for item in value]
        return value

    @property
    def is_running(self) -> bool:
        """Return the backend state rather than relying on client-side session state."""

        return self.running


class TimeEntriesResult(BaseModel):
    """Range-query result with an explicit signal for Toggl's 1000-entry cap."""

    model_config = ConfigDict(frozen=True)

    entries: list[TimeEntry]
    count: int
    possibly_truncated: bool
