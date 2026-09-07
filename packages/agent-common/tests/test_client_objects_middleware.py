"""Tests for the shared <client_objects> rendering and its injection sites.

Covers the middleware-utils helper (`append_volatile_context_message`) and the
`ClientObjectsMiddleware` behaviour: the volatile manifest is appended as ONE
trailing, flagged human message so every persisted message — system prompt and
conversation history alike — stays byte-stable across tool-loop iterations and
across turns (the provider prompt cache survives).
"""

from unittest.mock import patch

from agent_common.middleware.client_objects_middleware import (
    ClientObjectsMiddleware,
    render_client_objects_block,
    render_current_page_block,
)
from agent_common.middleware.utils import (
    VOLATILE_CONTEXT_KEY,
    append_to_system_message,
    append_volatile_context_message,
)
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

MANIFEST = [{"type": "form", "id": "f1", "scope": "page", "fields": ["name"]}]
PAGE = {"key": "/campaigns/7", "title": "Campaign 7", "view": {"tab": "targetings"}}


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


class TestAppendVolatileContextMessage:
    def test_appends_flagged_trailing_human_message(self):
        msgs = [HumanMessage(content="hello")]
        out = append_volatile_context_message(msgs, "BLOCK")
        assert len(out) == 2
        assert out[0] is msgs[0]  # persisted messages are passed through by identity
        assert isinstance(out[1], HumanMessage)
        assert out[1].content == "BLOCK"
        assert out[1].additional_kwargs[VOLATILE_CONTEXT_KEY] is True
        # original list is not mutated
        assert len(msgs) == 1

    def test_mid_tool_loop_block_follows_the_tool_result(self):
        """The block goes after the ToolMessage tail — nothing earlier is rewritten."""
        msgs = [
            HumanMessage(content="query"),
            AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}]),
            ToolMessage(content="result", tool_call_id="c1"),
        ]
        out = append_volatile_context_message(msgs, "BLOCK")
        assert out[:3] == msgs
        assert isinstance(out[2], ToolMessage)
        assert out[3].content == "BLOCK"

    def test_history_is_byte_stable_across_turns(self):
        """The regression this design fixes: on turn N+1 the messages sent BEFORE the
        block must be exactly what was sent on turn N plus the new turn — never a
        rewritten Human_N. (Appending to the last human message moved the block
        from Human_N to Human_N+1 and re-tokenised everything after Human_N.)"""
        h1 = HumanMessage(content="turn 1")
        turn1 = append_volatile_context_message([h1], "PAGE A")
        ai1 = AIMessage(content="answer 1")
        h2 = HumanMessage(content="turn 2")
        turn2 = append_volatile_context_message([h1, ai1, h2], "PAGE A")
        # turn-2 prefix == turn-1 prefix (without its block) + persisted continuation
        assert turn2[:1] == turn1[:-1]
        assert turn2[:-1] == [h1, ai1, h2]
        # and the same holds when the page changed: only the trailing block differs
        turn2b = append_volatile_context_message([h1, ai1, h2], "PAGE B")
        assert turn2b[:-1] == turn2[:-1]
        assert turn2b[-1].content != turn2[-1].content

    def test_works_without_any_human_message(self):
        msgs = [SystemMessage(content="sys"), AIMessage(content="ai")]
        out = append_volatile_context_message(msgs, "BLOCK")
        assert out[:2] == msgs and out[2].content == "BLOCK"


class TestClientObjectsMiddleware:
    def test_injects_manifest_as_trailing_message(self):
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
        # The user's message is untouched; the manifest is the trailing flagged message.
        assert out.messages[0].content == "do it"
        assert "<client_objects>" in out.messages[-1].content
        assert out.messages[-1].additional_kwargs[VOLATILE_CONTEXT_KEY] is True

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

    def test_appends_trailing_message_even_without_human_message(self):
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
        # Never in the system prompt — that would bust the cached prefix on every navigation.
        assert "<client_objects>" not in str(out.system_message.content)
        assert "<client_objects>" in out.messages[-1].content


class TestCurrentPage:
    def test_renders_all_declared_fields(self):
        block = render_current_page_block(
            {
                "key": "/campaigns/7",
                "title": "Campaign 7 – Targetings",
                "breadcrumbs": ["Campaigns", "Campaign 7", "Targetings"],
                "entity": {"type": "Campaign", "id": "7", "name": "Summer"},
                "view": {"tab": "targetings", "status": "active"},
                "visible": ["Geo CH", "Age 18-35"],
            }
        )
        assert block.startswith("<current_page>")
        assert "- path: /campaigns/7" in block
        assert "- title: Campaign 7 – Targetings" in block
        assert "- breadcrumbs: Campaigns > Campaign 7 > Targetings" in block
        assert "- on-screen entity: Campaign id=7 name='Summer'" in block
        assert '"tab": "targetings"' in block
        assert "- visible items: Geo CH, Age 18-35" in block

    def test_key_is_required_and_dict_shape_enforced(self):
        assert render_current_page_block(None) is None
        assert render_current_page_block({}) is None
        assert render_current_page_block({"title": "no key"}) is None
        assert render_current_page_block(["not", "a", "dict"]) is None
        # Key alone is enough.
        assert "- path: /home" in render_current_page_block({"key": "/home"})

    def test_incomplete_entity_and_empty_collections_are_skipped(self):
        block = render_current_page_block(
            {"key": "/x", "entity": {"type": "Campaign"}, "view": {}, "visible": [], "breadcrumbs": []}
        )
        assert "entity" not in block
        assert "view state" not in block
        assert "visible items" not in block
        assert "breadcrumbs" not in block

    def test_middleware_injects_page_before_manifest_in_trailing_message(self):
        request = _make_request(
            [HumanMessage(content="what is on this page?")],
            system_message=SystemMessage(content="sys"),
        )
        mw = ClientObjectsMiddleware()
        with (
            patch(
                "agent_common.middleware.client_objects_middleware._client_objects_from_config",
                return_value=MANIFEST,
            ),
            patch(
                "agent_common.middleware.client_objects_middleware._page_context_from_config",
                return_value=PAGE,
            ),
        ):
            out = mw._apply(request)

        assert out.messages[0].content == "what is on this page?"
        content = out.messages[-1].content
        assert "<current_page>" in content
        assert "<client_objects>" in content
        assert content.index("<current_page>") < content.index("<client_objects>")
        # System prompt stays byte-stable.
        assert out.system_message.content == "sys"

    def test_page_context_alone_still_injects(self):
        request = _make_request([HumanMessage(content="hi")])
        mw = ClientObjectsMiddleware()
        with (
            patch(
                "agent_common.middleware.client_objects_middleware._client_objects_from_config",
                return_value=None,
            ),
            patch(
                "agent_common.middleware.client_objects_middleware._page_context_from_config",
                return_value=PAGE,
            ),
        ):
            out = mw._apply(request)
        assert out.messages[0].content == "hi"
        assert "<current_page>" in out.messages[-1].content
        assert "<client_objects>" not in out.messages[-1].content


class TestRenderAndSystemHelper:
    def test_render_returns_none_for_empty(self):
        assert render_client_objects_block(None) is None
        assert render_client_objects_block([]) is None

    def test_append_to_system_message_from_none(self):
        out = append_to_system_message(None, "X")
        assert out.content_blocks == [{"type": "text", "text": "X"}]
