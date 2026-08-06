"""A chat model that returns pre-written messages instead of calling an LLM.

Lets a test drive a *real* orchestrator graph — real middleware stack, real
dispatch, real state channels — with a deterministic model, no gateway and no
credentials. The model is the only thing replaced; everything else is
production code.

None of langchain's stock fakes work here: ``FakeMessagesListChatModel`` and
friends inherit ``BaseChatModel.bind_tools``, which raises ``NotImplementedError``.
The orchestrator graph always binds tools, so a turn dies before it starts.

Binding is also worth observing in its own right, so ``bind_tools`` records
what it was handed. That is how a test checks the ``task`` tool actually
reached the model carrying the right ``subagent_type`` enum — the enum is built
per-request from ``subagent_registry`` in ``DynamicToolDispatchMiddleware``, and
nothing else can see it.

Usage::

    model = ScriptedChatModel(responses=[
        task_call("slack-client", "post to @john"),
        final_response("Done."),
    ])
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr


class ScriptedChatModel(BaseChatModel):
    """Replays ``responses`` in order, one per model call.

    Running out of responses raises rather than looping or returning empty:
    a turn that calls the model more often than the script expects means the
    graph took a path the test did not describe, and silently repeating the
    last message would hide it.
    """

    responses: list[AIMessage] = Field(default_factory=list)

    _cursor: int = PrivateAttr(default=0)
    _bound_tools: list[list[Any]] = PrivateAttr(default_factory=list)
    _tool_choices: list[Any] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self._cursor >= len(self.responses):
            raise AssertionError(
                f"ScriptedChatModel exhausted: the graph made {self._cursor + 1} model calls "
                f"but only {len(self.responses)} responses were scripted. The turn took an "
                f"unexpected path — add the missing response or fix the scenario."
            )
        message = self.responses[self._cursor]
        self._cursor += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(
        self,
        tools: Any,
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> BaseChatModel:
        """Record the binding and stay invocable.

        Returns ``self`` rather than a bound wrapper — the script decides what
        comes back regardless of what was bound, and keeping one object means
        the recordings below stay reachable from the test.
        """
        self._bound_tools.append(list(tools))
        self._tool_choices.append(tool_choice)
        return self

    # -- Recordings ---------------------------------------------------------

    @property
    def call_count(self) -> int:
        """How many times the graph asked the model to generate."""
        return self._cursor

    @property
    def last_bound_tools(self) -> list[Any]:
        """Tools handed to the most recent ``bind_tools`` call."""
        return self._bound_tools[-1] if self._bound_tools else []

    def bound_tool_names(self) -> list[str]:
        """Names of the tools bound most recently.

        Tools arrive in mixed shapes — ``BaseTool`` instances, and OpenAI-format
        dicts for anything injected at runtime (which is how the orchestrator
        bypasses ToolNode validation), so both are unwrapped here.
        """
        return [name for name in (_tool_name(tool) for tool in self.last_bound_tools) if name]

    def bound_tool(self, name: str) -> Any | None:
        """The most recently bound tool called *name*, or None."""
        for tool in self.last_bound_tools:
            if _tool_name(tool) == name:
                return tool
        return None


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        # OpenAI function format: {"type": "function", "function": {"name": ...}}
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name"):
            return str(function["name"])
        return str(tool["name"]) if tool.get("name") else None
    name = getattr(tool, "name", None)
    return str(name) if name else None


def subagent_enum(task_tool: Any) -> list[str]:
    """Pull the ``subagent_type`` enum out of a bound ``task`` tool.

    ``DynamicToolDispatchMiddleware`` rewrites the tool per request to advertise
    exactly the sub-agents in ``subagent_registry``; this is what the model sees
    and the only place that list is observable.
    """
    if not isinstance(task_tool, dict):
        return []
    properties = task_tool.get("function", {}).get("parameters", {}).get("properties", {})
    return list(properties.get("subagent_type", {}).get("enum", []) or [])
