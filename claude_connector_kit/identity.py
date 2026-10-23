"""What a connector says about itself, and a test that holds it to that.

A client lists a server by its title, description, version, website and icon, and a tool by its
hints and its output schema. `server_identity()` and `icon()` build the first; `landing_html()`
and `landing_route()` give the website a page to land on; `check_server()` reports what a
server has left out, for a test to assert empty.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.types import Icon
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

DESCRIPTION_LIMIT = 2048
HINTS = ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint")


def icon(svg: str) -> Icon:
    """An icon from SVG source, as a data: URI. `#` and the other reserved characters are
    percent-encoded, so a colour such as #2f5d50 survives."""
    return Icon(src="data:image/svg+xml," + quote(svg, safe="/:=,;-"), mime_type="image/svg+xml",
                sizes=["any"])


def server_identity(title: str, description: str, version: str, icon_svg: str, *,
                    website_url: str | None = None) -> dict[str, Any]:
    """The MCPServer keyword arguments that make up serverInfo: `MCPServer("thing",
    **server_identity(...))`. `website_url` is left out of the result when None, so a build
    with no public address (a stdio run) can pass it where it has one."""
    out: dict[str, Any] = {"title": title, "description": description, "version": version,
                           "icons": [icon(icon_svg)]}
    if website_url is not None:
        out["website_url"] = website_url
    return out


def landing_html(title: str, summary: str, *notes: str) -> str:
    """A small static page for the connector's website: what it is, then each note as a
    paragraph. Text is escaped; a note is plain text, not markup."""
    paragraphs = "\n".join(f"<p>{html.escape(text)}</p>" for text in (summary, *notes))
    name = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>
body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 36rem; margin: 3rem auto; padding: 0 1rem; }}
h1 {{ font-size: 1.5rem; }}
</style>
</head>
<body>
<h1>{name}</h1>
{paragraphs}
</body>
</html>
"""


def landing_route(page: str, path: str = "/") -> Route:
    """A GET route serving `page`. In an app that mounts the MCP app at "/", put it before
    that mount."""
    async def landing(request: Request) -> HTMLResponse:
        return HTMLResponse(page)
    return Route(path, landing, methods=["GET"])


def check_server(server: MCPServer, *, no_output: Iterable[str] = ()) -> list[str]:
    """What `server` leaves out that a client or a scorecard looks for, one string per gap;
    empty means conformant. Checked: title, description, version, website and an icon with a
    size; for each tool a title, a description within Claude Code's 2,048-character cut, all
    four boolean hints (read-only and destructive together contradict), a description on every
    input argument, and an output schema unless the tool is named in `no_output`. A tool gets an
    output schema from a return annotation that is a model, `str` or a list, or
    `Annotated[CallToolResult, Model]`; `-> dict`, `-> CallToolResult` and no annotation
    give none."""
    problems: list[str] = []
    for field in ("title", "description", "version", "website_url"):
        if not (getattr(server, field) or "").strip():
            problems.append(f"server has no {field.replace("_", " ")}")
    if not server.icons:
        problems.append("server has no icon")
    elif not all(i.sizes for i in server.icons):
        problems.append("an icon has no sizes")
    tools = server._tool_manager.list_tools()
    names = {t.name for t in tools}
    exempt = set(no_output)
    for name in sorted(exempt - names):
        problems.append(f"no_output names {name}, which is not a tool")
    for t in tools:
        if not (t.title or "").strip():
            problems.append(f"{t.name} has no title")
        if not (t.description or "").strip():
            problems.append(f"{t.name} has no description")
        elif len(t.description) > DESCRIPTION_LIMIT:
            problems.append(f"{t.name} description is {len(t.description)} characters, "
                            f"and Claude Code cuts it at {DESCRIPTION_LIMIT}")
        for hint in HINTS:
            if not isinstance(getattr(t.annotations, hint, None), bool):
                problems.append(f"{t.name} does not set {hint}")
        if t.annotations and t.annotations.read_only_hint and t.annotations.destructive_hint:
            problems.append(f"{t.name} is both read-only and destructive")
        for arg, spec in (t.parameters.get("properties") or {}).items():
            if not spec.get("description"):
                problems.append(f"{t.name} argument {arg} has no description")
        if t.output_schema is None and t.name not in exempt:
            problems.append(f"{t.name} has no output schema")
    return problems
