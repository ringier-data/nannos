"""Tests for get_response_format strategy selection."""

from unittest.mock import MagicMock

from agent_common.a2a.structured_response import (
    SubAgentResponseSchema,
    get_response_format,
)
from langchain.agents.structured_output import AutoStrategy, ToolStrategy


def _model_with_class(class_name: str) -> MagicMock:
    model = MagicMock()
    model.__class__ = type(class_name, (), {})
    return model


def test_chat_openai_uses_tool_strategy():
    """Plain ChatOpenAI (real OpenAI endpoint) must use ToolStrategy.

    AutoStrategy would resolve to the Responses API .parse() path, which
    requires every bound tool to be strict — dynamic MCP tools are not.
    """
    model = _model_with_class("ChatOpenAI")
    tools: list = []

    fmt = get_response_format(model, tools)

    assert isinstance(fmt, ToolStrategy)
    assert fmt.schema is SubAgentResponseSchema
    assert tools == []


def test_azure_chat_openai_uses_tool_strategy():
    model = _model_with_class("AzureChatOpenAI")
    tools: list = []

    fmt = get_response_format(model, tools)

    assert isinstance(fmt, ToolStrategy)
    assert fmt.schema is SubAgentResponseSchema


def test_bedrock_without_thinking_uses_auto_strategy():
    model = _model_with_class("ChatBedrockConverse")
    tools: list = []

    fmt = get_response_format(model, tools, thinking_enabled=False)

    assert isinstance(fmt, AutoStrategy)
    assert fmt.schema is SubAgentResponseSchema
    assert tools == []


def test_bedrock_with_thinking_returns_none_and_appends_tool():
    model = _model_with_class("ChatBedrockConverse")
    tools: list = []

    fmt = get_response_format(model, tools, thinking_enabled=True)

    assert fmt is None
    assert len(tools) == 1
    assert tools[0].name == "SubAgentResponseSchema"


def test_gemini_returns_none_and_appends_tool():
    model = _model_with_class("ChatGoogleGenerativeAI")
    tools: list = []

    fmt = get_response_format(model, tools)

    assert fmt is None
    assert len(tools) == 1
    assert tools[0].name == "SubAgentResponseSchema"


def test_unknown_model_falls_back_to_auto_strategy():
    model = _model_with_class("SomeOtherModel")
    tools: list = []

    fmt = get_response_format(model, tools)

    assert isinstance(fmt, AutoStrategy)
    assert fmt.schema is SubAgentResponseSchema
