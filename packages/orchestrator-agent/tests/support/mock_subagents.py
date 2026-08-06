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
``LocalA2ARunnable`` already implements ``astream()`` — input validation, cost
instrumentation, checkpoint isolation, and the A2A metadata wrapping that makes
``a2a_tracking`` populate downstream. A ``MagicMock`` would skip all of it and
quietly pass while the real plumbing was broken. Subclassing means a mock
travels the same path a real local sub-agent does; only the "think about it"
step is replaced by a canned reply.

No network, no model, no credentials. The real tier swaps these for live agents
and keeps the same assertions.

Usage::

    slack = MockSubAgent("slack-client", "Sends Slack messages.", reply="sent")
    user_config.sub_agents = [slack.compiled()]
    ...
    assert slack.called_with_substring("@john.doe")
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_common.a2a.base import LocalA2ARunnable, SubAgentInput
from deepagents import CompiledSubAgent

DEFAULT_DESCRIPTION = "Test double sub-agent."


class MockSubAgent(LocalA2ARunnable):
    """A sub-agent that records what it was asked and returns a canned reply.

    Args:
        name: Registry key — this is the string the model puts in
            ``task(subagent_type=...)``. Use realistic names (``slack-client``,
            ``agent-runner``); the model picks from these and the description,
            so invented names make real-tier routing tests meaningless.
        description: Shown to the model in the ``task`` tool description, and
            what it routes on. Keep it as close to the real agent's wording as
            the test allows.
        reply: Response content — a string, or a callable taking the received
            instruction and returning one, for replies that depend on input.
        input_modes: Content types the agent accepts. ``["text"]`` keeps the
            orchestrator on the text-only path; include ``"image"`` to exercise
            multimodal forwarding (which invokes LLM-based file filtering, so
            that path is not credential-free).
        error: When set, the agent fails with this message instead of replying,
            for exercising sub-agent failure handling.
    """

    def __init__(
        self,
        name: str,
        description: str = DEFAULT_DESCRIPTION,
        *,
        reply: str | Callable[[str], str] = "ok",
        input_modes: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        super().__init__()
        self._name = name
        self._description = description
        self._reply = reply
        self._input_modes = input_modes or ["text"]
        self._error = error
        self.received: list[str] = []
        """Instructions this agent was handed, in order — the ``description``
        argument of each ``task`` call that reached it."""

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

    async def _process(self, input_data: SubAgentInput, config: dict[str, Any]) -> Any:
        instruction = self._extract_message_content(input_data)
        self.received.append(instruction)

        if self._error is not None:
            return self._build_error_response(self._error)

        # Reuse the orchestrator's context_id on follow-up calls so multi-turn
        # continuity is exercised rather than silently bypassed.
        context_id, task_id = self._extract_tracking_ids(input_data)
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
