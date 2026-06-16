"""Tests for LangGraphBedrockAgent middleware configuration."""

import os
from unittest.mock import MagicMock, patch

import pytest
from langchain_aws.middleware.prompt_caching import BedrockPromptCachingMiddleware
from langchain_core.messages import HumanMessage

from ringier_a2a_sdk.agent.langgraph_bedrock import LangGraphBedrockAgent
from ringier_a2a_sdk.middleware.steering import SteeringMiddleware
from ringier_a2a_sdk.middleware.tool_schema_cleaning import ToolSchemaCleaningMiddleware


class TestLangGraphBedrockAgentMiddleware:
    """Tests for LangGraphBedrockAgent middleware setup."""

    @patch.dict(os.environ, {"AWS_BEDROCK_REGION": "us-east-1"})
    @patch("boto3.client")
    def test_bedrock_agent_includes_schema_cleaning_and_prompt_caching_by_default(
        self, mock_boto3_client
    ):
        """Test that LangGraphBedrockAgent includes both schema cleaning and prompt caching by default."""
        mock_boto3_client.return_value = MagicMock()

        class MinimalBedrockAgent(LangGraphBedrockAgent):
            """Minimal concrete implementation for testing."""

            async def _get_mcp_connections(self):
                return {}

            def _get_system_prompt(self):
                return "Test prompt"

            def _get_checkpoint_namespace(self):
                return "test-ns"

        agent = MinimalBedrockAgent()
        middleware = agent._get_middleware()

        # Verify that all middlewares are included in the correct order
        # Order: Prompt caching first (outer), then schema cleaning, then steering (inner)
        assert len(middleware) == 3
        assert isinstance(middleware[0], BedrockPromptCachingMiddleware)
        assert isinstance(middleware[1], ToolSchemaCleaningMiddleware)
        assert isinstance(middleware[2], SteeringMiddleware)

    @patch.dict(os.environ, {"AWS_BEDROCK_REGION": "us-east-1"})
    @patch("boto3.client")
    def test_subclass_can_extend_middleware(self, mock_boto3_client):
        """Test that subclasses can extend the middleware list by calling super()."""
        mock_boto3_client.return_value = MagicMock()

        from langchain.agents.middleware.types import AgentMiddleware

        class CustomMiddleware(AgentMiddleware):
            """Custom test middleware."""

            pass

        class ExtendedBedrockAgent(LangGraphBedrockAgent):
            """Bedrock agent that extends middleware."""

            async def _get_mcp_connections(self):
                return {}

            def _get_system_prompt(self):
                return "Test prompt"

            def _get_checkpoint_namespace(self):
                return "test-ns"

            def _get_middleware(self):
                # Extend parent middleware instead of replacing it
                return super()._get_middleware() + [CustomMiddleware()]

        agent = ExtendedBedrockAgent()
        middleware = agent._get_middleware()

        # Verify that all middlewares are present in the correct order
        # Prompt caching (outer) → Schema cleaning → Steering → Custom (innermost/added last)
        assert len(middleware) == 4
        assert isinstance(middleware[0], BedrockPromptCachingMiddleware)
        assert isinstance(middleware[1], ToolSchemaCleaningMiddleware)
        assert isinstance(middleware[2], SteeringMiddleware)
        assert isinstance(middleware[3], CustomMiddleware)

    def test_middleware_filters_invalid_tools(self):
        """Test that ToolSchemaCleaningMiddleware filters out invalid tools."""
        from unittest.mock import MagicMock

        from langchain.agents.middleware.types import ModelRequest
        from langchain_core.tools import tool

        @tool
        def valid_tool(x: int) -> int:
            """A valid tool."""
            return x * 2

        # Create tools with various invalid formats
        tools = [
            valid_tool,  # Valid BaseTool
            None,  # Invalid: None
            {"function": {"description": "Missing name"}},  # Invalid: missing name
            {"function": {"name": "", "description": "Empty name"}},  # Invalid: empty name
            {"function": {"name": 123, "description": "Invalid name type"}},  # Invalid: non-string name
            "not_a_tool",  # Invalid: string instead of tool
        ]

        middleware = ToolSchemaCleaningMiddleware()

        # Create a mock request with required model parameter
        mock_model = MagicMock()
        request = ModelRequest(
            system_message="Test",
            messages=[],
            tools=tools,
            model=mock_model,
        )

        # Mock handler that just returns the request
        def mock_handler(req):
            return req

        # Call the middleware
        result = middleware.wrap_model_call(request, mock_handler)

        # Should only have 1 valid tool (the valid_tool)
        assert len(result.tools) == 1
        assert result.tools[0]["function"]["name"] == "valid_tool"


class TestBedrockImagePreprocessing:
    """Tests for LangGraphBedrockAgent._preprocess_input_messages (URL→base64 conversion)."""

    def _make_agent(self):
        """Create a minimal LangGraphBedrockAgent for testing."""
        import os
        from unittest.mock import MagicMock, patch

        with (
            patch.dict(os.environ, {"AWS_BEDROCK_REGION": "us-east-1"}),
            patch("boto3.client") as mock_c,
        ):
            mock_c.return_value = MagicMock()

            class MinimalAgent(LangGraphBedrockAgent):
                async def _get_mcp_connections(self):
                    return {}

                def _get_system_prompt(self):
                    return "Test"

                def _get_checkpoint_namespace(self):
                    return "test"

            return MinimalAgent()

    @pytest.mark.asyncio
    async def test_text_only_messages_pass_through(self):
        """Text-only HumanMessages should not be modified."""
        agent = self._make_agent()
        msgs = [HumanMessage(content="hello world")]
        result = await agent._preprocess_input_messages(msgs)
        assert len(result) == 1
        assert result[0].content == "hello world"

    @pytest.mark.asyncio
    async def test_base64_images_pass_through(self):
        """Images already in base64 format should not be modified."""
        agent = self._make_agent()
        blocks = [
            {"type": "text", "text": "describe this"},
            {"type": "image", "base64": "abc123==", "mimeType": "image/png"},
        ]
        msgs = [HumanMessage(content=blocks)]
        result = await agent._preprocess_input_messages(msgs)
        assert len(result) == 1
        assert result[0].content == blocks  # unchanged

    @pytest.mark.asyncio
    async def test_url_images_converted_to_base64(self):
        """URL-based images should be downloaded and converted to base64."""
        import base64
        from unittest.mock import AsyncMock, patch

        agent = self._make_agent()
        blocks = [
            {"type": "text", "text": "describe this"},
            {"type": "image", "url": "https://example.com/image.png", "mime_type": "image/png"},
        ]
        msgs = [HumanMessage(content=blocks)]

        fake_image_bytes = b"\x89PNG\r\n\x1a\nfake_image_data"
        expected_b64 = base64.b64encode(fake_image_bytes).decode("utf-8")

        mock_response = MagicMock()
        mock_response.content = fake_image_bytes
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await agent._preprocess_input_messages(msgs)

        assert len(result) == 1
        content = result[0].content
        # text description + text URL label + image base64 = 3 blocks
        assert len(content) == 3
        assert content[0] == {"type": "text", "text": "describe this"}
        # URL is surfaced as text so the LLM can reference it in tool calls
        assert content[1]["type"] == "text"
        assert "image.png" in content[1]["text"]
        assert "https://example.com/image.png" in content[1]["text"]
        assert content[2]["type"] == "image"
        assert content[2]["base64"] == expected_b64
        assert content[2]["mime_type"] == "image/png"

    @pytest.mark.asyncio
    async def test_download_failure_falls_back_to_text(self):
        """If image download fails, the block should be replaced with a text description."""
        from unittest.mock import AsyncMock, patch

        agent = self._make_agent()
        blocks = [
            {"type": "image", "url": "https://example.com/broken.png", "mime_type": "image/png"},
        ]
        msgs = [HumanMessage(content=blocks)]

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await agent._preprocess_input_messages(msgs)

        assert len(result) == 1
        content = result[0].content
        assert len(content) == 1
        assert content[0]["type"] == "text"
        assert "broken.png" in content[0]["text"]
        assert "https://example.com/broken.png" in content[0]["text"]
        assert "could not load" in content[0]["text"]


class TestBedrockContentBlockPreprocessing:
    """Tests for preprocess_content_blocks_for_bedrock (image/file/video URL→base64)."""

    def _mock_httpx(self, payload: bytes):
        from unittest.mock import AsyncMock

        mock_response = MagicMock()
        mock_response.content = payload
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        return mock_client

    @pytest.mark.asyncio
    async def test_url_file_converted_to_base64_document(self):
        """URL-based file (document) blocks must be inlined as base64 for Bedrock."""
        import base64
        from unittest.mock import patch

        from ringier_a2a_sdk.utils.bedrock_image_processor import preprocess_content_blocks_for_bedrock

        blocks = [
            {"type": "text", "text": "summarize this"},
            {
                "type": "file",
                "url": "https://example.com/report.pdf?sig=abc",
                "mime_type": "application/pdf",
                "filename": "report.pdf",
            },
        ]
        fake_bytes = b"%PDF-1.4 fake"
        expected_b64 = base64.b64encode(fake_bytes).decode("utf-8")

        with patch("httpx.AsyncClient", return_value=self._mock_httpx(fake_bytes)):
            result = await preprocess_content_blocks_for_bedrock(blocks)

        # text + URL-reference text + base64 document
        assert len(result) == 3
        assert result[0] == {"type": "text", "text": "summarize this"}
        assert result[1]["type"] == "text"
        assert "report.pdf" in result[1]["text"]
        assert result[2]["type"] == "file"
        assert result[2]["base64"] == expected_b64
        assert result[2]["mime_type"] == "application/pdf"
        # Document name must be sanitized (no extension/dots) for Bedrock; carried
        # as top-level `filename` per the codebase convention (content_builder /
        # attachments_store), which Bedrock Converse also reads.
        assert result[2]["filename"] == "report"

    @pytest.mark.asyncio
    async def test_file_mime_inferred_from_extension(self):
        """A file block without a MIME type infers it from the filename extension."""
        from unittest.mock import patch

        from ringier_a2a_sdk.utils.bedrock_image_processor import preprocess_content_blocks_for_bedrock

        blocks = [{"type": "file", "url": "https://example.com/notes.csv"}]
        with patch("httpx.AsyncClient", return_value=self._mock_httpx(b"a,b,c")):
            result = await preprocess_content_blocks_for_bedrock(blocks)

        assert result[-1]["type"] == "file"
        assert result[-1]["mime_type"] == "text/csv"

    @pytest.mark.asyncio
    async def test_base64_file_passes_through(self):
        """Files already supplied as base64 are left unchanged."""
        from ringier_a2a_sdk.utils.bedrock_image_processor import preprocess_content_blocks_for_bedrock

        blocks = [{"type": "file", "base64": "x==", "mime_type": "application/pdf"}]
        result = await preprocess_content_blocks_for_bedrock(blocks)
        assert result == blocks

    @pytest.mark.asyncio
    async def test_file_download_failure_falls_back_to_text(self):
        """A document that fails to download degrades to a text reference."""
        from unittest.mock import AsyncMock, patch

        from ringier_a2a_sdk.utils.bedrock_image_processor import preprocess_content_blocks_for_bedrock

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("boom"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        blocks = [{"type": "file", "url": "https://example.com/x.pdf", "mime_type": "application/pdf"}]
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await preprocess_content_blocks_for_bedrock(blocks)

        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert "could not load" in result[0]["text"]
