"""Tests for scheduled-run conversation adoption (synthetic delegation turn).

A reply under a delivered scheduled-run notification arrives with a
DataPart {"scheduled_run": {...}}. On the first turn of the fresh parent
conversation the orchestrator prepends a synthetic delegation turn
(job prompt -> `task` tool call -> run output) and seeds `a2a_tracking`
so the next dispatch to that sub-agent resumes the run's own
agent-runner conversation.
"""

from a2a.types import Part
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Value
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.core.agent import (
    _build_scheduled_run_history,
    _extract_scheduled_run_provenance,
)

PROVENANCE = {
    "scheduled_run": {
        "context_id": "run-ctx-123",
        "scheduled_job_id": 7,
        "scheduled_job_run_id": 42,
        "sub_agent_id": 5,
        "sub_agent_name": "Report Agent",
        "prompt": "Summarize yesterday's sales.",
        "result_summary": "Sales were up 4%.",
    }
}


def _data_part(payload: dict) -> Part:
    return Part(data=ParseDict(payload, Value()))


class TestExtractScheduledRunProvenance:
    def test_extracts_from_data_part(self):
        parts = [Part(text="user reply"), _data_part(PROVENANCE)]
        run = _extract_scheduled_run_provenance(parts)
        assert run is not None
        assert run["context_id"] == "run-ctx-123"
        assert run["sub_agent_name"] == "Report Agent"

    def test_none_without_data_part(self):
        assert _extract_scheduled_run_provenance([Part(text="hello")]) is None

    def test_none_for_unrelated_data_part(self):
        parts = [_data_part({"decisions": [{"type": "approve"}]})]
        assert _extract_scheduled_run_provenance(parts) is None


class TestBuildScheduledRunHistory:
    def test_builds_synthetic_delegation_turn(self):
        result = _build_scheduled_run_history(dict(PROVENANCE["scheduled_run"]))
        assert result is not None
        messages, tracking = result

        human, ai, tool = messages
        # Role ordering: the first message must be human (providers reject a
        # leading assistant tool-call on a fresh conversation).
        assert isinstance(human, HumanMessage)
        assert "Summarize yesterday's sales." in human.content
        assert 'job_id="7"' in human.content
        assert 'run_id="42"' in human.content

        # The tool call mirrors a real delegation: shared `task` tool, the
        # prompt in `description`, agent name space-stripped like the
        # delegation waterfall's tracking key.
        assert isinstance(ai, AIMessage)
        (tool_call,) = ai.tool_calls
        assert tool_call["name"] == "task"
        assert tool_call["args"] == {
            "subagent_type": "ReportAgent",
            "description": "Summarize yesterday's sales.",
        }

        assert isinstance(tool, ToolMessage)
        assert tool.tool_call_id == tool_call["id"]
        assert tool.content == "Sales were up 4%."
        assert tool.additional_kwargs["a2a_metadata"]["context_id"] == "run-ctx-123"
        assert tool.additional_kwargs["a2a_metadata"]["state"] == "TASK_STATE_COMPLETED"

        # The tracking seed is what makes the sub-agent contextId waterfall
        # resume the run's own agent-runner conversation.
        assert tracking == {
            "ReportAgent": {
                "context_id": "run-ctx-123",
                "is_complete": True,
                "state": "TASK_STATE_COMPLETED",
            }
        }

    def test_float_ids_render_as_integers(self):
        # protobuf Struct numbers arrive as floats via MessageToDict
        run = dict(
            PROVENANCE["scheduled_run"], scheduled_job_id=7.0, scheduled_job_run_id=42.0
        )
        result = _build_scheduled_run_history(run)
        assert result is not None
        human = result[0][0]
        assert 'job_id="7"' in human.content
        assert 'run_id="42"' in human.content

    def test_none_without_sub_agent(self):
        # A watch notification that ran no sub-agent: nothing to reconstruct.
        run = dict(PROVENANCE["scheduled_run"], sub_agent_name=None)
        assert _build_scheduled_run_history(run) is None

    def test_none_without_context_id(self):
        run = dict(PROVENANCE["scheduled_run"], context_id=None)
        assert _build_scheduled_run_history(run) is None
