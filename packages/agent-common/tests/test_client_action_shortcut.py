"""The one-pause path for an approved ``client_action``.

A risk-gated ``client_action`` used to pause twice: once for the human, once for
the browser's result. The embed SDK now runs the directive when the user approves
the card and returns the outcome on the decision, so the middleware can answer
the tool call itself. Measured cost of the pause this removes: a full A2A resume,
which also replays the model node.
"""

from langchain_core.messages import ToolMessage

from agent_common.core.client_action_tool import CLIENT_ACTION_TOOL_NAME
from agent_common.middleware.conditional_hitl import _client_action_tool_message


def _call(name: str = CLIENT_ACTION_TOOL_NAME, kind: str = "apply") -> dict:
    return {
        "type": "tool_call",
        "name": name,
        "id": "tooluse_x",
        "args": {"kind": kind, "target_type": "Campaign", "target_id": "new", "values": {"name": "T"}},
    }


class TestClientActionShortcut:
    def test_an_applied_result_answers_the_tool_call(self):
        decision = {
            "type": "approve",
            "client_action_result": {"ok": True, "applied": ["name", "budget"], "rejected": []},
        }
        msg = _client_action_tool_message(decision, _call())
        assert isinstance(msg, ToolMessage)
        assert msg.tool_call_id == "tooluse_x"
        assert msg.status == "success"
        # The model must learn WHICH fields landed — that is the whole point of
        # the round trip this replaces.
        assert "name, budget" in msg.content
        assert "Nothing is persisted" in msg.content

    def test_rejected_fields_are_reported_not_hidden(self):
        decision = {
            "type": "approve",
            "client_action_result": {
                "ok": True,
                "applied": ["name"],
                "rejected": [{"field": "kpi", "reason": "failed schema validation"}],
            },
        }
        msg = _client_action_tool_message(decision, _call())
        assert "REJECTED" in msg.content
        assert "kpi" in msg.content

    def test_a_failed_result_is_an_error_message(self):
        decision = {"type": "approve", "client_action_result": {"ok": False, "reason": "unknown-target"}}
        msg = _client_action_tool_message(decision, _call())
        assert msg.status == "error"
        assert "no longer on the user's screen" in msg.content

    def test_no_result_falls_back_to_the_round_trip(self):
        # An older SDK sends a bare approve. The tool must still run and ask for
        # itself, so the shortcut has to decline.
        assert _client_action_tool_message({"type": "approve"}, _call()) is None
        assert (
            _client_action_tool_message({"type": "approve", "client_action_result": "nope"}, _call())
            is None
        )

    def test_a_reject_is_never_shortcut(self):
        decision = {"type": "reject", "client_action_result": {"ok": True, "applied": ["name"]}}
        assert _client_action_tool_message(decision, _call()) is None

    def test_another_tool_is_never_shortcut(self):
        decision = {"type": "approve", "client_action_result": {"ok": True, "applied": ["x"]}}
        assert _client_action_tool_message(decision, _call(name="send_email")) is None
