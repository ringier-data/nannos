"""Unit tests for ConditionalHumanInTheLoopMiddleware._apply_bypass_rule."""

import types

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

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


class TestConcurrentRiskScoring:
    """A step's calls are scored together, not one after another, and the requests still
    come out in tool-call order — the decision loop pairs them with interrupt_indices."""

    @staticmethod
    def _runtime():
        return types.SimpleNamespace(
            context=types.SimpleNamespace(tool_bypass_rules={}, tool_risk_cache=None, _pending_bypass_rules=[])
        )

    async def test_scores_run_concurrently_and_requests_keep_call_order(self, monkeypatch):
        import asyncio

        captured = TestPerCallIdStamping._capture_interrupt(monkeypatch)
        running = 0
        peak = 0

        async def scorer(name, args, *, tool=None, cache=None, server_slug=None):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            # The first call is the slowest: sequential scoring would finish it first anyway,
            # concurrent scoring finishes it last — the order must not follow completion.
            await asyncio.sleep({"wipe_a": 0.2, "wipe_b": 0.1}.get(name, 0.0))
            running -= 1
            return 0.99, None

        mw = ConditionalHumanInTheLoopMiddleware(
            interrupt_on={"danger": {"allowed_decisions": ["approve", "reject"]}},
            risk_scorer=scorer,
            default_risk_threshold=0.8,
        )
        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": "wipe_a", "args": {}, "id": "tc-0", "type": "tool_call"},
                {"name": "danger", "args": {}, "id": "tc-1", "type": "tool_call"},
                {"name": "wipe_b", "args": {}, "id": "tc-2", "type": "tool_call"},
            ],
        )

        await mw.aafter_model({"messages": [ai]}, self._runtime())

        assert peak == 2
        assert [ar["args"]["_call_id"] for ar in captured["request"]["action_requests"]] == ["tc-0", "tc-1", "tc-2"]

    async def test_a_failed_score_skips_only_its_own_call(self, monkeypatch):
        captured = TestPerCallIdStamping._capture_interrupt(monkeypatch)

        async def scorer(name, args, *, tool=None, cache=None, server_slug=None):
            if name == "broken":
                raise RuntimeError("classifier down")
            return 0.99, None

        mw = ConditionalHumanInTheLoopMiddleware(interrupt_on={}, risk_scorer=scorer, default_risk_threshold=0.8)
        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": "broken", "args": {}, "id": "tc-0", "type": "tool_call"},
                {"name": "wipe", "args": {}, "id": "tc-1", "type": "tool_call"},
            ],
        )

        await mw.aafter_model({"messages": [ai]}, self._runtime())

        assert [ar["args"]["_call_id"] for ar in captured["request"]["action_requests"]] == ["tc-1"]

    async def test_a_slow_assessment_says_what_the_turn_is_waiting_on(self, monkeypatch):
        import asyncio

        from agent_common.middleware.tool_status import RISK_ASSESSMENT_STATUS_TOOL, TOOL_STATUS_EVENT

        TestPerCallIdStamping._capture_interrupt(monkeypatch)
        parts: list = []
        monkeypatch.setattr("agent_common.core.stream_watchdog._stream_writer", lambda: parts.append)
        monkeypatch.setattr("agent_common.middleware.tool_status.get_stream_writer", lambda: parts.append)

        async def scorer(name, args, *, tool=None, cache=None, server_slug=None):
            await asyncio.sleep(2.2)  # past the 2s grace
            return 0.99, None

        mw = ConditionalHumanInTheLoopMiddleware(interrupt_on={}, risk_scorer=scorer, default_risk_threshold=0.8)
        ai = AIMessage(content="", tool_calls=[{"name": "wipe", "args": {}, "id": "tc-0", "type": "tool_call"}])

        await mw.aafter_model({"messages": [ai]}, self._runtime())

        statuses = [p[1] for p in parts if p[0] == TOOL_STATUS_EVENT]
        assert statuses == [{"status": "Assessing the risk of wipe…", "tool": RISK_ASSESSMENT_STATUS_TOOL}]


def _stub_tool(name: str):
    """A minimal BaseTool the gate can fetch — stands in for a ToolNode-registered tool."""
    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(func=lambda **kwargs: None, name=name, description=name)


class TestUnresolvableToolCall:
    """A camelCase PTC identifier emitted as a native tool call must be answered
    with the fix — never classified, never dispatched — while a snake_case tool the
    middleware simply cannot see stays scored and gated.
    """

    @staticmethod
    def _runtime(registry: dict | None = None):
        return types.SimpleNamespace(
            context=types.SimpleNamespace(
                tool_bypass_rules={},
                tool_risk_cache=None,
                _pending_bypass_rules=[],
                tool_registry=registry if registry is not None else {},
            )
        )

    @staticmethod
    def _mw(scored: list[str]):
        async def scorer(name, args, *, tool=None, cache=None, server_slug=None):
            scored.append(name)
            return 0.99, None  # always high-risk: an interrupt here is visible

        return ConditionalHumanInTheLoopMiddleware(
            interrupt_on={},
            risk_scorer=scorer,
            default_risk_threshold=0.8,
        )

    async def test_camel_alias_answered_with_eval_hint_and_never_scored(self, monkeypatch):
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
        scored: list[str] = []
        mw = self._mw(scored)
        ai = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "consoleCreateBugReport",
                    "args": {"description": "boom"},
                    "id": "tc-camel",
                    "type": "tool_call",
                }
            ],
        )

        result = await mw.aafter_model({"messages": [ai]}, self._runtime({"console_create_bug_report": None}))

        assert result is not None
        assert scored == []  # no classification for a tool nobody could fetch
        # The call is KEPT: a call that already has a ToolMessage is never dispatched,
        # while stripping it would orphan the tool_result (provider 400 on every later
        # turn, #184) and route an all-alias turn to END with the hint unread.
        assert [tc["id"] for tc in ai.tool_calls] == ["tc-camel"]
        tool_msg = result["messages"][-1]
        assert tool_msg.tool_call_id == "tc-camel"
        assert tool_msg.status == "error"
        assert "eval" in tool_msg.content
        assert "tools.consoleCreateBugReport" in tool_msg.content
        assert "console_create_bug_report" in tool_msg.content

    async def test_camel_alias_without_ptc_points_at_the_snake_name(self, monkeypatch):
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "0")
        scored: list[str] = []
        mw = self._mw(scored)
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "githubSearchIssues", "args": {}, "id": "tc", "type": "tool_call"}],
        )

        result = await mw.aafter_model({"messages": [ai]}, self._runtime({"github_search_issues": None}))

        assert scored == []
        assert "github_search_issues" in result["messages"][-1].content

    async def test_unresolvable_call_alongside_a_real_one(self, monkeypatch):
        """The corrective message must not disturb the other calls in the same turn."""
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
        monkeypatch.setattr(
            "agent_common.middleware.conditional_hitl.interrupt",
            lambda request: {"decisions": [{"type": "approve"} for _ in request["action_requests"]]},
        )
        scored: list[str] = []
        mw = self._mw(scored)
        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": "consoleSearchSkills", "args": {}, "id": "tc-bad", "type": "tool_call"},
                {"name": "console_search_skills", "args": {"q": "x"}, "id": "tc-good", "type": "tool_call"},
            ],
        )

        result = await mw.aafter_model({"messages": [ai]}, self._runtime({"console_search_skills": None}))

        assert scored == ["console_search_skills"]  # only the resolvable name is scored
        # Both calls stay on the AIMessage, in their original order: the aliased one is
        # answered by its ToolMessage (never dispatched), the approved one dispatches.
        assert [tc["id"] for tc in ai.tool_calls] == ["tc-bad", "tc-good"]
        assert [m.tool_call_id for m in result["messages"][1:]] == ["tc-bad"]

    async def test_every_answered_call_keeps_a_matching_tool_call(self, monkeypatch):
        """Every ToolMessage returned must have a ``tool_use`` left on the AIMessage.

        A ``tool_result`` with no matching ``tool_use`` is unrecoverable: ``add_messages``
        upserts the stripped AIMessage into the checkpoint and Bedrock/Anthropic then
        hard-400 every later turn on that thread (the #184 postmortem). It also matters
        for routing — langchain's ``model_to_tools`` returns the end destination on
        ``len(tool_calls) == 0``, *before* its artificial-tool-message branch, so an
        all-alias turn would end the run with the corrective hint never read.
        """
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
        mw = self._mw([])
        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": "consoleSearchSkills", "args": {}, "id": "a", "type": "tool_call"},
                {"name": "consoleGrepMcpTools", "args": {}, "id": "b", "type": "tool_call"},
            ],
        )

        result = await mw.aafter_model(
            {"messages": [ai]},
            self._runtime({"console_search_skills": None, "console_grep_mcp_tools": None}),
        )

        answered = {m.tool_call_id for m in result["messages"] if isinstance(m, ToolMessage)}
        remaining = {tc["id"] for tc in ai.tool_calls}
        assert answered == {"a", "b"}
        assert answered <= remaining, "a ToolMessage without its tool_call bricks the thread"
        # Not an empty tool_calls list — that would route the turn to END.
        assert ai.tool_calls

    async def test_unknown_name_is_still_scored(self, monkeypatch):
        """No camel match, no positive signal: absence alone must not refuse a call.

        Tools registered directly with ToolNode (``write_todos``, the filesystem
        tools, the response schemas) never appear in the middleware's view, so
        anything else keeps going through the gate.
        """
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
        monkeypatch.setattr(
            "agent_common.middleware.conditional_hitl.interrupt",
            lambda request: {"decisions": [{"type": "reject"} for _ in request["action_requests"]]},
        )
        scored: list[str] = []
        mw = self._mw(scored)
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "FinalResponseSchema", "args": {}, "id": "tc", "type": "tool_call"}],
        )

        await mw.aafter_model({"messages": [ai]}, self._runtime({"console_search_skills": None}))

        assert scored == ["FinalResponseSchema"]

    async def test_ptc_excluded_tool_is_not_advertised_inside_eval(self, monkeypatch):
        """``notify_user`` (and the other _PTC_EXCLUDED_TOOL_NAMES, and the raw listers)
        are deliberately kept natively bound because they always fail inside the sandbox.
        Sending the model into `eval` for one of them costs a round-trip and bounces
        straight back out through ``_not_a_function_hint``.
        """
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
        mw = self._mw([])
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "notifyUser", "args": {"note": "hi"}, "id": "tc", "type": "tool_call"}],
        )

        result = await mw.aafter_model({"messages": [ai]}, self._runtime({"notify_user": None}))

        content = result["messages"][-1].content
        assert "notify_user" in content
        assert "regular tool call" in content
        assert "tools.notifyUser" not in content

    async def test_hint_honours_the_checkpointed_ptc_exposure_set(self, monkeypatch):
        """A tool outside the set the PTC bridge actually exposed (e.g. filtered out by
        the toolset selector, or not whitelisted) is natively bound, not in `eval`.
        """
        from agent_common.core.graph_utils import PTC_EXPOSED_TOOL_NAMES_STATE_KEY

        monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
        mw = self._mw([])
        registry = {"console_search_skills": None, "github_search_issues": None}

        exposed = AIMessage(
            content="",
            tool_calls=[{"name": "consoleSearchSkills", "args": {}, "id": "tc", "type": "tool_call"}],
        )
        result = await mw.aafter_model(
            {"messages": [exposed], PTC_EXPOSED_TOOL_NAMES_STATE_KEY: ["console_search_skills"]},
            self._runtime(registry),
        )
        assert "tools.consoleSearchSkills" in result["messages"][-1].content

        not_exposed = AIMessage(
            content="",
            tool_calls=[{"name": "githubSearchIssues", "args": {}, "id": "tc2", "type": "tool_call"}],
        )
        result = await mw.aafter_model(
            {"messages": [not_exposed], PTC_EXPOSED_TOOL_NAMES_STATE_KEY: ["console_search_skills"]},
            self._runtime(registry),
        )
        content = result["messages"][-1].content
        assert "regular tool call" in content
        assert "tools.githubSearchIssues" not in content

    async def test_invented_name_is_answered_when_the_platform_set_is_exhaustive(self, monkeypatch):
        """With every dispatchable tool registered, absence IS proof the call cannot
        resolve — so answer it now rather than scoring it. A destructive-sounding
        invention would otherwise clear the floor and raise an approval card (and pay
        for a summary LLM call) for a tool that then fails to dispatch.
        """
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
        scored: list[str] = []

        async def scorer(name, args, *, tool=None, cache=None, server_slug=None):
            scored.append(name)
            return 0.99, None

        mw = ConditionalHumanInTheLoopMiddleware(
            interrupt_on={},
            risk_scorer=scorer,
            default_risk_threshold=0.8,
            platform_tools={"write_todos": _stub_tool("write_todos")},
            platform_tools_are_exhaustive=True,
        )
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "delete_all_campaigns", "args": {}, "id": "tc", "type": "tool_call"}],
        )

        result = await mw.aafter_model({"messages": [ai]}, self._runtime({}))

        assert scored == []
        assert [tc["id"] for tc in ai.tool_calls] == ["tc"]  # kept, answered, not dispatched
        assert "is not a tool that exists here" in result["messages"][-1].content

    async def test_a_registered_builtin_is_scored_not_refused(self, monkeypatch):
        """The flip side: `write_todos` is invisible to the registry but dispatchable.
        Registering it is what keeps the exhaustive verdict from breaking real tools.
        """
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
        monkeypatch.setattr(
            "agent_common.middleware.conditional_hitl.interrupt",
            lambda request: {"decisions": [{"type": "approve"} for _ in request["action_requests"]]},
        )
        seen: list = []

        async def scorer(name, args, *, tool=None, cache=None, server_slug=None):
            seen.append((name, tool is not None))
            return 0.99, None

        todo = _stub_tool("write_todos")
        mw = ConditionalHumanInTheLoopMiddleware(
            interrupt_on={},
            risk_scorer=scorer,
            default_risk_threshold=0.8,
            platform_tools={"write_todos": todo},
            platform_tools_are_exhaustive=True,
        )
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "write_todos", "args": {"todos": []}, "id": "tc", "type": "tool_call"}],
        )

        await mw.aafter_model({"messages": [ai]}, self._runtime({}))

        assert seen == [("write_todos", True)]  # fetched, so scored on its real schema

    async def test_absence_proves_nothing_without_an_exhaustive_set(self, monkeypatch):
        """Default (sub-agents, anything that has not registered everything): an
        unknown name is still scored, because ToolNode tools are invisible here.
        """
        monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
        monkeypatch.setattr(
            "agent_common.middleware.conditional_hitl.interrupt",
            lambda request: {"decisions": [{"type": "reject"} for _ in request["action_requests"]]},
        )
        scored: list[str] = []
        mw = self._mw(scored)
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "write_todos", "args": {}, "id": "tc", "type": "tool_call"}],
        )

        await mw.aafter_model({"messages": [ai]}, self._runtime({}))

        assert scored == ["write_todos"]

    async def test_platform_tool_is_resolved_and_gated(self, monkeypatch):
        """A callable tool the registry does not hold — ``FinalResponseSchema`` and the
        orchestrator's other static tools — must be fetched from ``platform_tools`` so
        it is gated on its real schema rather than on its name.
        """
        from langchain_core.tools import StructuredTool

        monkeypatch.setattr(
            "agent_common.middleware.conditional_hitl.interrupt",
            lambda request: {"decisions": [{"type": "approve"} for _ in request["action_requests"]]},
        )
        final_response = StructuredTool.from_function(
            func=lambda message: message,
            name="FinalResponseSchema",
            description="Format the final response.",
        )
        seen: list = []

        async def scorer(name, args, *, tool=None, cache=None, server_slug=None):
            seen.append((name, tool))
            return 0.99, None

        mw = ConditionalHumanInTheLoopMiddleware(
            interrupt_on={},
            risk_scorer=scorer,
            default_risk_threshold=0.8,
            platform_tools={"FinalResponseSchema": final_response},
        )
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "FinalResponseSchema", "args": {"message": "hi"}, "id": "tc", "type": "tool_call"}],
        )

        await mw.aafter_model({"messages": [ai]}, self._runtime({}))

        assert seen == [("FinalResponseSchema", final_response)]


class TestActionsThatRequireApproval:
    """A page action the host marked ``requiresApproval`` saves something: its invoke
    gets the save's card (click only), while an unmarked invoke still runs freely."""

    OBJECTS = [
        {
            "type": "GatewayModel",
            "id": "claude-haiku-4-5",
            "scope": "update",
            "actions": [
                {"name": "set_tier_default", "label": "Default low tier", "requiresApproval": True},
                {"name": "test", "label": "Test"},
            ],
        }
    ]

    async def _run(
        self,
        monkeypatch,
        action: str,
        bypass_rules: dict | None = None,
        objects: list | None = None,
        prior: list | None = None,
        extra: dict | None = None,
    ):
        captured: dict = {}

        def fake_interrupt(request):
            captured["request"] = request
            return {"decisions": [{"type": "approve"} for _ in request["action_requests"]]}

        monkeypatch.setattr("agent_common.middleware.conditional_hitl.interrupt", fake_interrupt)

        async def scorer(name, args, *, tool=None, cache=None, server_slug=None):
            return 0.1, None  # invoke's deterministic base score

        mw = ConditionalHumanInTheLoopMiddleware(interrupt_on={}, risk_scorer=scorer, default_risk_threshold=0.8)
        monkeypatch.setattr(mw, "_get_server_slug", lambda *_: "embed")
        args = {"kind": "invoke", "target_type": "GatewayModel", "target_id": "claude-haiku-4-5", "action": action}
        args.update(extra or {})
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "client_action", "args": args, "id": "tc-1", "type": "tool_call"}],
        )
        runtime = types.SimpleNamespace(
            context=types.SimpleNamespace(
                tool_bypass_rules=bypass_rules or {},
                tool_risk_cache=None,
                _pending_bypass_rules=[],
                client_objects=self.OBJECTS if objects is None else objects,
            )
        )
        await mw.aafter_model({"messages": [*(prior or []), ai]}, runtime)
        return captured.get("request")

    async def test_a_marked_action_is_asked_and_needs_a_click(self, monkeypatch):
        from agent_common.core.hitl_resume import _needs_a_click

        request = await self._run(monkeypatch, "set_tier_default")
        assert request is not None, "the marked action must raise a card"
        [action_request] = request["action_requests"]
        assert action_request["args"]["_requires_click"] is True
        assert _needs_a_click(request["action_requests"])

    async def test_an_unmarked_action_runs_without_a_card(self, monkeypatch):
        assert await self._run(monkeypatch, "test") is None

    async def test_a_marked_action_on_a_page_opened_this_turn_is_asked(self, monkeypatch):
        """The turn started elsewhere; a navigate landed on the page offering the action.
        Its flag comes from that navigate's result, not the turn-start object list."""
        navigate = AIMessage(
            content="",
            tool_calls=[
                {"name": "client_action", "args": {"kind": "navigate", "to": "/m"}, "id": "nav", "type": "tool_call"}
            ],
        )
        landed = ToolMessage(
            content="Navigation done.", name="client_action", tool_call_id="nav", artifact={"objects": self.OBJECTS}
        )
        prior = [HumanMessage(content="set haiku as the low default"), navigate, landed]
        request = await self._run(monkeypatch, "set_tier_default", objects=[], prior=prior)
        assert request is not None, "the marked action must raise a card"
        assert request["action_requests"][0]["args"]["_requires_click"] is True

    async def test_a_steering_message_does_not_hide_the_landed_page(self, monkeypatch):
        """A message the user sends mid-turn is injected as a steering HumanMessage: it does
        not start a new turn, so the page landed on before it still counts."""
        navigate = AIMessage(
            content="",
            tool_calls=[{"name": "client_action", "args": {"kind": "navigate", "to": "/m"}, "id": "nav", "type": "tool_call"}],
        )
        landed = ToolMessage(
            content="Navigation done.", name="client_action", tool_call_id="nav", artifact={"objects": self.OBJECTS}
        )
        steer = HumanMessage(content="use haiku", additional_kwargs={"steering": True})
        prior = [HumanMessage(content="set the low default"), navigate, landed, steer]
        request = await self._run(monkeypatch, "set_tier_default", objects=[], prior=prior)
        assert request is not None, "the marked action must raise a card"

    async def test_a_page_landed_on_in_an_earlier_turn_is_not_read(self, monkeypatch):
        navigate = AIMessage(
            content="",
            tool_calls=[
                {"name": "client_action", "args": {"kind": "navigate", "to": "/m"}, "id": "nav", "type": "tool_call"}
            ],
        )
        landed = ToolMessage(
            content="Navigation done.", name="client_action", tool_call_id="nav", artifact={"objects": self.OBJECTS}
        )
        prior = [navigate, landed, HumanMessage(content="now on another page")]
        assert await self._run(monkeypatch, "set_tier_default", objects=[], prior=prior) is None

    async def test_the_card_shows_pairs_as_an_object(self, monkeypatch):
        """``args`` travel as typed pairs; the card and the browser's directive read objects."""
        pairs = {"args": [{"name": "role", "value": '"chat"'}]}
        request = await self._run(monkeypatch, "set_tier_default", extra=pairs)
        assert request["action_requests"][0]["args"]["args"] == {"role": "chat"}

    async def test_a_bypass_rule_never_skips_the_click(self, monkeypatch):
        """The browser refuses a marked action without the click, so a standing bypass
        for ``client_action`` must not drop its card (it would fail on every try)."""
        rules = {"client_action::embed": {"bypass_all": True}}
        request = await self._run(monkeypatch, "set_tier_default", bypass_rules=rules)
        assert request is not None
        assert request["action_requests"][0]["args"]["_requires_click"] is True
        assert await self._run(monkeypatch, "test", bypass_rules=rules) is None


def test_a_steering_message_after_a_reject_lets_the_user_ask_again():
    """'Actually yes, save it' sent mid-run after a Reject is the user asking: the
    identical call is not answered NOT RUN."""
    from agent_common.core.hitl_resume import CLICKED_REJECT_LEAD
    from agent_common.middleware.conditional_hitl import _refused_this_turn

    call = {"name": "client_action", "args": {"kind": "invoke", "action": "save"}, "id": "c1", "type": "tool_call"}
    messages = [
        HumanMessage(content="save it"),
        AIMessage(content="", tool_calls=[call]),
        ToolMessage(content=f"{CLICKED_REJECT_LEAD} no", tool_call_id="c1", status="error"),
    ]
    assert _refused_this_turn(messages)
    steer = HumanMessage(content="actually yes, save it", additional_kwargs={"steering": True})
    assert not _refused_this_turn([*messages, steer])


class TestARefusedCallSentAgainAndAgain:
    """The first retry of a refused call is answered NOT RUN and goes back to the model;
    a second one ends the turn. Sent back again, it looped to the step budget on the
    orchestrator, where loop detection never sees calls this middleware answered."""

    SAVE = {"kind": "invoke", "target_type": "Settings", "target_id": "me", "action": "save"}

    @staticmethod
    def _call(call_id: str) -> AIMessage:
        call = {"name": "client_action", "args": dict(TestARefusedCallSentAgainAndAgain.SAVE), "id": call_id}
        return AIMessage(content="", tool_calls=[{**call, "type": "tool_call"}])

    async def _after(self, monkeypatch, messages):
        def never(request):
            raise AssertionError("a refused call must not be put to the user again")

        monkeypatch.setattr("agent_common.middleware.conditional_hitl.interrupt", never)

        async def scorer(name, args, *, tool=None, cache=None, server_slug=None):
            return 0.9, None

        mw = ConditionalHumanInTheLoopMiddleware(interrupt_on={}, risk_scorer=scorer, default_risk_threshold=0.8)
        monkeypatch.setattr(mw, "_get_server_slug", lambda *_: "embed")
        context = types.SimpleNamespace(
            tool_bypass_rules={}, tool_risk_cache=None, _pending_bypass_rules=[], client_objects=[]
        )
        return await mw.aafter_model({"messages": messages}, types.SimpleNamespace(context=context))

    async def test_the_first_retry_goes_back_to_the_model_the_second_ends_the_turn(self, monkeypatch):
        from agent_common.core.hitl_resume import CLICKED_REJECT_LEAD
        from agent_common.middleware.conditional_hitl import _REFUSED_AGAIN

        rejected = ToolMessage(content=f"{CLICKED_REJECT_LEAD}, no", tool_call_id="c1", status="error")
        turn = [HumanMessage(content="save it"), self._call("c1"), rejected]

        first = await self._after(monkeypatch, [*turn, self._call("c2")])
        assert first["jump_to"] == "model"
        assert first["messages"][-1].content == _REFUSED_AGAIN

        again = ToolMessage(content=_REFUSED_AGAIN, tool_call_id="c2", status="error")
        second = await self._after(monkeypatch, [*turn, self._call("c2"), again, self._call("c3")])
        assert second["jump_to"] == "end"
        assert second["messages"][-1].content == _REFUSED_AGAIN
