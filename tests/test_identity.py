from typing import Annotated, TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, Icon, ToolAnnotations
from starlette.applications import Starlette
from starlette.testclient import TestClient

from claude_connector_kit import (
    READS,
    about,
    check_server,
    icon,
    landing_html,
    landing_route,
    result,
    server_identity,
    tool,
    writes,
)

SVG = '<svg xmlns="http://www.w3.org/2000/svg"><rect fill="#2f5d50"/></svg>'


class Word(TypedDict):
    word: str


def conformant() -> MCPServer:
    s = MCPServer("t", **server_identity("Thing", "Does things.", "1.2.3", SVG, website_url="https://example.com"))

    @tool(s, "Echo", READS)
    def echo(word: Annotated[str, about("the word")]) -> Annotated[CallToolResult, Word]:
        """Echo a word."""
        return result({"word": word})
    return s


def test_a_conformant_server_has_no_problems():
    assert check_server(conformant()) == []


def test_identity_reaches_the_server_and_the_icon_keeps_its_colour():
    s = conformant()
    assert (s.title, s.description, s.version, s.website_url) == ("Thing", "Does things.", "1.2.3",
                                                                  "https://example.com")
    assert "%232f5d50" in s.icons[0].src and "#" not in s.icons[0].src
    assert s.icons[0].sizes == ["any"] and icon(SVG).mime_type == "image/svg+xml"


def test_no_website_is_left_out_so_one_can_be_passed_beside_it():
    assert "website_url" not in server_identity("T", "d", "1", SVG)
    s = MCPServer("t", **server_identity("T", "d", "1", SVG), website_url="https://example.com")
    assert s.website_url == "https://example.com"


def test_what_a_bare_server_leaves_out_is_named():
    s = MCPServer("bare")

    @s.tool()
    def bare(word: str) -> dict:
        """Bare."""
        return {}
    got = check_server(s)
    for want in ("server has no title", "server has no description", "server has no version",
                 "server has no website url", "server has no icon", "bare does not set read_only_hint",
                 "bare does not set open_world_hint", "bare argument word has no description",
                 "bare has no output schema"):
        assert want in got, want


def test_the_old_partial_hints_and_a_bare_dict_return_are_caught():
    s = conformant()

    @tool(s, "Loose", writes(destructive=False, idempotent=True))
    def loose(word: Annotated[str, about("the word")]) -> dict:
        """Loose."""
        return result({})
    assert check_server(s) == ["loose has no output schema"]
    assert check_server(s, no_output=["loose"]) == []


def test_the_two_hint_reads_of_the_earlier_kit_are_caught():
    s = conformant()
    two = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @tool(s, "Partial", two)
    def partial(word: Annotated[str, about("the word")]) -> Annotated[CallToolResult, Word]:
        """Partial."""
        return result({"word": word})
    assert check_server(s) == ["partial does not set destructive_hint", "partial does not set idempotent_hint"]


def test_an_icon_without_sizes_a_blank_title_and_a_tool_without_a_description_are_caught():
    s = MCPServer("t", title="  ", description="d", version="1", website_url="https://example.com",
                  icons=[Icon(src="data:image/svg+xml,x", mime_type="image/svg+xml")])

    @tool(s, "Mute", READS)
    def mute(word: Annotated[str, about("the word")]) -> Annotated[CallToolResult, Word]:
        return result({"word": word})
    assert check_server(s) == ["server has no title", "an icon has no sizes", "mute has no description"]


def test_a_missing_title_a_long_description_and_a_read_only_destructive_tool_are_caught():
    s = conformant()
    contradiction = ToolAnnotations(read_only_hint=True, destructive_hint=True, idempotent_hint=True,
                                    open_world_hint=False)

    @tool(s, "Both", contradiction)
    def both(word: Annotated[str, about("the word")]) -> Annotated[CallToolResult, Word]:
        """Both."""
        return result({"word": word})

    @s.tool(annotations=READS)
    def untitled(word: Annotated[str, about("the word")]) -> Annotated[CallToolResult, Word]:
        """Untitled."""
        return result({"word": word})

    def long(word: Annotated[str, about("the word")]) -> Annotated[CallToolResult, Word]:
        return result({"word": word})
    long.__doc__ = "x" * 2049
    tool(s, "Long", READS)(long)
    assert check_server(s) == [
        "both is both read-only and destructive",
        "untitled has no title",
        "long description is 2049 characters, and Claude Code cuts it at 2048"]


def test_an_exemption_for_a_tool_that_is_not_there_is_reported():
    assert check_server(conformant(), no_output=["gone"]) == ["no_output names gone, which is not a tool"]


def test_reads_sets_all_four_hints():
    assert READS.model_dump(exclude_none=True) == {"read_only_hint": True, "destructive_hint": False,
                                                    "idempotent_hint": True, "open_world_hint": False}


def test_the_landing_page_escapes_and_is_served_before_a_mount():
    page = landing_html("A & B", "Reads <things>.", "Not affiliated.")
    assert "<title>A &amp; B</title>" in page and "<p>Reads &lt;things&gt;.</p>" in page
    assert "<p>Not affiliated.</p>" in page
    client = TestClient(Starlette(routes=[landing_route(page)]))
    got = client.get("/")
    assert got.status_code == 200 and got.text == page
    assert client.post("/").status_code == 405
