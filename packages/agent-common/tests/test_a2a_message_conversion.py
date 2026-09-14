"""Round trips across the A2A boundary, and the thread conventions next to it.

Everything a local sub-agent receives now travels as an A2A message, so what a
caller puts in a ``HumanMessage`` must come back out identically on the graph
side — text, files, structured JSON, and the answers to a paused task.
"""

from a2a.types import TaskState, TaskStatus
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_common.a2a.extensions import (
    CLIENT_ACTION_EXTENSION,
    HUMAN_IN_THE_LOOP_EXTENSION,
    IN_TASK_AUTH_EXTENSION,
    new_auth_required_message,
    new_client_action_request_message,
    new_hitl_interrupt_message,
)
from agent_common.a2a.message_conversion import (
    PROPOSED_TASK_ID_KEY,
    a2a_message_to_human_message,
    answer_from_message,
    answer_to_human_message,
    human_messages_to_a2a_message,
    interrupt_value_from_status,
)
from agent_common.a2a.threads import local_sub_agent_thread_id, seal_dangling_tool_calls
from google.protobuf.json_format import MessageToDict


def test_plain_text_round_trips_to_a_string():
    msg = human_messages_to_a2a_message([HumanMessage(content="hello")], "ctx", None)
    assert msg.context_id == "ctx" and msg.task_id == ""
    assert a2a_message_to_human_message(msg).content == "hello"


def test_text_and_file_blocks_round_trip_to_typed_blocks():
    human = HumanMessage(
        content=[
            {"type": "text", "text": "look at this"},
            {"type": "image", "url": "https://x/y.png", "mime_type": "image/png"},
            {"type": "file", "url": "https://x/z.pdf", "mime_type": "application/pdf"},
        ]
    )
    back = a2a_message_to_human_message(human_messages_to_a2a_message([human], "ctx", None))
    kinds = [(b["type"], b.get("url"), b.get("mime_type")) for b in back.content]
    assert kinds == [
        ("text", None, None),
        ("image", "https://x/y.png", "image/png"),
        ("file", "https://x/z.pdf", "application/pdf"),
    ]
    assert back.content[0]["text"] == "look at this"


def test_json_input_round_trips_as_non_standard_block():
    human = HumanMessage(
        content=[{"type": "non_standard", "value": {"media_type": "application/json", "data": {"campaign": 7}}}]
    )
    back = a2a_message_to_human_message(human_messages_to_a2a_message([human], "ctx", None))
    assert back.content == [{"type": "non_standard", "value": {"media_type": "application/json", "data": {"campaign": 7.0}}}]


def test_message_metadata_carries_the_continuity_and_proposal_fields():
    msg = human_messages_to_a2a_message(
        [HumanMessage(content="go")],
        "ctx",
        "task-1",
        scheduled_job_id=7,
        message_formatting="slack",
        extra_metadata={PROPOSED_TASK_ID_KEY: "proposed", "ignored": None},
    )
    meta = MessageToDict(msg.metadata)
    assert msg.task_id == "task-1"
    assert meta["scheduled_job_id"] == 7
    assert meta["messageFormatting"] == "slack"
    assert meta[PROPOSED_TASK_ID_KEY] == "proposed"
    assert "ignored" not in meta


def test_structured_answers_round_trip_as_dicts_and_words_as_text():
    decisions = {"decisions": [{"type": "approve", "id": "c1"}]}
    msg = human_messages_to_a2a_message([answer_to_human_message(decisions)], "ctx", "t")
    assert answer_from_message(msg) == decisions

    authorization = {"authorization": {"decision": "declined", "message": "no"}}
    msg = human_messages_to_a2a_message([answer_to_human_message(authorization)], "ctx", "t")
    assert answer_from_message(msg) == authorization

    msg = human_messages_to_a2a_message([answer_to_human_message("ok go ahead")], "ctx", "t")
    assert answer_from_message(msg) == "ok go ahead"

    # Nothing to say travels as an empty dict, which the readers treat as "no answer".
    msg = human_messages_to_a2a_message([answer_to_human_message(None)], "ctx", "t")
    assert answer_from_message(msg) == {}


def test_interrupt_value_is_rebuilt_from_a_hitl_status():
    message = new_hitl_interrupt_message(
        "approve?",
        [{"name": "rm", "args": {"_call_id": "rm:1"}}],
        [{"action_name": "rm", "allowed_decisions": ["approve", "reject"]}],
        context_id="ctx",
        task_id="t",
    )
    assert HUMAN_IN_THE_LOOP_EXTENSION in message.extensions
    value = interrupt_value_from_status(TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED, message=message))
    assert value["action_requests"][0]["name"] == "rm"
    assert value["review_configs"][0]["action_name"] == "rm"


def test_interrupt_value_is_rebuilt_from_an_auth_status():
    from agent_common.a2a.authentication import AuthPayload

    payload = AuthPayload.for_service(service="github", resource="github_get_me", auth_url="https://auth").client_payload()
    message = new_auth_required_message("please log in", payload, context_id="ctx", task_id="t")
    assert IN_TASK_AUTH_EXTENSION in message.extensions
    value = interrupt_value_from_status(TaskStatus(state=TaskState.TASK_STATE_AUTH_REQUIRED, message=message))
    assert value["task_state"] == TaskState.TASK_STATE_AUTH_REQUIRED
    assert value["auth_url"] == "https://auth"
    assert value["tool"] == "github_get_me"
    assert value["service"] == "github"


def test_interrupt_value_is_rebuilt_from_a_client_action_request():
    message = new_client_action_request_message({"id": "c", "directive": {"kind": "apply"}}, context_id="ctx", task_id="t")
    assert CLIENT_ACTION_EXTENSION in message.extensions
    value = interrupt_value_from_status(TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED, message=message))
    assert value == {"client_action_request": {"id": "c", "directive": {"kind": "apply"}}}


def test_interrupt_value_of_a_bare_status_is_empty():
    assert interrupt_value_from_status(TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED)) == {}


def test_thread_id_convention():
    assert local_sub_agent_thread_id("ctx-1", "report-agent") == "ctx-1::dynamic-report-agent"


def test_seal_dangling_tool_calls_answers_only_the_unanswered():
    messages = [
        HumanMessage("do it"),
        AIMessage(content="", tool_calls=[{"id": "a", "name": "x", "args": {}}, {"id": "b", "name": "y", "args": {}}]),
        ToolMessage(content="ok", tool_call_id="a"),
    ]
    seals = seal_dangling_tool_calls(messages)
    assert [s.tool_call_id for s in seals] == ["b"]
    assert seals[0].status == "error"
    assert seal_dangling_tool_calls(messages + seals) == []
