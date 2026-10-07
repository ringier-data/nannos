"""The per-event log line in app.py carries ids and shape, never what the parts say."""

from app import _describe_agent_response


def test_the_log_line_has_no_part_content():
    response = {
        "kind": "status-update",
        "id": "t1",
        "contextId": "c1",
        "status": {
            "state": "TASK_STATE_INPUT_REQUIRED",
            "message": {
                "parts": [
                    {"text": "Your phone is +41795551234"},
                    {"data": {"client_action_request": {"directive": {"values": {"phone": "+41795551234"}}}}},
                ]
            },
        },
        "validation_errors": [],
    }
    line = _describe_agent_response(response)
    assert "+41795551234" not in line
    assert "id=t1" in line and "context=c1" in line and "state=TASK_STATE_INPUT_REQUIRED" in line
    assert "parts=data:1,text:1" in line and "text_chars=26" in line


def test_artifact_parts_are_counted_too():
    line = _describe_agent_response({"kind": "artifact-update", "artifact": {"parts": [{"text": "abc"}]}})
    assert "parts=text:1" in line and "text_chars=3" in line
