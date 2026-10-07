"""A turn force-stopped by loop detection is reported in plain words, never as the block text."""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_common.a2a.structured_response import REFUSED_REPLY, STOPPED_REPLY, unanswered_turn_reply


def _turn(last: ToolMessage) -> list:
    call = {"name": last.name or "client_action", "args": {}, "id": last.tool_call_id, "type": "tool_call"}
    return [AIMessage(content="", tool_calls=[call]), last]


def test_a_force_stop_reads_as_plain_words_and_keeps_the_step():
    # The step stays: a delegated sub-agent's reply reaches the orchestrator.
    blocked = ToolMessage(
        content="BLOCKED: 'client_action[invoke]' — called 7 times", tool_call_id="c", name="client_action"
    )
    reply = unanswered_turn_reply(_turn(blocked), "SubAgentResponseSchema")
    assert reply.startswith(STOPPED_REPLY)
    assert "client_action[invoke]" in reply


def test_a_refused_call_reads_as_not_done():
    from agent_common.middleware.conditional_hitl import _REFUSED_AGAIN

    refused = ToolMessage(content=_REFUSED_AGAIN, tool_call_id="c", name="client_action")
    assert unanswered_turn_reply(_turn(refused), "SubAgentResponseSchema") == REFUSED_REPLY


def test_another_unanswered_result_is_reported_as_it_is():
    other = ToolMessage(content="Gateway timeout", tool_call_id="c", name="console_get_job")
    assert unanswered_turn_reply(_turn(other), "SubAgentResponseSchema") == "Gateway timeout"


def test_an_answered_turn_has_no_such_reply():
    answered = ToolMessage(content="ok", tool_call_id="c", name="SubAgentResponseSchema")
    assert unanswered_turn_reply(_turn(answered), "SubAgentResponseSchema") is None
    assert unanswered_turn_reply([HumanMessage(content="hi"), AIMessage(content="Hello")], "X") is None
    assert unanswered_turn_reply([], "X") is None
