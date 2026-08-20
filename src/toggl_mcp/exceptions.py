"""Stable exception types for Toggl configuration, transport, and API failures."""

from __future__ import annotations


class TogglError(Exception):
    """Base class for all errors intentionally exposed by the client layer."""


class TogglConfigError(TogglError):
    """Environment configuration is missing or invalid."""


class TogglNetworkError(TogglError):
    """The HTTPS request failed before a usable response was received."""


class TogglResponseFormatError(TogglError):
    """Toggl returned a successful response that did not match the documented schema."""


class TogglAPIError(TogglError):
    """Base class for non-success HTTP responses from Toggl."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        detail: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds


class TogglRequestValidationError(TogglAPIError):
    """Toggl rejected the request parameters or body."""


class TogglAuthorizationError(TogglAPIError):
    """Authentication failed or the token cannot access the requested resource."""


class TogglNotFoundError(TogglAPIError):
    """The requested Toggl resource does not exist."""


class TogglConflictError(TogglAPIError):
    """The requested change conflicts with the current Toggl state."""


class TogglQuotaError(TogglAPIError):
    """The account's sliding-window API quota is exhausted (HTTP 402)."""


class TogglRateLimitError(TogglAPIError):
    """The per-token/IP request rate was exceeded (HTTP 429)."""


class TogglServerError(TogglAPIError):
    """Toggl returned a 5xx server error."""


class TimerAlreadyRunningError(TogglConflictError):
    """A safe local preflight found an existing running timer."""

    def __init__(self, current_entry_id: int) -> None:
        super().__init__(
            f"A timer is already running (time entry {current_entry_id}). Stop it first.",
            status_code=409,
        )
        self.current_entry_id = current_entry_id

