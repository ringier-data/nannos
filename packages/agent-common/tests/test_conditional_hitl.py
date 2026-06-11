"""Unit tests for ConditionalHumanInTheLoopMiddleware._apply_bypass_rule."""

import types

from langchain_core.messages import AIMessage

from agent_common.middleware.conditional_hitl import ConditionalHumanInTheLoopMiddleware
from agent_common.middleware.ptc_guard import PTC_CODE_INTERPRETER_TOOL_NAME


class TestApplyBypassRule:
    """Tests for the static _apply_bypass_rule method."""

    def _make_context(self, bypass_rules: dict | None = None) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            tool_bypass_rules=bypass_rules if bypass_rules is not None else {},
            _pending_bypass_rules=[],
        )

    def test_bypass_all(self):
        ctx = self._make_context()
        ConditionalHumanInTheLoopMiddleware._apply_bypass_rule(
            tool_name="execute",
            server_slug="_self",
            bypass_all=True,
            bypass_pattern=None,
            context=ctx,
        )
        assert ctx.tool_bypass_rules["execute::_self"] == {
            "bypass_all": True,
            "bypass_patterns": {},
        }
        assert len(ctx._pending_bypass_rules) == 1
        assert ctx._pending_bypass_rules[0]["key"] == "execute::_self"

    def test_bypass_pattern_matches_format(self):
        """Pattern from risk metadata: 'param matches `glob`'."""
        ctx = self._make_context()
        ConditionalHumanInTheLoopMiddleware._apply_bypass_rule(
            tool_name="execute",
            server_slug="_self",
            bypass_all=False,
            bypass_pattern="command matches `*python*`",
            context=ctx,
        )
        rule = ctx.tool_bypass_rules["execute::_self"]
        assert rule["bypass_all"] is False
        assert rule["bypass_patterns"] == {"command": ["*python*"]}
        assert len(ctx._pending_bypass_rules) == 1

    def test_bypass_pattern_colon_format(self):
        """Legacy format: 'param:glob'."""
        ctx = self._make_context()
        ConditionalHumanInTheLoopMiddleware._apply_bypass_rule(
            tool_name="execute",
            server_slug="_self",
            bypass_all=False,
            bypass_pattern="command:*python*",
            context=ctx,
        )
        rule = ctx.tool_bypass_rules["execute::_self"]
        assert rule["bypass_all"] is False
        assert rule["bypass_patterns"] == {"command": ["*python*"]}

    def test_bypass_pattern_merges_into_existing(self):
        ctx = self._make_context({"execute::_self": {"bypass_all": False, "bypass_patterns": {"command": ["*bash*"]}}})
        ConditionalHumanInTheLoopMiddleware._apply_bypass_rule(
            tool_name="execute",
            server_slug="_self",
            bypass_all=False,
            bypass_pattern="command matches `*python*`",
            context=ctx,
        )
        rule = ctx.tool_bypass_rules["execute::_self"]
        assert rule["bypass_patterns"]["command"] == ["*bash*", "*python*"]

    def test_unparseable_pattern_does_not_crash(self):
        """If bypass_pattern can't be parsed, no rule is stored and no KeyError."""
        ctx = self._make_context()
        ConditionalHumanInTheLoopMiddleware._apply_bypass_rule(
            tool_name="execute",
            server_slug="_self",
            bypass_all=False,
            bypass_pattern="something unparseable",
            context=ctx,
        )
        assert "execute::_self" not in ctx.tool_bypass_rules
        assert len(ctx._pending_bypass_rules) == 0

    def test_no_context_bypass_rules_is_noop(self):
        ctx = types.SimpleNamespace(tool_bypass_rules=None)
        # Should not raise
        ConditionalHumanInTheLoopMiddleware._apply_bypass_rule(
            tool_name="execute",
            server_slug="_self",
            bypass_all=True,
            bypass_pattern=None,
            context=ctx,
        )

    def test_duplicate_pattern_not_added_twice(self):
        ctx = self._make_context()
        for _ in range(2):
            ConditionalHumanInTheLoopMiddleware._apply_bypass_rule(
                tool_name="execute",
                server_slug="_self",
                bypass_all=False,
                bypass_pattern="command matches `*python*`",
                context=ctx,
            )
        rule = ctx.tool_bypass_rules["execute::_self"]
        assert rule["bypass_patterns"]["command"] == ["*python*"]


class TestIsBypassed:
    """Tests for the static _is_bypassed method."""

    def test_no_rule_returns_false(self):
        assert (
            ConditionalHumanInTheLoopMiddleware._is_bypassed(
                tool_name="execute",
                server_slug="_self",
                args={"command": "python3 script.py"},
                bypass_rules={},
            )
            is False
        )

    def test_bypass_all_returns_true(self):
        rules = {"execute::_self": {"bypass_all": True, "bypass_patterns": {}}}
        assert (
            ConditionalHumanInTheLoopMiddleware._is_bypassed(
                tool_name="execute",
                server_slug="_self",
                args={"command": "anything"},
                bypass_rules=rules,
            )
            is True
        )

    def test_matching_glob_pattern_returns_true(self):
        rules = {"execute::_self": {"bypass_all": False, "bypass_patterns": {"command": ["*python*"]}}}
        assert (
            ConditionalHumanInTheLoopMiddleware._is_bypassed(
                tool_name="execute",
                server_slug="_self",
                args={"command": "python3 /home/ubuntu/script.py"},
                bypass_rules=rules,
            )
            is True
        )

    def test_non_matching_glob_pattern_returns_false(self):
        rules = {"execute::_self": {"bypass_all": False, "bypass_patterns": {"command": ["*python*"]}}}
        assert (
            ConditionalHumanInTheLoopMiddleware._is_bypassed(
                tool_name="execute",
                server_slug="_self",
                args={"command": "rm -rf /"},
                bypass_rules=rules,
            )
            is False
        )

    def test_different_server_slug_not_matched(self):
        rules = {"execute::my-server": {"bypass_all": True, "bypass_patterns": {}}}
        assert (
            ConditionalHumanInTheLoopMiddleware._is_bypassed(
                tool_name="execute",
                server_slug="_self",
                args={"command": "python3 foo.py"},
                bypass_rules=rules,
            )
            is False
        )

    def test_missing_arg_value_returns_false(self):
        rules = {"execute::_self": {"bypass_all": False, "bypass_patterns": {"command": ["*python*"]}}}
        assert (
            ConditionalHumanInTheLoopMiddleware._is_bypassed(
                tool_name="execute",
                server_slug="_self",
                args={},  # no "command" arg
                bypass_rules=rules,
            )
            is False
        )

    def test_roundtrip_apply_then_check(self):
        """Apply a rule via _apply_bypass_rule, then verify _is_bypassed uses it."""
        ctx = types.SimpleNamespace(tool_bypass_rules={}, _pending_bypass_rules=[])
        ConditionalHumanInTheLoopMiddleware._apply_bypass_rule(
            tool_name="execute",
            server_slug="_self",
            bypass_all=False,
            bypass_pattern="command matches `*python*`",
            context=ctx,
        )
        assert (
            ConditionalHumanInTheLoopMiddleware._is_bypassed(
                tool_name="execute",
                server_slug="_self",
                args={"command": "python3 /home/ubuntu/skills/printing/scripts/print.py"},
                bypass_rules=ctx.tool_bypass_rules,
            )
            is True
        )
        assert (
            ConditionalHumanInTheLoopMiddleware._is_bypassed(
                tool_name="execute",
                server_slug="_self",
                args={"command": "ls -la"},
                bypass_rules=ctx.tool_bypass_rules,
            )
            is False
        )


class TestRiskScoringExclusions:
    """The risk-based guard must never interrupt dispatch/PTC primitives."""

    async def _run(self, tool_name: str):
        scored: list[str] = []

        async def scorer(name, args, *, tool=None, cache=None, server_slug=None):
            scored.append(name)
            return 0.99, None  # always high-risk

        mw = ConditionalHumanInTheLoopMiddleware(
            interrupt_on={},
            risk_scorer=scorer,
            default_risk_threshold=0.8,
        )
        ai = AIMessage(
            content="",
            tool_calls=[{"name": tool_name, "args": {"code": "x"}, "id": "1", "type": "tool_call"}],
        )
        state = {"messages": [ai]}
        runtime = types.SimpleNamespace(
            context=types.SimpleNamespace(tool_bypass_rules={}, tool_risk_cache=None, _pending_bypass_rules=[])
        )
        # Returns None (no interrupt) and never scores the excluded tool.
        result = await mw.aafter_model(state, runtime)
        return result, scored

    async def test_eval_tool_never_interrupted_or_scored(self):
        result, scored = await self._run(PTC_CODE_INTERPRETER_TOOL_NAME)
        assert result is None
        assert PTC_CODE_INTERPRETER_TOOL_NAME not in scored

    async def test_task_dispatch_never_interrupted_or_scored(self):
        result, scored = await self._run("task")
        assert result is None
        assert "task" not in scored


class TestPerCallIdStamping:
    """Every interrupted call must carry a top-level ``args._call_id`` — static guards
    and risk-scored alike — so the resume path aligns decisions by id (not position).
    """

    @staticmethod
    def _capture_interrupt(monkeypatch):
        """Patch the middleware's ``interrupt`` to capture the HITLRequest and approve."""
        captured: dict = {}

        def fake_interrupt(request):
            captured["request"] = request
            return {"decisions": [{"type": "approve"} for _ in request["action_requests"]]}

        monkeypatch.setattr("agent_common.middleware.conditional_hitl.interrupt", fake_interrupt)
        return captured

    async def test_static_guard_stamps_top_level_call_id(self, monkeypatch):
        captured = self._capture_interrupt(monkeypatch)
        mw = ConditionalHumanInTheLoopMiddleware(interrupt_on={"danger": {"allowed_decisions": ["approve", "reject"]}})
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "danger", "args": {"x": 1}, "id": "tc-static", "type": "tool_call"}],
        )
        runtime = types.SimpleNamespace(context=None)

        await mw.aafter_model({"messages": [ai]}, runtime)

        ar = captured["request"]["action_requests"][0]
        assert ar["args"]["_call_id"] == "tc-static"
        # Static guards carry no risk metadata.
        assert "_risk_metadata" not in ar["args"]

    async def test_risk_scored_stamps_top_level_call_id_not_in_risk_metadata(self, monkeypatch):
        captured = self._capture_interrupt(monkeypatch)

        async def scorer(name, args, *, tool=None, cache=None, server_slug=None):
            return 0.99, None

        mw = ConditionalHumanInTheLoopMiddleware(interrupt_on={}, risk_scorer=scorer, default_risk_threshold=0.8)
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "wipe", "args": {"path": "/"}, "id": "tc-risk", "type": "tool_call"}],
        )
        runtime = types.SimpleNamespace(
            context=types.SimpleNamespace(tool_bypass_rules={}, tool_risk_cache=None, _pending_bypass_rules=[])
        )

        await mw.aafter_model({"messages": [ai]}, runtime)

        ar = captured["request"]["action_requests"][0]
        assert ar["args"]["_call_id"] == "tc-risk"
        # call_id lives top-level now, not smuggled inside the risk blob.
        assert "call_id" not in ar["args"]["_risk_metadata"]

    async def test_sync_static_guard_stamps_top_level_call_id(self, monkeypatch):
        captured = self._capture_interrupt(monkeypatch)
        mw = ConditionalHumanInTheLoopMiddleware(interrupt_on={"danger": {"allowed_decisions": ["approve", "reject"]}})
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "danger", "args": {"x": 1}, "id": "tc-sync", "type": "tool_call"}],
        )

        mw.after_model({"messages": [ai]}, types.SimpleNamespace(context=None))

        assert captured["request"]["action_requests"][0]["args"]["_call_id"] == "tc-sync"
