"""Entry point for `mcp dev` that loads the server via normal import.

Why this exists
---------------
`mcp dev <file.py:obj>` loads the server with
``importlib.util.spec_from_file_location("server_module", file)`` followed by
``spec.loader.exec_module(module)`` WITHOUT registering the module in
``sys.modules`` (see ``mcp/cli/cli.py::_import_server``).

When the target module uses ``from __future__ import annotations`` together
with ``@dataclass``, Python's ``dataclasses`` module calls
``sys.modules.get(cls.__module__).__dict__`` to resolve the stringified type
annotations. Because the module is not in ``sys.modules`` yet, this returns
``None`` and crashes with::

    AttributeError: 'NoneType' object has no attribute '__dict__'

This wrapper registers the module in ``sys.modules`` before executing it, so
``mcp dev`` can load the real server module without touching its source.

Usage
-----
Instead of::

    mcp dev "src/toggl_mcp/server.py:mcp"

use::

    mcp dev "scripts/mcp_dev_entry.py:mcp"
"""

from __future__ import annotations

import os
import pathlib
import sys

# Make sure the project's `src` layout is importable when this file is loaded
# directly by path (mcp dev inserts the file's own directory, not `src`).
_src_dir = str(pathlib.Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

# `mcp dev` spawns the server subprocess via `uv run ... mcp run <file>`,
# which does NOT forward the parent's `--env-file .env`. Load `.env` here so
# TOGGL_API_KEY / TOGGL_ORGANIZATION_ID / TOGGL_WORKSPACE_ID are available
# to TogglConfig.from_env() at server startup.
_project_root = pathlib.Path(__file__).resolve().parent.parent
_env_file = _project_root / ".env"
if _env_file.is_file():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _val = _line.partition("=")
        _key, _val = _key.strip(), _val.strip().strip('"').strip("'")
        os.environ.setdefault(_key, _val)

# Import the real server module the normal way. This registers it in
# sys.modules and evaluates all @dataclass decorators safely.
from toggl_mcp import server as server  # noqa: E402  (re-export for `:mcp`)

mcp = server.mcp


if __name__ == "__main__":
    server.main()
