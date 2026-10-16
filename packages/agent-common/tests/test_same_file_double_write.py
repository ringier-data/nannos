"""Graph-level regression test: two mutations of the same file in one model step.

Since deepagents 0.7.16, ``FilesystemMiddleware.(a)wrap_tool_call`` refuses a
``write_file`` / ``edit_file`` / ``delete`` call whose (normalised) ``file_path`` was
already targeted by an *earlier* mutation call on the same ``AIMessage``: the first call
runs, every later one is answered with an error ``ToolMessage`` ("parallel file
mutations to the same path are not allowed") without touching the backend.

These tests pin that behaviour through *our* stack (``build_common_middleware_stack`` on a
real ``create_agent`` graph with a ``StateBackend`` and an in-memory checkpointer), so an
upstream upgrade or one of our own middlewares (retry, loop detection, tool status, ...)
cannot silently change which write wins, drop the error, or orphan a tool result.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
from deepagents.backends.state import StateBackend
from deepagents.backends.utils import create_file_data
from langchain.agents.factory import create_agent
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from agent_common.core.graph_utils import build_common_middleware_stack

from .test_loop_detection_graph_e2e import assert_tool_pairing_valid

_PATH = "/notes.md"
_SEED = "seed line\n"
_FINAL_ANSWER = "Done."
_REFUSAL = "parallel file mutations to the same path are not allowed"


class _ScriptedModel(BaseChatModel):
    """Emits one scripted ``AIMessage`` per call, then a plain final answer.

    Records every message list it is handed so the test can assert the run went back to
    the model after the tool results.
    """

    model_name: str = "scripted-test-model"
    script: list = []
    seen_requests: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: list, **kwargs: Any) -> "_ScriptedModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen_requests.append(list(messages))
        assert len(self.seen_requests) < 10, "run never terminated"
        step = len(self.seen_requests) - 1
        message = self.script[step] if step < len(self.script) else AIMessage(content=_FINAL_ANSWER)
        return ChatResult(generations=[ChatGeneration(message=message)])


def _write(call_id: str, content: str, path: str = _PATH) -> dict:
    return {"name": "write_file", "args": {"file_path": path, "content": content}, "id": call_id}


def _edit(call_id: str, old: str, new: str, path: str = _PATH) -> dict:
    return {
        "name": "edit_file",
        "args": {"file_path": path, "old_string": old, "new_string": new},
        "id": call_id,
    }


# (case id, pre-existing files, the two calls, expected final contents per path)
_CASES = [
    pytest.param(
        {},
        [_write("call-1", "first write\n"), _write("call-2", "second write\n")],
        {_PATH: "first write\n"},
        id="write+write",
    ),
    pytest.param(
        {},
        [_write("call-1", "first write\n"), _edit("call-2", "first", "edited")],
        {_PATH: "first write\n"},
        id="write+edit",
    ),
    pytest.param(
        {_PATH: _SEED},
        [_edit("call-1", "seed", "first"), _edit("call-2", "line", "second")],
        {_PATH: "first line\n"},
        id="edit+edit",
    ),
]


def _build_agent(model: _ScriptedModel):
    # The real stack builder, exactly as our agents use it (plain FilesystemMiddleware,
    # PatchToolCalls, FailureHandlingToolRetry, loop detection, ...), on a StateBackend so
    # the written files land in graph state.
    middleware = build_common_middleware_stack(model=model, backend=StateBackend())
    return create_agent(model=model, tools=[], middleware=middleware, checkpointer=InMemorySaver())


def _file_contents(state: dict) -> dict[str, str]:
    contents = {}
    for path, data in (state.get("files") or {}).items():
        content = data["content"]
        # Older FileData stored a list of lines; normalise either shape.
        contents[path] = "\n".join(content) if isinstance(content, list) else content
    return contents


@pytest.mark.asyncio
@pytest.mark.parametrize(("seed_files", "calls", "expected_files"), _CASES)
async def test_second_mutation_of_same_path_in_one_step_is_refused(seed_files, calls, expected_files):
    model = _ScriptedModel(script=[AIMessage(content="", tool_calls=calls)], seen_requests=[])
    agent = _build_agent(model)
    config = {"configurable": {"thread_id": "same-file-double-write"}}

    inputs: dict = {"messages": [HumanMessage(content="update the notes")]}
    if seed_files:
        inputs["files"] = {path: create_file_data(content) for path, content in seed_files.items()}

    result = await agent.ainvoke(inputs, config)
    messages = result["messages"]

    # Exactly one mutation took effect: the FIRST call on the AIMessage.
    assert _file_contents(result) == expected_files

    # Every tool call has exactly one result, in order, with nothing orphaned.
    assert_tool_pairing_valid(messages)
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    assert set(results) == {"call-1", "call-2"}

    # The first call ran normally; the second was refused with deepagents' error.
    assert results["call-1"].status != "error", results["call-1"].content
    assert results["call-2"].status == "error"
    assert _REFUSAL in str(results["call-2"].content)
    assert results["call-2"].name == calls[1]["name"]

    # The run went back to the model with both results, and ended on its answer.
    assert len(model.seen_requests) == 2
    followup_results = [m for m in model.seen_requests[1] if isinstance(m, ToolMessage)]
    assert {m.tool_call_id for m in followup_results} == {"call-1", "call-2"}
    assert messages[-1].content == _FINAL_ANSWER


@pytest.mark.asyncio
async def test_mutations_of_different_paths_in_one_step_both_run():
    """Control: the refusal is per path, not per step."""
    calls = [_write("call-1", "a\n", "/a.md"), _write("call-2", "b\n", "/b.md")]
    model = _ScriptedModel(script=[AIMessage(content="", tool_calls=calls)], seen_requests=[])
    agent = _build_agent(model)

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="write both")]},
        {"configurable": {"thread_id": "different-paths"}},
    )

    assert _file_contents(result) == {"/a.md": "a\n", "/b.md": "b\n"}
    assert_tool_pairing_valid(result["messages"])
    tool_results = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [m.status for m in tool_results] == ["success", "success"]
    assert result["messages"][-1].content == _FINAL_ANSWER
