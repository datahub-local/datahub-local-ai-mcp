"""Entry point for the MCP servers.

Usage:
    python -m mcp_runner --server homelab_facts [--host 0.0.0.0] [--port 8080]
    python -m mcp_runner --server homelab_facts --list-tools

Servers are discovered by import, not by a path walk or a registration table:
`servers/` is a packaging root (see `pyproject.toml`), so every directory under
it is a top-level package and `--server <name>` is `importlib.import_module`.
Any package exposing `register(registry)` is a valid server, which is why adding
one needs no change to this file.

`--list-tools` exists to be run in CI and before wiring an agent: it builds the
registry and prints the manifest without binding a port or reaching a backend,
so a tool that fails to register is caught by a build rather than by a silently
blander agent report.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys

from .server import Registry, build_app

logger = logging.getLogger(__name__)


def build_registry(server: str) -> Registry:
    module = importlib.import_module(server)
    registry = Registry(name=f"mcp-{server.replace('_', '-')}")
    module.register(registry)
    return registry


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s|[%(levelname)s]|%(name)s|%(message)s",
    )

    parser = argparse.ArgumentParser(prog="mcp_runner")
    parser.add_argument(
        "--server",
        required=True,
        help="a package under servers/ exposing register(registry)",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="print the tool manifest and exit, without binding a port",
    )
    parsed = parser.parse_args(argv)

    try:
        registry = build_registry(parsed.server)
    except ModuleNotFoundError:
        parser.error(f"no such server: {parsed.server!r}")

    if parsed.list_tools:
        for tool in registry.describe():
            print(f"{tool['name']}\n    {tool['description'].splitlines()[0]}")
        return

    logger.info(
        "serving server=%s tools=%d on %s:%d",
        parsed.server,
        len(registry.tools),
        parsed.host,
        parsed.port,
    )

    import uvicorn

    uvicorn.run(
        build_app(registry),
        host=parsed.host,
        port=parsed.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main(sys.argv[1:])
