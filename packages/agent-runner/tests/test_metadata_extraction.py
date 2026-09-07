"""Regression tests for _extract_message_metadata.

Over gRPC the A2A message metadata arrives as a protobuf Struct; a plain
dict() conversion only handles the top level, leaving nested values (the
scheduler's "watch" dict) as Structs that support ["key"] but not .get() —
which crashed _evaluate_watch (AttributeError: get).
"""

from a2a.types import Message, Task
from google.protobuf import struct_pb2
from google.protobuf.json_format import ParseDict

from agent.core import _extract_message_metadata


def _make_task_with_metadata(metadata: dict) -> Task:
    md = struct_pb2.Struct()
    ParseDict(metadata, md)
    return Task(history=[Message(metadata=md)])


def test_nested_watch_metadata_is_plain_dict():
    task = _make_task_with_metadata(
        {
            "job_type": "watch",
            "sub_agent_id": 42,
            "watch": {
                "check_tool": "gcal_list_events",
                "check_args": {"calendar": "primary"},
                "condition_expr": "$.events",
                "expected_value": None,
                "llm_condition": None,
                "last_check_result": None,
                "prompt": "Just say hi",
            },
        }
    )

    meta = _extract_message_metadata(task)

    watch = meta["watch"]
    assert isinstance(watch, dict)
    assert isinstance(watch["check_args"], dict)
    # The exact accesses _evaluate_watch and the sub-agent branch perform:
    assert (watch.get("check_args") or {}) == {"calendar": "primary"}
    assert watch.get("expected_value") is None
    assert (watch or {}).get("prompt") == "Just say hi"
    assert meta["job_type"] == "watch"


def test_empty_history_returns_empty_dict():
    assert _extract_message_metadata(Task(history=[])) == {}


def test_current_time_context_includes_timezone():
    from agent.core import _current_time_context

    line = _current_time_context("Europe/Zurich")
    assert line.startswith("Current time: ")
    assert "UTC" in line
    assert "Europe/Zurich" in line
    # An unresolvable zone falls back to UTC-only, never raises.
    assert "UTC" in _current_time_context("Not/AZone")
    assert "UTC" in _current_time_context(None)
