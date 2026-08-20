"""Environment-backed configuration for the Toggl 2.0 API client."""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, SecretStr, ValidationError

from toggl_mcp.exceptions import TogglConfigError

TOGGL_API_BASE_URL = "https://focus.toggl.com/api"


class TogglConfig(BaseModel):
    """Validated client configuration.

    The official base URL is deliberately not environment-configurable. This prevents a typo or
    hostile environment variable from redirecting the bearer credential to another host.
    """

    model_config = ConfigDict(frozen=True)

    api_key: SecretStr
    organization_id: PositiveInt
    workspace_id: PositiveInt
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    page_size: int = Field(default=100, ge=1, le=100)
    base_url: str = TOGGL_API_BASE_URL

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TogglConfig:
        """Load required settings from an environment mapping without reading a `.env` file."""

        values = os.environ if environ is None else environ
        api_key = values.get("TOGGL_API_KEY", values.get("TOGGL_API_TOKEN", "")).strip()

        missing = [
            name
            for name, value in (
                ("TOGGL_API_KEY", api_key),
                ("TOGGL_ORGANIZATION_ID", values.get("TOGGL_ORGANIZATION_ID", "")),
                ("TOGGL_WORKSPACE_ID", values.get("TOGGL_WORKSPACE_ID", "")),
            )
            if not value.strip()
        ]
        if missing:
            joined = ", ".join(missing)
            raise TogglConfigError(f"Missing required environment variable(s): {joined}")

        try:
            return cls.model_validate(
                {
                    "api_key": api_key,
                    "organization_id": values["TOGGL_ORGANIZATION_ID"].strip(),
                    "workspace_id": values["TOGGL_WORKSPACE_ID"].strip(),
                    "timeout_seconds": values.get("TOGGL_TIMEOUT_SECONDS", "10").strip(),
                }
            )
        except ValidationError as exc:
            fields = sorted({str(item["loc"][0]) for item in exc.errors() if item["loc"]})
            detail = ", ".join(fields) or "unknown setting"
            raise TogglConfigError(f"Invalid Toggl configuration field(s): {detail}") from exc
