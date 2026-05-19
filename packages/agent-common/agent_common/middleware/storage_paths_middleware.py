"""Storage Paths Instruction Middleware - Adds filesystem storage paths documentation to system prompt.

This middleware appends information about the different storage paths available in the
filesystem backend and their persistence guarantees to the system prompt. This ensures
agents are aware of:

1. Ephemeral storage (root `/`) - temporary files cleared after conversation
2. Personal storage (`/memories/`) - persistent files private to the user
3. Channel storage (`/channel_memories/`) - shared files for channel members
4. Group storage (`/group_memories/`) - shared files for group members

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

from .utils import append_to_system_message

logger = logging.getLogger(__name__)


_FILESYSTEM_STORAGE_PATHS_PROMPT = """
<filesystem_storage_paths>
The filesystem supports different storage locations with different persistence:

- Ephemeral storage (root `/`): Files without a prefix are temporary and cleared after conversation ends.
- Personal storage (`/memories/`): Files persist across conversations, private to you.
- Channel storage (`/channel_memories/`): Files shared with all channel members.
- Group storage (`/group_memories/`): Files shared with all group members.
- Skills storage (`/skills/`): Read-only. Contains agent skills (instructions and scripts).
  Use read_file to load full skill content.

Use `/memories/` for documents, notes, and data you want to keep long-term.
Use `/channel_memories/` in Slack channels when sharing files with team members.
Use `/group_memories/` for files shared within a user group.
</filesystem_storage_paths>"""


def _build_sandbox_prompt(sandbox_home: str) -> str:
    """Build the sandbox-aware filesystem instruction prompt."""
    return f"""
<filesystem_storage_paths>
You have access to two separate filesystems:

**1. Virtual Filesystem** (via read_file, write_file, edit_file, ls, grep):
Persistent storage with different scopes:
- `/memories/` — Personal files, persist across conversations, private to you.
- `/channel_memories/` — Shared with all channel members.
- `/group_memories/` — Shared with all group members.
- `/skills/` — Read-only. Contains agent skills (instructions and scripts).
- `/large_tool_results/` — Large tool outputs, scoped to this conversation.

**2. Sandbox** (via execute):
Shell commands run in an isolated container with its own filesystem at `{sandbox_home}/`.
The sandbox is your working directory for running code, scripts, and commands.

**IMPORTANT: These are separate filesystems.**
- Files on the virtual filesystem (e.g., `/memories/script.py`) do NOT exist in the sandbox.
- To use a virtual file in a shell command, first materialize it:
  1. Call `copy_to_sandbox("/memories/script.py")` — returns the sandbox path
  2. Use the returned path in execute(), e.g., `execute("python {sandbox_home}/memories/script.py")`
- Skill files (`/skills/`) are automatically pre-synced to `{sandbox_home}/skills/` in the sandbox.

**Sandbox files are working copies (like a git checkout):**
- You can read, edit, and execute them freely in the sandbox.
- Edits made via execute() are NOT saved back to the virtual filesystem.
- To persist sandbox changes, read the file content and use write_file() to save it back
  (e.g., to `/memories/`).

Use `/memories/` for documents, notes, and data you want to keep long-term.
Use `/channel_memories/` in Slack channels when sharing files with team members.
Use `/group_memories/` for files shared within a user group.
</filesystem_storage_paths>"""


class StoragePathsInstructionMiddleware(AgentMiddleware):
    """Middleware that appends filesystem storage paths documentation to the system prompt.

    This ensures agents are properly instructed about:
    - Available storage locations (/, /memories/, /channel_memories/)
    - Persistence guarantees for each location
    - When to use each storage type
    - For sandbox-enabled agents: the two-filesystem model and copy_to_sandbox usage

    The middleware is lightweight and context-agnostic, making it suitable for
    both the orchestrator and all sub-agents.
    """

    def __init__(
        self,
        sandbox_enabled: bool = False,
        sandbox_home: str | None = None,
    ) -> None:
        self._sandbox_enabled = sandbox_enabled
        self._prompt = (
            _build_sandbox_prompt(sandbox_home or "/home/ubuntu")
            if sandbox_enabled
            else _FILESYSTEM_STORAGE_PATHS_PROMPT
        )

    def _inject_prompt(self, request: ModelRequest) -> ModelRequest:
        """Inject the storage paths prompt into the request if not already present."""
        if request.system_message and self._prompt not in request.system_message.text:
            new_system_message = append_to_system_message(request.system_message, self._prompt)
            return request.override(system_message=new_system_message)
        elif not request.system_message:
            logger.debug("StoragePathsInstructionMiddleware: Set storage paths as initial system prompt")
            return request.override(
                system_message=SystemMessage(content_blocks=[{"type": "text", "text": self._prompt}])
            )
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
