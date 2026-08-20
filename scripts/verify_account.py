"""Read-only smoke test for a real Toggl Track account."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from toggl_mcp import TogglClient, TogglConfig
from toggl_mcp.exceptions import TogglError


async def verify() -> None:
    config = TogglConfig.from_env()
    now = datetime.now(UTC)
    start = now - timedelta(days=7)

    async with TogglClient(config) as client:
        projects = await client.list_projects()
        current = await client.get_current_timer()
        recent = await client.get_time_entries(start, now)

    print(
        "Authentication/workspace access: OK "
        f"(organization {config.organization_id}, workspace {config.workspace_id})"
    )
    print(f"Projects visible in configured workspace: {len(projects)}")
    if current is None:
        print("Current timer: none")
    else:
        print(
            "Current timer: "
            f"id={current.id}, description={current.description!r}, "
            f"start={current.start.isoformat()}"
        )
    print(
        "Time entries started in the last 7 days: "
        f"{recent.count} (possibly_truncated={recent.possibly_truncated})"
    )


def main() -> int:
    try:
        asyncio.run(verify())
    except TogglError as exc:
        print(f"Read-only verification failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
