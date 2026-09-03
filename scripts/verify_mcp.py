"""Verify the real stdio MCP boundary without printing Toggl credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp_types import CallToolResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]
READ_TOOL_NAMES = [
    "list_projects",
    "get_current_timer",
    "get_time_entries",
    "get_time_entry",
    "list_clients",
    "list_tags",
    "list_tasks",
    "summarize_time",
]
WRITE_TOOL_NAMES = [
    "start_timer",
    "stop_timer",
    "create_time_entry",
    "update_time_entry",
    "delete_time_entry",
    "create_project",
    "update_project",
    "delete_project",
    "create_client",
    "create_tag",
]


def _server_environment(*, enable_writes: bool) -> dict[str, str]:
    api_key = os.environ.get("TOGGL_API_KEY") or os.environ.get("TOGGL_API_TOKEN")
    required = {
        "TOGGL_API_KEY": api_key,
        "TOGGL_ORGANIZATION_ID": os.environ.get("TOGGL_ORGANIZATION_ID"),
        "TOGGL_WORKSPACE_ID": os.environ.get("TOGGL_WORKSPACE_ID"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")

    environment = {name: value for name, value in required.items() if value is not None}
    environment["TOGGL_ENABLE_WRITE_TOOLS"] = "true" if enable_writes else "false"
    timeout = os.environ.get("TOGGL_TIMEOUT_SECONDS")
    if timeout:
        environment["TOGGL_TIMEOUT_SECONDS"] = timeout
    return environment


def _structured_result(tool_name: str, result: CallToolResult) -> Any:
    if result.is_error:
        message = " ".join(getattr(item, "text", "") for item in result.content)
        raise RuntimeError(f"{tool_name} failed: {message}")
    if result.structured_content is None:
        raise RuntimeError(f"{tool_name} did not return structuredContent")
    return result.structured_content


def _print_result(label: str, value: Any) -> None:
    print(f"{label}:")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


async def verify(*, enable_writes: bool, list_only: bool) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "toggl_mcp.server"],
        env=_server_environment(enable_writes=enable_writes),
        cwd=PROJECT_ROOT,
    )

    async with Client(stdio_client(parameters)) as client:
        listed = await client.list_tools(cache_mode="bypass")
        names = [tool.name for tool in listed.tools]
        expected = READ_TOOL_NAMES + (WRITE_TOOL_NAMES if enable_writes else [])
        if names != expected:
            raise RuntimeError(f"Unexpected tools/list result: {names}")
        _print_result("tools/list", names)

        if list_only:
            return

        projects = await client.call_tool("list_projects")
        _print_result("list_projects", _structured_result("list_projects", projects))

        current = await client.call_tool("get_current_timer")
        _print_result(
            "get_current_timer",
            _structured_result("get_current_timer", current),
        )

        now = datetime.now().astimezone()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        entries = await client.call_tool(
            "get_time_entries",
            {"start_date": start.isoformat(), "end_date": now.isoformat()},
        )
        _print_result(
            "get_time_entries",
            _structured_result("get_time_entries", entries),
        )

        clients = await client.call_tool("list_clients")
        _print_result("list_clients", _structured_result("list_clients", clients))

        tags = await client.call_tool("list_tags")
        _print_result("list_tags", _structured_result("list_tags", tags))

        project_ids = [
            project["id"]
            for project in _structured_result("list_projects", projects)["projects"]
        ]
        summary = await client.call_tool(
            "summarize_time",
            {"start_date": start.isoformat(), "end_date": now.isoformat()},
        )
        _print_result(
            "summarize_time",
            _structured_result("summarize_time", summary),
        )

        if project_ids:
            try:
                tasks = await client.call_tool("list_tasks", {"project_id": project_ids[0]})
                _print_result("list_tasks", _structured_result("list_tasks", tasks))
            except RuntimeError as error:
                # Expected on Toggl plans without the tasks feature (the API answers 404).
                print(f"list_tasks: not available ({error})")
        else:
            print("list_tasks: skipped (no projects in workspace)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify tools/list and read-only tools/call through a real stdio MCP process."
    )
    parser.add_argument(
        "--enable-writes",
        action="store_true",
        help="Expose write tools for tools/list verification; does not call them.",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only verify tools/list; do not call account-reading tools.",
    )
    arguments = parser.parse_args()
    asyncio.run(
        verify(
            enable_writes=arguments.enable_writes,
            list_only=arguments.list_only,
        )
    )


if __name__ == "__main__":
    main()
