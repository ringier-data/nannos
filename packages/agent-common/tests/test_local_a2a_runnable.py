"""Tests for LocalA2ARunnable base class.

Covers:
- extend_config_for_checkpoint_isolation(): inherits parent config, overrides thread_id/checkpoint_ns
- extend_config_for_subagent(): adds sub_agent:{id} tag on top of checkpoint isolation
- No-overwrite of existing tags in extend_config_for_subagent()
- Checkpointer injection via __pregel_checkpointer when provided
- get_thread_id(): default pattern "{context_id}::{checkpoint_ns}"
- get_thread_id(): context_id empty → returns checkpoint_ns alone
- get_checkpointer(): default returns None
"""

import pytest
from typing import Any, Dict, List
from unittest.mock import MagicMock

from agent_common.a2a.base import LocalA2ARunnable, SubAgentInput
from langchain_core.messages import HumanMessage


class StubLocalAgent(LocalA2ARunnable):
    """Minimal concrete implementation of LocalA2ARunnable for testing."""

    @property
    def name(self) -> str:
        return "stub-agent"

    @property
    def input_modes(self) -> List[str]:
        return ["text"]

    @property
    def description(self) -> str:
        return "Stub agent for unit tests"

    def get_checkpoint_ns(self, input_data: SubAgentInput) -> str:
        return "stub-ns"

    def get_sub_agent_identifier(self, input_data: SubAgentInput) -> str:
        return "stub-id"

    async def _process(
        self,
        input_data: SubAgentInput,
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self._build_success_response("ok")

def _make_input(context_id: str = "ctx-123") -> SubAgentInput:
    return SubAgentInput(
        messages=[HumanMessage(content="hello")],
        a2a_tracking={"orchestrator": {"context_id": context_id, "task_id": "t1"}},
    )


def _base_config(**extra) -> Dict[str, Any]:
    return {
        "metadata": {"user_id": "user-1", "assistant_id": "asst-1"},
        "tags": ["parent-tag"],
        "callbacks": ["cb-placeholder"],
        "configurable": {
            "thread_id": "parent-thread",
            "checkpoint_ns": "parent-ns",
        },
        **extra,
    }


class TestExtendConfigForCheckpointIsolation:

    def setup_method(self):
        self.agent = StubLocalAgent()

    def test_inherits_parent_metadata(self):
        config = _base_config()
        result = self.agent.extend_config_for_checkpoint_isolation(
            config=config,
            thread_id="ctx::stub-ns",
            checkpoint_ns="stub-ns",
        )
        assert result["metadata"] == {"user_id": "user-1", "assistant_id": "asst-1"}

    def test_inherits_parent_tags(self):
        config = _base_config()
        result = self.agent.extend_config_for_checkpoint_isolation(
            config=config,
            thread_id="ctx::stub-ns",
            checkpoint_ns="stub-ns",
        )
        assert "parent-tag" in result["tags"]

    def test_inherits_parent_callbacks(self):
        config = _base_config()
        result = self.agent.extend_config_for_checkpoint_isolation(
            config=config,
            thread_id="ctx::stub-ns",
            checkpoint_ns="stub-ns",
        )
        assert result["callbacks"] == ["cb-placeholder"]

    def test_overrides_thread_id(self):
        config = _base_config()
        result = self.agent.extend_config_for_checkpoint_isolation(
            config=config,
            thread_id="ctx::stub-ns",
            checkpoint_ns="stub-ns",
        )
        assert result["configurable"]["thread_id"] == "ctx::stub-ns"

    def test_overrides_checkpoint_ns(self):
        config = _base_config()
        result = self.agent.extend_config_for_checkpoint_isolation(
            config=config,
            thread_id="ctx::stub-ns",
            checkpoint_ns="stub-ns",
        )
        assert result["configurable"]["checkpoint_ns"] == "stub-ns"

    def test_no_pregel_checkpointer_when_none_provided(self):
        config = _base_config()
        result = self.agent.extend_config_for_checkpoint_isolation(
            config=config,
            thread_id="ctx::stub-ns",
            checkpoint_ns="stub-ns",
            checkpointer=None,
        )
        assert "__pregel_checkpointer" not in result["configurable"]

    def test_injects_pregel_checkpointer_when_provided(self):
        mock_checkpointer = MagicMock(name="checkpointer")
        config = _base_config()
        result = self.agent.extend_config_for_checkpoint_isolation(
            config=config,
            thread_id="ctx::stub-ns",
            checkpoint_ns="stub-ns",
            checkpointer=mock_checkpointer,
        )
        assert result["configurable"]["__pregel_checkpointer"] is mock_checkpointer

    def test_empty_config_does_not_raise(self):
        result = self.agent.extend_config_for_checkpoint_isolation(
            config={},
            thread_id="t",
            checkpoint_ns="ns",
        )
        assert result["configurable"]["thread_id"] == "t"
        assert result["configurable"]["checkpoint_ns"] == "ns"


class TestExtendConfigForSubagent:

    def setup_method(self):
        self.agent = StubLocalAgent()

    def test_appends_sub_agent_tag(self):
        config = _base_config()
        result = self.agent.extend_config_for_subagent(
            config=config,
            sub_agent_identifier="stub-id",
            thread_id="ctx::stub-ns",
            checkpoint_ns="stub-ns",
        )
        assert "sub_agent:stub-id" in result["tags"]

    def test_does_not_overwrite_existing_tags(self):
        config = _base_config()
        result = self.agent.extend_config_for_subagent(
            config=config,
            sub_agent_identifier="stub-id",
            thread_id="ctx::stub-ns",
            checkpoint_ns="stub-ns",
        )
        # Both original tag and new sub_agent tag present
        assert "parent-tag" in result["tags"]
        assert "sub_agent:stub-id" in result["tags"]

    def test_thread_id_set_correctly(self):
        config = _base_config()
        result = self.agent.extend_config_for_subagent(
            config=config,
            sub_agent_identifier="stub-id",
            thread_id="ctx::stub-ns",
            checkpoint_ns="stub-ns",
        )
        assert result["configurable"]["thread_id"] == "ctx::stub-ns"

    def test_checkpoint_ns_set_correctly(self):
        config = _base_config()
        result = self.agent.extend_config_for_subagent(
            config=config,
            sub_agent_identifier="stub-id",
            thread_id="ctx::stub-ns",
            checkpoint_ns="stub-ns",
        )
        assert result["configurable"]["checkpoint_ns"] == "stub-ns"

    def test_metadata_inherited(self):
        config = _base_config()
        result = self.agent.extend_config_for_subagent(
            config=config,
            sub_agent_identifier="stub-id",
            thread_id="ctx::stub-ns",
            checkpoint_ns="stub-ns",
        )
        assert result["metadata"] == {"user_id": "user-1", "assistant_id": "asst-1"}

    def test_checkpointer_injected_when_provided(self):
        mock_cp = MagicMock()
        config = _base_config()
        result = self.agent.extend_config_for_subagent(
            config=config,
            sub_agent_identifier="stub-id",
            thread_id="ctx::stub-ns",
            checkpoint_ns="stub-ns",
            checkpointer=mock_cp,
        )
        assert result["configurable"]["__pregel_checkpointer"] is mock_cp

    def test_no_tags_in_parent_config(self):
        """When parent config has no tags list, sub_agent tag is still added."""
        config = {"configurable": {}}
        result = self.agent.extend_config_for_subagent(
            config=config,
            sub_agent_identifier="my-agent",
            thread_id="t",
            checkpoint_ns="ns",
        )
        assert "sub_agent:my-agent" in result["tags"]

    def test_multiple_sub_agent_tags_accumulate(self):
        """Config that already has a sub_agent tag gets another one appended."""
        config = {
            "tags": ["sub_agent:parent-agent"],
            "configurable": {},
        }
        result = self.agent.extend_config_for_subagent(
            config=config,
            sub_agent_identifier="child-agent",
            thread_id="t",
            checkpoint_ns="ns",
        )
        assert "sub_agent:parent-agent" in result["tags"]
        assert "sub_agent:child-agent" in result["tags"]

class TestGetThreadId:

    def setup_method(self):
        self.agent = StubLocalAgent()
        self.input_data = _make_input(context_id="ctx-abc")

    def test_default_pattern_context_id_and_checkpoint_ns(self):
        """Default: {context_id}::{checkpoint_ns}"""
        thread_id = self.agent.get_thread_id("ctx-abc", self.input_data)
        assert thread_id == "ctx-abc::stub-ns"

    def test_empty_context_id_returns_checkpoint_ns_alone(self):
        """When context_id is empty string, returns only checkpoint_ns."""
        thread_id = self.agent.get_thread_id("", self.input_data)
        assert thread_id == "stub-ns"

    def test_none_context_id_returns_checkpoint_ns_alone(self):
        """When context_id is None/falsy, returns only checkpoint_ns."""
        thread_id = self.agent.get_thread_id(None, self.input_data)  # type: ignore[arg-type]
        assert thread_id == "stub-ns"


class TestGetCheckpointer:

    def setup_method(self):
        self.agent = StubLocalAgent()
        self.input_data = _make_input()

    def test_default_returns_none(self):
        """Default implementation returns None (inherit parent checkpointer)."""
        result = self.agent.get_checkpointer(self.input_data)
        assert result is None
