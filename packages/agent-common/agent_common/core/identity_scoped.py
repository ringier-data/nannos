"""Identity-scoped tool wrapper (ADR 0006, shared-credential tier).

An *Identity-scoped tool* is an MCP tool whose input schema declares the
reserved field ``nannos__user_identity``. :func:`wrap_identity_scoped_tool`
wraps such a tool so that:

- the reserved field is hidden from every model-facing schema render (regular
  dispatch, toolset selector, PTC signature render) — the wrapper's
  ``args_schema`` simply doesn't contain it;
- at execution time the field is force-populated from the verified user email
  on the runtime context, overwriting any model-supplied value (same "no LLM
  round-trip" principle as ``pending_file_blocks``); the payload is the email
  only;
- execution fails **closed**: without a remembered consent grant the wrapper
  returns an ``auth_required`` error payload instead of calling the tool — on
  every execution path, including PTC ``eval`` and sub-agents, where no
  interrupt is possible.

Wrapping happens where MCP tools materialize into ``BaseTool`` objects (the
orchestrator's discovery service and the dynamic sub-agent's own MCP
rediscovery), so every consumer gets wrapped tools by construction.
:func:`wrap_identity_scoped_tools` is idempotent and cheap on wrapped input,
so belt-and-braces call sites are fine.

Consent answers are remembered per ``(user, tool_name)`` — keyed by tool name
alone, deliberately *not* by ``tool::server`` like HITL bypass rules: server
slugs resolve differently across execution paths (orchestrator discovery tags
``metadata.server_name``; sub-agent rediscovery does not), and a key that
depends on the path would silently stop matching. The interactive first-use
consent prompt itself lives in the orchestrator's
``IdentityConsentMiddleware``; this module only provides the shared state
helpers it uses.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

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
    """Whether ``tool`` declares the reserved ``nannos__user_identity`` field.

    Must never raise: it runs over whole registries at discovery/build time,
    and a single misbehaving tool schema must degrade (not identity-scoped),
    not kill the turn.
    """
    try:
        args_schema = getattr(tool, "args_schema", None)
        if args_schema is None:
            return False
        if isinstance(args_schema, dict):
            properties = args_schema.get("properties")
            return (
                isinstance(properties, dict)
                and NANNOS_USER_IDENTITY_FIELD in properties
            )
        # Pydantic-model schema: check declared fields without generating the
        # full JSON schema (this runs over hundreds of tools).
        model_fields = getattr(args_schema, "model_fields", None)
        if model_fields is not None:
            return NANNOS_USER_IDENTITY_FIELD in model_fields
        schema = _schema_as_dict(args_schema)
        if not schema:
            return False
        properties = schema.get("properties")
        return isinstance(properties, dict) and NANNOS_USER_IDENTITY_FIELD in properties
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "Identity-scoped detection failed for tool %r; treating as not identity-scoped",
            tool,
        )
        return False


def is_wrapped_identity_scoped_tool(tool: Any) -> bool:
    """Whether ``tool`` is an already-wrapped identity-scoped tool."""
    metadata = getattr(tool, "metadata", None)
    return bool(
        metadata
        and isinstance(metadata, dict)
        and metadata.get(IDENTITY_SCOPED_METADATA_KEY)
    )


def _schema_without_identity_field(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``schema`` with the reserved field removed (properties + required)."""
    cleaned = dict(schema)
    properties = cleaned.get("properties")
    if isinstance(properties, dict):
        cleaned["properties"] = {
            k: v for k, v in properties.items() if k != NANNOS_USER_IDENTITY_FIELD
        }
    required = cleaned.get("required")
    if isinstance(required, list):
        cleaned["required"] = [r for r in required if r != NANNOS_USER_IDENTITY_FIELD]
    return cleaned


def consent_state(context: Any, tool_name: str) -> bool | None:
    """The remembered consent answer for (user, tool): True/False, or None if unasked."""
    grants: dict[str, Any] | None = (
        getattr(context, "identity_consent_grants", None) if context else None
    )
    if not grants:
        return None
    grant = grants.get(tool_name)
    if not isinstance(grant, dict):
        return None
    granted = grant.get("granted")
    return bool(granted) if granted is not None else None


def record_consent(context: Any, tool_name: str, granted: bool) -> None:
    """Remember a consent answer in-session and queue it for durable persistence."""
    if context is None:
        return
    grants: dict[str, Any] | None = getattr(context, "identity_consent_grants", None)
    if grants is None:
        return
    grants[tool_name] = {"granted": granted}
    pending: list[dict[str, Any]] = getattr(context, "_pending_identity_consents", [])
    pending.append({"tool_name": tool_name, "granted": granted})
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

    The wrapper exposes ``inner``'s schema minus the reserved field,
    force-populates ``nannos__user_identity`` from the verified email on the
    runtime context at execution time, and fails closed (``auth_required``
    payload) when no consent grant is remembered. See the module docstring.
    """
    from langgraph.prebuilt import ToolRuntime

    schema = _schema_as_dict(inner.args_schema)
    if schema is None:  # pragma: no cover - detection guarantees a dict-able schema
        return inner
    cleaned_schema = _schema_without_identity_field(schema)
    tool_name = inner.name
    inner_metadata = inner.metadata if isinstance(inner.metadata, dict) else {}
    # MCP tools are response_format="content_and_artifact"; mirror it so
    # structuredContent survives the wrap. The early exits below must then
    # return (payload, None) tuples.
    response_format = getattr(inner, "response_format", "content")
    tuple_output = response_format == "content_and_artifact"

    def _blocked(rejected: bool) -> Any:
        payload = identity_auth_required_payload(tool_name, rejected=rejected)
        return (payload, None) if tuple_output else payload

    async def _identity_scoped(runtime: ToolRuntime = None, **kwargs: Any) -> Any:  # type: ignore[assignment]
        context: Any = getattr(runtime, "context", None)
        consent = consent_state(context, tool_name)
        if consent is not True:
            logger.info(
                "Blocking identity-scoped tool '%s' (consent=%s) — failing closed",
                tool_name,
                consent,
            )
            return _blocked(rejected=consent is False)

        email: str | None = getattr(context, "email", None) if context else None
        if not email:
            logger.warning(
                "Identity-scoped tool '%s' dispatched without a verified email on the runtime context — failing closed",
                tool_name,
            )
            return _blocked(rejected=False)

        # Force-populate, overwriting whatever the model attempted to supply.
        kwargs[NANNOS_USER_IDENTITY_FIELD] = email
        inner_coroutine = getattr(inner, "coroutine", None)
        if inner_coroutine is not None:
            # Call the raw coroutine so content_and_artifact tuples pass
            # through intact (arun without a tool_call_id would strip the
            # artifact half). Same direct-coroutine precedent as the
            # orchestrator's agent_name-defaulting wrapper.
            return await inner_coroutine(**kwargs)
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
        response_format=response_format,
        metadata={**inner_metadata, IDENTITY_SCOPED_METADATA_KEY: True},
    )


def wrap_identity_scoped_tools(tools: list[BaseTool]) -> list[BaseTool]:
    """Return ``tools`` with every identity-scoped tool wrapped (idempotent)."""
    wrapped_names: list[str] = []
    result: list[BaseTool] = []
    for tool in tools:
        if (
            isinstance(tool, BaseTool)
            and not is_wrapped_identity_scoped_tool(tool)
            and is_identity_scoped_tool(tool)
        ):
            result.append(wrap_identity_scoped_tool(tool))
            wrapped_names.append(tool.name)
        else:
            result.append(tool)
    if wrapped_names:
        logger.info(
            "Wrapped %d identity-scoped tool(s): %s", len(wrapped_names), wrapped_names
        )
    return result


def wrap_identity_scoped_tools_in_registry(tool_registry: dict[str, Any]) -> list[str]:
    """Wrap every identity-scoped tool in ``tool_registry`` in place.

    Backstop for registries assembled from mixed sources; discovery-time
    wrapping means this is normally a cheap no-op. Returns wrapped names.
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
        logger.info(
            "Wrapped %d identity-scoped registry tool(s): %s", len(wrapped), wrapped
        )
    return wrapped
