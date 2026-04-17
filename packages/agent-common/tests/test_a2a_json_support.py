"""Tests for JSON input support in A2A client runnable."""

import pytest
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from langchain_core.messages import HumanMessage

from agent_common.a2a.client_runnable import A2AClientRunnable


@pytest.fixture
def agent_card_json_capable():
    """Create an agent card that supports JSON input."""
    return AgentCard(
        name="json-agent",
        description="Agent that supports JSON input",
        url="https://json-agent.example.com/a2a",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True),
        skills=[AgentSkill(id="json", name="json-skill", description="JSON skill", tags=["json"])],
        default_input_modes=["application/json"],
        default_output_modes=["text/plain"],
    )


@pytest.fixture
def agent_card_text_only():
    """Create an agent card that only supports text input."""
    return AgentCard(
        name="text-agent",
        description="Agent that only supports text input",
        url="https://text-agent.example.com/a2a",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True),
        skills=[AgentSkill(id="text", name="text-skill", description="Text skill", tags=["text"])],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )


@pytest.fixture
def runnable_json(agent_card_json_capable):
    """Create a runnable for JSON-capable agent."""
    return A2AClientRunnable(agent_card=agent_card_json_capable)


@pytest.fixture
def runnable_text(agent_card_text_only):
    """Create a runnable for text-only agent."""
    return A2AClientRunnable(agent_card=agent_card_text_only)


class TestJsonMessageCreation:
    """Test A2A message creation from HumanMessage."""

    def test_from_human_message_with_json(self, runnable_json):
        """Should create A2A message with DataPart from HumanMessage with JSON block."""
        json_data = {"task": "test", "value": 123}
        context_id = "ctx-123"
        task_id = "task-456"

        # Create HumanMessage with JSON content block
        json_block = {
            "type": "non_standard",
            "id": "test-id",
            "value": {"media_type": "application/json", "data": json_data},
        }
        human_message = HumanMessage(content_blocks=[json_block])

        # Transform to A2A Message
        message = runnable_json._from_human_messages_to_a2a(
            [human_message],
            context_id,
            task_id,
        )

        # Verify message structure
        assert message.context_id == context_id
        assert message.task_id == task_id
        assert len(message.parts) >= 1

        # First part should be DataPart with JSON
        first_part = message.parts[0]
        assert first_part.root.kind == "data"
        assert first_part.root.data == json_data

    def test_from_human_message_with_text(self, runnable_json):
        """Should create A2A message with TextPart from plain HumanMessage."""
        text_content = "Analyze this data"
        context_id = "ctx-456"
        task_id = "task-789"

        human_message = HumanMessage(content=text_content)

        # Transform to A2A Message
        message = runnable_json._from_human_messages_to_a2a(
            [human_message],
            context_id,
            task_id,
        )

        # Verify message structure
        assert message.context_id == context_id
        assert message.task_id == task_id
        assert len(message.parts) >= 1

        # First part should be TextPart
        first_part = message.parts[0]
        assert first_part.root.kind == "text"
        assert first_part.root.text == text_content

    def test_from_human_message_has_metadata(self, runnable_json):
        """Should include timestamp and source metadata."""
        human_message = HumanMessage(content="Test message")

        message = runnable_json._from_human_messages_to_a2a(
            [human_message],
            context_id=None,
            task_id=None,
        )

        # Verify metadata
        assert "timestamp" in message.metadata
        assert message.metadata["source"] == "Orchestrator"
