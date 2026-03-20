"""Tests for StoragePathsInstructionMiddleware.

Covers:
- No system message → creates new SystemMessage with storage paths prompt
- Existing system message without prompt → appends prompt (idempotent marker absent)
- Existing system message already containing prompt → passes through unchanged
- Async wrap_model_call mirrors sync behavior
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, call
from langchain_core.messages import SystemMessage

from agent_common.middleware.storage_paths_middleware import (
    StoragePathsInstructionMiddleware,
    _FILESYSTEM_STORAGE_PATHS_PROMPT,
)


def _make_request(system_message=None):
    """Build a mock ModelRequest whose override() returns a new mock."""
    req = MagicMock()
    req.system_message = system_message

    def _override(**kwargs):
        new_req = MagicMock()
        new_req.system_message = kwargs.get("system_message", system_message)
        return new_req

    req.override.side_effect = _override
    return req


class TestWrapModelCallSync:
    """Tests for StoragePathsInstructionMiddleware.wrap_model_call."""

    def setup_method(self):
        self.middleware = StoragePathsInstructionMiddleware()

    def test_no_system_message_creates_new_with_storage_prompt(self):
        """When request has no system_message, a new one with the storage prompt is created."""
        request = _make_request(system_message=None)
        received_requests = []

        def handler(req):
            received_requests.append(req)
            return MagicMock()

        self.middleware.wrap_model_call(request, handler)

        # override must have been called with a system_message
        request.override.assert_called_once()
        _, kwargs = request.override.call_args
        new_sm = kwargs["system_message"]
        assert isinstance(new_sm, SystemMessage)
        assert _FILESYSTEM_STORAGE_PATHS_PROMPT in new_sm.text

        # handler receives the modified request
        assert len(received_requests) == 1
        assert received_requests[0].system_message is new_sm

    def test_existing_system_message_without_prompt_appends(self):
        """When system_message exists but does not contain the prompt, the prompt is appended."""
        existing_sm = SystemMessage(content="You are a helpful assistant.")
        request = _make_request(system_message=existing_sm)
        received_requests = []

        def handler(req):
            received_requests.append(req)
            return MagicMock()

        self.middleware.wrap_model_call(request, handler)

        # override must have been called exactly once
        request.override.assert_called_once()
        _, kwargs = request.override.call_args
        new_sm = kwargs["system_message"]
        assert isinstance(new_sm, SystemMessage)
        # Original content preserved
        assert "helpful assistant" in new_sm.text
        # Prompt appended
        assert _FILESYSTEM_STORAGE_PATHS_PROMPT in new_sm.text

        assert received_requests[0].system_message is new_sm

    def test_existing_system_message_with_prompt_is_idempotent(self):
        """When system_message already contains the prompt, no modification is made."""
        existing_sm = SystemMessage(
            content=f"You are helpful.\n\n{_FILESYSTEM_STORAGE_PATHS_PROMPT}"
        )
        request = _make_request(system_message=existing_sm)
        received_requests = []

        def handler(req):
            received_requests.append(req)
            return MagicMock()

        self.middleware.wrap_model_call(request, handler)

        # override must NOT have been called — request is passed unchanged
        request.override.assert_not_called()
        # The original request object is forwarded to the handler
        assert received_requests[0] is request

    def test_handler_return_value_is_forwarded(self):
        """The return value of handler is returned from wrap_model_call."""
        sentinel = MagicMock(name="model_response")
        request = _make_request(system_message=None)

        result = self.middleware.wrap_model_call(request, lambda req: sentinel)

        assert result is sentinel

    def test_prompt_only_system_message_is_idempotent(self):
        """System message that is exactly the storage-paths prompt → no modification."""
        existing_sm = SystemMessage(content=_FILESYSTEM_STORAGE_PATHS_PROMPT)
        request = _make_request(system_message=existing_sm)

        calls_to_handler = []
        self.middleware.wrap_model_call(request, lambda req: calls_to_handler.append(req) or MagicMock())

        request.override.assert_not_called()
        assert calls_to_handler[0] is request


class TestWrapModelCallAsync:
    """Tests for StoragePathsInstructionMiddleware.awrap_model_call."""

    def setup_method(self):
        self.middleware = StoragePathsInstructionMiddleware()

    @pytest.mark.asyncio
    async def test_no_system_message_async(self):
        """Async variant: no system_message → creates new with storage prompt."""
        request = _make_request(system_message=None)
        received_requests = []

        async def handler(req):
            received_requests.append(req)
            return MagicMock()

        await self.middleware.awrap_model_call(request, handler)

        request.override.assert_called_once()
        _, kwargs = request.override.call_args
        new_sm = kwargs["system_message"]
        assert isinstance(new_sm, SystemMessage)
        assert _FILESYSTEM_STORAGE_PATHS_PROMPT in new_sm.text
        assert received_requests[0].system_message is new_sm

    @pytest.mark.asyncio
    async def test_existing_system_message_without_prompt_async(self):
        """Async variant: existing system_message without prompt → appends."""
        existing_sm = SystemMessage(content="Act as an expert analyst.")
        request = _make_request(system_message=existing_sm)
        received_requests = []

        async def handler(req):
            received_requests.append(req)
            return MagicMock()

        await self.middleware.awrap_model_call(request, handler)

        request.override.assert_called_once()
        _, kwargs = request.override.call_args
        new_sm = kwargs["system_message"]
        assert "expert analyst" in new_sm.text
        assert _FILESYSTEM_STORAGE_PATHS_PROMPT in new_sm.text

    @pytest.mark.asyncio
    async def test_idempotent_async(self):
        """Async variant: prompt already present → no modification."""
        existing_sm = SystemMessage(
            content=f"Base prompt.\n\n{_FILESYSTEM_STORAGE_PATHS_PROMPT}"
        )
        request = _make_request(system_message=existing_sm)

        calls_to_handler = []

        async def handler(req):
            calls_to_handler.append(req)
            return MagicMock()

        await self.middleware.awrap_model_call(request, handler)

        request.override.assert_not_called()
        assert calls_to_handler[0] is request

    @pytest.mark.asyncio
    async def test_async_handler_return_value_is_forwarded(self):
        """Return value from async handler is forwarded."""
        sentinel = MagicMock(name="async_response")
        request = _make_request(system_message=None)

        async def handler(req):
            return sentinel

        result = await self.middleware.awrap_model_call(request, handler)

        assert result is sentinel
