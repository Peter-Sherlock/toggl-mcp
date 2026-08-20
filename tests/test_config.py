from __future__ import annotations

import pytest

from toggl_mcp.config import TogglConfig
from toggl_mcp.exceptions import TogglConfigError


def test_config_loads_environment_without_exposing_token() -> None:
    config = TogglConfig.from_env(
        {
            "TOGGL_API_KEY": "toggl_sk_super-secret-key",
            "TOGGL_ORGANIZATION_ID": "321",
            "TOGGL_WORKSPACE_ID": "123",
        }
    )

    assert config.organization_id == 321
    assert config.workspace_id == 123
    assert config.api_key.get_secret_value() == "toggl_sk_super-secret-key"
    assert "toggl_sk_super-secret-key" not in repr(config)


def test_config_reports_missing_names_without_values() -> None:
    with pytest.raises(
        TogglConfigError,
        match="TOGGL_API_KEY, TOGGL_ORGANIZATION_ID, TOGGL_WORKSPACE_ID",
    ):
        TogglConfig.from_env({})


def test_config_rejects_invalid_workspace() -> None:
    with pytest.raises(TogglConfigError, match="workspace_id"):
        TogglConfig.from_env(
            {
                "TOGGL_API_KEY": "toggl_sk_key",
                "TOGGL_ORGANIZATION_ID": "321",
                "TOGGL_WORKSPACE_ID": "0",
            }
        )


def test_config_accepts_legacy_api_token_environment_name() -> None:
    config = TogglConfig.from_env(
        {
            "TOGGL_API_TOKEN": "toggl_sk_legacy-name",
            "TOGGL_ORGANIZATION_ID": "321",
            "TOGGL_WORKSPACE_ID": "123",
        }
    )

    assert config.api_key.get_secret_value() == "toggl_sk_legacy-name"
