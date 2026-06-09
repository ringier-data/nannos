"""ToolsetSelectorMiddleware must never drop the PTC code interpreter (`eval`).

`eval` is a "base" tool (no server_name) so it survives Phase 1 server selection,
but Phase 2 tool-selection picks a bounded subset by relevance and can drop it.
With broaden_exposure, the GP agent's real tools are PTC-exposed and reachable
only via `eval`, so losing `eval` strands the model with no usable tools. The GP
agent therefore pins `eval` in `always_include`; this test verifies the
middleware honours that even when selection returns a subset without `eval`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool

from app.middleware import ToolsetSelectorMiddleware


def _tool(name: str) -> StructuredTool:
    async def _fn() -> str:
        return "ok"

    return StructuredTool.from_function(coroutine=_fn, name=name, description=name)


def _request(tools: list) -> SimpleNamespace:
    """Minimal ModelRequest stand-in supporting .tools/.messages/.override()."""
    req = SimpleNamespace(tools=tools, messages=[HumanMessage("do something")])

    def _override(**kwargs: Any) -> SimpleNamespace:
        new = SimpleNamespace(**{**req.__dict__})
        for k, v in kwargs.items():
            setattr(new, k, v)
        return new

    req.override = _override
    return req


@pytest.mark.asyncio
async def test_eval_survives_when_selection_drops_it(monkeypatch):
    eval_tool = _tool("eval")
    other = _tool("some_mcp_tool")
    selected_only = _tool("selected_tool")
    request = _request([eval_tool, other, selected_only])

    mw = ToolsetSelectorMiddleware(always_include=["eval"])

    # Simulate Phase 1/2 selection returning a subset WITHOUT eval.
    async def _fake_select(all_tools, messages):
        return [selected_only]

    monkeypatch.setattr(mw, "_select_tools", _fake_select)
    mw.clear_cache()

    captured: dict[str, list] = {}

    async def _handler(req):
        captured["tools"] = list(req.tools)
        return "RESPONSE"

    result = await mw.awrap_model_call(request, _handler)

    assert result == "RESPONSE"
    names = {t.name for t in captured["tools"]}
    assert "eval" in names, f"eval was dropped despite always_include; got {names}"
    assert "selected_tool" in names


@pytest.mark.asyncio
async def test_no_tools_passes_through(monkeypatch):
    """Empty tool list short-circuits without selection."""
    mw = ToolsetSelectorMiddleware(always_include=["eval"])
    mw.clear_cache()
    request = _request([])

    async def _handler(req):
        return "EMPTY_OK"

    assert await mw.awrap_model_call(request, _handler) == "EMPTY_OK"
