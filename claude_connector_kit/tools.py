"""Tools shaped for what a Claude client shows the model.

A Claude client puts a tool's name, description and input schema before the
model. Whether it also shows the output schema is unverified (a client may
not), and it shows no title, no annotations, and on claude.ai no server
instructions. The desktop app's Code and Cowork tabs strip an optional
argument written `anyOf: [X, null]`, type and all. So:

- `tool()` registers a function with its title and hints, publishes its input
  and output schemas lean (no generated titles, an optional argument as its
  plain type, every description kept), refuses an argument the tool does not
  take, and hands the client the message of each exception named in `errors`
  instead of the SDK's bare "Error executing tool".
- `about()` puts a description on an argument, where every client shows it.
- `result()` returns data as compact JSON text and the same data structured,
  instead of the SDK's indent-2 text, which costs the model a token per space.
"""

from __future__ import annotations

import functools
import inspect
import json
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ConfigDict, Field

# The hints a client reads to decide whether to ask the person before a call. A client that
# finds one unset has to assume the worst, so a read sets all four.
READS = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                        open_world_hint=False)


def writes(*, destructive: bool, idempotent: bool) -> ToolAnnotations:
    """The hints for a tool that writes. `destructive` is for one that throws away what
    someone asked for; `idempotent` for one whose repeat, same arguments, changes
    nothing more."""
    return ToolAnnotations(read_only_hint=False, destructive_hint=destructive,
                           idempotent_hint=idempotent, open_world_hint=False)


def about(text: str, **bounds: Any) -> Any:
    """An argument's description, shown on the argument in the tool's schema:
    `since: Annotated[str | None, about("...")] = None`. bounds are Field's, e.g. ge=1."""
    return Field(description=text, **bounds)


def result(data: dict[str, Any]) -> CallToolResult:
    """`data` as the call's answer: compact JSON text, and the same object structured,
    so either one carries the whole answer whichever a client passes on."""
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=data)


def tool(server: MCPServer, title: str, annotations: ToolAnnotations,
         errors: tuple[type[BaseException], ...] = ()) -> Callable[[Callable], Callable]:
    """`@tool(server, "Sessions", READS, errors=(ValueError,))` over a function registers it
    as described in this module's docstring. The function's docstring is its description,
    published with its source indentation collapsed."""
    def register(fn: Callable) -> Callable:
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapped(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await fn(*args, **kwargs)
                except errors as exc:
                    raise ToolError(str(exc)) from exc
        else:
            @functools.wraps(fn)
            def wrapped(*args: Any, **kwargs: Any) -> Any:
                try:
                    return fn(*args, **kwargs)
                except errors as exc:
                    raise ToolError(str(exc)) from exc
        server.tool(title=title, annotations=annotations)(wrapped)
        t = server._tool_manager.get_tool(fn.__name__)
        t.description = " ".join((t.description or "").split())
        t.parameters = lean(t.parameters)
        if t.fn_metadata.output_schema is not None:
            t.fn_metadata.output_schema = lean(t.fn_metadata.output_schema)
        refuse_unknown_arguments(server, fn.__name__)
        return wrapped
    return register


def lean(node: Any) -> Any:
    """A published schema without what a client does not use: the titles pydantic adds and an
    optional argument's null choice and null default (leaving it out says the same).
    Descriptions stay. Checks run on the tool's own models, not on this copy, so it changes
    what is sent and nothing that is enforced."""
    if isinstance(node, list):
        return [lean(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("properties", "$defs"):   # keys here are field names, one may be "title"
            out[key] = {name: lean(sub) for name, sub in value.items()}
        elif key == "title":
            continue
        else:
            out[key] = lean(value)
    choices = out.get("anyOf")
    if isinstance(choices, list) and {"type": "null"} in choices and "default" in out \
            and out["default"] is None:
        rest = [c for c in choices if c != {"type": "null"}]
        del out["anyOf"], out["default"]
        out = {**rest[0], **out} if len(rest) == 1 else {**out, "anyOf": rest}
    return out


def refuse_unknown_arguments(server: MCPServer, name: str) -> None:
    """Make tool `name` refuse an argument it does not take. The SDK drops one by default,
    so a misspelled argument would answer ok having done none of what it asked for. The
    schema says the same with additionalProperties."""
    t = server._tool_manager.get_tool(name)
    base = t.fn_metadata.arg_model
    t.fn_metadata.arg_model = type(base.__name__, (base,), {
        "model_config": ConfigDict(**base.model_config, extra="forbid")})
    t.parameters["additionalProperties"] = False


def model_view(server: MCPServer) -> list[dict[str, Any]]:
    """What a Claude client shows the model of each tool, for a test to hold to a budget."""
    return [{"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in server._tool_manager.list_tools()]
