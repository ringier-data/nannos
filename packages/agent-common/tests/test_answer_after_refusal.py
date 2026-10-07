"""After a refused retry or a repeated loop block, the next model step can only be the answer.

QA on forced tool choice (Haiku, GPT, Sonnet 4.6): told "not run, tell the user", the model
re-sent the refused save, or kept reading the page, until loop detection force-stopped the
run with no answer at all.
"""

from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from agent_common.a2a.structured_response import REFUSED_REPLY
from agent_common.core.hitl_resume import _SKIPPED_AUTH_MESSAGE, CLICKED_REJECT_LEAD
from agent_common.middleware.answer_after_refusal import AnswerAfterRefusalMiddleware, must_answer, stop_reason
from agent_common.middleware.conditional_hitl import _REFUSED_AGAIN
from agent_common.middleware.loop_detection_middleware import RepeatedToolCallMiddleware

BLOCK = RepeatedToolCallMiddleware()._build_error_message(
    {"tool_name": "client_action[apply]", "loop_type": "same_args", "description": "called 6 times"}
)


def _tool(name: str) -> StructuredTool:
    return StructuredTool.from_function(func=lambda: "", name=name, description=name)


def _step(result, call_id: str = "c1") -> list:
    call = {"name": "client_action", "args": {"kind": "invoke"}, "id": call_id, "type": "tool_call"}
    return [AIMessage(content="", tool_calls=[call]), ToolMessage(content=result, tool_call_id=call_id, status="error")]


def _turn(*results) -> list:
    messages = [HumanMessage(content="save it")]
    for i, result in enumerate(results):
        messages += _step(result, f"c{i}")
    return messages


class _Request(SimpleNamespace):
    def override(self, **changes):
        return _Request(**{**self.__dict__, **changes})


def _request(messages, tools) -> _Request:
    return _Request(messages=messages, tools=tools, system_message=None, response_format=None)


def _response(*calls: str) -> ModelResponse:
    return ModelResponse(
        result=[
            AIMessage(content="", tool_calls=[{"name": n, "args": {}, "id": n, "type": "tool_call"} for n in calls])
        ]
    )


def _offered(messages: list, tools: list) -> list:
    seen = {}

    def handler(request):
        seen["tools"] = request.tools
        return _response("SubAgentResponseSchema")

    AnswerAfterRefusalMiddleware().wrap_model_call(_request(messages, tools), handler)
    return seen["tools"]


class TestWhenTheStepMustBeTheAnswer:
    def test_a_refused_retry_or_a_skipped_or_declined_authorization(self):
        # The producers' own texts, not copies: rewording them must not switch this off.
        from agent_common.middleware.auth_error_middleware import AuthErrorDetectionMiddleware

        declined = AuthErrorDetectionMiddleware._refusal_message(
            AuthErrorDetectionMiddleware.__new__(AuthErrorDetectionMiddleware),
            SimpleNamespace(tool_call={"id": "c0"}),
            "gdrive_search",
            "",
            None,
        )
        assert stop_reason(_turn(_REFUSED_AGAIN)) == "refused"
        assert stop_reason(_turn(_SKIPPED_AUTH_MESSAGE)) == "refused"
        assert stop_reason(_turn(declined.content)) == "refused"

    def test_a_decline_that_names_an_alternative_stays_open(self):
        from agent_common.middleware.auth_error_middleware import AuthErrorDetectionMiddleware

        declined = AuthErrorDetectionMiddleware._refusal_message(
            AuthErrorDetectionMiddleware.__new__(AuthErrorDetectionMiddleware),
            SimpleNamespace(tool_call={"id": "c0"}),
            "gdrive_search",
            "no, search Slack instead",
            None,
        )
        assert not must_answer(_turn(declined.content))

    def test_a_loop_block_the_second_time_this_turn(self):
        assert stop_reason(_turn(BLOCK, BLOCK)) == "blocked"

    def test_not_a_first_block_or_a_clicked_reject(self):
        # Both invite another way ("try a different approach", "do X instead").
        assert not must_answer(_turn(BLOCK))
        assert not must_answer(_turn(f"{CLICKED_REJECT_LEAD}, so this call was NOT executed."))

    def test_not_after_a_new_user_message(self):
        # A force-stopped turn ends on the block; the next turn is a new ask.
        assert not must_answer([*_turn(_REFUSED_AGAIN), HumanMessage(content="ok, archive it instead")])
        assert not must_answer([*_turn(BLOCK), HumanMessage(content="try again"), *_step(BLOCK, "n")])

    def test_the_page_context_appended_each_step_is_not_a_new_ask(self):
        # ClientObjectsMiddleware appends <current_page> as a flagged HumanMessage; missed live.
        from agent_common.middleware.utils import append_volatile_context_message

        assert must_answer(append_volatile_context_message(_turn(_REFUSED_AGAIN), "<current_page>…"))

    def test_a_refusal_tagged_for_prompt_caching_still_counts(self):
        # The caching middleware (outer) turns the last message into a block list; missed live.
        blocks = [{"type": "text", "text": _REFUSED_AGAIN, "cache_control": {"type": "ephemeral"}}]
        assert must_answer(_turn(blocks))

    def test_not_for_a_result_that_asks_for_a_retry_or_a_normal_result(self):
        assert not must_answer(_turn("NOT RUN: 'save' saves, so it must be invoked on its own, in the next step."))
        assert not must_answer(_turn("The client executed the apply."))
        assert not must_answer([HumanMessage(content="hi")])


class TestTheNarrowedStep:
    def test_only_the_response_tool_is_offered(self):
        tools = [_tool("client_action"), _tool("SubAgentResponseSchema"), _tool("load_skill")]
        assert [t.name for t in _offered(_turn(_REFUSED_AGAIN), tools)] == ["SubAgentResponseSchema"]

    def test_registry_tools_injected_as_dicts_are_filtered_too(self):
        tools = [
            {"type": "function", "function": {"name": "console_create_bug_report"}},
            {"type": "function", "function": {"name": "FinalResponseSchema"}},
        ]
        assert _offered(_turn(_REFUSED_AGAIN), tools) == [tools[1]]

    def test_tool_strategy_keeps_no_tools_of_its_own(self):
        # ToolStrategy binds the structured-output tool itself: an empty list leaves it alone.
        assert _offered(_turn(_REFUSED_AGAIN), [_tool("client_action")]) == []

    def test_nothing_changes_otherwise(self):
        tools = [_tool("client_action"), _tool("SubAgentResponseSchema")]
        assert _offered(_turn("The client executed the apply."), tools) == tools

    def test_the_step_says_to_answer_now(self):
        seen = {}

        def handler(request):
            seen["system"] = request.system_message.text
            return _response("SubAgentResponseSchema")

        AnswerAfterRefusalMiddleware().wrap_model_call(
            _request(_turn(_REFUSED_AGAIN), [_tool("SubAgentResponseSchema")]), handler
        )
        assert "answer the user now with SubAgentResponseSchema" in seen["system"]

    def test_a_call_to_a_tool_not_offered_is_asked_for_once_more(self):
        # Live: offered only the response tool, Claude re-sent the refused client_action.
        replies = iter([_response("client_action"), _response("SubAgentResponseSchema")])
        calls = []

        def handler(request):
            calls.append(request)
            return next(replies)

        out = AnswerAfterRefusalMiddleware().wrap_model_call(
            _request(_turn(_REFUSED_AGAIN), [_tool("client_action")]), handler
        )
        assert len(calls) == 2
        assert out.result[0].tool_calls[0]["name"] == "SubAgentResponseSchema"

    def test_a_valid_answer_next_to_a_stray_call_is_kept(self):
        out = AnswerAfterRefusalMiddleware().wrap_model_call(
            _request(_turn(_REFUSED_AGAIN), [_tool("SubAgentResponseSchema")]),
            lambda request: _response("client_action", "SubAgentResponseSchema"),
        )
        assert [c["name"] for c in out.result[0].tool_calls] == ["SubAgentResponseSchema"]

    def test_a_model_that_keeps_calling_it_gets_its_answer_written(self):
        out = AnswerAfterRefusalMiddleware().wrap_model_call(
            _request(_turn(_REFUSED_AGAIN), [_tool("client_action"), _tool("SubAgentResponseSchema")]),
            lambda request: _response("client_action"),
        )
        [call] = out.result[0].tool_calls
        assert call["name"] == "SubAgentResponseSchema"
        assert call["args"] == {"task_state": "completed", "message": REFUSED_REPLY}


@pytest.mark.asyncio
async def test_end_to_end_on_tool_strategy_the_turn_ends_with_an_answer():
    """A real graph, forced tool choice, a model that only ever re-sends the refused call:
    the run ends with the written answer as its structured response, not a force-stop."""
    from langchain.agents import create_agent
    from langchain.agents.structured_output import ToolStrategy
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from agent_common.a2a.structured_response import SubAgentResponseSchema

    class Stubborn(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    refused = {"name": "client_action", "args": {"kind": "invoke"}, "id": "again", "type": "tool_call"}
    model = Stubborn(messages=iter([AIMessage(content="", tool_calls=[refused])] * 4))
    agent = create_agent(
        model,
        tools=[_tool("client_action")],
        middleware=[AnswerAfterRefusalMiddleware()],
        response_format=ToolStrategy(SubAgentResponseSchema),
    )
    out = await agent.ainvoke({"messages": _turn(_REFUSED_AGAIN)})
    assert out["structured_response"].message == REFUSED_REPLY
