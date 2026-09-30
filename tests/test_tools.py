import asyncio
import json
from typing import Annotated

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from claude_connector_kit import READS, about, model_view, result, tool


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def server():
    s = MCPServer("t")

    @tool(s, "Echo", READS, errors=(ValueError,))
    def echo(word: Annotated[str, about("the word to echo")],
             times: Annotated[int | None, about("how often")] = None) -> dict:
        """Echo a word.

            Indented source lines collapse."""
        if word == "bad":
            raise ValueError("bad is not a word here")
        if word == "boom":
            raise RuntimeError("internal")
        return result({"word": word * (times or 1)})
    return s


def schema(server):
    return model_view(server)[0]["input_schema"]


def test_an_optional_argument_is_its_plain_type_with_its_description(server):
    props = schema(server)["properties"]
    assert props["times"] == {"type": "integer", "description": "how often"}
    assert props["word"] == {"type": "string", "description": "the word to echo"}


def test_no_generated_titles_and_unknown_arguments_refused(server):
    s = schema(server)
    assert "title" not in json.dumps(s)
    assert s["additionalProperties"] is False
    with pytest.raises(ToolError, match="wrod"):
        run(server.call_tool("echo", {"word": "a", "wrod": "b"}))


def test_the_description_loses_its_source_indentation(server):
    assert model_view(server)[0]["description"] == "Echo a word. Indented source lines collapse."


def test_a_named_error_reaches_the_client_with_its_message(server):
    with pytest.raises(ToolError, match="bad is not a word here"):
        run(server.call_tool("echo", {"word": "bad"}))
    with pytest.raises(ToolError) as crash:   # an unnamed error stays opaque
        run(server.call_tool("echo", {"word": "boom"}))
    assert "internal" not in str(crash.value)


def test_the_result_is_compact_text_and_the_same_data_structured(server):
    got = run(server.call_tool("echo", {"word": "ab", "times": 2}))
    assert got.content[0].text == '{"word":"abab"}'
    assert got.structured_content == {"word": "abab"}


def test_an_async_tool_is_awaited_and_its_named_error_passes(server):
    @tool(server, "Later", READS, errors=(ValueError,))
    async def later(word: Annotated[str, about("a word")]) -> dict:
        """Echo a word, later."""
        if word == "bad":
            raise ValueError("bad later")
        return result({"word": word})
    assert run(server.call_tool("later", {"word": "ok"})).structured_content == {"word": "ok"}
    with pytest.raises(ToolError, match="bad later"):
        run(server.call_tool("later", {"word": "bad"}))


def test_bounds_on_an_argument_are_published_and_enforced():
    s = MCPServer("b")

    @tool(s, "Bounded", READS)
    def bounded(limit: Annotated[int, about("how many", ge=1, le=10)] = 5) -> dict:
        """Bounded."""
        return result({"limit": limit})
    assert model_view(s)[0]["input_schema"]["properties"]["limit"] == {
        "type": "integer", "description": "how many", "minimum": 1, "maximum": 10, "default": 5}
    with pytest.raises(ToolError):
        run(s.call_tool("bounded", {"limit": 0}))
