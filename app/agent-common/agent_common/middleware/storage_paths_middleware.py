"""Storage Paths Instruction Middleware - Adds filesystem storage paths documentation to system prompt.

This middleware appends information about the different storage paths available in the
filesystem backend and their persistence guarantees to the system prompt. This ensures
agents are aware of:

1. Ephemeral storage (root `/`) - temporary files cleared after conversation
2. Personal storage (`/memories/`) - persistent files private to the user
3. Channel storage (`/channel_memories/`) - shared files for channel members

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


_FILESYSTEM_STORAGE_PATHS_PROMPT = """## Filesystem Storage Paths

The filesystem supports different storage locations with different persistence:

- **Ephemeral storage** (root `/`): Files without a prefix are temporary and cleared after conversation ends
- **Personal storage** (`/memories/`): Files persist across conversations, private to you
- **Channel storage** (`/channel_memories/`): Files shared with all channel members

Use `/memories/` for documents, notes, and data you want to keep long-term.
Use `/channel_memories/` in Slack channels when sharing files with team members."""


class StoragePathsInstructionMiddleware(AgentMiddleware):
    """Middleware that appends filesystem storage paths documentation to the system prompt.

    This ensures agents are properly instructed about:
    - Available storage locations (/, /memories/, /channel_memories/)
    - Persistence guarantees for each location
    - When to use each storage type

    The middleware is lightweight and context-agnostic, making it suitable for
    both the orchestrator and all sub-agents.
    """

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        """Append storage paths prompt to system prompt before model call.

        Args:
            request: Model request with system prompt
            handler: Callback to execute the model

        Returns:
            Model response
        """
        # Only append if not already present (idempotent)
        if request.system_message and _FILESYSTEM_STORAGE_PATHS_PROMPT not in request.system_message.text:
            new_system_message = append_to_system_message(request.system_message, _FILESYSTEM_STORAGE_PATHS_PROMPT)
            request = request.override(system_message=new_system_message)
        elif not request.system_message:
            request = request.override(
                system_message=SystemMessage(
                    content_blocks=[{"type": "text", "text": _FILESYSTEM_STORAGE_PATHS_PROMPT}]
                )
            )
            logger.debug("StoragePathsInstructionMiddleware: Set storage paths as initial system prompt")

        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        """Async version of wrap_model_call.

        Args:
            request: Model request with system prompt
            handler: Async callback to execute the model

        Returns:
            Model response
        """
        # Only append if not already present (idempotent)
        if request.system_message and _FILESYSTEM_STORAGE_PATHS_PROMPT not in request.system_message.text:
            new_system_message = append_to_system_message(request.system_message, _FILESYSTEM_STORAGE_PATHS_PROMPT)
            request = request.override(system_message=new_system_message)
        elif not request.system_message:
            request = request.override(
                system_message=SystemMessage(
                    content_blocks=[{"type": "text", "text": _FILESYSTEM_STORAGE_PATHS_PROMPT}]
                )
            )
            logger.debug("StoragePathsInstructionMiddleware: Set storage paths as initial system prompt")

        return await handler(request)
