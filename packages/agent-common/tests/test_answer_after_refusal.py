"""After a refused or blocked call, the next model step is offered only the response tool.

QA on forced tool choice (Haiku, GPT): told "not run, tell the user", the model re-sent the
refused save, or kept reading the page, until loop detection force-stopped the run with no
answer at all.
"""

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from agent_common.core.hitl_resume import CLICKED_REJECT_LEAD
from agent_common.middleware.answer_after_refusal import AnswerAfterRefusalMiddleware, must_answer


def _tool(name: str) -> StructuredTool:
    return StructuredTool.from_function(func=lambda: "", name=name, description=name)


def _turn(result: str) -> list:
    call = {"name": "client_action", "args": {"kind": "invoke", "action": "save"}, "id": "c1", "type": "tool_call"}
    return [
        HumanMessage(content="save it"),
        AIMessage(content="", tool_calls=[call]),
        ToolMessage(content=result, tool_call_id="c1", status="error"),
    ]


class _Request(SimpleNamespace):
    def override(self, **changes):
        return _Request(**{**self.__dict__, **changes})


def _offered(messages: list, tools: list) -> list:
    seen = {}

    def handler(request):
        seen["tools"] = request.tools
        return "response"

    AnswerAfterRefusalMiddleware().wrap_model_call(_Request(messages=messages, tools=tools), handler)
    return seen["tools"]


def test_a_refusal_or_block_ends_the_step_choice():
    assert must_answer(_turn(f"{CLICKED_REJECT_LEAD} and declined it."))
    assert must_answer(_turn("NOT RUN: the user rejected this exact call a moment ago, so it was not put …"))
    assert must_answer(_turn("BLOCKED: 'client_action[read_current_page]' — Tool called 6 times"))


def test_a_result_that_asks_for_a_retry_or_a_normal_result_does_not():
    assert not must_answer(_turn("NOT RUN: 'save' saves, so it must be invoked on its own, in the next step."))
    assert not must_answer(_turn("The client executed the apply."))
    assert not must_answer([HumanMessage(content="hi")])


def test_only_the_response_tool_is_offered_after_a_refusal():
    tools = [_tool("client_action"), _tool("SubAgentResponseSchema"), _tool("load_skill")]
    offered = _offered(_turn(f"{CLICKED_REJECT_LEAD}."), tools)
    assert [t.name for t in offered] == ["SubAgentResponseSchema"]


def test_registry_tools_injected_as_dicts_are_filtered_too():
    tools = [
        {"type": "function", "function": {"name": "console_create_bug_report"}},
        {"type": "function", "function": {"name": "FinalResponseSchema"}},
    ]
    offered = _offered(_turn("BLOCKED: 'x' — looped"), tools)
    assert offered == [tools[1]]


def test_tool_strategy_keeps_no_tools_of_its_own():
    # ToolStrategy binds the structured-output tool itself: an empty list leaves it alone.
    assert _offered(_turn("BLOCKED: 'x' — looped"), [_tool("client_action")]) == []


def test_nothing_changes_without_a_refusal():
    tools = [_tool("client_action"), _tool("SubAgentResponseSchema")]
    assert _offered(_turn("The client executed the apply."), tools) == tools
