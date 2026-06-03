"""Storage Paths Instruction Middleware - Adds filesystem storage paths documentation to system prompt.

This middleware appends information about the different storage paths available in the
filesystem backend and their persistence guarantees to the system prompt, grouped into
two tiers, plus a shared decision tree for finding information. This ensures agents are
aware of:

Durable memory (persists across conversations):
1. Personal storage (`/memories/`) - persistent files private to the user
2. Channel storage (`/channel_memories/`) - shared files for channel members (channel context only)
3. Group storage (`/group_memories/`) - shared files for group members
4. Skills storage (`/skills/`) - read-only agent skills

Ephemeral / working storage (cleared when the conversation ends):
5. Root `/` - scratch files written without a known prefix
6. `/large_tool_results/` - where oversized tool outputs are auto-saved
7. `/attachments/` - read-only files the user attached to the current conversation
   (only present when the turn has attachments)

The prompt is built per-request and is context-aware: `/channel_memories/` and the
`read_personal_file` guidance are only included in channel context; `/attachments/`
is only included when the current turn has attached files (signalled via the
`has_attachments` config metadata set by ``DynamicLocalAgentRunnable``).

This middleware is context-agnostic and works with any agent configuration that includes
a filesystem backend with IndexingStoreBackend routes for `/memories/` and `/channel_memories/`.

Architecture:
- Appends storage paths prompt to system prompt at model call time
- Idempotent: Only appends if not already present
- Works for both orchestrator and sub-agents
- No dependency on specific context types
- Ideally shall be added right before the FilesystemMiddleware in the middleware stack to ensure instructions
    are in the proper order (middlewares are executed in reverse order of the list)
"""

import logging
from typing import Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import SystemMessage
from langgraph.config import get_config

from .utils import append_to_system_message

logger = logging.getLogger(__name__)


def _derive_context() -> str:
    """Derive the conversation context from runtime metadata.

    Returns ``"channel"`` when the conversation is a shared/channel scope,
    otherwise ``"direct"`` (one-to-one / personal). Mirrors the scope handling
    in ``ConversationContextToolsMiddleware`` so storage instructions match the
    tools actually available in this context.
    """
    try:
        config = get_config()
    except Exception:
        return "direct"
    if not config:
        return "direct"
    scope = config.get("metadata", {}).get("scope")
    return "channel" if scope == "channel" else "direct"


def _attachments_present() -> bool:
    """Return True when the current turn has files mounted at ``/attachments/``.

    Set per-invocation by ``DynamicLocalAgentRunnable`` via config metadata. The
    orchestrator never mounts attachments, so this stays False there.
    """
    try:
        config = get_config()
    except Exception:
        return False
    if not config:
        return False
    return bool(config.get("metadata", {}).get("has_attachments"))


def _build_decision_tree(context: str) -> str:
    """Build the shared "how to find information" decision tree.

    Identical for sandbox and non-sandbox agents so the guidance never drifts.
    The personal-file read line is only included in channel context, where
    ``read_personal_file`` is available.
    """
    channel_personal_read = ""
    if context == "channel":
        channel_personal_read = (
            "\n- Read one of the user's PERSONAL files from within a channel "
            "(requires approval) → use `read_personal_file`."
        )
    return f"""
<finding_information>
Choosing how to locate information:
- Exact or known string in a file → use `grep`.
- Read a specific file when you know its path → use `read_file`.
- Fuzzy/semantic search INSIDE one large in-hand blob (e.g. a big tool result under `/large_tool_results/`) → use `semantic_search_file`.
- Find something across your durable memory notes by meaning (not by path) → use `docstore_search`.{channel_personal_read}
</finding_information>"""


def _build_storage_tiers(context: str, has_attachments: bool = False) -> str:
    """Build the context-aware, two-tier storage description.

    Durable memory persists across conversations; ephemeral/working storage is
    cleared when the conversation ends. ``/channel_memories/`` is only shown in
    channel context (it is unavailable in direct conversations). ``/attachments/``
    is only shown when the user attached files to the current turn.
    """
    is_channel = context == "channel"
    channel_line = "\n- `/channel_memories/` — Shared with all channel members." if is_channel else ""
    channel_usage = (
        "\nUse `/channel_memories/` in Slack channels when sharing files with team members." if is_channel else ""
    )
    attachments_line = (
        "\n- `/attachments/` — Read-only. Files the user attached to THIS conversation "
        "(e.g. PDFs, images, documents). Use read_file to inspect them; for skills or "
        "sandbox commands that need the file on disk, copy it first."
        if has_attachments
        else ""
    )
    return f"""Durable memory (persists across conversations):
- `/memories/` — Personal files, private to you.{channel_line}
- `/group_memories/` — Shared with all members of your user group.
- `/skills/` — Read-only. Agent skills (instructions and scripts); use read_file to load full content.

Ephemeral / working storage (this conversation only, then cleared):
- Root `/` — scratch files written without a known prefix.
- `/large_tool_results/` — where oversized tool outputs are automatically saved.{attachments_line}

Use `/memories/` for documents, notes, and data you want to keep long-term.{channel_usage}
Use `/group_memories/` for files shared within a user group."""


def _build_non_sandbox_prompt(context: str, has_attachments: bool = False) -> str:
    """Build the non-sandbox filesystem instruction prompt."""
    return f"""
<filesystem_storage_paths>
The filesystem supports different storage locations with different persistence:

{_build_storage_tiers(context, has_attachments)}
</filesystem_storage_paths>
{_build_decision_tree(context)}"""


def _build_sandbox_prompt(sandbox_home: str, context: str, has_attachments: bool = False) -> str:
    """Build the sandbox-aware filesystem instruction prompt."""
    attachments_sandbox_note = (
        "\n- Files the user attached (`/attachments/`) also live on the virtual filesystem. "
        "To use one in a shell command, copy it first: "
        '`copy_to_sandbox("/attachments/<file>")` then use the returned path in execute().'
        if has_attachments
        else ""
    )
    return f"""
<filesystem_storage_paths>
You have access to two separate filesystems:

**1. Virtual Filesystem** (via read_file, write_file, edit_file, ls, grep):

{_build_storage_tiers(context, has_attachments)}

**2. Sandbox** (via execute):
Shell commands run in an isolated container with its own filesystem at `{sandbox_home}/`.
The sandbox is your working directory for running code, scripts, and commands.

**IMPORTANT: These are separate filesystems.**
- Files on the virtual filesystem (e.g., `/memories/script.py`) do NOT exist in the sandbox.
- To use a virtual file in a shell command, first materialize it:
  1. Call `copy_to_sandbox("/memories/script.py")` — returns the sandbox path
  2. Use the returned path in execute(), e.g., `execute("python {sandbox_home}/memories/script.py")`
- Skill files (`/skills/`) are automatically pre-synced to `{sandbox_home}/skills/` in the sandbox.{attachments_sandbox_note}

**Sandbox files are working copies (like a git checkout):**
- You can read, edit, and execute them freely in the sandbox.
- Edits made via execute() are NOT saved back to the virtual filesystem.
- To persist sandbox changes, read the file content and use write_file() to save it back
  (e.g., to `/memories/`).
</filesystem_storage_paths>
{_build_decision_tree(context)}"""


class StoragePathsInstructionMiddleware(AgentMiddleware):
    """Middleware that appends filesystem storage paths documentation to the system prompt.

    This ensures agents are properly instructed about:
    - Available storage locations, grouped into durable memory vs. ephemeral/working tiers
    - Persistence guarantees for each location
    - When to use each storage type
    - A shared decision tree for finding information (grep / read_file /
      semantic_search_file / docstore_search / read_personal_file)
    - For sandbox-enabled agents: the two-filesystem model and copy_to_sandbox usage

    The prompt is built per-request and is context-aware: ``/channel_memories/``
    and the ``read_personal_file`` guidance are only included in channel context.
    """

    def __init__(
        self,
        sandbox_enabled: bool = False,
        sandbox_home: str | None = None,
    ) -> None:
        self._sandbox_enabled = sandbox_enabled
        self._sandbox_home = sandbox_home or "/home/ubuntu"

    def _build_prompt(self) -> str:
        """Build the context-aware storage prompt for the current request."""
        context = _derive_context()
        has_attachments = _attachments_present()
        if self._sandbox_enabled:
            return _build_sandbox_prompt(self._sandbox_home, context, has_attachments)
        return _build_non_sandbox_prompt(context, has_attachments)

    def _inject_prompt(self, request: ModelRequest) -> ModelRequest:
        """Inject the storage paths prompt into the request if not already present."""
        prompt = self._build_prompt()
        if request.system_message and "<filesystem_storage_paths>" not in request.system_message.text:
            new_system_message = append_to_system_message(request.system_message, prompt)
            return request.override(system_message=new_system_message)
        elif not request.system_message:
            logger.debug("StoragePathsInstructionMiddleware: Set storage paths as initial system prompt")
            return request.override(system_message=SystemMessage(content_blocks=[{"type": "text", "text": prompt}]))
        return request

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        """Append storage paths prompt to system prompt before model call."""
        request = self._inject_prompt(request)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        """Async version of wrap_model_call."""
        request = self._inject_prompt(request)
        return await handler(request)
