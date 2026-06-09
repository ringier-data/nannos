"""Tests for StoragePathsInstructionMiddleware.

Covers:
- No system message → creates new SystemMessage with storage paths prompt
- Existing system message without prompt → appends prompt (idempotent marker absent)
- Existing system message already containing prompt → passes through unchanged
- Async wrap_model_call mirrors sync behavior
- Context-aware prompts: /channel_memories/ and read_personal_file only in channel
- Shared decision tree present in both sandbox and non-sandbox prompts
"""

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import SystemMessage

from agent_common.middleware.storage_paths_middleware import (
    StoragePathsInstructionMiddleware,
    _attachments_present,
    _build_decision_tree,
    _build_non_sandbox_prompt,
    _build_sandbox_prompt,
    _derive_context,
)

_MARKER = "<filesystem_storage_paths>"


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

        request.override.assert_called_once()
        _, kwargs = request.override.call_args
        new_sm = kwargs["system_message"]
        assert isinstance(new_sm, SystemMessage)
        assert _MARKER in new_sm.text

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

        request.override.assert_called_once()
        _, kwargs = request.override.call_args
        new_sm = kwargs["system_message"]
        assert isinstance(new_sm, SystemMessage)
        assert "helpful assistant" in new_sm.text
        assert _MARKER in new_sm.text

        assert received_requests[0].system_message is new_sm

    def test_existing_system_message_with_prompt_is_idempotent(self):
        """When system_message already contains the marker, no modification is made."""
        existing_sm = SystemMessage(content=f"You are helpful.\n\n{_MARKER}")
        request = _make_request(system_message=existing_sm)
        received_requests = []

        def handler(req):
            received_requests.append(req)
            return MagicMock()

        self.middleware.wrap_model_call(request, handler)

        request.override.assert_not_called()
        assert received_requests[0] is request

    def test_handler_return_value_is_forwarded(self):
        """The return value of handler is returned from wrap_model_call."""
        sentinel = MagicMock(name="model_response")
        request = _make_request(system_message=None)

        result = self.middleware.wrap_model_call(request, lambda req: sentinel)

        assert result is sentinel


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
        assert _MARKER in new_sm.text
        assert received_requests[0].system_message is new_sm

    @pytest.mark.asyncio
    async def test_idempotent_async(self):
        """Async variant: prompt already present → no modification."""
        existing_sm = SystemMessage(content=f"Base prompt.\n\n{_MARKER}")
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


class TestSandboxMode:
    """Tests for sandbox-enabled StoragePathsInstructionMiddleware."""

    def test_sandbox_prompt_includes_copy_to_sandbox(self):
        """Sandbox prompt should mention copy_to_sandbox tool."""
        prompt = _build_sandbox_prompt("/home/ubuntu", "direct")
        assert "copy_to_sandbox" in prompt
        assert "/home/ubuntu" in prompt

    def test_sandbox_prompt_mentions_skills_presync(self):
        """Sandbox prompt should tell agent skills are pre-synced."""
        prompt = _build_sandbox_prompt("/home/ubuntu", "direct")
        assert "skills" in prompt.lower()
        assert "pre-synced" in prompt or "automatically" in prompt

    def test_sandbox_prompt_mentions_write_file_persist(self):
        """Sandbox prompt should instruct agent to use write_file to persist."""
        prompt = _build_sandbox_prompt("/home/ubuntu", "direct")
        assert "write_file()" in prompt

    def test_sandbox_mode_uses_sandbox_prompt(self):
        """When sandbox_enabled=True, _build_prompt should produce the sandbox variant."""
        mw = StoragePathsInstructionMiddleware(sandbox_enabled=True, sandbox_home="/home/ubuntu")
        prompt = mw._build_prompt()
        assert "copy_to_sandbox" in prompt

    def test_non_sandbox_mode_omits_sandbox_text(self):
        """When sandbox_enabled=False (default), prompt should not mention sandbox copy tool."""
        mw = StoragePathsInstructionMiddleware()
        prompt = mw._build_prompt()
        assert "copy_to_sandbox" not in prompt


class TestContextAwareness:
    """Tests for context-aware prompt content."""

    def test_direct_context_omits_channel_memories(self):
        prompt = _build_non_sandbox_prompt("direct")
        assert "/channel_memories/" not in prompt
        assert "/memories/" in prompt
        assert "/group_memories/" in prompt

    def test_channel_context_includes_channel_memories(self):
        prompt = _build_non_sandbox_prompt("channel")
        assert "/channel_memories/" in prompt

    def test_decision_tree_shared_tools(self):
        """Decision tree must reference grep, read_file, semantic_search_file, docstore_search."""
        tree = _build_decision_tree("direct")
        for tool in ("grep", "read_file", "semantic_search_file", "docstore_search"):
            assert tool in tree

    def test_decision_tree_direct_omits_read_personal_file(self):
        tree = _build_decision_tree("direct")
        assert "read_personal_file" not in tree

    def test_decision_tree_channel_includes_read_personal_file(self):
        tree = _build_decision_tree("channel")
        assert "read_personal_file" in tree

    def test_sandbox_prompt_is_context_aware(self):
        direct = _build_sandbox_prompt("/home/ubuntu", "direct")
        channel = _build_sandbox_prompt("/home/ubuntu", "channel")
        assert "/channel_memories/" not in direct
        assert "/channel_memories/" in channel


class TestAttachments:
    """Tests for the conditional /attachments/ instruction."""

    def test_non_sandbox_omits_attachments_by_default(self):
        prompt = _build_non_sandbox_prompt("direct")
        assert "/attachments/" not in prompt

    def test_non_sandbox_includes_attachments_when_present(self):
        prompt = _build_non_sandbox_prompt("direct", has_attachments=True)
        assert "/attachments/" in prompt
        assert "attached to THIS conversation" in prompt

    def test_sandbox_omits_attachments_by_default(self):
        prompt = _build_sandbox_prompt("/home/ubuntu", "direct")
        assert "/attachments/" not in prompt

    def test_sandbox_includes_attachments_and_copy_hint(self):
        prompt = _build_sandbox_prompt("/home/ubuntu", "direct", has_attachments=True)
        assert "/attachments/" in prompt
        assert "copy_to_sandbox" in prompt

    def test_attachments_present_reads_config(self):
        with patch(
            "agent_common.middleware.storage_paths_middleware.get_config",
            return_value={"metadata": {"has_attachments": True}},
        ):
            assert _attachments_present() is True

    def test_attachments_present_defaults_false(self):
        with patch(
            "agent_common.middleware.storage_paths_middleware.get_config",
            return_value={"metadata": {}},
        ):
            assert _attachments_present() is False

    def test_attachments_present_handles_no_config(self):
        with patch(
            "agent_common.middleware.storage_paths_middleware.get_config",
            side_effect=RuntimeError("outside runnable"),
        ):
            assert _attachments_present() is False


class TestDeriveContext:
    """Tests for _derive_context scope resolution."""

    def test_no_config_defaults_direct(self):
        with patch(
            "agent_common.middleware.storage_paths_middleware.get_config",
            return_value=None,
        ):
            assert _derive_context() == "direct"

    def test_get_config_raises_defaults_direct(self):
        with patch(
            "agent_common.middleware.storage_paths_middleware.get_config",
            side_effect=RuntimeError("outside runnable"),
        ):
            assert _derive_context() == "direct"

    def test_channel_scope(self):
        with patch(
            "agent_common.middleware.storage_paths_middleware.get_config",
            return_value={"metadata": {"scope": "channel"}},
        ):
            assert _derive_context() == "channel"

    def test_personal_scope_is_direct(self):
        with patch(
            "agent_common.middleware.storage_paths_middleware.get_config",
            return_value={"metadata": {"scope": "personal"}},
        ):
            assert _derive_context() == "direct"
