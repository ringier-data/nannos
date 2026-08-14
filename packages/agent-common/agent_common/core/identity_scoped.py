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
  returns an ``auth_required`` error payload instead of calling the tool, on
  every execution path. Inside PTC ``eval``, where ``interrupt()`` is
  impossible, an *unasked* tool first queues a consent question on the PTC turn
  collector (:func:`record_ptc_consent_request`) so the code interpreter can
  fire the interrupt and re-run the code — only a genuinely unanswerable call
  dead-ends.

Wrapping happens where MCP tools materialize into ``BaseTool`` objects (the
orchestrator's discovery service and the dynamic sub-agent's own MCP
rediscovery), so every consumer gets wrapped tools by construction.
:func:`wrap_identity_scoped_tools` is idempotent and cheap on wrapped input,
so belt-and-braces call sites are fine.

Consent answers are remembered per ``(user, mcp_server)``: an integration is
what the user actually reasons about ("may Salesforce know who I am"), and
per-tool answers meant re-approving the same disclosure once per tool on a
server that exposes a dozen of them. The key is the bare server slug, *not* the
``tool::server`` compound HITL bypass rules use — that table encodes a
per-action risk tolerance, a different axis (see ADR 0006). Slug resolution
must agree across execution paths or a grant silently stops matching, so
:func:`identity_server_slug` reads the orchestrator's authoritative
``tool_server_map`` off the runtime context first (the sub-agent's own MCP
rediscovery connects through the gateway and tags every tool with the gateway
name, not the server slug) and only then falls back to the tool's own
metadata. The interactive first-use consent prompt for regular tool calls lives in
``agent_common.middleware.identity_consent.IdentityConsentMiddleware`` (on the
orchestrator graph and every sub-agent graph); this module provides the shared
state helpers it uses, plus the ``eval``-side consent request above.
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


IDENTITY_SERVER_METADATA_KEY = "server_name"
"""Tool-metadata key MCP discovery stamps with the owning server slug."""

SELF_SERVER_SLUG = "_self"
"""Slug for tools with no resolvable MCP server (in-process platform tools)."""


def identity_server_slug(tool_name: str, context: Any, tool: Any = None) -> str:
    """Resolve the MCP server slug that a consent answer is keyed by.

    Resolution order mirrors ``ConditionalHumanInTheLoopMiddleware._get_server_slug``,
    with the context map first: it is the orchestrator's discovery-time mapping and
    is the only source that agrees across every execution path (see module docstring).
    """
    if context is not None:
        server_map: dict[str, str] | None = getattr(context, "tool_server_map", None)
        if server_map and tool_name in server_map:
            return str(server_map[tool_name])
        registry: dict[str, Any] | None = getattr(context, "tool_registry", None)
        if tool is None and registry:
            tool = registry.get(tool_name)
    metadata = getattr(tool, "metadata", None) if tool is not None else None
    if metadata and isinstance(metadata, dict):
        server_name = metadata.get(IDENTITY_SERVER_METADATA_KEY)
        if server_name:
            return str(server_name)
    return SELF_SERVER_SLUG


def consent_state(context: Any, server_slug: str) -> bool | None:
    """The remembered consent answer for (user, server): True/False, or None if unasked."""
    grants: dict[str, Any] | None = (
        getattr(context, "identity_consent_grants", None) if context else None
    )
    if not grants:
        return None
    grant = grants.get(server_slug)
    if not isinstance(grant, dict):
        return None
    granted = grant.get("granted")
    return bool(granted) if granted is not None else None


def record_consent(context: Any, server_slug: str, granted: bool) -> None:
    """Remember a consent answer in-session and queue it for durable persistence."""
    if context is None:
        return
    grants: dict[str, Any] | None = getattr(context, "identity_consent_grants", None)
    if grants is None:
        return
    grants[server_slug] = {"granted": granted}
    pending: list[dict[str, Any]] = getattr(context, "_pending_identity_consents", [])
    pending.append({"server_slug": server_slug, "granted": granted})
    if not hasattr(context, "_pending_identity_consents"):
        context._pending_identity_consents = pending


def identity_consent_call_key(server_slug: str) -> str:
    """Collector key for a pending consent question (per server, not per call)."""
    return f"identity_consent:{server_slug}"


def record_ptc_consent_request(runtime: Any, tool_name: str, server_slug: str) -> bool:
    """Queue a first-use consent question on the active PTC turn, if there is one.

    Inside ``eval`` no ``interrupt()`` is possible (the guard runs on the PTC
    bridge's threadsafe-dispatched task), so an unasked identity-scoped tool
    would otherwise just fail closed and the model would tell the user
    "permission is required" with no way to grant it. Instead the question is
    recorded on the same per-turn collector the risk guard uses: the code
    interpreter's ``awrap_tool_call`` drains it, fires one batched interrupt,
    records the answer on the runtime context, and re-runs ``eval``.

    Returns ``True`` when the question was queued (the caller must block this
    run), ``False`` when there is no PTC turn — the caller then fails closed as
    usual.
    """
    context: Any = getattr(runtime, "context", None)
    if not (getattr(context, "email", None) if context else None):
        # No verified email: the tool can never succeed, so asking for consent
        # would only produce a grant for a permanently broken tool.
        return False
    try:
        from agent_common.middleware.ptc_guard import (
            _PendingApproval,
            get_ptc_turn,
            resolve_ptc_thread_id,
        )

        turn = get_ptc_turn(resolve_ptc_thread_id(runtime))
        if turn is None:
            return False
        turn.record_pending(
            _PendingApproval(
                # Keyed per server, not per call: every eval call to any tool of
                # this integration shares one consent question (record_pending
                # dedups by key).
                call_key=identity_consent_call_key(server_slug),
                # Display only — the answer is recorded against server_slug.
                tool_name=tool_name,
                args={},
                server_slug=server_slug,
                allowed_actions=["approve", "reject"],
                score=0.0,
                threshold=0.0,
                matched_pattern=None,
                identity_consent=True,
            )
        )
        return True
    except Exception:  # pragma: no cover - defensive: never break tool dispatch
        logger.exception(
            "Failed to queue PTC identity-consent request for '%s'; failing closed",
            server_slug,
        )
        return False


def identity_auth_required_payload(
    tool_name: str, *, rejected: bool, server_slug: str | None = None
) -> str:
    """Structured tool-error payload the orchestrator treats as auth_required."""
    integration = (
        f"the '{server_slug}' integration"
        if server_slug and server_slug != SELF_SERVER_SLUG
        else f"the integration behind '{tool_name}'"
    )
    if rejected:
        message = (
            f"The user declined to share their identity with {integration}, so "
            f"'{tool_name}' is blocked. Identity-scoped tools need the user's verified "
            "email address to scope access to their own records. Tools of other "
            "integrations remain available."
        )
    else:
        message = (
            f"'{tool_name}' requires the user's explicit consent for {integration} to "
            "receive their verified email address, and no consent has been granted "
            "yet. The user must approve identity disclosure for this integration first."
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

    def _blocked(rejected: bool, server_slug: str) -> Any:
        payload = identity_auth_required_payload(
            tool_name, rejected=rejected, server_slug=server_slug
        )
        return (payload, None) if tuple_output else payload

    async def _identity_scoped(runtime: ToolRuntime = None, **kwargs: Any) -> Any:  # type: ignore[assignment]
        context: Any = getattr(runtime, "context", None)
        # Consent is per (user, MCP server): one answer covers every tool of the
        # integration. ``inner`` (not the wrapper) carries the discovery metadata
        # fallback used when the context has no tool_server_map.
        server_slug = identity_server_slug(tool_name, context, inner)
        consent = consent_state(context, server_slug)
        if consent is None and record_ptc_consent_request(
            runtime, tool_name, server_slug
        ):
            # Inside ``eval``: the consent question was handed to the PTC turn
            # collector, which fires a batched interrupt after this run and
            # re-runs the code once answered (same mechanism as the risk guard).
            return _blocked(rejected=False, server_slug=server_slug)
        if consent is not True:
            logger.info(
                "Blocking identity-scoped tool '%s' (server=%s, consent=%s) — failing closed",
                tool_name,
                server_slug,
                consent,
            )
            return _blocked(rejected=consent is False, server_slug=server_slug)

        email: str | None = getattr(context, "email", None) if context else None
        if not email:
            logger.warning(
                "Identity-scoped tool '%s' dispatched without a verified email on the runtime context — failing closed",
                tool_name,
            )
            return _blocked(rejected=False, server_slug=server_slug)

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
