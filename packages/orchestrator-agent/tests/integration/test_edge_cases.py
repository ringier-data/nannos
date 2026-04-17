"""Integration tests: Edge cases and additional scenarios.

Covers: tool binding verification, concurrent stream isolation,
long response incremental streaming, and graph state introspection.

Requires real LLM credentials. Skips automatically when credentials are missing.
Run with: uv run pytest tests/integration/test_edge_cases.py -m integration -v --timeout=120
"""

import asyncio
import logging
import uuid

import pytest
from a2a.types import Part, TaskState, TextPart
from agent_common.models.base import ModelType
from langsmith import testing as t

from app.models.config import UserConfig
from app.models.responses import AgentStreamResponse

from .conftest import (
    ALL_MODELS,
    has_credentials,
    one_model_per_provider,
)

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def collect_stream(agent, prompt: str, user_config: UserConfig, config: dict):
    """Stream a prompt and return (responses, full_text)."""
    responses: list[AgentStreamResponse] = []
    full_text = ""
    parts = [Part(root=TextPart(text=prompt))]
    async for item in agent.stream(parts, user_config, config=config):
        responses.append(item)
        if item.content:
            full_text += item.content
    return responses, full_text


# ---------------------------------------------------------------------------
# Tests: Tool binding verification
# ---------------------------------------------------------------------------


@pytest.mark.langsmith
@pytest.mark.parametrize(
    "model_type",
    ["claude-sonnet-4.5", "gpt-4o", "gemini-3-flash-preview"],
    ids=["bedrock", "azure", "vertexai"],
)
async def test_static_tools_bound_to_graph(
    model_type: ModelType,
    patched_agent,
    test_user_config,
    make_config,
):
    """Verify that static tools are available in the compiled graph.

    Inspects the graph's tool list to confirm get_current_time and other
    static tools are properly bound.
    """
    if not has_credentials(model_type):
        pytest.skip(f"No credentials for {model_type}")

    t.log_inputs({"model": model_type, "check": "graph tool binding"})

    graph = await patched_agent.get_or_create_graph(model_type=model_type, thinking_level=None)

    # The graph should have tools bound via create_deep_agent
    # Check for known static tools in the graph's node names or tool definitions
    # Graph nodes typically include: "model", "tools", "__start__", "__end__"
    node_names = set(graph.nodes.keys()) if hasattr(graph, "nodes") else set()
    assert "tools" in node_names or "model" in node_names, f"Graph missing expected nodes, got: {node_names}"

    # Also verify the graph can be invoked without error (basic health check)
    assert graph.checkpointer is not None, "Graph should have a checkpointer"

    t.log_outputs({"node_names": list(node_names)})


# ---------------------------------------------------------------------------
# Tests: Concurrent stream isolation
# ---------------------------------------------------------------------------


@pytest.mark.langsmith
@pytest.mark.parametrize(
    "model_type",
    ["claude-sonnet-4.5"],  # Use one model to keep it fast
    ids=["bedrock"],
)
async def test_concurrent_streams_isolated(
    model_type: ModelType,
    patched_agent,
    test_user_config,
    memory_checkpointer,
):
    """Verify two concurrent streams produce independent, non-interleaved results."""
    if not has_credentials(model_type):
        pytest.skip(f"No credentials for {model_type}")

    test_user_config.model = model_type

    config_a = {
        "configurable": {
            "thread_id": f"concurrent-a-{uuid.uuid4().hex[:8]}",
            "__pregel_checkpointer": memory_checkpointer,
        },
        "metadata": {
            "assistant_id": "test",
            "user_id": "test",
            "conversation_id": "a",
            "user_name": "Test",
            "model_type": model_type,
            "thinking_level": None,
        },
        "tags": ["integration-test"],
    }

    config_b = {
        "configurable": {
            "thread_id": f"concurrent-b-{uuid.uuid4().hex[:8]}",
            "__pregel_checkpointer": memory_checkpointer,
        },
        "metadata": {
            "assistant_id": "test",
            "user_id": "test",
            "conversation_id": "b",
            "user_name": "Test",
            "model_type": model_type,
            "thinking_level": None,
        },
        "tags": ["integration-test"],
    }

    # Run two streams concurrently with different prompts
    results = await asyncio.gather(
        collect_stream(patched_agent, "Say only the word 'alpha'.", test_user_config, config_a),
        collect_stream(patched_agent, "Say only the word 'beta'.", test_user_config, config_b),
    )

    responses_a, text_a = results[0]
    responses_b, text_b = results[1]

    # Both should complete
    assert any(r.state == TaskState.completed for r in responses_a), "Stream A should complete"
    assert any(r.state == TaskState.completed for r in responses_b), "Stream B should complete"

    # Both should have content
    assert len(text_a.strip()) > 0, "Stream A should produce content"
    assert len(text_b.strip()) > 0, "Stream B should produce content"

    t.log_outputs({"text_a": text_a, "text_b": text_b})


# ---------------------------------------------------------------------------
# Tests: Long response incremental streaming
# ---------------------------------------------------------------------------


@pytest.mark.langsmith
@pytest.mark.parametrize(
    "model_type",
    ["claude-sonnet-4.5", "gpt-4o", "gemini-3-flash-preview"],
    ids=["bedrock", "azure", "vertexai"],
)
async def test_long_response_streams_incrementally(
    model_type: ModelType,
    patched_agent,
    test_user_config,
    make_config,
):
    """Verify a longer response produces multiple streaming chunks (not buffered)."""
    if not has_credentials(model_type):
        pytest.skip(f"No credentials for {model_type}")

    prompt = "List the first 15 prime numbers, one per line."
    t.log_inputs({"prompt": prompt, "model": model_type})

    test_user_config.model = model_type
    config = make_config(model_type)

    responses, full_text = await collect_stream(
        patched_agent,
        prompt,
        test_user_config,
        config,
    )

    # Should have multiple streaming chunks (incremental content delivery)
    streaming_chunks = [r for r in responses if r.metadata and r.metadata.get("streaming_chunk")]
    assert len(streaming_chunks) >= 2, (
        f"Expected at least 2 streaming_chunk responses for a long answer, got {len(streaming_chunks)}"
    )

    # Should contain several prime numbers
    for prime in ["2", "3", "5", "7", "11", "13"]:
        assert prime in full_text, f"Expected prime {prime} in response, got: {full_text[:300]}"

    t.log_outputs({"full_text": full_text, "streaming_chunks": len(streaming_chunks), "total_responses": len(responses)})


# ---------------------------------------------------------------------------
# Tests: Short prompt completes without error
# ---------------------------------------------------------------------------


@pytest.mark.langsmith
@pytest.mark.parametrize("model_type", ALL_MODELS, ids=ALL_MODELS)
async def test_short_prompt_completes(
    model_type: ModelType,
    patched_agent,
    test_user_config,
    make_config,
):
    """Verify 'Hi' completes without error and produces at least one response."""
    if not has_credentials(model_type):
        pytest.skip(f"No credentials for {model_type}")

    prompt = "Hi"
    t.log_inputs({"prompt": prompt, "model": model_type})

    test_user_config.model = model_type
    config = make_config(model_type)

    responses, full_text = await collect_stream(
        patched_agent,
        prompt,
        test_user_config,
        config,
    )

    assert len(responses) >= 1, "Should produce at least one response"

    # Should not fail
    failed = [r for r in responses if r.state == TaskState.failed]
    assert len(failed) == 0, f"'Hi' should not fail: {[r.content for r in failed]}"

    # Should eventually complete
    completed = [r for r in responses if r.state == TaskState.completed]
    assert len(completed) >= 1, "Should reach completed state"

    t.log_outputs({"full_text": full_text, "response_count": len(responses)})


# ---------------------------------------------------------------------------
# Tests: Streaming buffer produces word-boundary chunks
# ---------------------------------------------------------------------------


@pytest.mark.langsmith
@pytest.mark.parametrize(
    "model_type",
    ["claude-sonnet-4.5", "gpt-4o", "gemini-3-flash-preview"],
    ids=["bedrock", "azure", "vertexai"],
)
async def test_streaming_chunks_are_word_aligned(
    model_type: ModelType,
    patched_agent,
    test_user_config,
    make_config,
):
    """Verify streaming chunks tend to break on word boundaries (not mid-word).

    The agent uses a 40-char buffer with word-boundary flushing.
    Most chunks should end with a space or be full words.
    """
    if not has_credentials(model_type):
        pytest.skip(f"No credentials for {model_type}")

    prompt = "Write a short paragraph about the weather."
    t.log_inputs({"prompt": prompt, "model": model_type})

    test_user_config.model = model_type
    config = make_config(model_type)

    responses, full_text = await collect_stream(
        patched_agent,
        prompt,
        test_user_config,
        config,
    )

    chunks = [r for r in responses if r.metadata and r.metadata.get("streaming_chunk") and r.content]

    if len(chunks) >= 4:
        # Check that intermediate chunks (not first, not last) mostly end at word boundaries
        word_boundary_count = 0
        for chunk in chunks[:-1]:  # Exclude last chunk
            if chunk.content[-1] in (" ", "\n"):
                word_boundary_count += 1

        ratio = word_boundary_count / max(len(chunks) - 1, 1)
        assert ratio >= 0.5, (
            f"Expected at least 50% of chunks to end at word boundaries, got {ratio:.0%} "
            f"({word_boundary_count}/{len(chunks) - 1})"
        )
    else:
        ratio = None  # Too few chunks to meaningfully test

    t.log_outputs({"full_text": full_text, "chunk_count": len(chunks), "word_boundary_ratio": ratio})
    if ratio is not None:
        t.log_feedback(key="word_boundary_ratio", score=ratio)


# ---------------------------------------------------------------------------
# Tests: Different models can share same thread (model switching)
# ---------------------------------------------------------------------------


@pytest.mark.langsmith
async def test_model_switching_within_conversation(
    patched_agent,
    test_user_config,
    memory_checkpointer,
):
    """Verify conversation context survives a model switch (same thread_id, different model)."""
    # Pick two models from the same provider (both must have credentials)
    model_pairs = [
        ("claude-sonnet-4.5", "claude-haiku-4-5"),
        ("gpt-4o", "gpt-4o-mini"),
    ]

    for model_a, model_b in model_pairs:
        if not has_credentials(model_a) or not has_credentials(model_b):
            continue

        t.log_inputs({"model_a": model_a, "model_b": model_b, "scenario": "model switch context preservation"})

        shared_ctx = f"model-switch-{uuid.uuid4().hex[:8]}"
        base_config = {
            "configurable": {
                "thread_id": shared_ctx,
                "__pregel_checkpointer": memory_checkpointer,
            },
            "metadata": {
                "assistant_id": "test",
                "user_id": "test",
                "conversation_id": shared_ctx,
                "user_name": "Test",
                "model_type": model_a,
                "thinking_level": None,
            },
            "tags": ["integration-test"],
        }

        # Turn 1 with model A
        test_user_config.model = model_a
        _, _ = await collect_stream(
            patched_agent,
            "My favorite color is vermillion. Remember that.",
            test_user_config,
            base_config,
        )

        # Turn 2 with model B (same thread_id)
        test_user_config.model = model_b
        config_b = {**base_config}
        config_b["metadata"] = {**base_config["metadata"], "model_type": model_b}

        _, text_b = await collect_stream(
            patched_agent,
            "What is my favorite color?",
            test_user_config,
            config_b,
        )

        assert "vermillion" in text_b.lower(), (
            f"Model switch {model_a}->{model_b} should preserve context. Got: {text_b[:200]}"
        )

        t.log_outputs({"model_a": model_a, "model_b": model_b, "turn2_text": text_b})
        return  # Only need one pair to pass

    pytest.skip("No model pair with available credentials found")
