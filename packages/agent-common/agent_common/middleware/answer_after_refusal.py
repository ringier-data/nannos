"""After a refused or blocked call, the model's next step can only be its answer.

A refusal (the user clicked Reject or said no, or an identical retry of a refused call
was answered ``NOT RUN``) and a loop block (``BLOCKED: …``) both tell the model to stop
and tell the user. A model that must call a tool on every step — the forced
``tool_choice`` of the ``ToolStrategy`` path (Haiku, GPT) — cannot do that by writing
text, and in QA did not pick the response tool either: it re-sent the refused save six
times, or kept reading the page, until loop detection force-stopped the run with no
answer at all. Offering only the response tool on that step leaves the answer as the
one possible move, whatever the model or strategy:

- ``ToolStrategy``: the request keeps no tools of its own, and langchain binds the
  structured-output tool alone, still forced.
- Bind-as-tool (``FinalResponseSchema`` / ``SubAgentResponseSchema`` among the tools):
  only that tool is kept.

Offered only the response tool, Claude Sonnet 4.6 still emitted the refused call again,
copied from its own history: so the step also says so in the system prompt, a reply that
calls a tool it was not offered is asked for once more, and if it still does, the answer
is written for it — "that was not done" — in the response shape its strategy expects.

It must sit innermost: the orchestrator injects its tool registry in an outer
``wrap_model_call``, and an outer filter would be overridden.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from agent_common.a2a.structured_response import REFUSED_REPLY, STOPPED_REPLY
from agent_common.core.hitl_resume import SKIPPED_AUTH_LEAD
from agent_common.core.turn_stops import BLOCKED_LEAD, REFUSED_AGAIN_LEAD
from agent_common.middleware.utils import VOLATILE_CONTEXT_KEY, append_to_system_message

logger = logging.getLogger(__name__)

#: The response tools a turn ends with, in either structured-output strategy.
RESPONSE_TOOL_NAMES: frozenset[str] = frozenset({"FinalResponseSchema", "SubAgentResponseSchema"})


def _text(content: Any) -> str:
    """A message's text, also when it is a list of blocks (prompt caching tags the last
    message as ``[{"type": "text", "text": …, "cache_control": …}]``)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return ""


#: Results that leave nothing to do but answer: the user already refused this exact call,
#: or skipped the authorization it needs. A clicked Reject or a typed "no, do X instead" is
#: NOT here: it invites the agent to do something else, and narrowing would forbid it.
REFUSAL_STOP_LEADS: tuple[str, ...] = (REFUSED_AGAIN_LEAD, SKIPPED_AUTH_LEAD)


def _users(message: BaseMessage) -> bool:
    """A message from the user — not the per-call page context a middleware appends as one
    (``append_volatile_context_message``), which sits after the tool results every step."""
    return isinstance(message, HumanMessage) and not message.additional_kwargs.get(VOLATILE_CONTEXT_KEY)


def stop_reason(messages: Sequence[BaseMessage]) -> str | None:
    """``"refused"`` or ``"blocked"`` when the step after these messages must be the answer.

    Only the results of the last step count, and only within the user's ask: a user
    message after them (a new turn, or a steer) is a new ask. A loop block counts the
    second time this turn — the first one says "try a different approach", and that must
    stay possible.
    """
    last_ai = next((i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], AIMessage)), None)
    if last_ai is None or any(_users(m) for m in messages[last_ai + 1 :]):
        return None
    results = [_text(m.content) for m in messages[last_ai + 1 :] if isinstance(m, ToolMessage)]
    if any(text.startswith(REFUSAL_STOP_LEADS) for text in results):
        return "refused"
    if any(text.startswith(BLOCKED_LEAD) for text in results):
        turn_start = next((i for i in range(last_ai, -1, -1) if _users(messages[i])), -1)
        earlier = [
            m
            for m in messages[turn_start + 1 : last_ai]
            if isinstance(m, ToolMessage) and _text(m.content).startswith(BLOCKED_LEAD)
        ]
        return "blocked" if earlier else None
    return None


def must_answer(messages: Sequence[BaseMessage]) -> bool:
    """Whether the next step may only be the answer (see :func:`stop_reason`)."""
    return stop_reason(messages) is not None


def _tool_name(tool: Any) -> str | None:
    if isinstance(tool, dict):
        return (tool.get("function") or {}).get("name") or tool.get("name")
    return getattr(tool, "name", None)


#: Said on the narrowed step: offered only the response tool, Claude still re-sent the
#: refused call, copying its own history, so the rule is spelled out as well.
ANSWER_NOW = (
    "The user already refused this call, or it kept being blocked. Do not call any other tool, and "
    "do not repeat that call: answer the user now with {tools}: say plainly what was not done, and "
    "ask what they want instead if that is unclear."
)


def _answer(request: ModelRequest, text: str) -> ModelResponse:
    """The step's answer, written for the model in whichever shape its strategy expects."""
    args = {"task_state": "completed", "message": text}
    call_id = f"answer-{uuid.uuid4().hex[:12]}"
    strategy = request.response_format
    if isinstance(strategy, ToolStrategy) and strategy.schema_specs:
        # As langchain's own structured-output branch would return it.
        spec = strategy.schema_specs[0]
        structured = spec.schema(**args)
        call = {"name": spec.name, "args": args, "id": call_id, "type": "tool_call"}
        return ModelResponse(
            result=[
                AIMessage(content="", tool_calls=[call]),
                ToolMessage(
                    content=strategy.tool_message_content or f"Returning structured response: {structured}",
                    tool_call_id=call_id,
                    name=spec.name,
                ),
            ],
            structured_response=structured,
        )
    name = next((n for tool in request.tools if (n := _tool_name(tool)) in RESPONSE_TOOL_NAMES), None)
    if name is None:
        return ModelResponse(result=[AIMessage(content=text)])
    return ModelResponse(
        result=[AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])]
    )


def _off_list_calls(response: ModelResponse, allowed: set[str]) -> list[str]:
    return [
        call.get("name", "")
        for message in response.result
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call.get("name") not in allowed
    ]


class AnswerAfterRefusalMiddleware(AgentMiddleware):
    """See the module docstring."""

    def _narrowed(self, request: ModelRequest) -> tuple[ModelRequest, set[str]] | None:
        if not must_answer(request.messages):
            return None
        kept = [tool for tool in request.tools if _tool_name(tool) in RESPONSE_TOOL_NAMES]
        named = sorted({name for tool in kept if (name := _tool_name(tool))}) or ["your final response tool"]
        logger.info(
            "A call was refused or blocked; offering only the response tool on this step (%d of %d tools kept)",
            len(kept),
            len(request.tools),
        )
        system = append_to_system_message(request.system_message, ANSWER_NOW.format(tools=", ".join(named)))
        # ``ToolStrategy`` binds its structured-output tool itself; any response tool name is allowed.
        return request.override(tools=kept, system_message=system), set(RESPONSE_TOOL_NAMES)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        narrowed = self._narrowed(request)
        if narrowed is None:
            return handler(request)
        request, allowed = narrowed
        response = handler(request)
        if _off_list_calls(response, allowed):
            logger.info("The model called a tool it was not offered after a refusal; asking once more")
            response = handler(request)
        if _off_list_calls(response, allowed):
            logger.warning("Still calling a tool it was not offered after a refusal; answering for it")
            reply = STOPPED_REPLY if stop_reason(request.messages) == "blocked" else REFUSED_REPLY
            response = _answer(request, reply)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        narrowed = self._narrowed(request)
        if narrowed is None:
            return await handler(request)
        request, allowed = narrowed
        response = await handler(request)
        if _off_list_calls(response, allowed):
            logger.info("The model called a tool it was not offered after a refusal; asking once more")
            response = await handler(request)
        if _off_list_calls(response, allowed):
            logger.warning("Still calling a tool it was not offered after a refusal; answering for it")
            reply = STOPPED_REPLY if stop_reason(request.messages) == "blocked" else REFUSED_REPLY
            response = _answer(request, reply)
        return response
