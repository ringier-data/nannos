"""Shared streaming utilities for LangGraph-based A2A agents.

Provides reusable components for streaming LangGraph graph execution output
to the A2A protocol layer. Used by LangGraphAgent._stream_impl, the
orchestrator's stream(), GPAgentRunnable, and DynamicLocalAgentRunnable
to avoid duplicating buffer management, structured response parsing,
and content extraction logic.

Requires the ``langgraph`` optional dependency group.
"""

from typing import Any, Dict, List, Optional, Tuple

from langchain_core.utils.json import parse_partial_json

# Minimum characters to accumulate before flushing on a word boundary.
# Prevents sending hundreds of single-token A2A events while keeping latency low.
DEFAULT_CHUNK_MIN = 40


class StreamBuffer:
    """Accumulates text and flushes on word boundaries in >=chunk_min sized pieces.

    Avoids sending per-token A2A events by batching tokens into readable chunks
    that break on whitespace (space or newline) boundaries.

    Usage::

        buf = StreamBuffer()
        buf.append(token_text)
        for chunk in buf.flush_ready():
            yield {"type": "artifact_update", "content": chunk}
        # At end of stream:
        remaining = buf.flush_all()
        if remaining:
            yield {"type": "artifact_update", "content": remaining}
    """

    def __init__(self, chunk_min: int = DEFAULT_CHUNK_MIN) -> None:
        self._buffer = ""
        self._chunk_min = chunk_min

    def append(self, text: str) -> None:
        """Add text to the buffer."""
        self._buffer += text

    def flush_ready(self) -> List[str]:
        """Return chunks that are ready to flush (>= chunk_min, word-boundary aligned).

        Returns a list (usually 0 or 1 items) so callers can iterate without
        needing to check Optional.
        """
        if len(self._buffer) < self._chunk_min:
            return []

        half = self._chunk_min // 2
        cut = max(
            self._buffer.rfind(" ", half),
            self._buffer.rfind("\n", half),
        )
        if cut == -1:
            cut = len(self._buffer)
        else:
            cut += 1  # include the trailing whitespace character

        flush, self._buffer = self._buffer[:cut], self._buffer[cut:]
        return [flush] if flush else []

    def flush_all(self) -> str:
        """Flush everything remaining in the buffer. Call at end of stream."""
        remaining = self._buffer
        self._buffer = ""
        return remaining

    @property
    def pending(self) -> str:
        """The current unflushed content (for inspection/testing)."""
        return self._buffer


class StructuredResponseStreamer:
    """Incrementally extracts the ``message`` field from a structured response tool call.

    LLMs emit FinalResponseSchema / SubAgentResponseSchema as tool_call_chunks
    whose ``args`` arrive as partial JSON tokens.  This class accumulates the
    JSON string, uses ``parse_partial_json`` to extract the ``message`` field,
    and returns only the new delta text on each feed.

    Usage::

        streamer = StructuredResponseStreamer("FinalResponseSchema")
        for tc_chunk in msg_chunk.tool_call_chunks:
            delta = streamer.feed(tc_chunk)
            if delta:
                buf.append(delta)
    """

    def __init__(self, schema_name: str) -> None:
        self._schema_name = schema_name
        self._tracking = False
        self._args = ""
        self._streamed = ""

    @property
    def tracking(self) -> bool:
        """Whether a structured response tool call is currently being tracked."""
        return self._tracking

    def feed(self, tc_chunk: Dict[str, Any]) -> Optional[str]:
        """Process a single tool_call_chunk and return the new message delta, if any.

        Returns ``None`` when there is no new text to emit (chunk belongs to a
        different tool call, or the message field hasn't grown).
        """
        # Detect start of the target schema
        chunk_name = tc_chunk.get("name")
        if chunk_name == self._schema_name:
            self._tracking = True
            self._args = ""
            self._streamed = ""

        if not self._tracking:
            return None

        args_delta = tc_chunk.get("args", "")
        if not args_delta:
            return None

        self._args += args_delta
        parsed = parse_partial_json(self._args)
        if not parsed or "message" not in parsed:
            return None

        current_message: str = parsed["message"]
        if len(current_message) <= len(self._streamed):
            return None

        delta = current_message[len(self._streamed) :]
        self._streamed = current_message
        return delta


def extract_text_from_content(content: Any) -> Tuple[str, List[Dict[str, str]]]:
    """Extract plain text and thinking blocks from an AIMessageChunk's content.

    Filters out protocol noise like tool_use blocks that should not be displayed to users.

    Bedrock models with extended thinking return content as a list of blocks::

        [{"type": "reasoning_content", "reasoning_content": {"text": "..."}},
         {"type": "text", "text": "..."}]

    Gemini models with thinking return::

        [{"type": "thinking", "thinking": "..."},
         {"type": "text", "text": "..."}]

    Tool calls appear as::

        [{"type": "tool_use", "name": "tool_name", "input": {...}, "id": "..."}]

    GPT-4o and other models return content as a simple string.

    Args:
        content: ``AIMessageChunk.content`` — a string or list of content blocks.

    Returns:
        A tuple of ``(plain_text, thinking_blocks)`` where:
        - ``plain_text`` is the concatenated text content (tool_use blocks filtered out)
        - ``thinking_blocks`` is a list of ``{"thinking": "..."}`` dicts
    """
    if isinstance(content, str):
        return content, []

    if not isinstance(content, list):
        return str(content), []

    text_parts: List[str] = []
    thinking_blocks: List[Dict[str, str]] = []

    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type")
            if block_type == "thinking":
                # Gemini thinking block format
                thinking_text = block.get("thinking", "")
                if thinking_text:
                    thinking_blocks.append({"thinking": thinking_text})
            elif block_type == "reasoning_content":
                # Bedrock extended thinking format
                reasoning = block.get("reasoning_content", {})
                thinking_text = reasoning.get("text", "") if isinstance(reasoning, dict) else ""
                if thinking_text:
                    thinking_blocks.append({"thinking": thinking_text})
            elif block_type == "text":
                text = block.get("text", "")
                if text:
                    text_parts.append(text)
            elif block_type == "tool_use":
                # Skip tool_use blocks - these are handled separately as tool_call_chunks
                continue
        elif isinstance(block, str):
            text_parts.append(block)

    return "".join(text_parts), thinking_blocks
