"""Guard against drift between the project-scoped Codex config and the real tool surface."""

from __future__ import annotations

import tomllib
from pathlib import Path

import httpx
import pytest
from mcp.client import Client
from pydantic import SecretStr

from toggl_mcp.config import TogglConfig
from toggl_mcp.server import create_server

CODEX_CONFIG_PATH = Path(__file__).resolve().parents[2] / ".codex" / "config.toml"


def _codex_server_config() -> dict[str, object]:
    assert CODEX_CONFIG_PATH.is_file(), (
        f"Project-scoped Codex configuration is missing: {CODEX_CONFIG_PATH}"
    )
    with CODEX_CONFIG_PATH.open("rb") as config_file:
        document = tomllib.load(config_file)
    server = document["mcp_servers"]["toggl_track"]
    assert isinstance(server, dict)
    return server


def _offline_config() -> TogglConfig:
    return TogglConfig(
        api_key=SecretStr("toggl_sk_config-test"),
        organization_id=321,
        workspace_id=123,
    )


@pytest.mark.asyncio
async def test_codex_enabled_tools_match_registered_tools() -> None:
    server_config = _codex_server_config()
    enabled = server_config["enabled_tools"]
    assert isinstance(enabled, list)

    server = create_server(
        config_loader=_offline_config,
        transport=httpx.MockTransport(lambda _request: httpx.Response(204)),
        enable_write_tools=True,
    )
    async with Client(server) as client:
        listed = await client.list_tools(cache_mode="bypass")

    registered = [tool.name for tool in listed.tools]
    assert sorted(enabled) == sorted(registered), (
        "Codex enabled_tools drifted from the registered MCP tool surface"
    )


def test_codex_write_gate_matches_enabled_write_tools() -> None:
    server_config = _codex_server_config()
    enabled = server_config["enabled_tools"]
    assert isinstance(enabled, list)
    env = server_config.get("env", {})
    assert isinstance(env, dict)

    writes_listed = {"start_timer", "stop_timer"}.issubset(set(enabled))
    gate = env.get("TOGGL_ENABLE_WRITE_TOOLS")
    if writes_listed:
        assert gate == "true", (
            "Codex lists write tools but its TOGGL_ENABLE_WRITE_TOOLS gate is not 'true'; "
            "the server would never register them."
        )
    else:
        assert gate != "true", (
            "Codex enables the write gate but does not list the write tools."
        )
