"""First-use consent gate for identity-scoped tools (Gate 3, ADR 0006).

The wrapper that hides and force-populates the reserved
``nannos__user_identity`` field lives in
``agent_common.core.identity_scoped`` (it must wrap tools at every discovery
point, including the dynamic sub-agent's own MCP rediscovery). This module
holds the orchestrator-only interactive piece: the HITL-shaped consent
interrupt raised before the first dispatch of an identity-scoped tool per
``(user, tool)`` pair.

Deliberately *not* gated via the per-user ``tool_bypass_rules`` HITL config:
that table encodes a user's own risk tolerance for action approval, a
different axis from identity disclosure (see ADR 0006).
"""

from __future__ import annotations

import logging
from typing import Any

from agent_common.core.identity_scoped import (
    NANNOS_USER_IDENTITY_FIELD,  # noqa: F401  (re-exported convenience)
    consent_state,
    identity_auth_required_payload,
    is_wrapped_identity_scoped_tool,
    record_consent,
)
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.human_in_the_loop import (
    ActionRequest,
    HITLRequest,
    ReviewConfig,
)
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import interrupt

logger = logging.getLogger(__name__)


def _display_server(tool: Any) -> str | None:
    """Server name for prompt wording only (never part of the consent key)."""
    metadata = getattr(tool, "metadata", None)
    if metadata and isinstance(metadata, dict):
        server_name = metadata.get("server_name")
        if server_name:
            return str(server_name)
    return None


def _blocked_tool_message(tool_call: dict[str, Any], *, rejected: bool) -> ToolMessage:
    return ToolMessage(
        content=identity_auth_required_payload(tool_call["name"], rejected=rejected),
        tool_call_id=tool_call["id"],
        name=tool_call["name"],
        status="error",
    )


class IdentityConsentMiddleware(AgentMiddleware):
    """First-use consent gate for identity-scoped tools (Gate 3, ADR 0006).

    Runs in ``aafter_model``: for each identity-scoped tool call in the last
    AI message,

    - with a remembered **grant** → pass through (the tool wrapper injects the
      email at execution time);
    - with a remembered **denial** → answer it with an artificial
      ``auth_required`` ToolMessage, no re-prompt;
    - with **no remembered answer** → raise a single HITL-shaped
      ``interrupt()`` asking once per *tool* (calls to the same tool share one
      consent question — consent is per ``(user, tool)``, not per call), then
      record the decision on the runtime context
      (``identity_consent_grants``) and queue it for durable persistence
      (``_pending_identity_consents``).

    Blocked calls are **kept** in the AIMessage and answered with an
    artificial ToolMessage — the tool_use/tool_result pairing must survive for
    the provider, and the agent loop skips tool calls that already have a
    ToolMessage (same semantics as the upstream HITL reject path).

    Timeout/no-answer keeps the graph interrupted — nothing executes, so the
    gate fails closed by construction. A malformed/unknown decision type
    blocks the call for this turn *without* remembering a denial.
    """

    async def aafter_model(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:  # type: ignore[override]
        messages = state.get("messages") if isinstance(state, dict) else None
        if not messages:
            return None
        last_ai_msg = next(
            (m for m in reversed(messages) if isinstance(m, AIMessage)), None
        )
        if not last_ai_msg or not last_ai_msg.tool_calls:
            return None

        context: Any = getattr(runtime, "context", None)
        tool_registry: dict[str, Any] = getattr(context, "tool_registry", None) or {}
        email = getattr(context, "email", None) if context else None

        # Partition identity-scoped calls by remembered consent state. Calls to
        # the same tool share one consent question (consent is per tool).
        denied_calls: list[dict[str, Any]] = []
        ask_calls_by_tool: dict[str, list[dict[str, Any]]] = {}
        for tool_call in last_ai_msg.tool_calls:
            tool = tool_registry.get(tool_call["name"])
            if tool is None or not is_wrapped_identity_scoped_tool(tool):
                continue
            consent = consent_state(context, tool_call["name"])
            if consent is True:
                continue
            if consent is False:
                denied_calls.append(tool_call)
            elif not email:
                # Without a verified email the wrapper fails closed regardless;
                # prompting (and recording a grant) would only create a
                # remembered grant for a permanently broken tool. Block for
                # this turn without remembering anything.
                logger.warning(
                    "Identity-scoped tool '%s' called without a verified email on the "
                    "runtime context — blocking without consent prompt",
                    tool_call["name"],
                )
                denied_calls.append(dict(tool_call, _identity_unasked=True))
            else:
                ask_calls_by_tool.setdefault(tool_call["name"], []).append(tool_call)

        if not denied_calls and not ask_calls_by_tool:
            return None

        artificial_tool_messages: list[ToolMessage] = [
            _blocked_tool_message(tc, rejected=not tc.get("_identity_unasked"))
            for tc in denied_calls
        ]

        if ask_calls_by_tool:
            ask_tools = list(ask_calls_by_tool)
            action_requests: list[ActionRequest] = []
            review_configs: list[ReviewConfig] = []
            for tool_name in ask_tools:
                first_call = ask_calls_by_tool[tool_name][0]
                tool = tool_registry.get(tool_name)
                server = _display_server(tool)
                origin = f" ({server})" if server else ""
                description = (
                    f"First use of '{tool_name}'{origin}: this tool will receive your "
                    f"verified email address ({email}) to scope its access to your own records "
                    "on the target system. Your answer is remembered for this tool. "
                    "Declining blocks only this tool, not the whole integration."
                )
                action_requests.append(
                    ActionRequest(
                        name=tool_name,
                        # Display-only; the reserved field is injected at execution
                        # time and is never part of the model-visible args. The
                        # metadata rides in the `_risk_metadata` envelope all HITL
                        # clients already hide, discriminated by `source`.
                        args={
                            **first_call.get("args", {}),
                            "_call_id": first_call["id"],
                            "_risk_metadata": {
                                "source": "identity_consent",
                                "tool_name": tool_name,
                                "server_slug": server or "_self",
                            },
                        },
                        description=description,
                    )
                )
                review_configs.append(
                    ReviewConfig(
                        action_name=tool_name, allowed_decisions=["approve", "reject"]
                    )
                )

            hitl_request = HITLRequest(
                action_requests=action_requests, review_configs=review_configs
            )
            decisions = interrupt(hitl_request)["decisions"]
            if len(decisions) != len(ask_tools):
                msg = (
                    f"Number of consent decisions ({len(decisions)}) does not match "
                    f"number of identity-scoped tools awaiting consent ({len(ask_tools)})."
                )
                raise ValueError(msg)

            for tool_name, decision in zip(ask_tools, decisions):
                decision_type = (
                    decision.get("type") if isinstance(decision, dict) else None
                )
                if decision_type == "approve":
                    record_consent(context, tool_name, granted=True)
                    continue
                # Remember only explicit rejections; a malformed/unknown
                # decision — or the executor's synthesized `_defaulted` safe
                # reject for a missing/stale call_id — blocks this turn
                # without recording a denial, and the payload must not claim
                # a denial exists.
                explicit_reject = decision_type == "reject" and not decision.get(
                    "_defaulted"
                )
                if explicit_reject:
                    record_consent(context, tool_name, granted=False)
                artificial_tool_messages.extend(
                    _blocked_tool_message(tc, rejected=explicit_reject)
                    for tc in ask_calls_by_tool[tool_name]
                )

        if not artificial_tool_messages:
            return None
        # Tool calls are never removed: each blocked call stays paired with its
        # artificial ToolMessage, and the agent loop skips answered calls.
        return {"messages": artificial_tool_messages}
