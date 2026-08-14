"""First-use consent gate for identity-scoped tools (Gate 3, ADR 0006).

The wrapper that hides and force-populates the reserved
``nannos__user_identity`` field lives in
``agent_common.core.identity_scoped`` (it must wrap tools at every discovery
point, including the dynamic sub-agent's own MCP rediscovery). This module
holds the interactive piece: the HITL-shaped consent interrupt raised before
the first dispatch of an identity-scoped tool per ``(user, tool)`` pair.

It runs on the orchestrator graph *and* on every dynamic sub-agent graph.
Sub-agents dispatch identity-scoped tools on their own (a `task(...)`
delegation is where most of them actually get called), and their interrupts
propagate to the executor exactly like the risk-HITL ones do — without the
gate there, the wrapper's fail-closed path is the only thing left and the
sub-agent just tells the user "permission is required" with no way to grant
it.

Deliberately *not* gated via the per-user ``tool_bypass_rules`` HITL config:
that table encodes a user's own risk tolerance for action approval, a
different axis from identity disclosure (see ADR 0006).
"""

from __future__ import annotations

import logging
from typing import Any

from agent_common.core.identity_scoped import (
    NANNOS_USER_IDENTITY_FIELD,  # noqa: F401  (re-exported convenience)
    SELF_SERVER_SLUG,
    consent_state,
    identity_server_slug,
    identity_auth_required_payload,
    is_wrapped_identity_scoped_tool,
    record_consent,
)
from agent_common.core.tool_catalog import CATALOG_CALL_TOOL_NAME
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


def _blocked_tool_message(
    tool_call: dict[str, Any], tool_name: str, server_slug: str, *, rejected: bool
) -> ToolMessage:
    """Artificial answer for a blocked call.

    ``tool_name`` is the *gated* tool (which differs from the call's own name
    when the catalog's ``call_tool`` dispatches it), while the ToolMessage keeps
    the call's name/id so the provider's tool_use/tool_result pairing holds.
    """
    return ToolMessage(
        content=identity_auth_required_payload(
            tool_name, rejected=rejected, server_slug=server_slug
        ),
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
      ``interrupt()`` asking once per *MCP server* (every call to any tool of
      that integration shares one consent question — consent is per
      ``(user, server)``, not per tool and not per call), then
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

    def __init__(self, tool_registry: dict[str, Any] | None = None) -> None:
        """Args:
        tool_registry: Optional ``{tool_name: BaseTool}`` map used to decide
            whether a called tool is identity-scoped. The orchestrator leaves
            this unset — its runtime context carries the whole registry. Sub-agent
            graphs bind their tools directly and their runtime context has no
            registry (except GP's catalog), so they pass their resolved tools
            here; a tool absent from both lookups is simply not gated, which is
            safe: the wrapper still fails closed at execution time.
        """
        super().__init__()
        self._tool_registry = tool_registry or {}

    def _resolve_tool(self, context: Any, tool_name: str) -> Any:
        """Find the tool object for ``tool_name`` (runtime context first, then the injected map)."""
        registry: dict[str, Any] = getattr(context, "tool_registry", None) or {}
        return registry.get(tool_name) or self._tool_registry.get(tool_name)

    def _gated_tool_name(self, tool_call: dict[str, Any]) -> str:
        """The tool this call actually dispatches.

        In catalog mode (PTC off) the model reaches most tools through
        ``call_tool({name, args})``; ToolCatalogMiddleware only rewrites the call
        to the real tool at ``wrap_tool_call`` time, i.e. after this hook. Reading
        the inner name here keeps identity-scoped tools gated on that path instead
        of leaving them to the wrapper's dead-end fail-closed.
        """
        if tool_call["name"] != CATALOG_CALL_TOOL_NAME:
            return tool_call["name"]
        inner = (tool_call.get("args") or {}).get("name")
        return inner if isinstance(inner, str) and inner else tool_call["name"]

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
        email = getattr(context, "email", None) if context else None

        # Partition identity-scoped calls by remembered consent state. Calls to
        # any tool of the same MCP server share one consent question (consent is
        # per (user, server) — one answer covers the whole integration).
        denied_calls: list[dict[str, Any]] = []
        ask_calls_by_server: dict[str, list[dict[str, Any]]] = {}
        for tool_call in last_ai_msg.tool_calls:
            gated_name = self._gated_tool_name(tool_call)
            tool = self._resolve_tool(context, gated_name)
            if tool is None or not is_wrapped_identity_scoped_tool(tool):
                continue
            server_slug = identity_server_slug(gated_name, context, tool)
            tool_call = dict(
                tool_call,
                _identity_tool_name=gated_name,
                _identity_server_slug=server_slug,
            )
            consent = consent_state(context, server_slug)
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
                    gated_name,
                )
                denied_calls.append(dict(tool_call, _identity_unasked=True))
            else:
                ask_calls_by_server.setdefault(server_slug, []).append(tool_call)

        if not denied_calls and not ask_calls_by_server:
            return None

        artificial_tool_messages: list[ToolMessage] = [
            _blocked_tool_message(
                tc,
                tc["_identity_tool_name"],
                tc["_identity_server_slug"],
                rejected=not tc.get("_identity_unasked"),
            )
            for tc in denied_calls
        ]

        if ask_calls_by_server:
            ask_servers = list(ask_calls_by_server)
            action_requests: list[ActionRequest] = []
            review_configs: list[ReviewConfig] = []
            for server_slug in ask_servers:
                calls = ask_calls_by_server[server_slug]
                first_call = calls[0]
                triggering_tool = first_call["_identity_tool_name"]
                integration = (
                    f"the '{server_slug}' integration"
                    if server_slug != SELF_SERVER_SLUG
                    else f"the integration behind '{triggering_tool}'"
                )
                description = (
                    f"First use of {integration} (via '{triggering_tool}'): its "
                    f"identity-scoped tools will receive your verified email address "
                    f"({email}) to scope their access to your own records on the target "
                    "system. Your answer is remembered for this integration. Declining "
                    "blocks only its identity-scoped tools, not other integrations."
                )
                action_requests.append(
                    ActionRequest(
                        # The consent subject is the integration, not one call, so the
                        # action is named after the server; ReviewConfig matches on it.
                        name=server_slug,
                        # Display-only; the reserved field is injected at execution
                        # time and is never part of the model-visible args. The
                        # metadata rides in the `_risk_metadata` envelope all HITL
                        # clients already hide, discriminated by `source`.
                        args={
                            **first_call.get("args", {}),
                            "_call_id": first_call["id"],
                            "_risk_metadata": {
                                "source": "identity_consent",
                                "tool_name": triggering_tool,
                                "server_slug": server_slug,
                            },
                        },
                        description=description,
                    )
                )
                review_configs.append(
                    ReviewConfig(
                        action_name=server_slug, allowed_decisions=["approve", "reject"]
                    )
                )

            hitl_request = HITLRequest(
                action_requests=action_requests, review_configs=review_configs
            )
            decisions = interrupt(hitl_request)["decisions"]
            if len(decisions) != len(ask_servers):
                msg = (
                    f"Number of consent decisions ({len(decisions)}) does not match "
                    f"number of integrations awaiting identity consent ({len(ask_servers)})."
                )
                raise ValueError(msg)

            for server_slug, decision in zip(ask_servers, decisions):
                decision_type = (
                    decision.get("type") if isinstance(decision, dict) else None
                )
                if decision_type == "approve":
                    record_consent(context, server_slug, granted=True)
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
                    record_consent(context, server_slug, granted=False)
                artificial_tool_messages.extend(
                    _blocked_tool_message(
                        tc,
                        tc["_identity_tool_name"],
                        server_slug,
                        rejected=explicit_reject,
                    )
                    for tc in ask_calls_by_server[server_slug]
                )

        if not artificial_tool_messages:
            return None
        # Tool calls are never removed: each blocked call stays paired with its
        # artificial ToolMessage, and the agent loop skips answered calls.
        return {"messages": artificial_tool_messages}
