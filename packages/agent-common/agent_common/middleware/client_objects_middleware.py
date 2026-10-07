"""Shared `<client_objects>` rendering for Embedded Nannos.

Renders the on-screen ontology manifest as a trailing per-call message for *any* agent —
the orchestrator main graph or a LOCAL domain sub-agent (the embedded entrypoint).
The manifest is read from the **RunnableConfig metadata** (provider-neutral), so a
single implementation serves every build path without depending on the
orchestrator's typed `GraphRuntimeContext`.

Manifest entry shape: `{type, id, scope, label?, fields?, fieldSpecs?, values?, unsaved?, actions?}`,
with `actions: [{name, label, description?, params?: [{name, type, description?, enum?}]}]`.
The orchestrator's `UserPreferencesMiddleware` reuses `inject_embedded_context`
(it sources the manifest from its context); `ClientObjectsMiddleware` is for
sub-agents that get the manifest via config metadata. Both place the block the
same way — see `inject_embedded_context`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.config import get_config
from langgraph.types import Command

from .utils import append_volatile_context_message

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


def _render_action(action: object) -> str | None:
    """An invokable action `{name, label, description?, params?}`; params reuse the
    field descriptor shape. None for an entry without a name (nothing to invoke)."""
    if not isinstance(action, dict) or not action.get("name"):
        return None
    rendered = str(action["name"])
    label = action.get("label")
    if label and label != action["name"]:
        rendered += f" ({label!r})"
    # It saves: asked like a save, and the agent should know before proposing it.
    if action.get("requiresApproval") is True:
        rendered += " [requires approval]"
    if action.get("description"):
        rendered += f": {action['description']}"
    params = action.get("params")
    if isinstance(params, list) and params:
        rendered += f" [args: {', '.join(_render_field(p) for p in params)}]"
    return rendered


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
        # The host's own dirty state — what the user typed, not only the agent's fills.
        if obj.get("unsaved") is True:
            desc += " unsaved"
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
        actions = obj.get("actions")
        rendered_actions: list[str] = []
        if isinstance(actions, list):
            rendered_actions = [a for a in (_render_action(x) for x in actions) if a]
            if rendered_actions:
                desc += "\n  actions: " + "; ".join(rendered_actions)
        # Shown, but nothing to fill and nothing to click: the host put it on screen
        # read-only for this user (someone else's object, a role the user lacks). Said
        # here, so the model does not read the absence as "use a server tool instead".
        if obj.get("scope") == "view" and not field_specs and not rendered_actions:
            desc += "\n  read-only here: no fields and no actions for this user"
        lines.append(desc)
    if not lines:
        return None
    return (
        "<client_objects>\n"
        "The user's application has registered these on-screen objects. You can act on "
        "them with the `client_action` tool: kind='apply' fills a form with values (nothing "
        "is saved; the changed fields are marked for the user), kind='highlight' points at an object, "
        "kind='navigate' opens a path, kind='invoke' runs one of an object's listed `actions` — "
        "including saving: an open form offers `save`. An action marked `requires approval` (`save`, "
        "and buttons like run now or set as default) changes something for real: invoke it only when "
        "the user wants that done, alone, after the results of your fills; the user approves it with a "
        "click. Only target objects listed here, and only use fields and actions "
        "they declare. A detail page in view mode offers an action such as `edit` — invoke it "
        "before apply. A form behind a button is reached through that button's action. If no "
        "action exists for what the user wants, tell them exactly which button to click; never "
        "invent a limitation. An object marked `read-only here` cannot be changed by this user "
        "from this page — and a server write tool for it is not the way around that: say they "
        "lack write access here (and who has it), do not propose the server write.\n"
        "KEEP THE SCREEN CURRENT: a tool other than `client_action` (a server/MCP tool) does not "
        "update the page. Before your final answer, if such a tool changed something this page "
        "shows (the entity the page is about, a listed object, a row on screen), the screen is "
        "stale: invoke an object's `refresh` action (e.g. type=Page) when one is listed and no "
        "object here is marked `unsaved`; otherwise tell the user the page still shows the old "
        "state until they save or discard their edits. An `unsaved` form holds edits that are "
        "not saved — possibly typed by the user: describe the saved state, not the form's "
        "values, and never save or discard those edits unasked. Example: you paused the job open on /app/scheduler/7 with a "
        "server tool → invoke refresh on the Page object, then answer. If the change lives on "
        "another page, offer to navigate there.\n" + "\n".join(lines) + "\n</client_objects>"
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


def inject_embedded_context(
    request: ModelRequest,
    page_context: Any,
    client_objects: Any,
) -> ModelRequest:
    """Attach the embedded-client context (`<current_page>` then `<client_objects>`)
    to a model request as ONE trailing, flagged human message.

    Single placement policy for both injection sites (orchestrator
    `UserPreferencesMiddleware`, sub-agent `ClientObjectsMiddleware`). The block is
    volatile on-screen state that is never checkpointed, so it must come AFTER all
    persisted messages to keep the provider prompt cache warm — see
    `agent_common.middleware.utils.append_volatile_context_message` for why the
    previous "last human message" placement busted the cache every turn.
    Returns the request unchanged when there is nothing to render.
    """
    blocks = [render_current_page_block(page_context), render_client_objects_block(client_objects)]
    block = "\n\n".join(b for b in blocks if b)
    if not block:
        return request
    return request.override(messages=append_volatile_context_message(request.messages, block))


#: Where a host's tool result says its object is shown (the console's ``console_path``).
_PAGE_PATH = re.compile(r'"(?:console_path|page_path)"\s*:\s*"(/[^"\s]*)"')


def where_it_shows_note(content: str, page_context: Any, client_objects: Any = None) -> str | None:
    """A note for a tool result about ONE object that is shown on another page than the user's.

    Told in the prompt to offer navigating to a change made on another page, the agent did
    not (it paused a job from the sub-agents list and just said so). Said in the result
    itself, next to the change, when it names exactly one page that is not the current one
    — a list (many pages) or the page the user is on gets nothing.
    """
    paths = list(dict.fromkeys(_PAGE_PATH.findall(content)))
    if len(paths) != 1:
        return None
    key = page_context.get("key") if isinstance(page_context, dict) else None
    current = re.split(r"[?#]", key)[0] if isinstance(key, str) else None
    if current is None:
        return None
    if paths[0] == current:
        # On this page, but its refresh is refused over unsaved edits: the agent then
        # neither refreshed nor said the header still showed "Active" for a paused job.
        unsaved = [o for o in client_objects or [] if isinstance(o, dict) and o.get("unsaved") is True]
        if not unsaved:
            return None
        return (
            "\n\n[This page shows it, but a form here holds unsaved edits, so the page is not refreshed "
            "and still shows the old state.] If this call changed it, say so in your answer: the page shows "
            "the old state until they save or discard their edits."
        )
    return (
        f"\n\n[Shown on {paths[0]} — the user is on {current}.] If this call changed it, end your answer "
        f"by naming that page and offering to open it; navigate only if they say yes."
    )


def _command_fields(command: Command) -> dict[str, Any]:
    """A Command's constructor fields, to rebuild it with a changed update."""
    return {k: getattr(command, k) for k in ("graph", "update", "resume", "goto") if getattr(command, k, None) is not None}


class ClientObjectsMiddleware(AgentMiddleware):
    """Append the embedded-client context sections (`<current_page>` and
    `<client_objects>`) for a LOCAL sub-agent, reading both from RunnableConfig
    metadata. Attach via `build_sub_agent_graph(extra_middlewares=[...])`."""

    def _apply(self, request: ModelRequest) -> ModelRequest:
        return inject_embedded_context(request, _page_context_from_config(), _client_objects_from_config())

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

    @staticmethod
    def _with_note(message: ToolMessage, page_context: Any, client_objects: Any = None) -> ToolMessage:
        content = message.content
        text = content if isinstance(content, str) else json.dumps(content, default=str)
        note = where_it_shows_note(text, page_context, client_objects)
        if not note:
            return message
        if isinstance(content, str):
            return message.model_copy(update={"content": content + note})
        return message.model_copy(update={"content": [*content, {"type": "text", "text": note.strip()}]})

    def _annotate(self, request: ToolCallRequest, result: ToolMessage | Command) -> ToolMessage | Command:
        if request.tool_call.get("name") == "client_action":
            return result
        page_context = _page_context_from_config()
        client_objects = _client_objects_from_config()
        if isinstance(result, ToolMessage):
            return self._with_note(result, page_context, client_objects)
        # The code interpreter (``eval``) answers with a Command carrying its ToolMessage.
        update = getattr(result, "update", None)
        if isinstance(update, dict) and isinstance(update.get("messages"), list):
            messages = [
                self._with_note(m, page_context, client_objects) if isinstance(m, ToolMessage) else m
                for m in update["messages"]
            ]
            return Command(**{**_command_fields(result), "update": {**update, "messages": messages}})
        return result

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        return self._annotate(request, handler(request))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        return self._annotate(request, await handler(request))
