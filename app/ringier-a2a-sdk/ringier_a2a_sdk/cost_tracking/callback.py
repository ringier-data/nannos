"""Cost tracking callback handler for usage metering."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from .logger import CostLogger

logger = logging.getLogger(__name__)


class CostTrackingCallback(BaseCallbackHandler):
    """
    Callback handler to track billing unit usage and costs.

    Extracts usage metadata from LLM responses and queues them
    for async batch logging to the backend API.

    This callback should be registered with LLM model instances in A2A agents.
    """

    def __init__(self, cost_logger: CostLogger, sub_agent_id: Optional[int] = None):
        """
        Initialize the cost tracking callback.

        Args:
            cost_logger: The async cost logger instance
            sub_agent_id: Optional sub-agent ID for attribution (if this agent is registered in backend)
        """
        super().__init__()
        self.cost_logger = cost_logger
        self.sub_agent_id = sub_agent_id

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """
        Called when LLM finishes.

        Extracts usage metadata and queues for logging.

        Args:
            response: LLM result with generations
            **kwargs: Additional context (run_id, tags, etc.)
        """
        try:
            logger.info("[COST TRACKING] on_llm_end callback invoked")
            for generation_list in response.generations:
                for generation in generation_list:
                    message = generation.message

                    # Extract usage metadata if available
                    if not hasattr(message, "usage_metadata") or not message.usage_metadata:
                        logger.info("[COST TRACKING] No usage_metadata in LLM response, skipping cost tracking")
                        continue

                    usage_metadata = message.usage_metadata
                    response_metadata = getattr(message, "response_metadata", {})

                    # Extract provider and model info
                    provider = self._detect_provider(response_metadata)
                    model_name = response_metadata.get("model_name", "unknown")

                    # Extract billing unit breakdown
                    billing_unit_breakdown = self._extract_billing_unit_breakdown(usage_metadata, response_metadata)

                    if not billing_unit_breakdown:
                        logger.warning(f"Empty billing unit breakdown for {provider}/{model_name}, skipping")
                        continue

                    # Get run context from LangSmith
                    run_id = kwargs.get("run_id")
                    langsmith_run_id = str(run_id) if run_id else None

                    # TODO: Extract trace_id (root run ID) for LangSmith trace linking
                    # The langsmith_run_id is sufficient for now
                    langsmith_trace_id = None

                    # Extract user_id, conversation_id, and sub_agent_id from tags
                    # LangGraph passes these via tags in config (unified for local and remote agents)
                    tags = kwargs.get("tags", [])
                    user_id = None
                    conversation_id = None
                    sub_agent_id = None

                    logger.debug(f"[COST TRACKING] Processing LLM callback with tags: {tags}")

                    for tag in tags:
                        if tag.startswith("user:"):
                            user_id = tag.split(":", 1)[1]
                        elif tag.startswith("conversation:"):
                            conversation_id = tag.split(":", 1)[1]
                        elif tag.startswith("sub_agent:"):
                            try:
                                sub_agent_id = int(tag.split(":", 1)[1])
                            except (ValueError, IndexError) as e:
                                logger.warning(f"Failed to parse sub_agent_id from tag '{tag}': {e}")

                    if not user_id:
                        logger.warning("No user_id found in callback tags, skipping cost tracking")
                        continue

                    # Queue for async logging with extracted sub_agent_id from tags
                    # This provides unified tracking for both local and remote agents
                    self.cost_logger.log_cost_async(
                        user_id=user_id,
                        provider=provider,
                        model_name=model_name,
                        billing_unit_breakdown=billing_unit_breakdown,
                        conversation_id=conversation_id,
                        langsmith_run_id=langsmith_run_id,
                        langsmith_trace_id=langsmith_trace_id,
                        invoked_at=datetime.now(tz=timezone.utc),
                        _sub_agent_id_from_tag=sub_agent_id,  # Internal: extracted from tag, not user-settable
                    )

                    logger.info(
                        f"[COST TRACKING] Queued for {provider}/{model_name}: "
                        f"{sum(billing_unit_breakdown.values())} units (user={user_id}, "
                        f"conversation={conversation_id}, sub_agent={sub_agent_id})"
                    )

        except Exception as e:
            logger.error(f"Error in cost tracking callback: {e}", exc_info=True)
            # Don't re-raise - callback errors shouldn't break LLM calls

    def _detect_provider(self, response_metadata: Dict[str, Any]) -> str:
        """
        Detect provider from response metadata.

        Args:
            response_metadata: Response metadata dict

        Returns:
            Provider name ('bedrock_converse', 'openai', 'google_genai', etc.')
        """
        # Check for explicit provider field
        if "model_provider" in response_metadata:
            return response_metadata["model_provider"]

        # Check for Vertex AI Gemini specific fields (before generic checks)
        if any(
            key in response_metadata for key in ["prompt_token_count", "candidates_token_count", "thoughts_token_count"]
        ):
            return "google_genai"

        # Infer from other fields
        if "system_fingerprint" in response_metadata:
            return "openai"  # Azure OpenAI or OpenAI

        if "ResponseMetadata" in response_metadata:
            return "bedrock_converse"  # AWS Bedrock (ChatBedrockConverse)

        if "token_usage" in response_metadata:
            return "openai"  # Likely OpenAI-compatible

        logger.warning(f"Could not detect provider from metadata: {response_metadata.keys()}")
        return "unknown"

    def _extract_billing_unit_breakdown(
        self,
        usage_metadata: Dict[str, Any],
        response_metadata: Dict[str, Any],
    ) -> Dict[str, int]:
        """
        Extract billing unit breakdown from usage metadata using LangChain's nested structure.

        LangChain enforces this structure:
        {
            "input_tokens": 350,
            "output_tokens": 240,
            "total_tokens": 590,
            "input_token_details": {
                "audio": 10,
                "cache_creation": 200,
                "cache_read": 100,
            },
            "output_token_details": {
                "audio": 10,
                "reasoning": 200,
            },
        }

        The detail dicts contain tokens that are INCLUDED in the top-level counts.
        We calculate base tokens as: base = total - sum(details)

        Args:
            usage_metadata: Usage metadata from AIMessage
            response_metadata: Response metadata from AIMessage

        Returns:
            Dict of billing_unit -> count (only non-zero values)
            Format: {"base_input_tokens": X, "cache_read_input_tokens": Y, ...}
        """
        breakdown = {}

        # Get total token counts (use prompt_tokens/completion_tokens for legacy OpenAI format)
        total_input = usage_metadata.get("input_tokens") or usage_metadata.get("prompt_tokens") or 0
        total_output = usage_metadata.get("output_tokens") or usage_metadata.get("completion_tokens") or 0

        # Process input token details (nested breakdown)
        input_details = usage_metadata.get("input_token_details", {})
        input_details_sum = 0

        for detail_key, detail_count in input_details.items():
            if detail_count > 0:
                # Map detail keys to standard billing unit names
                # LangChain uses: audio, cache_creation, cache_read
                billing_unit = f"{detail_key}_input_tokens"
                breakdown[billing_unit] = detail_count
                input_details_sum += detail_count

        # Calculate base input tokens (total - details)
        base_input = total_input - input_details_sum
        if base_input > 0:
            breakdown["base_input_tokens"] = base_input
        elif base_input < 0:
            logger.warning(
                f"Negative base_input_tokens calculated: total={total_input}, details_sum={input_details_sum}. "
                f"Using 0 for base and emitting details as-is."
            )
            breakdown["base_input_tokens"] = 0
        elif total_input > 0:
            # base_input == 0 but total_input > 0 (all tokens are in details)
            breakdown["base_input_tokens"] = 0

        # Process output token details (nested breakdown)
        output_details = usage_metadata.get("output_token_details", {})
        output_details_sum = 0

        for detail_key, detail_count in output_details.items():
            if detail_count > 0:
                # LangChain uses: audio, reasoning
                billing_unit = f"{detail_key}_output_tokens"
                breakdown[billing_unit] = detail_count
                output_details_sum += detail_count

        # Calculate base output tokens (total - details)
        base_output = total_output - output_details_sum
        if base_output > 0:
            breakdown["base_output_tokens"] = base_output
        elif base_output < 0:
            logger.warning(
                f"Negative base_output_tokens calculated: total={total_output}, details_sum={output_details_sum}. "
                f"Using 0 for base and emitting details as-is."
            )
            breakdown["base_output_tokens"] = 0
        elif total_output > 0:
            # base_output == 0 but total_output > 0 (all tokens are in details)
            breakdown["base_output_tokens"] = 0

        # Remove negative values and zero values from details only
        # Keep zero base tokens if there were non-zero total tokens (edge case handling)
        result = {}
        for k, v in breakdown.items():
            if v > 0:
                result[k] = v
            elif v == 0 and k in ("base_input_tokens", "base_output_tokens"):
                # Keep zero base tokens if they were explicitly calculated (edge case)
                if (k == "base_input_tokens" and total_input > 0) or (k == "base_output_tokens" and total_output > 0):
                    result[k] = v

        return result
