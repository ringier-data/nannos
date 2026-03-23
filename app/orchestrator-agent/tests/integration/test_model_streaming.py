"""Integration tests: Per-model streaming validation at the agent level.

These tests call OrchestratorDeepAgent.stream() directly to validate that
each supported model streams tokens correctly via AgentStreamResponse.

Requires real LLM credentials. Skips automatically when credentials are missing.
Run with: uv run pytest tests/integration/test_model_streaming.py -m integration -v --timeout=120
"""

import logging
import uuid

import pytest
from a2a.types import Part, TaskState, TextPart
from agent_common.models.base import ModelType, ThinkingLevel
from langsmith import testing as t

from app.models.config import UserConfig
from app.models.responses import AgentStreamResponse

from .conftest import (
    ALL_MODELS,
    THINKING_MODELS,
    has_credentials,
    one_model_per_provider,
)

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def collect_stream(agent, prompt: str, user_config: UserConfig, config: dict):
    """Stream a prompt and collect all responses + concatenated content."""
    responses: list[AgentStreamResponse] = []
    full_text = ""
    parts = [Part(root=TextPart(text=prompt))]

    async for item in agent.stream(parts, user_config, config=config):
        responses.append(item)
        if item.content:
            full_text += item.content

    return responses, full_text


# ---------------------------------------------------------------------------
# Tests: Token streaming per model
# ---------------------------------------------------------------------------


@pytest.mark.langsmith
@pytest.mark.parametrize("model_type", ALL_MODELS, ids=ALL_MODELS)
async def test_model_streams_tokens(
    model_type: ModelType,
    patched_agent,
    test_user_config,
    make_config,
):
    """Verify each model streams multiple AgentStreamResponse items with streaming_chunk metadata."""
    if not has_credentials(model_type):
        pytest.skip(f"No credentials for {model_type}")

    prompt = "Say hello in exactly one sentence."
    t.log_inputs({"prompt": prompt, "model": model_type})

    test_user_config.model = model_type
    config = make_config(model_type)

    responses, full_text = await collect_stream(
        patched_agent,
        prompt,
        test_user_config,
        config,
    )

    assert len(responses) >= 2, f"Expected streaming (>=2 responses), got {len(responses)}"

    # At least one streaming chunk
    streaming_chunks = [r for r in responses if r.metadata and r.metadata.get("streaming_chunk")]
    assert len(streaming_chunks) >= 1, "Expected at least one streaming_chunk"

    # All streaming chunks should be in working state
    for chunk in streaming_chunks:
        assert chunk.state == TaskState.working

    # Final response should be completed
    terminal = [r for r in responses if r.state == TaskState.completed]
    assert len(terminal) >= 1, "Expected a completed response at the end"

    # Concatenated content should be non-empty
    assert len(full_text.strip()) > 0, "Streamed content should not be empty"

    t.log_outputs({"full_text": full_text, "response_count": len(responses), "streaming_chunks": len(streaming_chunks)})


@pytest.mark.langsmith
@pytest.mark.parametrize(
    "model_type,thinking_level",
    [
        (model, level)
        for model, levels in THINKING_MODELS.items()
        for level in levels
    ],
    ids=[
        f"{model}-{level.value}"
        for model, levels in THINKING_MODELS.items()
        for level in levels
    ],
)
async def test_thinking_model_streaming(
    model_type: ModelType,
    thinking_level: ThinkingLevel,
    patched_agent,
    test_user_config,
    make_config,
):
    """Verify thinking models stream correctly and no thinking tokens leak into output."""
    if not has_credentials(model_type):
        pytest.skip(f"No credentials for {model_type}")

    prompt = "What is 15 * 37? Reply with just the answer."
    t.log_inputs({"prompt": prompt, "model": model_type, "thinking_level": thinking_level.value})

    test_user_config.model = model_type
    config = make_config(model_type, thinking_level=thinking_level)

    responses, full_text = await collect_stream(
        patched_agent,
        prompt,
        test_user_config,
        config,
    )

    # Must produce streaming responses
    assert len(responses) >= 2, f"Expected streaming, got {len(responses)} responses"

    # Should reach completed state
    states = [r.state for r in responses]
    assert TaskState.completed in states, f"Expected completed state, got: {set(states)}"

    # Result should contain the correct answer
    assert "555" in full_text, f"Expected '555' in response, got: {full_text[:200]}"

    # No thinking tokens should leak into streamed content
    for chunk in responses:
        if chunk.content:
            assert "<thinking>" not in chunk.content, "Thinking tokens leaked into stream"
            assert "</thinking>" not in chunk.content, "Thinking tokens leaked into stream"
            assert "<antThinking>" not in chunk.content, "Thinking tokens leaked into stream"

    t.log_outputs({"full_text": full_text, "response_count": len(responses)})


@pytest.mark.langsmith
@pytest.mark.parametrize(
    "model_type",
    ["claude-sonnet-4.5", "gpt-4o", "gemini-3-flash-preview"],
    ids=["bedrock", "azure", "vertexai"],
)
async def test_static_tools_available(
    model_type: ModelType,
    patched_agent,
    test_user_config,
    make_config,
):
    """Verify the model can use the get_current_time static tool."""
    if not has_credentials(model_type):
        pytest.skip(f"No credentials for {model_type}")

    prompt = "What time is it right now? Use the get_current_time tool to find out."
    t.log_inputs({"prompt": prompt, "model": model_type})

    test_user_config.model = model_type
    config = make_config(model_type)

    responses, full_text = await collect_stream(
        patched_agent,
        prompt,
        test_user_config,
        config,
    )

    # Should complete successfully (not fail)
    states = [r.state for r in responses]
    assert TaskState.completed in states, f"Expected completed, got: {set(states)}"
    assert TaskState.failed not in states, f"Tool call failed: {full_text[:300]}"

    # Response should mention time
    text_lower = full_text.lower()
    assert any(
        indicator in text_lower
        for indicator in ["time", ":", "am", "pm", "utc", "cet", "hour", "minute", "o'clock", "zurich"]
    ), f"Response should mention time, got: {full_text[:200]}"

    t.log_outputs({"full_text": full_text, "response_count": len(responses)})


@pytest.mark.langsmith
@pytest.mark.parametrize(
    "model_type",
    ["claude-sonnet-4.5", "gpt-4o", "gemini-3-flash-preview"],
    ids=["bedrock", "azure", "vertexai"],
)
async def test_multiturn_context_preservation(
    model_type: ModelType,
    patched_agent,
    test_user_config,
    make_config,
    memory_checkpointer,
):
    """Verify multi-turn conversation preserves context across turns via checkpoint."""
    if not has_credentials(model_type):
        pytest.skip(f"No credentials for {model_type}")

    test_user_config.model = model_type

    # Use a shared context_id for both turns
    shared_ctx = f"multiturn-{uuid.uuid4().hex[:8]}"
    config = {
        "configurable": {
            "thread_id": shared_ctx,
            "__pregel_checkpointer": memory_checkpointer,
        },
        "metadata": {
            "assistant_id": "integration-test-user",
            "user_id": "integration-test-user",
            "conversation_id": shared_ctx,
            "user_name": "Integration Test",
            "model_type": model_type,
            "thinking_level": None,
        },
        "tags": ["integration-test"],
    }

    # Turn 1: introduce name
    _, _ = await collect_stream(
        patched_agent,
        "My name is Aloysius. Remember my name.",
        test_user_config,
        config,
    )

    # Turn 2: ask for name back
    responses2, full_text2 = await collect_stream(
        patched_agent,
        "What is my name?",
        test_user_config,
        config,
    )

    # Should complete
    states = [r.state for r in responses2]
    assert TaskState.completed in states

    # Should recall the name
    assert "Aloysius" in full_text2, f"Expected 'Aloysius' in turn 2, got: {full_text2[:300]}"

    t.log_outputs({"turn2_text": full_text2})


@pytest.mark.langsmith
@pytest.mark.parametrize(
    "model_type",
    ALL_MODELS,
    ids=ALL_MODELS,
)
async def test_failed_state_not_on_simple_prompt(
    model_type: ModelType,
    patched_agent,
    test_user_config,
    make_config,
):
    """Verify simple prompts complete without failures."""
    if not has_credentials(model_type):
        pytest.skip(f"No credentials for {model_type}")

    prompt = "Hello!"
    t.log_inputs({"prompt": prompt, "model": model_type})

    test_user_config.model = model_type
    config = make_config(model_type)

    responses, full_text = await collect_stream(
        patched_agent,
        prompt,
        test_user_config,
        config,
    )

    failed_responses = [r for r in responses if r.state == TaskState.failed]
    assert len(failed_responses) == 0, f"Simple prompt should not fail: {[r.content for r in failed_responses]}"

    completed = [r for r in responses if r.state == TaskState.completed]
    assert len(completed) >= 1, "Should reach completed state"

    t.log_outputs({"full_text": full_text, "response_count": len(responses)})
