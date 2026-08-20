"""Attachment forwarding: what a sub-agent actually receives for an attached file.

``tests/test_content_builder.py`` already covers the parsing half — A2A file
parts to text descriptions and typed ContentBlocks. What was untested is the
*forwarding* half: whether those blocks reach a sub-agent, and in what shape.
That is the path the ticket means by "verify parsing (e.g. attachments)", and it
is where the interesting behaviour lives, because it differs by sub-agent:

- A **text-only** sub-agent gets file URIs appended to its instruction as text,
  so it can fetch them with its own tools.
- A **multimodal** sub-agent gets the blocks themselves, after LLM-based
  relevance filtering (stubbed here — that filter is the only part of this path
  that needs a model).

There is also a deliberate asymmetry worth protecting: the orchestrator's own
prompt never sees raw URIs, only descriptions, so it cannot hallucinate or
corrupt a long pre-signed URL. Sub-agents get the exact URI. If those ever
converge, files silently break.

URLs here are plain https so nothing touches object storage; an ``s3://`` URI
would trigger presigned-URL generation.
"""

from __future__ import annotations

import json

import pytest
from a2a.types import Part

from app.core.content_builder import build_text_content
from app.middleware.dynamic_tool_dispatch import DynamicToolDispatchMiddleware
from tests.support.graph_harness import runtime_context
from tests.support.mock_subagents import MockSubAgent

IMAGE_URL = "https://files.example.com/vacation.jpg?sig=abc123"
PDF_URL = "https://files.example.com/report.pdf?sig=def456"


def _file_part(url: str, filename: str, media_type: str) -> Part:
    return Part(url=url, filename=filename, media_type=media_type)


async def _blocks_for(*parts: Part) -> tuple[str, list]:
    """Run the real parser so the blocks under test are the ones production makes."""
    return await build_text_content(list(parts))


async def _dispatch_with_files(agent: MockSubAgent, blocks: list, description: str = "look at this") -> None:
    """Dispatch a `task` call with attachments pending, via the real middleware."""
    context = runtime_context(agent, pending_file_blocks=blocks)
    middleware = DynamicToolDispatchMiddleware()
    await middleware._adispatch_task_tool(
        {
            "id": "c1",
            "name": "task",
            "args": {"subagent_type": agent.name, "description": description},
            "type": "tool_call",
        },
        context,
        {"messages": []},
        {"configurable": {"thread_id": "attachment-test"}},
    )


# ---------------------------------------------------------------------------
# The orchestrator/sub-agent asymmetry
# ---------------------------------------------------------------------------


async def test_orchestrator_text_omits_the_uri_but_the_block_keeps_it():
    """The anti-hallucination guarantee, end to end.

    The orchestrator prompt gets a description only; the block carries the exact
    URI for deterministic forwarding. A long pre-signed URL in the prompt is
    something an LLM will eventually mangle.
    """
    text, blocks = await _blocks_for(_file_part(IMAGE_URL, "vacation.jpg", "image/jpeg"))

    assert IMAGE_URL not in text, f"raw URI leaked into the orchestrator prompt: {text}"
    assert "vacation.jpg" in text, "the orchestrator should still know a file is attached"

    assert len(blocks) == 1
    assert blocks[0]["url"] == IMAGE_URL
    assert blocks[0]["type"] == "image"


async def test_pdf_becomes_a_file_block_not_an_image_block():
    """Block type drives whether a multimodal agent can consume it natively."""
    _, blocks = await _blocks_for(_file_part(PDF_URL, "report.pdf", "application/pdf"))

    assert blocks[0]["type"] == "file"
    assert blocks[0]["url"] == PDF_URL


# ---------------------------------------------------------------------------
# Text-only sub-agents: URIs as text
# ---------------------------------------------------------------------------


async def test_text_only_subagent_receives_the_uri_in_its_instruction():
    _, blocks = await _blocks_for(_file_part(IMAGE_URL, "vacation.jpg", "image/jpeg"))
    agent = MockSubAgent("agent-runner", "Runs jobs.", input_modes=["text"])

    await _dispatch_with_files(agent, blocks, description="describe the attached image")

    received = agent.received[0]
    assert "describe the attached image" in received
    assert "[Attached files]" in received
    assert IMAGE_URL in received, "a text-only agent can only reach the file via the URI"
    assert "image/jpeg" in received, "mime type tells the agent how to handle it"


async def test_text_only_subagent_receives_every_attachment():
    _, blocks = await _blocks_for(
        _file_part(IMAGE_URL, "vacation.jpg", "image/jpeg"),
        _file_part(PDF_URL, "report.pdf", "application/pdf"),
    )
    agent = MockSubAgent("agent-runner", "Runs jobs.", input_modes=["text"])

    await _dispatch_with_files(agent, blocks)

    received = agent.received[0]
    assert IMAGE_URL in received
    assert PDF_URL in received


async def test_no_attachments_means_no_file_section():
    """A bare instruction must not grow an empty '[Attached files]' block."""
    agent = MockSubAgent("agent-runner", "Runs jobs.", input_modes=["text"])

    await _dispatch_with_files(agent, [], description="just do the thing")

    assert agent.received == ["just do the thing"]


# ---------------------------------------------------------------------------
# Multimodal sub-agents: the blocks themselves
# ---------------------------------------------------------------------------


async def test_multimodal_subagent_receives_the_content_block(monkeypatch):
    """The multimodal path filters files by relevance with an LLM. Stubbed to
    pass everything through, so this stays credential-free and asserts the
    forwarding rather than the filtering."""

    async def _no_filtering(self, description_text, blocks):
        return blocks

    monkeypatch.setattr(DynamicToolDispatchMiddleware, "_filter_files_with_llm", _no_filtering)

    _, blocks = await _blocks_for(_file_part(IMAGE_URL, "vacation.jpg", "image/jpeg"))
    agent = MockSubAgent("file-analyzer", "Analyses files.", input_modes=["text", "image"])

    await _dispatch_with_files(agent, blocks, description="what is in this picture?")

    # The sub-agent receives content blocks rather than a flat string, so the
    # base runnable serialises the last block — the image — as JSON.
    received = agent.received[0]
    assert IMAGE_URL in received, f"image block never reached the sub-agent: {received}"
    assert "image" in json.loads(received).get("type", "")


async def test_multimodal_subagent_gets_text_only_when_the_filter_drops_everything(monkeypatch):
    """Filtering to nothing must still deliver the instruction, not an empty message."""

    async def _drop_all(self, description_text, blocks):
        return []

    monkeypatch.setattr(DynamicToolDispatchMiddleware, "_filter_files_with_llm", _drop_all)

    _, blocks = await _blocks_for(_file_part(IMAGE_URL, "vacation.jpg", "image/jpeg"))
    agent = MockSubAgent("file-analyzer", "Analyses files.", input_modes=["text", "image"])

    await _dispatch_with_files(agent, blocks, description="what is in this picture?")

    assert agent.received == ["what is in this picture?"]


@pytest.mark.parametrize(
    "media_type,expected_block_type",
    [
        ("image/png", "image"),
        ("audio/mpeg", "audio"),
        ("application/pdf", "file"),
        ("text/plain", "file"),
    ],
)
async def test_media_type_maps_to_block_type(media_type, expected_block_type):
    """Block type decides the forwarding path, so the mapping is load-bearing."""
    _, blocks = await _blocks_for(_file_part(f"https://files.example.com/x?t={media_type}", "x", media_type))

    assert blocks[0]["type"] == expected_block_type
