"""Mock sub-agents that make orchestrator routing testable.

Naming follows AGENTS.md: "Mock A2A transport for sub-agent communication
tests". These are the A2A transport mocks for the orchestrator side.

The problem this solves
-----------------------
Sub-agents reach the orchestrator only through
``GraphRuntimeContext.subagent_registry``, populated at request time from
discovery. The integration fixtures stub discovery with ``MagicMock()`` and
hand ``UserConfig`` empty ``sub_agents``/``local_subagents``, so the registry is
empty, the ``task`` tool's ``subagent_type`` enum lists nothing, and there is
nothing for the model to route *to*. Every routing assertion is unfalsifiable
until something is registered.

Why subclass ``LocalA2ARunnable`` instead of patching
-----------------------------------------------------
``LocalA2ARunnable`` already implements ``astream()`` — the whole in-process A2A
exchange: message conversion, the request handler and task store, the executor,
cost instrumentation, checkpoint isolation, and the typed result that makes
``a2a_tracking`` populate downstream. A ``MagicMock`` would skip all of it and
quietly pass while the real plumbing was broken. Subclassing means a mock
travels the same path a real local sub-agent does; only the "think about it"
step is replaced by a canned reply.

No network, no model, no credentials. The real tier swaps these for live agents
and keeps the same assertions.

Usage::

    slack = MockSubAgent("slack-notifier", "Sends Slack messages.", reply="sent")
    user_config.sub_agents = [slack.compiled()]
    ...
    assert slack.called_with_substring("@john.doe")
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Callable
from typing import Any

from agent_common.a2a.base import LocalA2ARunnable, SubAgentInput
from agent_common.a2a.stream_events import StreamEvent, TaskUpdate
from deepagents import CompiledSubAgent
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, Interrupt

#: A valid xxh3_128 hexdigest — the format LangGraph uses for interrupt ids.
APPROVAL_INTERRUPT_ID = "45fda8478b2ef754419799e10992af06"

DEFAULT_DESCRIPTION = "Test double sub-agent."


class MockSubAgent(LocalA2ARunnable):
    """A sub-agent that records what it was asked and returns a canned reply.

    Args:
        name: Registry key — this is the string the model puts in
            ``task(subagent_type=...)``. The model picks from these and the
            description, so invented names make real-tier routing tests
            meaningless. Name it like a registry sub-agent (``slack-notifier``,
            ``revenue-analyst``), never after a client or a peer service:
            ``client-slack`` and ``agent-runner`` are not delegation targets.
        description: Shown to the model in the ``task`` tool description, and
            what it routes on. Keep it as close to the real agent's wording as
            the test allows.
        reply: Response content — a string, or a callable taking the received
            instruction and returning one, for replies that depend on input.
        input_modes: Content types the agent accepts. ``["text"]`` keeps the
            orchestrator on the text-only path; include ``"image"`` to exercise
            multimodal forwarding (which invokes LLM-based file filtering, so
            that path is not credential-free).
        error: When set, the agent fails with this message instead of replying —
            terminal state ``failed``.
        input_required: When set, the agent stops and asks for something instead
            of replying — terminal state ``input_required``. Distinct from
            ``error``: the work is not wrong, it is unfinished, and the
            orchestrator is supposed to relay the question rather than answer
            it. Mutually exclusive with ``error``.
        approval: When set, the agent's graph parks on a tool-approval interrupt
            for a tool of this name before replying — the shape a risk-gated
            tool produces. The task pauses on ``input_required`` with the
            interrupt on the wire; the orchestrator surfaces the approval card
            and the replayed delegation delivers the decision, which lands here
            as ``resumed_with`` (the id-keyed ``Command.resume`` the graph would
            see). Then the canned ``reply`` is returned.
    """

    def __init__(
        self,
        name: str,
        description: str = DEFAULT_DESCRIPTION,
        *,
        reply: str | Callable[[str], str] = "ok",
        input_modes: list[str] | None = None,
        error: str | None = None,
        input_required: str | None = None,
        approval: str | None = None,
    ) -> None:
        super().__init__()
        if error is not None and input_required is not None:
            raise ValueError(f"{name}: error and input_required are mutually exclusive terminal states")
        self._name = name
        self._description = description
        self._reply = reply
        self._input_modes = input_modes or ["text"]
        self._error = error
        self._input_required = input_required
        self._approval = approval
        self._pending: list[Interrupt] = []
        self._parked_ids: tuple[str | None, str | None] = (None, None)
        self.received: list[str] = []
        """Instructions this agent was handed, in order — the ``description``
        argument of each ``task`` call that reached it."""
        self.resumed_with: list[Any] = []
        """The ``Command.resume`` payloads delivered to a parked approval, in order."""

    # -- BaseA2ARunnable contract ------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    def get_supported_input_modes(self) -> list[str]:
        return list(self._input_modes)

    def get_checkpoint_ns(self, input_data: SubAgentInput) -> str:
        return self._name

    def get_sub_agent_identifier(self, input_data: SubAgentInput) -> str:
        return self._name

    async def aget_pending_interrupts(self, config: dict[str, Any]) -> list:
        return list(self._pending)

    async def _astream_impl(self, input_data: Any, config: dict[str, Any]) -> AsyncIterable[StreamEvent]:
        """Park on the approval when asked to; otherwise the plain ``_process`` reply."""
        if isinstance(input_data, Command):
            self.resumed_with.append(input_data.resume)
            self._pending = []
            context_id, task_id = self._parked_ids
            reply = self._reply(self.received[-1]) if callable(self._reply) else self._reply
            yield TaskUpdate(data=self._build_success_response(reply, context_id=context_id, task_id=task_id))
            return
        result = await self._process(input_data, config)
        if self._approval is not None and self._error is None and self._input_required is None:
            self._parked_ids = self._extract_tracking_ids(input_data)
            interrupt = Interrupt(
                value={
                    "action_requests": [
                        {
                            "name": self._approval,
                            "args": {"_call_id": f"{self._approval}:1"},
                            "description": f"Tool '{self._approval}' needs your approval",
                        }
                    ],
                    "review_configs": [{"action_name": self._approval, "allowed_decisions": ["approve", "reject"]}],
                },
                id=APPROVAL_INTERRUPT_ID,
            )
            self._pending = [interrupt]
            raise GraphInterrupt((interrupt,))
        yield TaskUpdate(data=result)

    async def _process(self, input_data: SubAgentInput, config: dict[str, Any]) -> Any:
        instruction = self._extract_message_content(input_data)
        self.received.append(instruction)

        # Reuse the orchestrator's context_id on follow-up calls so multi-turn
        # continuity is exercised rather than silently bypassed. Non-success
        # outcomes carry the ids too: `a2a_tracking` is keyed off them, and a
        # failure the tracking channel cannot correlate is a failure no
        # multi-turn assertion can follow.
        context_id, task_id = self._extract_tracking_ids(input_data)

        # Every branch goes through the production builders in
        # `agent_common.a2a.base` rather than hand-rolling a response dict —
        # that is what keeps the double's terminal states identical in shape to
        # a real sub-agent's.
        if self._error is not None:
            return self._build_error_response(self._error, context_id=context_id, task_id=task_id)
        if self._input_required is not None:
            return self._build_input_required_response(self._input_required, context_id=context_id, task_id=task_id)

        reply = self._reply(instruction) if callable(self._reply) else self._reply
        return self._build_success_response(reply, context_id=context_id, task_id=task_id)

    # -- Test conveniences --------------------------------------------------

    def compiled(self) -> CompiledSubAgent:
        """Wrap as the ``CompiledSubAgent`` dict the registry expects.

        ``build_runtime_context`` copies each entry of ``UserConfig.sub_agents``
        into ``subagent_registry`` keyed by ``name``, so this is all it takes to
        make the agent routable.
        """
        return CompiledSubAgent(name=self._name, description=self._description, runnable=self)  # type: ignore[typeddict-item]

    @property
    def called(self) -> bool:
        return bool(self.received)

    @property
    def call_count(self) -> int:
        return len(self.received)

    def called_with_substring(self, needle: str) -> bool:
        """Whether any instruction contained *needle*, case-insensitively.

        Substring matching is deliberate: the model phrases the hand-off freely,
        so asserting on an exact instruction makes tests fail on rewording
        rather than on behaviour.
        """
        return any(needle.lower() in got.lower() for got in self.received)


def mock_subagents(*agents: MockSubAgent) -> list[CompiledSubAgent]:
    """Compile several mocks into the list ``UserConfig.sub_agents`` wants."""
    return [agent.compiled() for agent in agents]
