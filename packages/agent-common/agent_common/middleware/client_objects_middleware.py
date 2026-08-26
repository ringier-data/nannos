"""Shared `<client_objects>` rendering for Embedded Nannos.

Renders the on-screen ontology manifest into the system prompt for *any* agent —
the orchestrator main graph or a LOCAL domain sub-agent (the embedded entrypoint).
The manifest is read from the **RunnableConfig metadata** (provider-neutral), so a
single implementation serves every build path without depending on the
orchestrator's typed `GraphRuntimeContext`.

Manifest entry shape: `{type, id, scope, label?, fields?, fieldSpecs?, values?}`.
The orchestrator's `UserPreferencesMiddleware` reuses `render_client_objects_block`
(it sources the manifest from its context); `ClientObjectsMiddleware` is for
sub-agents that get the manifest via config metadata.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langgraph.config import get_config

from .utils import append_to_last_human_message, append_to_system_message

logger = logging.getLogger(__name__)

# Metadata keys the manifest may arrive under (camelCase from the A2A wire,
# snake_case when plumbed server-side).
CLIENT_OBJECTS_METADATA_KEYS = ("client_objects", "clientObjects")

# Same convention for the live page context the embedding client publishes —
# the page the user is on RIGHT NOW: {key, title?, breadcrumbs?, entity?,
# view?, visible?}, merged from the host's layers and sanitized client-side
# (SDK core/page-context.ts).
PAGE_CONTEXT_METADATA_KEYS = ("page_context", "pageContext")


def _render_field(spec: object) -> str:
    """A field is either a bare name (str) or a typed descriptor dict
    `{name, type?, enum?, description?}` (Embedded Nannos fieldSpecs)."""
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        name = spec.get("name", "?")
        parts = []
        if spec.get("enum"):
            parts.append("one of: " + "|".join(str(v) for v in spec["enum"]))
        elif spec.get("type"):
            parts.append(str(spec["type"]))
        if spec.get("description"):
            parts.append(str(spec["description"]))
        return f"{name} ({'; '.join(parts)})" if parts else str(name)
    return str(spec)


def render_client_objects_block(client_objects: Any) -> str | None:
    """Render the `<client_objects>` prompt section, or None if empty/invalid."""
    if not client_objects or not isinstance(client_objects, list):
        return None
    lines: list[str] = []
    for obj in client_objects:
        if not isinstance(obj, dict):
            continue
        desc = f"- type={obj.get('type')} id={obj.get('id')} scope={obj.get('scope')}"
        if obj.get("label"):
            desc += f" label={obj['label']!r}"
        # Prefer typed field descriptors (name/type/enum) over bare names so the
        # agent uses exact keys and valid enum values.
        field_specs = obj.get("fieldSpecs") or obj.get("fields")
        if field_specs:
            desc += f"\n  fields: {', '.join(_render_field(f) for f in field_specs)}"
        # Current on-screen values (when the host opts in) so the agent works from
        # actual state, not just field definitions.
        values = obj.get("values")
        if isinstance(values, dict) and values:
            try:
                rendered_values = json.dumps(values, default=str, ensure_ascii=False)
            except Exception:
                rendered_values = str(values)
            desc += f"\n  current values: {rendered_values}"
        lines.append(desc)
    if not lines:
        return None
    return (
        "<client_objects>\n"
        "The user's application has registered these on-screen objects. You can act on "
        "them with the `client_action` tool: kind='apply' fills a form with values (the "
        "user reviews and saves manually — nothing persists directly), kind='highlight' "
        "points at an object, kind='navigate' opens a path. Only target objects listed "
        "here, and only use fields they declare.\n" + "\n".join(lines) + "\n</client_objects>"
    )


def render_current_page_block(page_context: Any) -> str | None:
    """Render the `<current_page>` prompt section, or None if empty/invalid.

    ``page_context`` is the live page descriptor the embedding client sends
    with every turn: {key, title?, breadcrumbs?, entity?, view?, visible?}
    (sanitized client-side; caps + secret-key deny list). It rides the last
    human message for the same reason as the manifest: it changes as the user
    navigates, and the cached system prefix must stay byte-stable.
    """
    if not isinstance(page_context, dict) or not page_context.get("key"):
        return None
    lines = [f"- path: {page_context['key']}"]
    if page_context.get("title"):
        lines.append(f"- title: {page_context['title']}")
    breadcrumbs = page_context.get("breadcrumbs")
    if isinstance(breadcrumbs, list) and breadcrumbs:
        lines.append(f"- breadcrumbs: {' > '.join(str(b) for b in breadcrumbs)}")
    # The entity resolves "this campaign" to an id the tools accept.
    entity = page_context.get("entity")
    if isinstance(entity, dict) and entity.get("type") and entity.get("id"):
        described = f"- on-screen entity: {entity['type']} id={entity['id']}"
        if entity.get("name"):
            described += f" name={entity['name']!r}"
        lines.append(described)
    # Active tab / filter / selection, as the page declared them.
    view = page_context.get("view")
    if isinstance(view, dict) and view:
        try:
            rendered_view = json.dumps(view, default=str, ensure_ascii=False)
        except Exception:
            rendered_view = str(view)
        lines.append(f"- view state: {rendered_view}")
    # Names of what the user can see, so "the second one" can be resolved.
    visible = page_context.get("visible")
    if isinstance(visible, list) and visible:
        lines.append(f"- visible items: {', '.join(str(v) for v in visible)}")
    return (
        "<current_page>\n"
        "The user is currently on this page in the application. It updates as they "
        "navigate, so earlier messages in the conversation may have been sent from "
        "other pages. Resolve references like \"this page\", \"here\", \"this "
        "campaign\", or \"the second one\" against it.\n"
        + "\n".join(lines)
        + "\n</current_page>"
    )


def _from_config_metadata(keys: tuple[str, ...]) -> Any:
    """Pull a value from the current RunnableConfig metadata (provider-neutral)."""
    try:
        config = get_config()
    except Exception:
        return None
    metadata = (config or {}).get("metadata") or {}
    for key in keys:
        if metadata.get(key):
            return metadata[key]
    return None


def _client_objects_from_config() -> Any:
    return _from_config_metadata(CLIENT_OBJECTS_METADATA_KEYS)


def _page_context_from_config() -> Any:
    return _from_config_metadata(PAGE_CONTEXT_METADATA_KEYS)


class ClientObjectsMiddleware(AgentMiddleware):
    """Append the embedded-client context sections (`<current_page>` and
    `<client_objects>`) for a LOCAL sub-agent, reading both from RunnableConfig
    metadata. Attach via `build_sub_agent_graph(extra_middlewares=[...])`."""

    def _apply(self, request: ModelRequest) -> ModelRequest:
        blocks = [
            render_current_page_block(_page_context_from_config()),
            render_client_objects_block(_client_objects_from_config()),
        ]
        block = "\n\n".join(b for b in blocks if b)
        if not block:
            return request
        # The manifest reflects volatile on-screen state, so ride the last human
        # message instead of the system prompt — this keeps the cached system
        # prefix byte-stable as the user navigates the app. Fall back to the
        # system prompt only when there is no human message to carry it.
        new_messages = append_to_last_human_message(request.messages, block)
        if new_messages is not None:
            return request.override(messages=new_messages)
        return request.override(
            system_message=append_to_system_message(request.system_message, "\n\n" + block)
        )

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._apply(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._apply(request))
