"""Asynchronous Toggl 2.0 API client with no MCP dependency."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from toggl_mcp.config import TogglConfig
from toggl_mcp.exceptions import (
    TimerAlreadyRunningError,
    TogglAPIError,
    TogglAuthorizationError,
    TogglConflictError,
    TogglNetworkError,
    TogglNotFoundError,
    TogglQuotaError,
    TogglRateLimitError,
    TogglRequestValidationError,
    TogglResponseFormatError,
    TogglServerError,
)
from toggl_mcp.models import Project, TimeEntriesResult, TimeEntry

ModelT = TypeVar("ModelT", bound=BaseModel)
JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None


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
        self._scope = (
            f"/organizations/{config.organization_id}/workspaces/{config.workspace_id}"
        )
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
            raw_projects, total = self._extract_page(payload, endpoint="projects")
            page = [self._validate(Project, item) for item in raw_projects]
            projects.extend(
                project
                for project in page
                if project.workspace_id == self._config.workspace_id
            )

            if len(page) < self._config.page_size or (
                total is not None and len(projects) >= total
            ):
                break
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
        """Return entries whose start time is within an aware RFC3339 range."""

        self._require_aware_datetime(start_date, name="start_date")
        self._require_aware_datetime(end_date, name="end_date")
        if start_date > end_date:
            raise ValueError("start_date must be before or equal to end_date")

        entries: list[TimeEntry] = []
        page_number = 1
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
            validated_entries = [self._validate(TimeEntry, item) for item in raw_entries]
            entries.extend(
                entry.model_copy(update={"running": entry.duration is None})
                for entry in validated_entries
            )
            if len(raw_entries) < self._config.page_size:
                break
            page_number += 1

        return TimeEntriesResult(
            entries=entries,
            count=len(entries),
            possibly_truncated=False,
        )

    async def start_timer(
        self,
        description: str,
        project_id: int | None = None,
    ) -> TimeEntry:
        """Start a timer only when Toggl reports that no timer is already running."""

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
        return self._validate(TimeEntry, self._unwrap_data(payload))

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
