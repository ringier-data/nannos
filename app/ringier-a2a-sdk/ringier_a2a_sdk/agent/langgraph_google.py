"""LangGraph + Google Generative AI (Gemini) agent implementation.

This module provides a Google Generative AI-specific subclass of LangGraphAgent that uses
Google Gemini via langchain-google-genai for the LLM and DynamoDB for checkpointing.

ChatGoogleGenerativeAI streams natively with tools, making it useful for validating
the end-to-end streaming pipeline.
"""

import json
import logging
import os

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph.state import CompiledStateGraph

from .dynamodb_checkpointer_mixin import DynamoDBCheckpointerMixin
from .langgraph import LangGraphAgent

logger = logging.getLogger(__name__)

# Re-export FinalResponseSchema for backward compatibility
from .langgraph import FinalResponseSchema  # noqa: F401, E402


class LangGraphGoogleGenAIAgent(DynamoDBCheckpointerMixin, LangGraphAgent):
    """LangGraph agent using Google Generative AI (Gemini) and DynamoDB checkpointing.

    Uses ChatGoogleGenerativeAI with streaming=True so tokens are emitted incrementally
    even when tools are bound to the model.

    Configuration:
    - GCP_KEY: JSON service account key (optional, falls back to ADC)
    - GCP_PROJECT_ID: Google Cloud project ID (required)
    - GCP_LOCATION: Google Cloud location (optional, default: europe-west4)
    - GCP_MODEL_ID: Gemini model ID (optional, default: gemini-2.0-flash)

    Subclasses must still implement:
    - _get_mcp_connections(): Return MCP server connection configuration
    - _get_system_prompt(): Return agent-specific system prompt
    - _get_checkpoint_namespace(): Return unique checkpoint namespace

    Optional overrides:
    - _get_gcp_model_id(): Return Google AI model ID (default: gemini-2.0-flash)
    - _get_thinking_level(): Return thinking level (minimal/low/medium/high, default: None for disabled)
    - _get_middleware(): Return agent middleware list (default: [])
    - _get_tool_interceptors(): Return tool interceptors (default: [])
    - _create_graph(): Create LangGraph with tools (has default implementation)
    """

    def __init__(self, tool_query_regex: str | None = None, recursion_limit: int | None = None):
        """Initialize the LangGraph Google Generative AI Agent.

        Sets up Google Generative AI configuration before calling the generic LangGraphAgent
        init, which will call _create_model() and _create_checkpointer().

        Args:
            tool_query_regex: Optional regex pattern to filter MCP tools by name
            recursion_limit: Maximum number of LangGraph steps (default: from LANGGRAPH_RECURSION_LIMIT env var or 50)
        """
        self.gcp_project = os.getenv("GCP_PROJECT_ID")
        self.gcp_location = os.getenv("GCP_LOCATION", "europe-west4")
        self.gcp_model_id = self._get_gcp_model_id()
        self.thinking_level = self._get_thinking_level()

        super().__init__(tool_query_regex=tool_query_regex, recursion_limit=recursion_limit)

    def _create_model(self) -> BaseChatModel:
        """Create ChatGoogleGenerativeAI model with streaming=True.

        Credentials are loaded from the GCP_KEY environment variable (JSON
        service account key). If GCP_KEY is absent, falls back to Application
        Default Credentials so the model still works with `gcloud auth
        application-default login` during local development.

        streaming=True is set explicitly to ensure token-level streaming is
        active even when tools are bound to the model. This proves the
        downstream streaming pipeline (SSE → orchestrator → backend → Socket.IO)
        works correctly.

        Returns:
            ChatGoogleGenerativeAI model instance
        """
        # Load credentials from GCP_KEY or fall back to ADC
        credentials = None
        gcp_key = os.getenv("GCP_KEY")
        if gcp_key:
            try:
                from google.oauth2 import service_account

                credentials = service_account.Credentials.from_service_account_info(
                    json.loads(gcp_key),
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
                logger.info("Google Generative AI: using service account credentials from GCP_KEY")
            except (json.JSONDecodeError, ValueError) as e:
                raise ValueError(f"Failed to parse GCP_KEY as valid service account JSON: {e}") from e
        else:
            logger.info("Google Generative AI: GCP_KEY not set, falling back to Application Default Credentials")

        # Configure thinking mode if enabled
        gemini_thinking_level = None
        include_thoughts = False
        temperature = 0.7

        if self.thinking_level:
            gemini_thinking_level = self.thinking_level
            # Match orchestrator strategy: include_thoughts=True to stream thoughts properly
            include_thoughts = True
            temperature = 1.0  # CRITICAL: Gemini 3.0+ requires 1.0 with thinking mode
            logger.info(f"Gemini thinking mode enabled with level={gemini_thinking_level}")

        model = ChatGoogleGenerativeAI(
            model=self.gcp_model_id,
            credentials=credentials,
            project=self.gcp_project,
            location=self.gcp_location,
            temperature=temperature,
            thinking_level=gemini_thinking_level,
            include_thoughts=include_thoughts,
            streaming=True,  # Explicit — proves end-to-end streaming pipeline
        )

        logger.info(
            f"Initialized Google Generative AI model (streaming=True): {self.gcp_model_id} "
            f"project={self.gcp_project} location={self.gcp_location} "
            f"thinking_level={gemini_thinking_level}"
        )
        return model

    def _get_gcp_model_id(self) -> str:
        """Return Google Generative AI model ID. Default: gemini-2.0-flash via env var."""
        return os.getenv("GCP_MODEL_ID", "gemini-2.0-flash")

    def _get_thinking_level(self) -> str | None:
        """Return thinking level if enabled. Can be: minimal, low, medium, high. Default: None (disabled)."""
        return os.getenv("GCP_THINKING_LEVEL")

    def _create_graph(self, tools: list[BaseTool]) -> CompiledStateGraph:
        """Create LangGraph with explicit FinalResponseSchema tool for Gemini.

        Gemini's AutoStrategy resolves to ToolStrategy but the model embeds the
        structured JSON in content text instead of tool_call_chunks, preventing
        incremental streaming of the response. Using response_format=None with an
        explicit FinalResponseSchema tool ensures proper tool_call_chunks output.

        This matches the orchestrator's approach in graph_factory.py.
        """
        from deepagents import create_deep_agent

        return create_deep_agent(
            model=self._model,
            tools=tools + [self._create_response_tool()],
            subagents=[],
            system_prompt=self._get_system_prompt(),
            checkpointer=self._checkpointer,
            middleware=self._get_middleware(),
            response_format=None,
        )
