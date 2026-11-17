"""Lattice Talk MCP server (stdio): `python -m lattice_wildfire.talk.mcp_server`.

Needs the `talk` extra (`uv sync --extra talk`). Built on the `mcp` 2.x SDK, where the FastMCP
class is named `MCPServer` (same decorator API). The Lattice client is built on the first tool
call, so `tools/list` works without Lattice credentials.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from importlib.metadata import version
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from lattice_wildfire.talk.tools import TOOL_SPECS, TalkTools

SERVER_NAME = "lattice-wildfire-talk"
INSTRUCTIONS = (
    "Read-only views of the Lattice wildfire picture for Los Angeles County: picture counts, "
    "entities near a point or an LA County Fire station, analyst briefs and task status. "
    "Cite entity ids. request_investigation is the only write and stays a dry run unless confirm=true."
)

_DESCRIPTIONS = {spec["function"]["name"]: spec["function"]["description"] for spec in TOOL_SPECS}
_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)


def _default_tools() -> TalkTools:
    from lattice_wildfire.core.client import make_client

    return TalkTools(make_client())


def build_server(tools_factory: Callable[[], TalkTools] = _default_tools) -> MCPServer:
    """Build the MCP server. `tools_factory` runs once, on the first tool call."""
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, version=version("lattice-wildfire"))
    holder: list[TalkTools] = []

    def tools() -> TalkTools:
        if not holder:
            holder.append(tools_factory())
        return holder[0]

    @server.tool(description=_DESCRIPTIONS["wildfire_picture"], annotations=_READ_ONLY)
    def wildfire_picture(
        bbox: str | None = None, kinds: list[str] | None = None, limit: int = 50
    ) -> dict[str, Any]:
        return tools().wildfire_picture(bbox=bbox, kinds=kinds, limit=limit)

    @server.tool(description=_DESCRIPTIONS["get_entity"], annotations=_READ_ONLY)
    def get_entity(entity_id: str) -> dict[str, Any]:
        return tools().get_entity(entity_id)

    @server.tool(description=_DESCRIPTIONS["near"], annotations=_READ_ONLY)
    def near(lat: float, lon: float, radius_km: float, kinds: list[str] | None = None) -> dict[str, Any]:
        return tools().near(lat, lon, radius_km, kinds=kinds)

    @server.tool(description=_DESCRIPTIONS["near_station"], annotations=_READ_ONLY)
    def near_station(
        station_number: str, radius_km: float = 10.0, kinds: list[str] | None = None
    ) -> dict[str, Any]:
        return tools().near_station(station_number, radius_km=radius_km, kinds=kinds)

    @server.tool(description=_DESCRIPTIONS["latest_brief"], annotations=_READ_ONLY)
    def latest_brief(entity_id: str | None = None, task_id: str | None = None) -> dict[str, Any]:
        return tools().latest_brief(entity_id=entity_id, task_id=task_id)

    @server.tool(description=_DESCRIPTIONS["task_status"], annotations=_READ_ONLY)
    def task_status(task_id: str) -> dict[str, Any]:
        return tools().task_status(task_id)

    @server.tool(description=_DESCRIPTIONS["request_investigation"], annotations=_WRITE)
    def request_investigation(
        entity_id: str, confirm: bool = False, assignee_entity_id: str | None = None
    ) -> dict[str, Any]:
        return tools().request_investigation(
            entity_id, confirm=confirm, assignee_entity_id=assignee_entity_id
        )

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="lw-talk-mcp", description=__doc__.splitlines()[0])
    parser.parse_args(argv)
    build_server().run("stdio")


if __name__ == "__main__":
    main()
