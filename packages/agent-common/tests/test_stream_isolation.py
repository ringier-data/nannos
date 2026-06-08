"""Sub-agent stream isolation: a sub-agent's token stream must not leak into the
parent graph's ``messages`` stream.

A sub-agent graph invoked inside a parent node inherits the parent's langgraph
``StreamMessagesHandler`` via the runnable-config contextvar (it is an
*inheritable* callback). Without isolation, the sub-agent's LLM token / tool-call
chunks fire the parent's handler too and surface on the parent's ``messages``
stream stamped with the *parent's* ``thread_id`` — leaking unattributed sub-agent
activity into the orchestrator (e.g. an unprefixed ``"Using eval…"``).

``isolate_parent_stream_context()`` removes only that inherited handler for the
sub-agent invocation, stopping the leak while preserving the sub-agent's own
stream and all other handlers (tracers, cost trackers).
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterator, Optional, TypedDict

from langchain_core.callbacks import AsyncCallbackHandler, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import StructuredTool
from langchain.agents.factory import create_agent
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from agent_common.core.graph_utils import denest_parent_pregel_context, isolate_parent_stream_context


class _StreamingModel(BaseChatModel):
    """Fake model that streams a scripted AIMessage via ``_stream`` (token path)."""

    scripts: deque = deque()

    @property
    def _llm_type(self) -> str:
        return "streaming-scripted"

    def bind_tools(self, tools: list, **kwargs: Any) -> "_StreamingModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self.scripts.popleft())])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        msg = self.scripts.popleft()
        if msg.tool_calls:
            tc = msg.tool_calls[0]
            chunk = AIMessageChunk(
                content="",
                tool_call_chunks=[{"name": tc["name"], "args": "{}", "id": tc["id"], "index": 0}],
            )
        else:
            chunk = AIMessageChunk(content=msg.content)
        gen = ChatGenerationChunk(message=chunk)
        if run_manager:
            run_manager.on_llm_new_token("", chunk=gen)
        yield gen


def _build_inner() -> Any:
    model = _StreamingModel()
    model.scripts = deque(
        [
            AIMessage(content="", id="ai-1", tool_calls=[{"id": "c1", "name": "eval", "args": {}}]),
            AIMessage(content="SUBAGENT_CONTENT", id="ai-2"),
        ]
    )

    async def _eval(code: str = "") -> str:
        return "ok"

    tool = StructuredTool.from_function(coroutine=_eval, name="eval", description="repl")
    return create_agent(model=model, tools=[tool], checkpointer=InMemorySaver())


class _OuterState(TypedDict):
    done: bool


def _describe(chunk: Any) -> Optional[str]:
    if not isinstance(chunk, AIMessageChunk):
        return None
    if chunk.tool_call_chunks:
        return "tc:" + ",".join(str(t.get("name")) for t in chunk.tool_call_chunks)
    if chunk.content:
        return "content:" + str(chunk.content)
    return None


_INNER_CFG = {"configurable": {"thread_id": "ctx::dynamic-eval-agent", "checkpoint_ns": ""}}


async def _run(*, isolate: bool) -> tuple[list[str], list[str]]:
    """Run an outer graph whose node streams an inner agent; return (outer_leak, inner_seen)."""
    inner = _build_inner()
    inner_seen: list[str] = []

    async def _node(state: _OuterState) -> dict:
        import contextlib

        ctx = contextlib.ExitStack()
        ctx.enter_context(denest_parent_pregel_context())
        if isolate:
            ctx.enter_context(isolate_parent_stream_context())
        with ctx:
            async for part in inner.astream(
                {"messages": [HumanMessage("go")]},
                config=_INNER_CFG,
                stream_mode=["custom", "messages"],
                version="v2",
            ):
                if part["type"] == "messages":
                    d = _describe(part["data"][0])
                    if d:
                        inner_seen.append(d)
        return {"done": True}

    outer = StateGraph(_OuterState)
    outer.add_node("d", _node)
    outer.add_edge(START, "d")
    outer.add_edge("d", END)
    graph = outer.compile(checkpointer=InMemorySaver())

    leak: list[str] = []
    async for part in graph.astream(
        {"done": False},
        config={"configurable": {"thread_id": "orchestrator"}},
        stream_mode=["custom", "messages"],
        version="v2",
    ):
        if part["type"] == "messages":
            d = _describe(part["data"][0])
            if d:
                leak.append(d)
    return leak, inner_seen


async def test_subagent_stream_leaks_into_parent_without_isolation():
    """REGRESSION baseline: without isolation the sub-agent's tool-call + content
    chunks leak into the parent's ``messages`` stream (the production symptom)."""
    leak, inner_seen = await _run(isolate=False)
    assert "tc:eval" in leak, f"expected the eval tool-call leak; got {leak!r}"
    assert any(s.startswith("content:") for s in leak), f"expected content leak; got {leak!r}"
    # The sub-agent's own stream still captures its events either way.
    assert "tc:eval" in inner_seen


async def test_isolate_parent_stream_context_stops_the_leak():
    """With isolation the parent stream sees NOTHING from the sub-agent, while the
    sub-agent's own stream still captures its events for proper forwarding."""
    leak, inner_seen = await _run(isolate=True)
    assert leak == [], f"sub-agent events leaked into parent stream despite isolation: {leak!r}"
    assert "tc:eval" in inner_seen and any(s.startswith("content:") for s in inner_seen), (
        f"sub-agent's own stream lost its events: {inner_seen!r}"
    )


async def test_isolation_preserves_other_callbacks():
    """Isolation must remove ONLY the stream handler — tracers / cost handlers
    (other inheritable handlers) must still fire for the sub-agent."""
    fired: list[str] = []

    class _FakeTracer(AsyncCallbackHandler):
        async def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
            fired.append("start")

    inner = _build_inner()

    async def _node(state: _OuterState) -> dict:
        with denest_parent_pregel_context(), isolate_parent_stream_context():
            async for _ in inner.astream(
                {"messages": [HumanMessage("go")]},
                config={**_INNER_CFG, "callbacks": [_FakeTracer()]},
                stream_mode=["custom", "messages"],
                version="v2",
            ):
                pass
        return {"done": True}

    outer = StateGraph(_OuterState)
    outer.add_node("d", _node)
    outer.add_edge(START, "d")
    outer.add_edge("d", END)
    graph = outer.compile(checkpointer=InMemorySaver())
    async for _ in graph.astream(
        {"done": False},
        config={"configurable": {"thread_id": "orchestrator"}},
        stream_mode=["custom", "messages"],
        version="v2",
    ):
        pass

    assert fired, "non-stream handler (tracer/cost) was dropped by isolation"
