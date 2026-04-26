"""Tests for VoiceCallRequest model and phone number resolution in VoiceAgent."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from a2a.types import Message, Task, TaskState
from pydantic import SecretStr
from ringier_a2a_sdk.models import AgentStreamResponse, UserConfig

from voice_agent.a2a_agent import JSON_SCHEMA, VoiceAgent, VoiceCallRequest

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_user_config(**overrides) -> UserConfig:
    defaults = dict(
        user_sub="test-user-sub",
        access_token=SecretStr("test-token"),
        name="Test User",
        email="test@example.com",
    )
    defaults.update(overrides)
    return UserConfig(**defaults)


def _make_task(**overrides) -> Mock:
    task = Mock(spec=Task)
    task.id = overrides.get("id", "task-1")
    task.context_id = overrides.get("context_id", "ctx-1")
    return task


def _make_message_with_data(data: dict) -> Message:
    """Build an A2A Message containing a DataPart with the given dict."""
    from a2a.types import DataPart, Part, Role

    part = Part(root=DataPart(data=data))
    return Message(role=Role.user, parts=[part], messageId="msg-1")


# ── VoiceCallRequest Pydantic model tests ──────────────────────────────────


class TestVoiceCallRequest:
    def test_all_fields_optional(self):
        """All fields have defaults — empty payload is valid."""
        req = VoiceCallRequest()
        assert req.sub_agent_id is None
        assert req.system_prompt is None
        assert req.voice_name is None

    def test_full_payload(self):
        req = VoiceCallRequest(
            sub_agent_id=42,
            system_prompt="You are a helpful assistant",
            voice_name="Kore",
        )
        assert req.sub_agent_id == 42
        assert req.voice_name == "Kore"

    def test_json_schema_generated_from_model(self):
        """JSON_SCHEMA is derived from the Pydantic model, not hand-coded."""
        schema = VoiceCallRequest.model_json_schema()
        assert schema == JSON_SCHEMA
        assert "properties" in schema
        # phone_number is NOT in the schema — resolved from JWT only
        assert "phone_number" not in schema["properties"]
        assert "sub_agent_id" in schema["properties"]

    def test_phone_number_not_required_in_schema(self):
        """The phone_number field must NOT appear in the schema (resolved from JWT only)."""
        schema = VoiceCallRequest.model_json_schema()
        assert "phone_number" not in schema.get("properties", {})


# ── Phone number resolution tests ──────────────────────────────────────────


@pytest.mark.asyncio
class TestPhoneNumberResolution:
    """Tests for _handle_phone_call's phone resolution logic."""

    async def _collect_responses(self, agent: VoiceAgent, messages, user_config, task) -> list[AgentStreamResponse]:
        responses = []
        async for resp in agent._stream_impl(messages, user_config, task):
            responses.append(resp)
        return responses

    @patch.object(VoiceAgent, "_stream_phone_call")
    async def test_phone_from_user_config(self, mock_call):
        """phone_number from JWT (Keycloak resolved value) is used for the call."""

        async def _fake_call(**kwargs):
            assert kwargs["phone_number"] == "+41792222222"
            yield AgentStreamResponse(state=TaskState.completed, content="Done")

        mock_call.side_effect = _fake_call

        agent = VoiceAgent()
        user_config = _make_user_config(
            phone_number="+41792222222",  # resolved phone from Keycloak script mapper
        )
        task = _make_task()
        msg = _make_message_with_data({"system_prompt": "Hello"})

        responses = await self._collect_responses(agent, [msg], user_config, task)
        assert any(r.state == TaskState.completed for r in responses)

    async def test_no_phone_fails(self):
        """When phone_number is not set, agent returns failure."""
        agent = VoiceAgent()
        user_config = _make_user_config(phone_number=None)
        task = _make_task()
        msg = _make_message_with_data({"system_prompt": "Hello"})

        responses = await self._collect_responses(agent, [msg], user_config, task)
        assert any(r.state == TaskState.failed for r in responses)
        failure = next(r for r in responses if r.state == TaskState.failed)
        assert "phone number" in failure.content.lower()

    @patch.object(VoiceAgent, "_stream_phone_call")
    async def test_payload_phone_number_ignored(self, mock_call):
        """Even if payload has phone_number, it's ignored — user_config takes precedence."""

        async def _fake_call(**kwargs):
            # Must be the user_config phone, NOT the payload phone
            assert kwargs["phone_number"] == "+41791111111"
            yield AgentStreamResponse(state=TaskState.completed, content="Done")

        mock_call.side_effect = _fake_call

        agent = VoiceAgent()
        user_config = _make_user_config(phone_number="+41791111111")
        task = _make_task()
        # Payload contains a DIFFERENT phone number — should be ignored
        msg = _make_message_with_data(
            {
                "phone_number": "+41799999999",
                "system_prompt": "Hello",
            }
        )

        responses = await self._collect_responses(agent, [msg], user_config, task)
        assert any(r.state == TaskState.completed for r in responses)

    async def test_empty_payload_fails_gracefully(self):
        """Empty config dict triggers validation error or meaningful failure."""
        agent = VoiceAgent()
        user_config = _make_user_config(phone_number="+41791111111")
        task = _make_task()
        # Message with no DataPart — empty config
        msg = Message(role="user", parts=[], messageId="msg-1")

        responses = await self._collect_responses(agent, [msg], user_config, task)
        assert any(r.state == TaskState.failed for r in responses)
