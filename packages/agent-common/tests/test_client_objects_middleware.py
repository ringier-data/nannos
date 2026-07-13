"""Tests for the shared <client_objects> rendering and its injection sites.

Covers the middleware-utils helpers (`append_to_last_human_message`) and the
`ClientObjectsMiddleware` behaviour: the volatile manifest rides the last human
message to keep the cached system prefix stable, with a system-prompt fallback
when no human message is present.
"""

from unittest.mock import patch

from agent_common.middleware.client_objects_middleware import (
    ClientObjectsMiddleware,
    render_client_objects_block,
)
from agent_common.middleware.utils import (
    append_to_last_human_message,
    append_to_system_message,
)
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

MANIFEST = [{"type": "form", "id": "f1", "scope": "page", "fields": ["name"]}]


def _make_request(messages, system_message=None):
    return ModelRequest(
        model=None,
        messages=messages,
        system_message=system_message,
        tool_choice=None,
        tools=[],
        response_format=None,
        state={},
        runtime=None,
        model_settings={},
    )


class TestAppendToLastHumanMessage:
    def test_appends_to_string_content(self):
        msgs = [HumanMessage(content="hello")]
        out = append_to_last_human_message(msgs, "BLOCK")
        assert out is not None
        assert out[0].content == "hello\n\nBLOCK"
        # original is not mutated
        assert msgs[0].content == "hello"

    def test_appends_text_block_to_list_content(self):
        msgs = [HumanMessage(content=[{"type": "text", "text": "hi"}])]
        out = append_to_last_human_message(msgs, "BLOCK")
        assert out is not None
        assert out[0].content == [
            {"type": "text", "text": "hi"},
            {"type": "text", "text": "BLOCK"},
        ]

    def test_targets_last_human_not_literal_tail(self):
        """Mid-tool-loop the tail is a ToolMessage; the block must land on the
        (earlier) human message so message roles stay valid."""
        msgs = [
            HumanMessage(content="query"),
            AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}]),
            ToolMessage(content="result", tool_call_id="c1"),
        ]
        out = append_to_last_human_message(msgs, "BLOCK")
        assert out is not None
        assert out[0].content == "query\n\nBLOCK"
        # tail is untouched
        assert isinstance(out[-1], ToolMessage)
        assert out[-1].content == "result"

    def test_picks_the_most_recent_human_message(self):
        msgs = [HumanMessage(content="first"), AIMessage(content="ok"), HumanMessage(content="second")]
        out = append_to_last_human_message(msgs, "BLOCK")
        assert out is not None
        assert out[0].content == "first"
        assert out[2].content == "second\n\nBLOCK"

    def test_returns_none_when_no_human_message(self):
        msgs = [SystemMessage(content="sys"), AIMessage(content="ai")]
        assert append_to_last_human_message(msgs, "BLOCK") is None


class TestClientObjectsMiddleware:
    def test_injects_manifest_into_last_human_message(self):
        request = _make_request(
            [HumanMessage(content="do it")], system_message=SystemMessage(content="sys")
        )
        mw = ClientObjectsMiddleware()
        with patch(
            "agent_common.middleware.client_objects_middleware._client_objects_from_config",
            return_value=MANIFEST,
        ):
            out = mw._apply(request)

        # System prompt is untouched (stays byte-stable → cacheable).
        assert out.system_message.content == "sys"
        # Manifest rode the human message.
        assert "<client_objects>" in out.messages[0].content

    def test_no_manifest_is_a_noop(self):
        request = _make_request([HumanMessage(content="do it")])
        mw = ClientObjectsMiddleware()
        with patch(
            "agent_common.middleware.client_objects_middleware._client_objects_from_config",
            return_value=None,
        ):
            out = mw._apply(request)
        assert out is request
        assert out.messages[0].content == "do it"

    def test_falls_back_to_system_prompt_without_human_message(self):
        request = _make_request(
            [SystemMessage(content="sys"), AIMessage(content="ai")],
            system_message=SystemMessage(content="sys"),
        )
        mw = ClientObjectsMiddleware()
        with patch(
            "agent_common.middleware.client_objects_middleware._client_objects_from_config",
            return_value=MANIFEST,
        ):
            out = mw._apply(request)
        assert "<client_objects>" in str(out.system_message.content)


class TestRenderAndSystemHelper:
    def test_render_returns_none_for_empty(self):
        assert render_client_objects_block(None) is None
        assert render_client_objects_block([]) is None

    def test_append_to_system_message_from_none(self):
        out = append_to_system_message(None, "X")
        assert out.content_blocks == [{"type": "text", "text": "X"}]
