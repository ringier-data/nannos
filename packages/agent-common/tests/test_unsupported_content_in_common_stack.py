"""Graph-level: graphs built on ``build_common_middleware_stack`` (dynamic sub-agents,
agent-runner, the console's embedded assistant) replace content the model cannot take.

``create_deep_agent`` adds ``UnsupportedContentMiddleware`` itself (the orchestrator);
plain ``create_agent`` graphs get it only from our stack. Without it, a Word file read
through ``read_file`` reaches a Chat Completions gateway client as an inline base64
``file`` block: the provider rejects the request, and since the block stays in the
thread, every later request in that conversation fails too.
"""

from __future__ import annotations

import base64

import pytest
from deepagents.backends.state import StateBackend
from deepagents.backends.utils import create_file_data
from deepagents.middleware import UnsupportedContentMiddleware
from langchain.agents.factory import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent_common.core.graph_utils import build_common_middleware_stack

from .test_same_file_double_write import _ScriptedModel

_DOCX = "/report.docx"
_PLACEHOLDER = "was not attached because this model does not support file content"


def _blocks_of_read_result(request: list) -> list:
    results = [m for m in request if isinstance(m, ToolMessage) and m.tool_call_id == "read-1"]
    assert len(results) == 1, "the read_file result must reach the model's next request"
    content = results[0].content
    return content if isinstance(content, list) else [{"type": "text", "text": content}]


async def _second_request(*, with_middleware: bool) -> list:
    model = _ScriptedModel(
        script=[AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": _DOCX}, "id": "read-1"}])],
        seen_requests=[],
    )
    middleware = build_common_middleware_stack(model=model, backend=StateBackend())
    if not with_middleware:
        middleware = [m for m in middleware if not isinstance(m, UnsupportedContentMiddleware)]
    agent = create_agent(model=model, tools=[], middleware=middleware, checkpointer=InMemorySaver())
    docx = create_file_data(base64.b64encode(b"PK\x03\x04 fake docx bytes").decode(), encoding="base64")

    await agent.ainvoke(
        {"messages": [HumanMessage(content="what does the report say?")], "files": {_DOCX: docx}},
        {"configurable": {"thread_id": f"unsupported-{with_middleware}"}},
    )
    assert len(model.seen_requests) == 2
    return model.seen_requests[1]


@pytest.mark.asyncio
async def test_a_word_file_read_reaches_the_model_as_a_placeholder():
    blocks = _blocks_of_read_result(await _second_request(with_middleware=True))

    assert not any(b.get("type") == "file" for b in blocks)
    assert any(_PLACEHOLDER in (b.get("text") or "") for b in blocks)


@pytest.mark.asyncio
async def test_control_without_the_middleware_the_raw_file_block_is_sent():
    blocks = _blocks_of_read_result(await _second_request(with_middleware=False))

    assert any(b.get("type") == "file" for b in blocks)
