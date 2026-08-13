"""Identity-scoped tool support (ADR 0006, shared-credential tier).

An *Identity-scoped tool* is an MCP tool whose input schema declares the
reserved field ``nannos__user_identity``. Two cooperating pieces make that
channel safe to use:

- :func:`wrap_identity_scoped_tool` — wraps the tool so the reserved field is
  hidden from every model-facing schema render (regular dispatch, toolset
  selector, PTC signature render) and force-populated from the verified
  ``GraphRuntimeContext.email`` at execution time, overwriting any
  model-supplied value (same "no LLM round-trip" principle as
  ``pending_file_blocks``). The payload is the email only. Execution fails
  **closed**: without a remembered consent grant the wrapper returns an
  ``auth_required`` error payload instead of calling the tool — on every
  execution path, including PTC ``eval`` and sub-agents, where no interrupt
  is possible.
- :class:`IdentityConsentMiddleware` — the first-use consent gate (Gate 3).
  Before the first dispatch of an identity-scoped tool for a ``(user, tool)``
  pair it raises a HITL-shaped ``interrupt()`` asking for explicit consent and
  remembers the answer (in-session on the runtime context, durably via the
  console backend). Rejection blocks only the identity-requiring tool —
  surfaced to the orchestrator as an ``auth_required`` tool error — never the
  whole integration.

Deliberately *not* gated via the per-user ``tool_bypass_rules`` HITL config:
that table encodes a user's own risk tolerance for action approval, a
different axis from identity disclosure (see ADR 0006).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.human_in_the_loop import (
    ActionRequest,
    HITLRequest,
    ReviewConfig,
)
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.prebuilt import ToolRuntime
from langgraph.runtime import Runtime
from langgraph.types import interrupt

logger = logging.getLogger(__name__)

NANNOS_USER_IDENTITY_FIELD = "nannos__user_identity"
"""Reserved input-schema field marking a tool as identity-scoped."""

IDENTITY_SCOPED_METADATA_KEY = "identity_scoped"
"""Metadata flag stamped on wrapped identity-scoped tools."""


def _schema_as_dict(args_schema: Any) -> dict[str, Any] | None:
    """Return the tool's input schema as a plain JSON-schema dict, or None."""
    if isinstance(args_schema, dict):
        return args_schema
    model_json_schema = getattr(args_schema, "model_json_schema", None)
    if callable(model_json_schema):
        try:
            return model_json_schema()
        except Exception:  # pragma: no cover - defensive
            return None
    return None


def is_identity_scoped_tool(tool: Any) -> bool:
    """Whether ``tool`` declares the reserved ``nannos__user_identity`` field."""
    schema = _schema_as_dict(getattr(tool, "args_schema", None))
    if not schema:
        return False
    properties = schema.get("properties")
    return isinstance(properties, dict) and NANNOS_USER_IDENTITY_FIELD in properties


def is_wrapped_identity_scoped_tool(tool: Any) -> bool:
    """Whether ``tool`` is an already-wrapped identity-scoped tool."""
    metadata = getattr(tool, "metadata", None)
    return bool(
        metadata
        and isinstance(metadata, dict)
        and metadata.get(IDENTITY_SCOPED_METADATA_KEY)
    )


def _schema_without_identity_field(schema: dict[str, Any]) -> dict[str, Any]:
    """Deep-copied schema with the reserved field removed (properties + required)."""
    cleaned = json.loads(json.dumps(schema))
    properties = cleaned.get("properties")
    if isinstance(properties, dict):
        properties.pop(NANNOS_USER_IDENTITY_FIELD, None)
    required = cleaned.get("required")
    if isinstance(required, list):
        cleaned["required"] = [r for r in required if r != NANNOS_USER_IDENTITY_FIELD]
    return cleaned


def consent_key(tool_name: str, server_slug: str) -> str:
    """Key a consent grant by tool + server (same shape as bypass-rule keys)."""
    return f"{tool_name}::{server_slug}"


def _resolve_server_slug(
    tool_name: str, context: Any, metadata: dict[str, Any] | None
) -> str:
    """Resolve the MCP server slug for a tool (mirrors the HITL middleware)."""
    if context is not None:
        tool_server_map: dict[str, str] | None = getattr(
            context, "tool_server_map", None
        )
        if tool_server_map and tool_name in tool_server_map:
            return tool_server_map[tool_name]
    if metadata and isinstance(metadata, dict):
        server_name = metadata.get("server_name")
        if server_name:
            return str(server_name)
    return "_self"


def _consent_state(
    context: Any, tool_name: str, metadata: dict[str, Any] | None
) -> bool | None:
    """The remembered consent answer for (user, tool): True/False, or None if unasked."""
    grants: dict[str, Any] | None = (
        getattr(context, "identity_consent_grants", None) if context else None
    )
    if not grants:
        return None
    key = consent_key(tool_name, _resolve_server_slug(tool_name, context, metadata))
    grant = grants.get(key)
    if not isinstance(grant, dict):
        return None
    granted = grant.get("granted")
    return bool(granted) if granted is not None else None


def _record_consent(
    context: Any, tool_name: str, metadata: dict[str, Any] | None, granted: bool
) -> None:
    """Remember a consent answer in-session and queue it for durable persistence."""
    if context is None:
        return
    grants: dict[str, Any] | None = getattr(context, "identity_consent_grants", None)
    if grants is None:
        return
    key = consent_key(tool_name, _resolve_server_slug(tool_name, context, metadata))
    grant = {"granted": granted}
    grants[key] = grant
    pending: list[dict[str, Any]] = getattr(context, "_pending_identity_consents", [])
    pending.append({"key": key, "grant": grant})
    if not hasattr(context, "_pending_identity_consents"):
        context._pending_identity_consents = pending


def identity_auth_required_payload(tool_name: str, *, rejected: bool) -> str:
    """Structured tool-error payload the orchestrator treats as auth_required."""
    if rejected:
        message = (
            f"The user declined to share their identity with tool '{tool_name}'. "
            "This tool requires the user's verified email address to scope access "
            "to their own records and is blocked until the user grants consent. "
            "Other tools of the same integration remain available."
        )
    else:
        message = (
            f"Tool '{tool_name}' requires the user's explicit consent to receive "
            "their verified email address, and no consent has been granted yet. "
            "The user must approve identity disclosure for this tool first."
        )
    return json.dumps(
        {"error": True, "error_code": "auth_required", "message": message}
    )


def wrap_identity_scoped_tool(inner: BaseTool) -> BaseTool:
    """Wrap an identity-scoped tool for safe exposure to the model.

    The wrapper:
    - exposes ``inner``'s schema **minus** the reserved field, so no
      model-facing render (tool dicts, toolset selector, PTC signatures) ever
      shows it;
    - at execution time force-populates ``nannos__user_identity`` from the
      verified ``GraphRuntimeContext.email``, overwriting any value the model
      managed to supply;
    - fails **closed**: without a remembered consent grant it returns an
      ``auth_required`` payload instead of executing. This is the enforcement
      backstop for execution paths where no interrupt is possible (PTC
      ``eval``, sub-agents); the interactive consent prompt itself lives in
      :class:`IdentityConsentMiddleware`.
    """
    schema = _schema_as_dict(inner.args_schema)
    if schema is None:  # pragma: no cover - detection guarantees a dict schema
        return inner
    cleaned_schema = _schema_without_identity_field(schema)
    tool_name = inner.name
    inner_metadata = inner.metadata if isinstance(inner.metadata, dict) else {}

    async def _identity_scoped(runtime: ToolRuntime = None, **kwargs: Any) -> Any:  # type: ignore[assignment]
        context: Any = getattr(runtime, "context", None)
        consent = _consent_state(context, tool_name, inner_metadata)
        if consent is not True:
            logger.info(
                "Blocking identity-scoped tool '%s' (consent=%s) — failing closed",
                tool_name,
                consent,
            )
            return identity_auth_required_payload(tool_name, rejected=consent is False)

        email: str | None = getattr(context, "email", None) if context else None
        if not email:
            logger.warning(
                "Identity-scoped tool '%s' dispatched without a verified email on the "
                "runtime context — failing closed",
                tool_name,
            )
            return identity_auth_required_payload(tool_name, rejected=False)

        # Force-populate, overwriting whatever the model attempted to supply.
        kwargs[NANNOS_USER_IDENTITY_FIELD] = email
        return await inner.arun(kwargs)

    # ``from __future__ import annotations`` stores annotations as strings, so
    # pin the real type object for injected-arg detection (same reasoning as
    # ``wrap_tool_for_ptc``).
    _identity_scoped.__annotations__["runtime"] = ToolRuntime

    return StructuredTool(
        name=tool_name,
        description=inner.description,
        args_schema=cleaned_schema,
        coroutine=_identity_scoped,
        metadata={**inner_metadata, IDENTITY_SCOPED_METADATA_KEY: True},
    )


def wrap_identity_scoped_tools(tool_registry: dict[str, Any]) -> list[str]:
    """Wrap every identity-scoped tool in ``tool_registry`` in place.

    Returns the names of the wrapped tools (for logging).
    """
    wrapped: list[str] = []
    for name, tool in list(tool_registry.items()):
        if (
            isinstance(tool, BaseTool)
            and not is_wrapped_identity_scoped_tool(tool)
            and is_identity_scoped_tool(tool)
        ):
            tool_registry[name] = wrap_identity_scoped_tool(tool)
            wrapped.append(name)
    if wrapped:
        logger.info("Wrapped %d identity-scoped tool(s): %s", len(wrapped), wrapped)
    return wrapped


class IdentityConsentMiddleware(AgentMiddleware):
    """First-use consent gate for identity-scoped tools (Gate 3, ADR 0006).

    Runs in ``aafter_model``: for each identity-scoped tool call in the last
    AI message,

    - with a remembered **grant** → pass through (the tool wrapper injects the
      email at execution time);
    - with a remembered **denial** → drop the call and answer it with an
      artificial ``auth_required`` ToolMessage, no re-prompt;
    - with **no remembered answer** → raise a single HITL-shaped
      ``interrupt()`` covering all first-use calls in the message, then record
      each decision on the runtime context (``identity_consent_grants``) and
      queue it for durable persistence (``_pending_identity_consents``).

    Timeout/no-answer keeps the graph interrupted — nothing executes, so the
    gate fails closed by construction. A decision missing from the resume
    payload defaults to reject for this call *without* remembering a denial.
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

        # Partition identity-scoped calls by remembered consent state.
        denied_indices: set[int] = set()
        ask_indices: list[int] = []
        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            tool = tool_registry.get(tool_call["name"])
            if tool is None or not is_wrapped_identity_scoped_tool(tool):
                continue
            consent = _consent_state(
                context, tool_call["name"], getattr(tool, "metadata", None)
            )
            if consent is True:
                continue
            if consent is False:
                denied_indices.add(idx)
            else:
                ask_indices.append(idx)

        if not denied_indices and not ask_indices:
            return None

        decisions: list[dict[str, Any]] = []
        if ask_indices:
            email = getattr(context, "email", None) if context else None
            action_requests: list[ActionRequest] = []
            review_configs: list[ReviewConfig] = []
            for idx in ask_indices:
                tool_call = last_ai_msg.tool_calls[idx]
                tool_name = tool_call["name"]
                tool = tool_registry.get(tool_name)
                server_slug = _resolve_server_slug(
                    tool_name, context, getattr(tool, "metadata", None)
                )
                description = (
                    f"First use of '{tool_name}' ({server_slug}): this tool will receive your "
                    f"verified email address ({email}) to scope its access to your own records "
                    "on the target system. Your answer is remembered for this tool. "
                    "Declining blocks only this tool, not the whole integration."
                )
                action_requests.append(
                    ActionRequest(
                        name=tool_name,
                        # Display-only; the reserved field is injected at execution
                        # time and is never part of the model-visible args.
                        args={
                            **tool_call.get("args", {}),
                            "_call_id": tool_call["id"],
                            "_consent_metadata": {
                                "source": "identity_consent",
                                "server_slug": server_slug,
                                "tool_name": tool_name,
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
            if len(decisions) != len(ask_indices):
                msg = (
                    f"Number of consent decisions ({len(decisions)}) does not match "
                    f"number of pending identity-scoped tool calls ({len(ask_indices)})."
                )
                raise ValueError(msg)

        revised_tool_calls: list[Any] = []
        artificial_tool_messages: list[ToolMessage] = []
        decision_by_index = dict(zip(ask_indices, decisions))
        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            tool_name = tool_call["name"]
            if idx in denied_indices:
                artificial_tool_messages.append(
                    ToolMessage(
                        content=identity_auth_required_payload(
                            tool_name, rejected=True
                        ),
                        tool_call_id=tool_call["id"],
                        name=tool_name,
                        status="error",
                    )
                )
                continue
            if idx in decision_by_index:
                decision = decision_by_index[idx]
                tool = tool_registry.get(tool_name)
                metadata = getattr(tool, "metadata", None)
                decision_type = (
                    decision.get("type") if isinstance(decision, dict) else None
                )
                if decision_type == "approve":
                    _record_consent(context, tool_name, metadata, granted=True)
                    revised_tool_calls.append(tool_call)
                else:
                    # Remember only explicit rejections; a defaulted/missing
                    # decision blocks this call without recording a denial.
                    if decision_type == "reject":
                        _record_consent(context, tool_name, metadata, granted=False)
                    artificial_tool_messages.append(
                        ToolMessage(
                            content=identity_auth_required_payload(
                                tool_name, rejected=True
                            ),
                            tool_call_id=tool_call["id"],
                            name=tool_name,
                            status="error",
                        )
                    )
                continue
            revised_tool_calls.append(tool_call)

        last_ai_msg.tool_calls = revised_tool_calls
        return {"messages": [last_ai_msg, *artificial_tool_messages]}
