"""Integration tests: Full A2A protocol compliance via executor + EventQueue.

These tests verify the A2A event sequence produced by the executor when
processing real LLM responses: TaskStatusUpdateEvent and TaskArtifactUpdateEvent
are emitted in the correct order following the A2A protocol.

Requires real LLM credentials. Skips automatically when credentials are missing.
Run with: uv run pytest tests/integration/test_a2a_streaming.py -m integration -v --timeout=120
"""

import logging
import uuid

import pytest
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    Part,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)
from a2a.utils import new_agent_text_message
from agent_common.models.base import ModelType
from langsmith import testing as t

from app.models.config import UserConfig
from app.models.responses import AgentStreamResponse

from .conftest import (
    ALL_MODELS,
    has_credentials,
)

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def collect_events_from_stream(
    agent,
    prompt: str,
    user_config: UserConfig,
    config: dict,
    timeout: float = 120.0,
) -> tuple[list[AgentStreamResponse], str]:
    """Stream a prompt through the agent and collect all AgentStreamResponse items.

    Returns:
        Tuple of (list of AgentStreamResponse, concatenated text content)
    """
    responses: list[AgentStreamResponse] = []
    full_text = ""

    parts = [Part(root=TextPart(text=prompt))]

    async for item in agent.stream(parts, user_config, config=config):
        responses.append(item)
        if item.content:
            full_text += item.content

    return responses, full_text


async def simulate_executor_events(
    agent,
    prompt: str,
    user_config: UserConfig,
    config: dict,
) -> list[TaskStatusUpdateEvent | TaskArtifactUpdateEvent]:
    """Simulate the executor's event emission logic to collect A2A events.

    This mirrors the executor's _handle_stream_item() logic to produce the
    same TaskStatusUpdateEvent and TaskArtifactUpdateEvent sequence.
    """
    event_queue = EventQueue()
    task_id = str(uuid.uuid4())
    context_id = config["configurable"]["thread_id"]

    updater = TaskUpdater(event_queue, task_id, context_id)

    # Emit initial working status (like executor does)
    await updater.update_status(
        TaskState.working,
        new_agent_text_message("Agent execution started.", context_id, task_id),
    )

    streaming_artifact_id = str(uuid.uuid4())
    first_chunk_sent = False

    parts = [Part(root=TextPart(text=prompt))]

    async for item in agent.stream(parts, user_config, config=config):
        state = item.state
        content = item.content
        metadata = item.metadata or {}

        if state == TaskState.working and metadata.get("streaming_chunk"):
            append = first_chunk_sent
            await updater.add_artifact(
                [Part(root=TextPart(text=content))],
                artifact_id=streaming_artifact_id,
                append=append,
                last_chunk=False,
                metadata={},
            )
            first_chunk_sent = True

        elif state == TaskState.completed:
            if first_chunk_sent:
                await updater.add_artifact(
                    [Part(root=TextPart(text=""))],
                    artifact_id=streaming_artifact_id,
                    append=True,
                    last_chunk=True,
                    metadata={},
                )
                await updater.update_status(TaskState.completed, metadata=metadata or None)
            else:
                await updater.update_status(
                    TaskState.completed,
                    new_agent_text_message(content or "Done", context_id, task_id),
                    metadata=metadata or None,
                )

        elif state == TaskState.failed:
            await updater.update_status(
                TaskState.failed,
                new_agent_text_message(content or "Error", context_id, task_id),
            )

        elif state == TaskState.working:
            await updater.update_status(
                TaskState.working,
                new_agent_text_message(content, context_id, task_id),
                metadata=metadata or None,
            )

    # Drain the queue
    events = []
    while not event_queue.queue.empty():
        event = event_queue.queue.get_nowait()
        if isinstance(event, (TaskStatusUpdateEvent, TaskArtifactUpdateEvent)):
            events.append(event)

    return events


# ---------------------------------------------------------------------------
# Tests: A2A Event Sequence Compliance
# ---------------------------------------------------------------------------


@pytest.mark.langsmith
@pytest.mark.parametrize(
    "model_type",
    ALL_MODELS,
    ids=ALL_MODELS,
)
async def test_simple_prompt_event_sequence(
    model_type: ModelType,
    patched_agent,
    test_user_config,
    make_config,
):
    """Verify the A2A event sequence for a simple prompt.

    Expected sequence:
    1. TaskStatusUpdateEvent(state=working) — initial status
    2. TaskArtifactUpdateEvent(append=False) — first streaming chunk
    3. TaskArtifactUpdateEvent(append=True) * N — subsequent chunks
    4. TaskArtifactUpdateEvent(append=True, last_chunk=True) — stream close
    5. TaskStatusUpdateEvent(state=completed, final=True) — terminal status
    """
    if not has_credentials(model_type):
        pytest.skip(f"No credentials for {model_type}")

    prompt = "Hi, respond in one short sentence."
    t.log_inputs({"prompt": prompt, "model": model_type})
    test_user_config.model = model_type
    config = make_config(model_type)

    events = await simulate_executor_events(
        patched_agent,
        prompt,
        test_user_config,
        config,
    )

    # Must have at least: initial status + some artifact(s) + final status
    assert len(events) >= 3, f"Expected >=3 events, got {len(events)}: {events}"

    # First event: working status
    first = events[0]
    assert isinstance(first, TaskStatusUpdateEvent)
    assert first.status.state == TaskState.working

    # Last event: completed status
    last = events[-1]
    assert isinstance(last, TaskStatusUpdateEvent)
    assert last.status.state == TaskState.completed

    # Artifact events in between
    artifact_events = [e for e in events if isinstance(e, TaskArtifactUpdateEvent)]
    assert len(artifact_events) >= 1, "Expected at least one artifact event"

    # First artifact: append=False (or None)
    if len(artifact_events) >= 2:
        assert artifact_events[0].append in (False, None), "First artifact should have append=False"

    # Last artifact: last_chunk=True
    assert artifact_events[-1].last_chunk is True, "Last artifact must have last_chunk=True"

    # All artifacts share the same artifact_id
    artifact_ids = {e.artifact.artifact_id for e in artifact_events}
    assert len(artifact_ids) == 1, f"All artifacts should share one ID, got {artifact_ids}"

    # Verify content is non-empty (concatenate all artifact parts)
    full_text = ""
    for ae in artifact_events:
        for part in ae.artifact.parts:
            if hasattr(part.root, "text"):
                full_text += part.root.text
    assert len(full_text.strip()) > 0, "Artifact content should not be empty"

    t.log_outputs({"full_text": full_text, "event_count": len(events), "artifact_count": len(artifact_events)})


@pytest.mark.langsmith
@pytest.mark.parametrize(
    "model_type",
    ["claude-sonnet-4.5", "gpt-4o", "gemini-3-flash-preview"],
    ids=["bedrock", "azure", "google-genai"],
)
async def test_time_tool_invocation_events(
    model_type: ModelType,
    patched_agent,
    test_user_config,
    make_config,
):
    """Verify the model uses the get_current_time tool and events follow A2A protocol."""
    if not has_credentials(model_type):
        pytest.skip(f"No credentials for {model_type}")

    prompt = "What is the current time? Use the get_current_time tool."
    t.log_inputs({"prompt": prompt, "model": model_type})
    test_user_config.model = model_type
    config = make_config(model_type)

    events = await simulate_executor_events(
        patched_agent,
        prompt,
        test_user_config,
        config,
    )

    # Basic protocol compliance
    status_events = [e for e in events if isinstance(e, TaskStatusUpdateEvent)]
    assert any(e.status.state == TaskState.working for e in status_events), "Must have working status"

    # Should complete (tool errors would cause 'failed')
    terminal_events = [
        e for e in status_events if e.status.state in (TaskState.completed, TaskState.failed, TaskState.canceled)
    ]
    assert len(terminal_events) >= 1, "Must have at least one terminal status event"
    assert terminal_events[-1].status.state == TaskState.completed, (
        f"Expected completed, got {terminal_events[-1].status.state}"
    )

    # Collect response text
    full_text = ""
    for ae in events:
        if isinstance(ae, TaskArtifactUpdateEvent):
            for part in ae.artifact.parts:
                if hasattr(part.root, "text"):
                    full_text += part.root.text
        elif isinstance(ae, TaskStatusUpdateEvent) and ae.status.message:
            for part in ae.status.message.parts:
                if hasattr(part.root, "text"):
                    full_text += part.root.text

    # Response should mention time-related content
    text_lower = full_text.lower()
    assert any(
        indicator in text_lower for indicator in ["time", ":", "am", "pm", "utc", "cet", "hour", "minute", "o'clock"]
    ), f"Response should mention time, got: {full_text[:200]}"

    t.log_outputs({"full_text": full_text, "event_count": len(events)})


@pytest.mark.langsmith
@pytest.mark.parametrize(
    "model_type",
    ["claude-sonnet-4.5", "gpt-4o", "gemini-3-flash-preview"],
    ids=["bedrock", "azure", "google-genai"],
)
async def test_event_ids_consistent(
    model_type: ModelType,
    patched_agent,
    test_user_config,
    make_config,
):
    """Verify all events share the same task_id and context_id."""
    if not has_credentials(model_type):
        pytest.skip(f"No credentials for {model_type}")

    prompt = "Hello!"
    t.log_inputs({"prompt": prompt, "model": model_type})
    test_user_config.model = model_type
    config = make_config(model_type)

    events = await simulate_executor_events(
        patched_agent,
        prompt,
        test_user_config,
        config,
    )

    assert len(events) >= 2, "Expected at least 2 events"

    task_ids = set()
    context_ids = set()
    for event in events:
        task_ids.add(event.task_id)
        context_ids.add(event.context_id)

    assert len(task_ids) == 1, f"All events must share one task_id, got {task_ids}"
    assert len(context_ids) == 1, f"All events must share one context_id, got {context_ids}"

    t.log_outputs({"event_count": len(events), "task_id": task_ids.pop(), "context_id": context_ids.pop()})
