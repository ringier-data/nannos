"""Extended-thinking round-trip: capture signed thinking_blocks off the gateway stream and
replay them on Anthropic/Bedrock so Converse doesn't drop thinking on tool-call turns.

Covers:
- coalesce_thinking_blocks(): fragment stream -> complete signed blocks
- _GatewayChatOpenAI capture: streamed delta.thinking_blocks -> additional_kwargs
- _GatewayChatOpenAI replay: gated injection of top-level thinking_blocks into the payload
"""

from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_common.core.model_factory import (
    _gateway_chat_openai_cls,
    coalesce_thinking_blocks,
)

_PROVIDER = "agent_common.core.model_factory.get_model_provider"


def _client(model="claude-sonnet-4.6"):
    cls = _gateway_chat_openai_cls()
    return cls(api_key="sk-test", base_url="http://localhost:9/v1", model=model)


# --- coalesce_thinking_blocks ----------------------------------------------------------


def test_coalesce_text_fragments_then_signature():
    # The real wire shape: text streams in pieces, signature arrives last and closes the block.
    frags = [
        {"type": "thinking", "thinking": "Let me "},
        {"type": "thinking", "thinking": "reason."},
        {"type": "thinking", "signature": "SIG==", "thinking": ""},
    ]
    assert coalesce_thinking_blocks(frags) == [
        {"type": "thinking", "thinking": "Let me reason.", "signature": "SIG=="}
    ]


def test_coalesce_drops_unsigned_trailing_text():
    # An unsigned thinking block is rejected by Bedrock, so incomplete text is dropped.
    assert coalesce_thinking_blocks([{"type": "thinking", "thinking": "no sig"}]) == []


def test_coalesce_multiple_blocks_each_closed_by_signature():
    frags = [
        {"type": "thinking", "thinking": "first"},
        {"type": "thinking", "signature": "S1", "thinking": ""},
        {"type": "thinking", "thinking": "second"},
        {"type": "thinking", "signature": "S2", "thinking": ""},
    ]
    assert coalesce_thinking_blocks(frags) == [
        {"type": "thinking", "thinking": "first", "signature": "S1"},
        {"type": "thinking", "thinking": "second", "signature": "S2"},
    ]


def test_coalesce_passes_through_redacted_thinking():
    frags = [{"type": "redacted_thinking", "data": "ENC"}]
    assert coalesce_thinking_blocks(frags) == [
        {"type": "redacted_thinking", "data": "ENC"}
    ]


def test_coalesce_idempotent_on_complete_block():
    complete = [{"type": "thinking", "thinking": "done", "signature": "S"}]
    assert coalesce_thinking_blocks(complete) == complete


def test_coalesce_handles_empty_and_garbage():
    assert coalesce_thinking_blocks(None) == []
    assert coalesce_thinking_blocks([]) == []
    assert coalesce_thinking_blocks(["not a dict", 5, None]) == []


# --- capture: streamed delta.thinking_blocks -> additional_kwargs ----------------------


def _chunk(delta: dict) -> dict:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "claude-sonnet-4.6",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


def test_capture_grafts_thinking_blocks_into_additional_kwargs():
    from langchain_core.messages import AIMessageChunk

    client = _client()
    gen = client._convert_chunk_to_generation_chunk(
        _chunk(
            {"content": "", "thinking_blocks": [{"type": "thinking", "thinking": "hi"}]}
        ),
        AIMessageChunk,
        {},
    )
    assert gen is not None
    assert gen.message.additional_kwargs.get("thinking_blocks") == [
        {"type": "thinking", "thinking": "hi"}
    ]


def test_capture_also_keeps_reasoning_content():
    from langchain_core.messages import AIMessageChunk

    client = _client()
    gen = client._convert_chunk_to_generation_chunk(
        _chunk({"content": "", "reasoning_content": "because"}),
        AIMessageChunk,
        {},
    )
    assert gen.message.additional_kwargs.get("reasoning_content") == "because"


def test_capture_noop_without_thinking_blocks():
    from langchain_core.messages import AIMessageChunk

    client = _client()
    gen = client._convert_chunk_to_generation_chunk(
        _chunk({"content": "plain"}), AIMessageChunk, {}
    )
    assert "thinking_blocks" not in gen.message.additional_kwargs


# --- replay: gated top-level thinking_blocks injection --------------------------------


def _history():
    """A typical tool-loop history: assistant made a tool call (with captured thinking
    fragments), tool replied, now we ask the model to continue."""
    return [
        HumanMessage(content="what time is it?"),
        AIMessage(
            content="",
            tool_calls=[{"name": "get_time", "args": {}, "id": "t1"}],
            additional_kwargs={
                "thinking_blocks": [
                    {"type": "thinking", "thinking": "I should "},
                    {"type": "thinking", "thinking": "call the tool."},
                    {"type": "thinking", "signature": "SIG==", "thinking": ""},
                ]
            },
        ),
        ToolMessage(content="2026-06-25T17:18:00", tool_call_id="t1"),
    ]


def _assistant_dicts(payload):
    return [m for m in payload["messages"] if m.get("role") == "assistant"]


def test_replay_injects_coalesced_blocks_on_bedrock():
    client = _client()
    with patch(_PROVIDER, return_value="bedrock_converse"):
        payload = client._get_request_payload(_history())
    assistant = _assistant_dicts(payload)[0]
    assert assistant["thinking_blocks"] == [
        {
            "type": "thinking",
            "thinking": "I should call the tool.",
            "signature": "SIG==",
        }
    ]


def test_replay_skipped_for_non_bedrock_provider():
    client = _client()
    with patch(_PROVIDER, return_value="openai"):
        payload = client._get_request_payload(_history())
    assert "thinking_blocks" not in _assistant_dicts(payload)[0]


def test_replay_noop_when_no_thinking_blocks_captured():
    client = _client()
    history = [HumanMessage(content="hi"), AIMessage(content="hello")]
    with patch(_PROVIDER, return_value="bedrock_converse"):
        payload = client._get_request_payload(history)
    assert "thinking_blocks" not in _assistant_dicts(payload)[0]


# --- cache_control restore on tool messages -------------------------------------------
#
# langchain_openai's tool-message sanitizer rebuilds text blocks as {type, text} only,
# dropping the cache_control breakpoint our prompt-caching middleware places on the latest
# tool result. The gateway subclass restores it so conversation caching survives on
# tool-terminated turns (the common case in the orchestrator's dispatch loop).

_CC = {"type": "ephemeral"}


def _tool_history():
    """A tool-loop turn whose latest tool result carries the conversation cache breakpoint."""
    return [
        HumanMessage(content="run it"),
        AIMessage(content="", tool_calls=[{"name": "task", "args": {}, "id": "t1"}]),
        ToolMessage(
            content=[{"type": "text", "text": "BIG RESULT", "cache_control": _CC}],
            tool_call_id="t1",
        ),
    ]


def _tool_dicts(payload):
    return [m for m in payload["messages"] if m.get("role") == "tool"]


def test_cache_control_restored_on_tool_message():
    client = _client()
    with patch(_PROVIDER, return_value="bedrock_converse"):
        payload = client._get_request_payload(_tool_history())
    assert _tool_dicts(payload)[0]["content"][-1]["cache_control"] == _CC


def test_cache_control_restore_skipped_for_non_caching_provider():
    client = _client()
    with patch(_PROVIDER, return_value="openai"):
        payload = client._get_request_payload(_tool_history())
    # OpenAI takes the early return; the stripped marker is not re-added (OpenAI can't cache).
    assert all("cache_control" not in b for b in _tool_dicts(payload)[0]["content"])


def test_cache_control_noop_on_untagged_tool_message():
    client = _client()
    history = [
        HumanMessage(content="run it"),
        AIMessage(content="", tool_calls=[{"name": "task", "args": {}, "id": "t1"}]),
        ToolMessage(content="plain result", tool_call_id="t1"),
    ]
    with patch(_PROVIDER, return_value="bedrock_converse"):
        payload = client._get_request_payload(history)
    content = _tool_dicts(payload)[0]["content"]
    blocks = content if isinstance(content, list) else []
    assert all(not (isinstance(b, dict) and "cache_control" in b) for b in blocks)
