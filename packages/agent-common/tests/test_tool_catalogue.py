"""Tests for the raw-bytes tool catalogue (representation, lazy tools, ingest paths, store)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from mcp.types import ListToolsResult, Tool as MCPTool, ToolAnnotations

from agent_common.core.catalogue_ingest import (
    StatelessListError,
    StatelessListUnsupported,
    fetch_catalogue_mcp,
    fetch_catalogue_stateless,
    iter_array_at,
    parse_tools_list_reply,
)
from agent_common.core.tool_catalogue import (
    ServerCatalogue,
    build_lazy_tools,
    build_server_catalogue,
    compute_interface_hash,
    make_catalogue_tool,
    sanitize_tool_name,
)

SCHEMA = {
    "type": "object",
    "properties": {"q": {"type": "string", "description": "query"}},
    "required": ["q"],
}


def _catalogue(*names: str, server: str = "srv") -> ServerCatalogue:
    tools = [
        make_catalogue_tool(server_name=server, name=n, description=f"desc {n}", input_schema=SCHEMA) for n in names
    ]
    return build_server_catalogue(server, tools, source="mcp")


# --------------------------------------------------------------------------------------
# Representation
# --------------------------------------------------------------------------------------


class TestRepresentation:
    def test_card_holds_names_and_params_but_no_schema(self):
        tool = make_catalogue_tool(
            server_name="s",
            name="t",
            description="line one\nline two",
            input_schema=SCHEMA,
        )
        assert tool.card.param_names == ("q",)
        assert tool.card.first_line == "line one"
        assert isinstance(tool.schema_bytes, bytes)
        assert json.loads(tool.schema_bytes) == SCHEMA

    def test_interface_hash_is_order_independent_and_schema_sensitive(self):
        a = _catalogue("x", "y")
        b = _catalogue("y", "x")
        assert a.interface_hash == b.interface_hash
        changed = build_server_catalogue(
            "srv",
            [
                make_catalogue_tool(
                    server_name="srv",
                    name="x",
                    description="desc x",
                    input_schema={"type": "object"},
                )
            ],
            source="mcp",
        )
        assert changed.interface_hash != _catalogue("x").interface_hash
        # Same interface from either ingest path hashes the same.
        assert compute_interface_hash(a.tools.values()) == a.interface_hash

    def test_duplicate_names_keep_first(self):
        tools = [
            make_catalogue_tool(server_name="s", name="dup", description="first", input_schema=SCHEMA),
            make_catalogue_tool(server_name="s", name="dup", description="second", input_schema=SCHEMA),
        ]
        cat = build_server_catalogue("s", tools, source="stateless")
        assert cat.tools["dup"].card.description == "first"


class TestNameSanitisation:
    """A tool's exposed name must survive PTC's ``tools.<camelCase(name)>`` namespace."""

    @pytest.mark.parametrize(
        ("wire", "exposed"),
        [
            ("authrion-atp-v1_okrs.v1.search_okrs", "authrion-atp-v1_okrs_v1_search_okrs"),
            ("plain_tool", "plain_tool"),
            ("kebab-tool", "kebab-tool"),
            ("has space", "has_space"),
            ("2fa_check", "_2fa_check"),
            ("-leading", "_leading"),
        ],
    )
    def test_exposed_name_folds_ptc_hostile_characters(self, wire, exposed):
        assert sanitize_tool_name(wire) == exposed
        assert sanitize_tool_name(exposed) == exposed, "idempotent"

    def test_sanitised_names_are_valid_js_identifiers_after_camel_casing(self):
        from langchain_quickjs._prompt import is_valid_ptc_tool_name

        for wire in ("authrion-atp-v1_okrs.v1.search_okrs", "a.b.c", "2fa_check", "-leading", "weird!name"):
            assert is_valid_ptc_tool_name(sanitize_tool_name(wire)), wire

    def test_catalogue_exposes_the_sanitised_name_and_keeps_the_wire_name(self):
        cat = _catalogue("okrs.v1.search_okrs")
        (entry,) = cat.tools.values()
        assert entry.name == "okrs_v1_search_okrs"
        assert entry.call_name == "okrs.v1.search_okrs"
        assert "okrs_v1_search_okrs" in cat.tools

    def test_unsanitised_name_keeps_no_wire_name(self):
        (entry,) = _catalogue("plain").tools.values()
        assert entry.wire_name is None and entry.call_name == "plain"

    @pytest.mark.asyncio
    async def test_dispatch_calls_the_server_by_its_wire_name(self):
        (tool,) = build_lazy_tools(_catalogue("okrs.v1.search_okrs"), connection={})
        seen: dict = {}

        async def fake_coroutine(runtime=None, **arguments):
            return ("ok", None)

        def fake_convert(session, mcp_tool, **kwargs):
            seen["mcp_tool"] = mcp_tool
            delegate = Mock()
            delegate.coroutine = fake_coroutine
            return delegate

        with patch(
            "langchain_mcp_adapters.tools.convert_mcp_tool_to_langchain_tool",
            side_effect=fake_convert,
        ):
            await tool.ainvoke({"q": "hi"})

        assert tool.name == "okrs_v1_search_okrs", "the model sees the sanitised name"
        assert seen["mcp_tool"].name == "okrs.v1.search_okrs", "the server is called by its own name"


class TestLazyMcpTool:
    def test_listing_attributes_never_decode(self):
        (tool,) = build_lazy_tools(_catalogue("t"), connection={"transport": "streamable_http", "url": "u"})
        assert isinstance(tool, BaseTool)
        assert tool.name == "t"
        assert tool.description == "desc t"
        assert tool.metadata["server_name"] == "srv"
        assert not tool.schema_decoded

    def test_schema_decodes_once_on_first_use(self):
        (tool,) = build_lazy_tools(_catalogue("t"), connection={"transport": "streamable_http", "url": "u"})
        params = convert_to_openai_tool(tool)["function"]["parameters"]
        assert params == SCHEMA
        assert tool.schema_decoded
        first = tool.args_schema
        assert tool.args_schema is first  # cached, not re-decoded
        assert tool.args == SCHEMA["properties"]

    def test_metadata_mirrors_adapter_shape_and_extra(self):
        entry = make_catalogue_tool(
            server_name="s",
            name="t",
            description="d",
            input_schema=SCHEMA,
            annotations={"readOnlyHint": True},
            meta={"k": "v"},
        )
        cat = build_server_catalogue("s", [entry], source="stateless")
        (tool,) = build_lazy_tools(cat, connection={}, extra_metadata={"compression_enabled": True})
        assert tool.metadata == {
            "readOnlyHint": True,
            "_meta": {"k": "v"},
            "server_name": "s",
            "compression_enabled": True,
        }

    @pytest.mark.asyncio
    async def test_invoke_delegates_to_adapter_tool_for_that_one_tool(self):
        connection = {
            "transport": "streamable_http",
            "url": "https://gw/mcp",
            "headers": {"Authorization": "Bearer x"},
        }
        (tool,) = build_lazy_tools(_catalogue("t"), connection=connection, tool_interceptors=["icpt"])
        seen: dict = {}

        async def fake_coroutine(runtime=None, **arguments):
            seen["args"] = arguments
            return ("result text", None)

        def fake_convert(session, mcp_tool, **kwargs):
            seen["mcp_tool"] = mcp_tool
            seen["kwargs"] = kwargs
            delegate = Mock()
            delegate.coroutine = fake_coroutine
            return delegate

        with patch(
            "langchain_mcp_adapters.tools.convert_mcp_tool_to_langchain_tool",
            side_effect=fake_convert,
        ):
            out = await tool.ainvoke({"q": "hi"})
            await tool.ainvoke({"q": "again"})

        assert out == "result text"
        assert seen["args"] == {"q": "again"}
        assert seen["mcp_tool"].name == "t" and seen["mcp_tool"].inputSchema == SCHEMA
        assert seen["kwargs"]["connection"] is connection
        assert seen["kwargs"]["tool_interceptors"] == ["icpt"]
        assert seen["kwargs"]["server_name"] == "srv"
        assert tool._get_delegate() is tool._get_delegate(), "delegate is built once per tool"


# --------------------------------------------------------------------------------------
# Stateless tools/list ingest
# --------------------------------------------------------------------------------------

TOOLS_PAGE_1 = [
    {
        "name": "srv_plain",
        "description": "plain desc",
        "inputSchema": SCHEMA,
        "annotations": {"readOnlyHint": True},
        "_meta": {"k": "v"},
    },
    {
        "name": "srv_tricky",
        "description": 'has "tools": [ in the description',
        # A schema with a property literally named "tools" must not derail the scan.
        "inputSchema": {"type": "object", "properties": {"tools": {"type": "array", "items": {"type": "string"}}}},
        "outputSchema": {"type": "object"},
    },
    {"description": "no name → skipped", "inputSchema": SCHEMA},
]
TOOLS_PAGE_2 = [{"name": "srv_second_page", "inputSchema": {"type": "object"}}]


def _envelope(tools, cursor=None, *, id_="catalogue-tools-list"):
    result = {"tools": tools}
    if cursor:
        result["nextCursor"] = cursor
    return {"jsonrpc": "2.0", "id": id_, "result": result}


class TestScanner:
    def test_iter_array_at_walks_nested_path_and_keeps_siblings(self):
        text = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"nextCursor": "c2", "tools": [1, {"a": 1}], "x": 3}})
        siblings: dict = {}
        items = list(iter_array_at(text, ("result", "tools"), siblings, keep=frozenset({"nextCursor", "id"})))
        assert items == [1, {"a": 1}]
        assert siblings["1.nextCursor"] == "c2" and siblings["0.id"] == 1

    def test_iter_array_at_handles_empty_and_missing(self):
        assert list(iter_array_at('{"result": {"tools": []}}', ("result", "tools"))) == []
        assert list(iter_array_at('{"result": {"other": 1}}', ("result", "tools"))) == []
        with pytest.raises(ValueError):
            list(iter_array_at("[1,2]", ("result", "tools")))
        with pytest.raises(IndexError):
            list(iter_array_at('{"result": {"tools": [{"a": 1}', ("result", "tools")))  # truncated body


class TestStatelessIngest:
    def test_parse_reply_flattens_tools_and_reads_cursor(self):
        tools, cursor = parse_tools_list_reply("srv", json.dumps(_envelope(TOOLS_PAGE_1, cursor="p2")))
        assert [t.name for t in tools] == ["srv_plain", "srv_tricky"]
        assert cursor == "p2"
        assert tools[0].annotations == {"readOnlyHint": True} and tools[0].meta == {"k": "v"}
        assert tools[1].card.param_names == ("tools",)
        assert tools[1].output_schema_bytes is not None

    def test_parse_reply_tolerates_empty_or_odd_result_shapes(self):
        assert parse_tools_list_reply("srv", '{"jsonrpc": "2.0", "id": 1, "result": {}}') == ([], None)
        assert parse_tools_list_reply("srv", '{"result": {"tools": []}}') == ([], None)
        assert parse_tools_list_reply("srv", '{"result": {"tools": [], "nextCursor": ""}}') == ([], None)
        assert parse_tools_list_reply("srv", '{"other": {"deep": {"tools": [1]}}}') == ([], None)

    def test_parse_reply_maps_rpc_errors(self):
        with pytest.raises(StatelessListUnsupported):
            parse_tools_list_reply("srv", json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "Bad Request: No valid session ID"}}))
        with pytest.raises(StatelessListError):
            parse_tools_list_reply("srv", json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "upstream down"}}))
        with pytest.raises(ValueError):
            parse_tools_list_reply("srv", "{not json")

    @pytest.mark.asyncio
    async def test_fetch_stateless_json_reply_with_pagination(self):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            body = json.loads(request.content)
            assert body["method"] == "tools/list" and body["jsonrpc"] == "2.0"
            if body["params"].get("cursor") == "p2":
                return httpx.Response(200, json=_envelope(TOOLS_PAGE_2, id_=body["id"]))
            return httpx.Response(200, json=_envelope(TOOLS_PAGE_1, cursor="p2", id_=body["id"]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cat = await fetch_catalogue_stateless(
                client, url="https://gw/mcp?includeOnlyServerSlugs=srv", headers={"Authorization": "Bearer tok"}, server_slug="srv"
            )
        assert cat.source == "stateless"
        assert set(cat.tools) == {"srv_plain", "srv_tricky", "srv_second_page"}
        assert len(calls) == 2
        assert calls[0].headers["Authorization"] == "Bearer tok"
        assert calls[0].headers["Accept"] == "application/json, text/event-stream"
        assert str(calls[0].url) == "https://gw/mcp?includeOnlyServerSlugs=srv"

    @pytest.mark.asyncio
    async def test_fetch_stateless_sse_reply_skips_notifications(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            frames = [
                'data: {"jsonrpc":"2.0","method":"notifications/message","params":{"level":"info"}}',
                "data: " + json.dumps(_envelope(TOOLS_PAGE_1, id_=body["id"])),
            ]
            return httpx.Response(200, text="\n\n".join(frames) + "\n\n", headers={"content-type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cat = await fetch_catalogue_stateless(client, url="https://gw/mcp", headers=None, server_slug="srv")
        assert set(cat.tools) == {"srv_plain", "srv_tricky"}

    @pytest.mark.asyncio
    async def test_fetch_stateless_status_mapping(self):
        def handler(request: httpx.Request) -> httpx.Response:
            slug = request.url.params.get("includeOnlyServerSlugs")
            if slug == "refuses":
                return httpx.Response(400, text="Bad Request: No valid session ID provided")
            if slug == "denied":
                return httpx.Response(403, text="forbidden")
            if slug == "flaky":
                return httpx.Response(502, text="bad gateway")
            if slug == "moved":
                return httpx.Response(307, headers={"location": "https://gw/mcp/"})
            return httpx.Response(200, text="{not json")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for slug, exc in (
                ("refuses", StatelessListUnsupported),
                ("denied", StatelessListError),
                ("flaky", StatelessListError),
                ("moved", StatelessListError),  # redirect not followed → per-server fallback, not a refusal
                ("garbage", StatelessListError),
            ):
                with pytest.raises(exc):
                    await fetch_catalogue_stateless(
                        client, url=f"https://gw/mcp?includeOnlyServerSlugs={slug}", headers=None, server_slug=slug
                    )


# --------------------------------------------------------------------------------------
# MCP fallback ingest
# --------------------------------------------------------------------------------------


class TestMcpIngest:
    @pytest.mark.asyncio
    async def test_paginated_tools_list_is_flattened(self):
        pages = [
            ListToolsResult(
                tools=[
                    MCPTool(
                        name="a",
                        description="A",
                        inputSchema=SCHEMA,
                        annotations=ToolAnnotations(readOnlyHint=True),
                        _meta={"x": 1},
                    )
                ],
                nextCursor="p2",
            ),
            ListToolsResult(
                tools=[MCPTool(name="b", inputSchema={"type": "object"})],
                nextCursor=None,
            ),
        ]
        session = Mock()
        session.list_tools = AsyncMock(side_effect=pages)

        @asynccontextmanager
        async def factory():
            yield session

        cat = await fetch_catalogue_mcp(factory, server_slug="srv")
        assert cat.source == "mcp"
        assert set(cat.tools) == {"a", "b"}
        assert cat.tools["a"].annotations == {"readOnlyHint": True}
        assert cat.tools["a"].meta == {"x": 1}
        assert cat.tools["b"].card.description == ""
        assert session.list_tools.await_args_list[1].kwargs == {"cursor": "p2"}


# --------------------------------------------------------------------------------------
# Endpoint capability memo
# --------------------------------------------------------------------------------------


def test_stateless_capability_memo_is_per_url():
    from agent_common.core.catalogue_ingest import reset_stateless_memo, set_stateless_supported, stateless_supported

    reset_stateless_memo()
    assert stateless_supported("https://gw/mcp") is None
    set_stateless_supported("https://gw/mcp", False)
    assert stateless_supported("https://gw/mcp") is False
    assert stateless_supported("https://other/mcp") is None
    reset_stateless_memo()
    assert stateless_supported("https://gw/mcp") is None


class TestFetchCatalogueStrategy:
    """stateless → per-URL memo → SDK fallback, shared by orchestrator discovery and agent-runner."""

    def setup_method(self):
        from agent_common.core.catalogue_ingest import reset_stateless_memo

        reset_stateless_memo()

    @staticmethod
    def _session_factory(*names: str):
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        from mcp.types import ListToolsResult, Tool as MCPTool

        session = MagicMock()
        session.list_tools = AsyncMock(
            return_value=ListToolsResult(tools=[MCPTool(name=n, inputSchema=SCHEMA) for n in names], nextCursor=None)
        )

        @asynccontextmanager
        async def _cm():
            yield session

        return _cm, session

    @pytest.mark.asyncio
    async def test_stateless_success_is_remembered_and_skips_the_sdk(self):
        from unittest.mock import patch

        from agent_common.core.catalogue_ingest import fetch_catalogue, stateless_supported

        factory, session = self._session_factory("via_sdk")
        with patch("agent_common.core.catalogue_ingest.fetch_catalogue_stateless", return_value=_catalogue("fast")) as fast:
            cat = await fetch_catalogue(
                server_slug="srv", url="https://gw/mcp", headers={"Authorization": "Bearer t"}, http_client=object(), session_factory=factory
            )
        assert cat.tools.keys() == {"fast"}
        assert fast.await_args.kwargs == {"url": "https://gw/mcp", "headers": {"Authorization": "Bearer t"}, "server_slug": "srv"}
        assert stateless_supported("https://gw/mcp") is True
        session.list_tools.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refusal_marks_url_and_falls_back_for_good(self):
        from unittest.mock import patch

        from agent_common.core.catalogue_ingest import fetch_catalogue, stateless_supported

        factory, session = self._session_factory("via_sdk")
        with patch(
            "agent_common.core.catalogue_ingest.fetch_catalogue_stateless", side_effect=StatelessListUnsupported("400")
        ) as fast:
            first = await fetch_catalogue(server_slug="srv", url="https://gw/mcp", headers=None, http_client=object(), session_factory=factory)
            second = await fetch_catalogue(server_slug="srv", url="https://gw/mcp", headers=None, http_client=object(), session_factory=factory)
        assert first.source == second.source == "mcp" and first.tools.keys() == {"via_sdk"}
        assert fast.await_count == 1, "never probed again once refused"
        assert stateless_supported("https://gw/mcp") is False
        assert session.list_tools.await_count == 2

    @pytest.mark.asyncio
    async def test_transient_error_falls_back_without_marking_the_url(self):
        from unittest.mock import patch

        from agent_common.core.catalogue_ingest import fetch_catalogue, stateless_supported

        factory, _ = self._session_factory("via_sdk")
        with patch("agent_common.core.catalogue_ingest.fetch_catalogue_stateless", side_effect=StatelessListError("502")):
            cat = await fetch_catalogue(server_slug="srv", url="https://gw/mcp", headers=None, http_client=object(), session_factory=factory)
        assert cat.source == "mcp"
        assert stateless_supported("https://gw/mcp") is None

    @pytest.mark.asyncio
    async def test_stateless_disabled_or_no_http_client_goes_straight_to_sdk(self):
        from unittest.mock import patch

        from agent_common.core.catalogue_ingest import fetch_catalogue

        factory, _ = self._session_factory("via_sdk")
        with patch("agent_common.core.catalogue_ingest.fetch_catalogue_stateless", side_effect=AssertionError("must not probe")):
            a = await fetch_catalogue(server_slug="srv", url="https://gw/mcp", headers=None, http_client=object(), session_factory=factory, stateless=False)
            b = await fetch_catalogue(server_slug="srv", url="https://gw/mcp", headers=None, http_client=None, session_factory=factory)
        assert a.source == b.source == "mcp"
