# claude-connector-kit

The shared parts of an MCP connector meant for Claude clients (claude.ai, the
desktop app, the phone, Claude Code): an OAuth 2.1 server with a login page,
so adding the connector is a URL and a password, and tool registration shaped
for what a Claude client actually shows the model.

It sits on the official MCP Python SDK (`mcp` 2.x) and adds no framework of
its own.

## What a Claude client shows the model

Only a tool's name, description and input schema. Not its output schema, title
or annotations; on claude.ai, not the server's instructions either. The
desktop app's Code and Cowork tabs strip an optional argument written
`anyOf: [X, null]`, type and enum included. Claude Code cuts a description at
2,048 characters and warns at a 10,000-token result. So the kit:

- publishes each input schema lean: no generated titles, an optional argument
  as its plain type, `additionalProperties: false`, and an argument the tool
  does not take is refused rather than silently dropped;
- puts guidance on arguments (`about()`), where every client shows it, rather
  than in instructions, which some drop;
- returns results as compact JSON (`result()`); the SDK's default is indent 2,
  a token per space;
- hands the model the message of an expected error (`errors=`), where the SDK
  would say only "Error executing tool".

## Use

```python
from typing import Annotated, TypedDict
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult
from claude_connector_kit import (READS, LoginRefused, Provider, Store, about, add_login,
                                  auth_settings, result, server_identity, tool)

URL, SCOPES = "https://thing.example.com", ["read"]
ICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128"><rect width="128" height="128"/></svg>'

class Widgets(TypedDict):
    columns: list[str]
    rows: list[list]

def check_login(login, password):
    if password != PASSWORD:
        raise LoginRefused("wrong password")
    return "signed-in"          # kept as the session; a backend token for a real login

provider = Provider(Store("/data/oauth.db"), URL, check_login, SCOPES)
server = MCPServer("thing", auth_server_provider=provider, auth=auth_settings(URL, SCOPES),
                   **server_identity("Thing", "Lists the widgets a person has made.", "1.0.0",
                                     ICON_SVG, website_url=URL))
add_login(server, provider, what="thing", fixed_login="owner")   # password only, one person

@tool(server, "Widgets", READS, errors=(ValueError,))
def widgets(since: Annotated[str | None, about("from this date, YYYY-MM-DD")] = None
            ) -> Annotated[CallToolResult, Widgets]:
    """Widgets made since a date, newest first."""
    return result({"columns": ["id", "made"], "rows": [[1, "2026-09-29"]]})

app = server.streamable_http_app()
```

Put it behind HTTPS with a publicly trusted certificate, and pass `/mcp`,
`/authorize`, `/token`, `/register`, `/revoke`, `/login` and `/.well-known/`
to it. In claude.ai: Settings, Connectors, Add custom connector, the `/mcp`
URL, no client id or secret. In Claude Code: `claude mcp add --transport http
--scope user thing https://thing.example.com/mcp`, then `/mcp`, Authenticate.

A script signs in the same way without a browser:

```python
from claude_connector_kit import sign_in
token = sign_in("https://thing.example.com/mcp", password)
```

`model_view(server)` returns what a client shows the model, for a test that
holds the tool list to a budget.

## What a connector directory asks for

A title, description, version, website and icon on the server, all four boolean
hints on every tool, a description on every argument, and an output schema.

- `server_identity(title, description, version, icon_svg, website_url=...)` is
  the `MCPServer` keyword arguments for the first group; `website_url` is left
  out when None, so a build with no public address can pass it elsewhere.
  The version is the caller's: the kit only checks it is not empty.
- `READS` sets all four hints for a tool that only reads; `writes()` for one
  that does not.
- `landing_html(title, summary, *notes)` and `landing_route(page)` give the
  website a page. Put the route before the mount that serves `/mcp`.
- A tool has an output schema when it is annotated `-> str`, `-> list[Model]` or
  `-> Annotated[CallToolResult, Model]`. `-> dict`, `-> CallToolResult` and no
  annotation give none.
- `check_server(server, no_output=())` returns what is missing, one string per
  gap: the server fields above, and per tool a title, a description within
  Claude Code's 2,048-character cut, all four hints (not read-only and
  destructive together), a description on every argument and an output schema.
  Hold a server to it in a test: `assert check_server(server) == []`.

What a linter also scores is the wording, which no check can judge. Say, in a
tool's description, what it returns (the fields) and when to choose it over a
neighbour; in an argument's, its meaning, unit, where an id comes from, any
rule the schema cannot state, and what leaving an optional one out does. Do not
restate an enum the schema already lists.

## Grants

A sign-in stores a session for its login, and every grant (one per client
device) acts through it. Access tokens last an hour, refresh tokens 90 days. A
revoke from any client ends the session and so signs every client of that
login out; `Store.end_session(login, credential)` does the same when a backend
refuses the stored credential.

## Tests

```bash
python -m pip install -e '.[dev]' && python -m pytest -q && ruff check .
```
