"""Tests for SteeringMiddleware."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.types import FilePart, FileWithUri, TextPart
from a2a.types import Message as A2AMessage
from a2a.types import Part as A2APart

from ringier_a2a_sdk.middleware.steering import SteeringMiddleware


def _make_a2a_message(text: str, context_id: str = "ctx-1") -> A2AMessage:
    """Create a simple A2A Message for testing."""
    return A2AMessage(
        role="user",
        parts=[A2APart(root=TextPart(text=text))],
        message_id="msg-test",
        context_id=context_id,
    )


class TestSteeringMiddleware:
    def _make_config(self, context_id: str = "ctx-1"):
        """Create a mock LangGraph config dict."""
        return {
            "metadata": {"conversation_id": context_id},
            "configurable": {"thread_id": f"{context_id}::orchestrator"},
        }

    def _make_runtime(self):
        """Create a mock runtime (config is read via get_config(), not runtime.config)."""
        return MagicMock()

    def _make_state(self):
        return {"messages": []}

    @pytest.mark.asyncio
    async def test_no_pending_messages_returns_none(self):
        middleware = SteeringMiddleware(get_pending_messages=lambda ctx: [])
        with patch("ringier_a2a_sdk.middleware.steering.get_config", return_value=self._make_config()):
            result = await middleware.abefore_model(self._make_state(), self._make_runtime())
        assert result is None

    @pytest.mark.asyncio
    async def test_no_context_id_returns_none(self):
        middleware = SteeringMiddleware(get_pending_messages=lambda ctx: [_make_a2a_message("hi")])
        with patch("ringier_a2a_sdk.middleware.steering.get_config", return_value={}):
            result = await middleware.abefore_model(self._make_state(), self._make_runtime())
        assert result is None

    @pytest.mark.asyncio
    async def test_injects_human_messages(self):
        msgs = [_make_a2a_message("follow up 1"), _make_a2a_message("follow up 2")]
        middleware = SteeringMiddleware(get_pending_messages=lambda ctx: msgs)

        with (
            patch("ringier_a2a_sdk.middleware.steering.get_config", return_value=self._make_config()),
            patch("ringier_a2a_sdk.middleware.steering.get_stream_writer") as mock_writer,
        ):
            mock_writer.return_value = MagicMock()
            result = await middleware.abefore_model(self._make_state(), self._make_runtime())

        assert result is not None
        assert len(result["messages"]) == 2
        assert result["messages"][0].content == [{"type": "text", "text": "follow up 1"}]
        assert result["messages"][1].content == [{"type": "text", "text": "follow up 2"}]
        # Check steering marker
        assert result["messages"][0].additional_kwargs.get("steering") is True

    @pytest.mark.asyncio
    async def test_empty_text_parts_are_skipped(self):
        msg = A2AMessage(role="user", parts=[], message_id="m1", context_id="ctx-1")
        middleware = SteeringMiddleware(get_pending_messages=lambda ctx: [msg])

        with patch("ringier_a2a_sdk.middleware.steering.get_config", return_value=self._make_config()):
            result = await middleware.abefore_model(self._make_state(), self._make_runtime())
        assert result is None

    @pytest.mark.asyncio
    async def test_sync_callback_called(self):
        msgs = [_make_a2a_message("steer")]
        callback = MagicMock()
        middleware = SteeringMiddleware(
            get_pending_messages=lambda ctx: msgs,
            on_messages_received=callback,
        )

        with (
            patch("ringier_a2a_sdk.middleware.steering.get_config", return_value=self._make_config()),
            patch("ringier_a2a_sdk.middleware.steering.get_stream_writer") as mock_writer,
        ):
            mock_writer.return_value = MagicMock()
            await middleware.abefore_model(self._make_state(), self._make_runtime())

        callback.assert_called_once_with("ctx-1", msgs)

    @pytest.mark.asyncio
    async def test_async_callback_awaited(self):
        msgs = [_make_a2a_message("steer")]
        callback = AsyncMock()
        middleware = SteeringMiddleware(
            get_pending_messages=lambda ctx: msgs,
            on_messages_received=callback,
        )

        with (
            patch("ringier_a2a_sdk.middleware.steering.get_config", return_value=self._make_config()),
            patch("ringier_a2a_sdk.middleware.steering.get_stream_writer") as mock_writer,
        ):
            mock_writer.return_value = MagicMock()
            await middleware.abefore_model(self._make_state(), self._make_runtime())

        callback.assert_awaited_once_with("ctx-1", msgs)

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_break_injection(self):
        msgs = [_make_a2a_message("steer")]
        callback = MagicMock(side_effect=RuntimeError("boom"))
        middleware = SteeringMiddleware(
            get_pending_messages=lambda ctx: msgs,
            on_messages_received=callback,
        )

        with (
            patch("ringier_a2a_sdk.middleware.steering.get_config", return_value=self._make_config()),
            patch("ringier_a2a_sdk.middleware.steering.get_stream_writer") as mock_writer,
        ):
            mock_writer.return_value = MagicMock()
            result = await middleware.abefore_model(self._make_state(), self._make_runtime())

        assert result is not None
        assert len(result["messages"]) == 1

    @pytest.mark.asyncio
    async def test_resolves_context_from_thread_id_fallback(self):
        msgs = [_make_a2a_message("steer")]
        middleware = SteeringMiddleware(get_pending_messages=lambda ctx: msgs if ctx == "thread-1" else [])

        config_no_conversation_id = {
            "metadata": {},  # no conversation_id
            "configurable": {"thread_id": "thread-1"},
        }

        with (
            patch("ringier_a2a_sdk.middleware.steering.get_config", return_value=config_no_conversation_id),
            patch("ringier_a2a_sdk.middleware.steering.get_stream_writer") as mock_writer,
        ):
            mock_writer.return_value = MagicMock()
            result = await middleware.abefore_model(self._make_state(), self._make_runtime())

        assert result is not None
        assert len(result["messages"]) == 1

    @pytest.mark.asyncio
    async def test_emits_activity_log_stream_event(self):
        msgs = [_make_a2a_message("steer")]
        middleware = SteeringMiddleware(get_pending_messages=lambda ctx: msgs)

        mock_writer_fn = MagicMock()
        with (
            patch("ringier_a2a_sdk.middleware.steering.get_config", return_value=self._make_config()),
            patch("ringier_a2a_sdk.middleware.steering.get_stream_writer", return_value=mock_writer_fn),
        ):
            await middleware.abefore_model(self._make_state(), self._make_runtime())

        mock_writer_fn.assert_called_once()
        event = mock_writer_fn.call_args[0][0]
        assert event["activity_log"] is True
        assert "1 message" in event["text"]

    @pytest.mark.asyncio
    async def test_multimodal_message_injected_as_list_content(self):
        """A message with text + image file should produce a HumanMessage with list content."""
        multimodal_msg = A2AMessage(
            role="user",
            parts=[
                A2APart(root=TextPart(text="describe this")),
                A2APart(root=FilePart(file=FileWithUri(uri="https://example.com/img.jpg", mime_type="image/jpeg"))),
            ],
            message_id="m-mm",
            context_id="ctx-1",
        )
        middleware = SteeringMiddleware(get_pending_messages=lambda ctx: [multimodal_msg])

        with (
            patch("ringier_a2a_sdk.middleware.steering.get_config", return_value=self._make_config()),
            patch("ringier_a2a_sdk.middleware.steering.get_stream_writer") as mock_writer,
        ):
            mock_writer.return_value = MagicMock()
            result = await middleware.abefore_model(self._make_state(), self._make_runtime())

        assert result is not None
        assert len(result["messages"]) == 1
        content = result["messages"][0].content
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "describe this"}
        assert content[1]["type"] == "image"
        assert content[1]["url"] == "https://example.com/img.jpg"

    @pytest.mark.asyncio
    async def test_get_config_unavailable_returns_none(self):
        """When called outside a LangGraph run, get_config() raises RuntimeError."""
        middleware = SteeringMiddleware(get_pending_messages=lambda ctx: [_make_a2a_message("hi")])
        with patch(
            "ringier_a2a_sdk.middleware.steering.get_config",
            side_effect=RuntimeError("no runnable context"),
        ):
            result = await middleware.abefore_model(self._make_state(), self._make_runtime())
        assert result is None
