"""Unit tests for ``_build_subagent_resume_command``.

Covers the LangGraph >=1.2 interrupt-id-keyed resume migration: local in-process
sub-agents must be resumed with an id-keyed map (so >1 pending interrupt does not
raise RuntimeError), while remote A2A sub-agents keep the plain payload (the remote
rebuilds its own resume from the A2A DataPart).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from langgraph.types import Command

from agent_common.a2a.base import LocalA2ARunnable
from agent_common.a2a.client_runnable import A2AClientRunnable
from app.middleware.dynamic_tool_dispatch import _build_subagent_resume_command

# A valid xxh3_128 hexdigest (32 lowercase hex chars) — the format LangGraph uses
# for interrupt ids / namespace hashes.
INTERRUPT_ID = "45fda8478b2ef754419799e10992af06"
DECISIONS = {"decisions": [{"type": "approve"}]}


def _local_runnable() -> LocalA2ARunnable:
    return MagicMock(spec=LocalA2ARunnable)


def _remote_runnable() -> A2AClientRunnable:
    return MagicMock(spec=A2AClientRunnable)


def test_local_runnable_produces_id_keyed_map():
    intr = SimpleNamespace(id=INTERRUPT_ID, value={"action_requests": [{"name": "x"}]})
    cmd = _build_subagent_resume_command(_local_runnable(), intr, DECISIONS)
    assert isinstance(cmd, Command)
    assert cmd.resume == {INTERRUPT_ID: DECISIONS}


def test_remote_runnable_keeps_plain_payload():
    intr = SimpleNamespace(id=INTERRUPT_ID, value={"action_requests": [{"name": "x"}]})
    cmd = _build_subagent_resume_command(_remote_runnable(), intr, DECISIONS)
    assert cmd.resume == DECISIONS


def test_local_runnable_without_interrupt_id_falls_back_to_plain():
    intr = SimpleNamespace(value={"action_requests": [{"name": "x"}]})  # no .id
    cmd = _build_subagent_resume_command(_local_runnable(), intr, DECISIONS)
    assert cmd.resume == DECISIONS


def test_interrupt_id_extracted_from_dict():
    intr = {"id": INTERRUPT_ID, "value": {"action_requests": [{"name": "x"}]}}
    cmd = _build_subagent_resume_command(_local_runnable(), intr, DECISIONS)
    assert cmd.resume == {INTERRUPT_ID: DECISIONS}


def test_non_dict_user_decisions_become_empty_payload():
    intr = SimpleNamespace(id=INTERRUPT_ID, value={})
    cmd = _build_subagent_resume_command(_local_runnable(), intr, "not-a-dict")
    assert cmd.resume == {INTERRUPT_ID: {}}


def test_none_interrupt_obj_is_safe():
    cmd = _build_subagent_resume_command(_local_runnable(), None, DECISIONS)
    assert cmd.resume == DECISIONS
