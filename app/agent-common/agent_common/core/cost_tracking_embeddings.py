"""BedrockEmbeddings subclass that reports costs via CostLogger.

LangChain's BaseCallbackHandler has no embedding hook, so we subclass
BedrockEmbeddings and override _invoke_model to log costs directly after
every API call.  This covers all embedding paths:

- embed_documents / aembed_documents (document storage)
- embed_query / aembed_query (semantic search)
- Boundary-detection calls in semantic_chunking.chunk_with_context
- AsyncPostgresStore.aput calls that embed the 'contextualized_content' field
"""

import logging
from typing import Any, Dict, Optional

from langchain_aws import BedrockEmbeddings
from ringier_a2a_sdk.cost_tracking import CostLogger
from ringier_a2a_sdk.cost_tracking.logger import (
    add_to_cost_batch,
    get_request_conversation_id,
    get_request_user_sub,
    is_cost_batching_active,
)

logger = logging.getLogger(__name__)


class CostTrackingBedrockEmbeddings(BedrockEmbeddings):
    """BedrockEmbeddings that logs input-token costs via CostLogger.

    Drop-in replacement for BedrockEmbeddings.  Pass a CostLogger instance
    at construction time; all subsequent embed calls will queue a cost record
    automatically.

    For Titan Embeddings the actual ``inputTextTokenCount`` from the API
    response is used.  For other providers that do not return a token count,
    the count is estimated as ``total_chars // 4``.

    Args:
        cost_logger: CostLogger instance to log costs to.
        **kwargs: Forwarded verbatim to BedrockEmbeddings.
    """

    # Pydantic v2 model fields - exclude from serialization
    model_config = {"arbitrary_types_allowed": True}

    cost_logger: Optional[CostLogger] = None

    def _invoke_model(self, input_body: Dict[str, Any] = {}) -> Dict[str, Any]:
        """Call the Bedrock model and log token cost from the response."""
        response = super()._invoke_model(input_body)

        if self.cost_logger:
            user_sub = get_request_user_sub()
            conversation_id = get_request_conversation_id()
            logger.debug(
                f"[COST TRACKING] Inside _invoke_model: user_sub={user_sub}, conversation_id={conversation_id}"
            )
            if user_sub:
                # Titan Embeddings returns the actual token count in the response.
                token_count = response.get("inputTextTokenCount")

                if token_count is None:
                    # Fallback: estimate from the input for models that don't
                    # return a token count (e.g. Cohere, Nova).
                    input_text = input_body.get("inputText", "") or " ".join(input_body.get("texts", []))
                    token_count = max(1, len(input_text) // 4)

                # Check if cost batching is active (e.g., during document indexing)
                if is_cost_batching_active():
                    # Accumulate costs instead of logging immediately
                    add_to_cost_batch(
                        token_count=token_count,
                        provider="bedrock_embeddings",
                        model_name=self.model_id,
                    )
                    logger.debug(f"[COST TRACKING] Added to batch: {token_count} tokens (model={self.model_id})")
                else:
                    # Log immediately if not batching
                    self.cost_logger.log_cost_async(
                        user_sub=user_sub,
                        billing_unit_breakdown={"input_tokens": token_count},
                        provider="bedrock_embeddings",
                        model_name=self.model_id,
                        conversation_id=conversation_id,  # Include conversation_id for attribution
                    )
                    logger.debug(
                        f"[COST TRACKING] Embedding cost queued: {token_count} tokens "
                        f"(model={self.model_id}, conversation={conversation_id})"
                    )
            else:
                logger.warning("[COST TRACKING] user_sub is None, cost NOT logged for embeddings call")

        return response

    # async def aembed_documents(self, texts: List[str]) -> List[List[float]]:
    #     """Async embed documents with context propagation.

    #     Uses asyncio.to_thread() which automatically propagates ContextVars
    #     to the worker thread, ensuring user_sub is available in _invoke_model.
    #     """
    #     return await asyncio.to_thread(super().embed_documents, texts)

    # async def aembed_query(self, text: str) -> List[float]:
    #     """Async embed query with context propagation.

    #     Uses asyncio.to_thread() which automatically propagates ContextVars
    #     to the worker thread, ensuring user_sub is available in _invoke_model.
    #     """
    #     return await asyncio.to_thread(super().embed_query, text)
