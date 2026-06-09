"""Conditional Human-in-the-Loop middleware.

Extends LangChain's HumanInTheLoopMiddleware to support:
1. Argument-based conditions (static guards via ``interrupt_on`` dict)
2. Dynamic risk scoring (LLM-based scoring via ``risk_scorer`` callable)

The two modes compose: static guards (interrupt_on) always fire regardless of score.
Dynamic scoring evaluates all OTHER tool calls against a risk threshold.

Usage (static only — backward compatible):

    middleware = ConditionalHumanInTheLoopMiddleware(interrupt_on={
        "read_personal_file": {
            "allowed_decisions": ["approve", "reject"],
            "description": "Agent wants to read your personal file.",
        },
    })

Usage (dynamic scoring):

    from agent_common.core.tool_risk_scorer import score_tool_risk

    middleware = ConditionalHumanInTheLoopMiddleware(
        interrupt_on={},  # Static guards (or omit for DB-driven)
        risk_scorer=score_tool_risk,
        default_risk_threshold=0.8,
    )
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langchain.agents.middleware.human_in_the_loop import (
    ActionRequest,
    HITLRequest,
    HumanInTheLoopMiddleware,
    ReviewConfig,
    ToolMessage,
)
from langchain.agents.middleware.types import AgentState, ContextT, ResponseT, StateT
from langchain_core.messages import AIMessage, ToolCall
from langchain_core.tools import BaseTool
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from agent_common.core.tool_risk_cache import ToolRiskCache, ToolRiskEntry
from agent_common.middleware.ptc_guard import PTC_CODE_INTERPRETER_TOOL_NAME

logger = logging.getLogger(__name__)

# Type alias for the risk scorer callable.
# Signature: (tool_name, args, *, tool, cache, server_slug) -> (score, entry | None)
RiskScorerFn = Callable[
    ...,
    Awaitable[tuple[float, ToolRiskEntry | None]],
]

# Type alias for a condition function that gates static interrupts on args.
ConditionFn = Callable[[dict[str, Any]], bool]


class BypassRule(TypedDict, total=False):
    """Structure of a single tool bypass rule in runtime context."""

    bypass_all: bool
    bypass_patterns: dict[str, list[str]]


class _RiskMetadata(TypedDict, total=False):
    """Internal metadata attached to each risk-triggered interrupt."""

    source: str
    score: float
    threshold: float
    matched_pattern: str | None
    server_slug: str
    allowed_actions: list[str]


class ConditionalHumanInTheLoopMiddleware(HumanInTheLoopMiddleware[StateT, ContextT, ResponseT]):
    """HumanInTheLoopMiddleware with conditional guarding and dynamic risk scoring.

    Supports two complementary guard modes:

    1. **Static guards** (``interrupt_on`` dict): Tools listed here ALWAYS trigger
       an interrupt (optionally gated by a ``condition`` callable on args).

    2. **Dynamic risk scoring** (``risk_scorer`` callable): All other tool calls
       are scored asynchronously. If score >= threshold, an interrupt fires.

    The ``aafter_model`` method is async-native and handles both modes.
    The sync ``after_model`` only handles static guards (no scoring).
    """

    def __init__(
        self,
        interrupt_on: dict[str, bool | dict[str, Any]] | None = None,
        *,
        description_prefix: str = "Tool execution requires approval",
        risk_scorer: RiskScorerFn | None = None,
        default_risk_threshold: float = 0.8,
        tool_risk_cache: ToolRiskCache | None = None,
        tool_server_map: dict[str, str] | None = None,
        platform_tools: dict[str, BaseTool] | None = None,
    ) -> None:
        interrupt_on = interrupt_on or {}

        # Store conditions separately before calling super().__init__
        # because InterruptOnConfig doesn't know about 'condition'.
        self._conditions: dict[str, ConditionFn] = {}
        for tool_name, tool_config in interrupt_on.items():
            if isinstance(tool_config, dict) and "condition" in tool_config:
                self._conditions[tool_name] = tool_config["condition"]

        # Dynamic risk scoring
        self._risk_scorer = risk_scorer
        self._default_risk_threshold = default_risk_threshold
        # Fallback cache for graphs that don't pass context (e.g. sub-agents)
        self._tool_risk_cache: ToolRiskCache | None = tool_risk_cache
        # Fallback server map for sub-agents that don't pass context
        self._tool_server_map: dict[str, str] | None = tool_server_map
        # Platform tools (e.g. filesystem tools from FilesystemMiddleware) that
        # aren't in the runtime tool_registry but need schema for risk scoring
        self._platform_tools: dict[str, BaseTool] = platform_tools or {}

        # Pass through to parent (it safely ignores unknown keys in the dict)
        super().__init__(interrupt_on=interrupt_on, description_prefix=description_prefix)

    def _should_interrupt(self, tool_call: ToolCall) -> bool:
        """Check whether a tool call should be statically interrupted.

        Returns True if:
        - The tool is in interrupt_on AND
        - Either no condition is defined, OR the condition returns True for the args.
        """
        tool_name: str = tool_call["name"]
        if tool_name not in self.interrupt_on:
            return False

        condition: ConditionFn | None = self._conditions.get(tool_name)
        if condition is None:
            return True

        return bool(condition(tool_call.get("args", {})))

    def after_model(self, state: AgentState[Any], runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """Sync handler: only processes static interrupt_on guards.

        Does NOT invoke risk scoring (which requires async). If you need
        dynamic risk scoring, ensure your graph uses the async execution path
        which calls ``aafter_model``.
        """
        messages = state["messages"]
        if not messages:
            return None

        last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
        if not last_ai_msg or not last_ai_msg.tool_calls:
            return None

        # Create action requests and review configs for tools that need approval
        action_requests: list[ActionRequest] = []
        review_configs: list[ReviewConfig] = []
        interrupt_indices: list[int] = []

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if self._should_interrupt(tool_call):
                config = self.interrupt_on[tool_call["name"]]
                action_request, review_config = self._create_action_and_config(tool_call, config, state, runtime)
                action_requests.append(action_request)
                review_configs.append(review_config)
                interrupt_indices.append(idx)

        # If no interrupts needed, return early
        if not action_requests:
            return None

        # Create single HITLRequest with all actions and configs
        hitl_request = HITLRequest(
            action_requests=action_requests,
            review_configs=review_configs,
        )

        # Send interrupt and get response
        decisions = interrupt(hitl_request)["decisions"]

        # Validate that the number of decisions matches the number of interrupt tool calls
        if (decisions_len := len(decisions)) != (interrupt_count := len(interrupt_indices)):
            msg = (
                f"Number of human decisions ({decisions_len}) does not match "
                f"number of hanging tool calls ({interrupt_count})."
            )
            raise ValueError(msg)

        # Process decisions and rebuild tool calls in original order
        revised_tool_calls: list[ToolCall] = []
        artificial_tool_messages: list[ToolMessage] = []
        decision_idx = 0

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if idx in interrupt_indices:
                # This was an interrupt tool call - process the decision
                config = self.interrupt_on[tool_call["name"]]
                decision = decisions[decision_idx]
                decision_idx += 1

                revised_tool_call, tool_message = self._process_decision(decision, tool_call, config)
                if revised_tool_call is not None:
                    revised_tool_calls.append(revised_tool_call)
                if tool_message:
                    artificial_tool_messages.append(tool_message)
            else:
                # This was auto-approved - keep original
                revised_tool_calls.append(tool_call)

        # Update the AI message to only include approved tool calls
        last_ai_msg.tool_calls = revised_tool_calls

        return {"messages": [last_ai_msg, *artificial_tool_messages]}

    async def aafter_model(self, state: AgentState[Any], runtime: Runtime[ContextT]) -> dict[str, Any] | None:
        """Async handler: combines static guards + dynamic risk scoring.

        Flow for each tool call:
        1. If tool_name == "task" -> auto-approve (sub-agent owns its own HITL)
        2. If tool is in static interrupt_on -> use static guard (same as sync)
        3. If risk_scorer is configured -> score the tool call:
           a. Check bypass rules from runtime context
           b. Await scorer (cache lookup or LLM call)
           c. Compare against threshold
           d. If score >= threshold -> interrupt with allowed_actions from entry
        4. Otherwise -> auto-approve
        """
        messages = state["messages"]
        if not messages:
            return None

        last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
        if not last_ai_msg or not last_ai_msg.tool_calls:
            return None

        action_requests: list[ActionRequest] = []
        review_configs: list[ReviewConfig] = []
        interrupt_indices: list[int] = []
        # Store risk metadata per interrupt for inclusion in the payload
        _risk_metadata: list[_RiskMetadata] = []

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            tool_name: str = tool_call["name"]
            args: dict[str, Any] = tool_call.get("args", {})

            # 1. Sub-agent dispatch and the PTC code interpreter are never
            #    interrupted here. ``task`` is a dispatch primitive; ``eval`` (the
            #    code interpreter) carries its risk guard on the inner wrapped
            #    tool calls (which return an approval-required payload instead of
            #    executing). Interrupting ``eval`` would trigger a graph
            #    interrupt/resume cycle the PTC bridge is designed to avoid.
            if tool_name in ("task", PTC_CODE_INTERPRETER_TOOL_NAME):
                continue

            # 2. Static guards take priority
            if self._should_interrupt(tool_call):
                config = self.interrupt_on[tool_name]
                action_request, review_config = self._create_action_and_config(tool_call, config, state, runtime)
                action_requests.append(action_request)
                review_configs.append(review_config)
                interrupt_indices.append(idx)
                _risk_metadata.append({"source": "static_guard"})
                continue

            # 3. Dynamic risk scoring
            if self._risk_scorer is None:
                continue

            # Check bypass rules from runtime context
            context: Any = getattr(runtime, "context", None)
            bypass_rules: dict[str, BypassRule] | None = (
                getattr(context, "tool_bypass_rules", None) if context else None
            )
            server_slug: str = self._get_server_slug(tool_name, context)

            if bypass_rules and self._is_bypassed(tool_name, server_slug, args, bypass_rules):
                continue

            # Get tool instance and cache from context
            tool_instance: BaseTool | None = self._get_tool_instance(tool_name, context)
            cache: ToolRiskCache | None = (
                getattr(context, "tool_risk_cache", None) if context else None
            ) or self._tool_risk_cache

            # Score the tool call
            try:
                score: float
                entry: ToolRiskEntry | None
                score, entry = await self._risk_scorer(
                    tool_name,
                    args,
                    tool=tool_instance,
                    cache=cache,
                    server_slug=server_slug,
                )
            except Exception:
                logger.exception("Risk scoring failed for tool '%s', skipping guard", tool_name)
                continue

            # Compare against threshold
            threshold: float = self._get_threshold(context)
            if score < threshold:
                continue

            # Score exceeds threshold — interrupt
            allowed_actions: list[str] = entry.allowed_actions if entry else ["approve", "edit", "reject"]
            matched_pattern: str | None = entry.get_matched_pattern(args) if entry else None

            # Build action request for this tool call
            description = f"Tool '{tool_name}' has risk score {score:.2f} (threshold: {threshold:.2f})"
            if matched_pattern:
                description += f" — {matched_pattern}"

            # Include structured risk metadata in args for frontend rendering
            enriched_args: dict[str, Any] = {
                **args,
                "_risk_metadata": {
                    "source": "risk_score",
                    "score": score,
                    "threshold": threshold,
                    "matched_pattern": matched_pattern,
                    "server_slug": server_slug,
                    "tool_name": tool_name,
                },
            }

            action_request = ActionRequest(
                name=tool_name,
                args=enriched_args,
                description=description,
            )
            review_config = ReviewConfig(
                action_name=tool_name,
                allowed_decisions=allowed_actions,
            )
            # Include args_schema if "edit" is allowed
            if "edit" in allowed_actions and tool_instance is not None:
                try:
                    review_config["args_schema"] = tool_instance.get_input_schema().model_json_schema()
                except Exception:
                    pass

            action_requests.append(action_request)
            review_configs.append(review_config)
            interrupt_indices.append(idx)
            _risk_metadata.append(
                {
                    "source": "risk_score",
                    "score": score,
                    "threshold": threshold,
                    "matched_pattern": matched_pattern,
                    "server_slug": server_slug,
                    "allowed_actions": allowed_actions,
                }
            )

        # If no interrupts needed, return early
        if not action_requests:
            return None

        # Create single HITLRequest with all actions and configs
        hitl_request = HITLRequest(
            action_requests=action_requests,
            review_configs=review_configs,
        )

        # Send interrupt and get response
        decisions = interrupt(hitl_request)["decisions"]

        # Validate decisions count
        if (decisions_len := len(decisions)) != (interrupt_count := len(interrupt_indices)):
            msg = (
                f"Number of human decisions ({decisions_len}) does not match "
                f"number of hanging tool calls ({interrupt_count})."
            )
            raise ValueError(msg)

        # Process decisions and rebuild tool calls in original order
        revised_tool_calls: list[ToolCall] = []
        artificial_tool_messages: list[ToolMessage] = []
        decision_idx = 0

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if idx in interrupt_indices:
                tool_name = tool_call["name"]
                decision = decisions[decision_idx]
                metadata = _risk_metadata[decision_idx]
                decision_idx += 1

                # Determine config for _process_decision
                if tool_name in self.interrupt_on:
                    config = self.interrupt_on[tool_name]
                else:
                    # Dynamic guard — build a synthetic config
                    entry_actions = metadata.get("allowed_actions") or ["approve", "edit", "reject"]
                    config = {"allowed_decisions": entry_actions}

                revised_tool_call, tool_message = self._process_decision(decision, tool_call, config)
                if revised_tool_call is not None:
                    revised_tool_calls.append(revised_tool_call)
                if tool_message:
                    artificial_tool_messages.append(tool_message)

                # Handle bypass-next-time: if user approved with bypass flag,
                # update in-memory bypass rules for this session
                if (
                    decision.get("type") == "approve"
                    and decision.get("bypass")
                    and metadata.get("source") == "risk_score"
                ):
                    self._apply_bypass_rule(
                        tool_name=tool_name,
                        server_slug=metadata.get("server_slug", "_self"),
                        bypass_all=bool(decision.get("bypass_all", False)),
                        bypass_pattern=decision.get("bypass_pattern"),
                        context=context,
                    )
            else:
                # Auto-approved (no interrupt)
                revised_tool_calls.append(tool_call)

        # Update the AI message
        last_ai_msg.tool_calls = revised_tool_calls

        return {"messages": [last_ai_msg, *artificial_tool_messages]}

    # ------------------------------------------------------------------
    # Helper methods for dynamic risk scoring
    # ------------------------------------------------------------------

    def _get_server_slug(self, tool_name: str, context: Any) -> str:
        """Resolve the MCP server slug for a tool.

        MCP tools resolve to their server name (e.g. 'console', 'github').
        In-process tools (read_personal_file, docstore_search) fall back to '_self'.

        Resolution order:
        1. tool_server_map on context (orchestrator pre-builds this)
        2. Middleware-level _tool_server_map (sub-agents inject at build time)
        3. tool.metadata["server_name"] on the tool instance (set by MCP discovery)
        4. Fallback to '_self' (in-process platform tools)
        """
        # Check tool_server_map on context (orchestrator path)
        if context is not None:
            tool_server_map: dict[str, str] | None = getattr(context, "tool_server_map", None)
            if tool_server_map and tool_name in tool_server_map:
                return tool_server_map[tool_name]

        # Check middleware-level fallback (sub-agent path)
        if self._tool_server_map and tool_name in self._tool_server_map:
            return self._tool_server_map[tool_name]

        # Fall back to tool metadata on context's tool_registry
        if context is not None:
            tool_registry: dict[str, Any] | None = getattr(context, "tool_registry", None)
            if tool_registry and tool_name in tool_registry:
                tool = tool_registry[tool_name]
                metadata = getattr(tool, "metadata", None)
                if metadata and isinstance(metadata, dict):
                    server_name = metadata.get("server_name")
                    if server_name:
                        return server_name

        # Default: platform tools
        return "_self"

    def _get_tool_instance(self, tool_name: str, context: Any) -> BaseTool | None:
        """Get a BaseTool instance from the runtime context's tool registry or platform tools."""
        # Check runtime context's tool_registry first
        if context is not None:
            tool_registry: dict[str, BaseTool] | None = getattr(context, "tool_registry", None)
            if tool_registry and tool_name in tool_registry:
                return tool_registry[tool_name]

        # Fallback to platform tools (e.g. filesystem tools from FilesystemMiddleware)
        if tool_name in self._platform_tools:
            return self._platform_tools[tool_name]

        return None

    def _get_threshold(self, context: Any) -> float:
        """
        Get the risk threshold

        TODO: potentially role-adjusted from context.
        """
        if context is None:
            return self._default_risk_threshold

        # Allow per-request threshold override from context
        threshold: float | None = getattr(context, "risk_threshold", None)
        if threshold is not None:
            return float(threshold)

        return self._default_risk_threshold

    @staticmethod
    def _is_bypassed(
        tool_name: str,
        server_slug: str,
        args: dict[str, Any],
        bypass_rules: dict[str, BypassRule],
    ) -> bool:
        """Check if a tool call is bypassed by user rules.

        Bypass rules format:
        {
            "tool_name::server_slug": {"bypass_all": True},
            "other_tool::server": {"bypass_patterns": {"param": ["glob1", "glob2"]}}
        }
        """
        key: str = f"{tool_name}::{server_slug}"
        rule: BypassRule | None = bypass_rules.get(key)
        if rule is None:
            return False

        # bypass_all: skip entirely
        if rule.get("bypass_all"):
            return True

        # bypass_patterns: check if the specific pattern that would trigger
        # is in the bypass list
        bypass_patterns: dict[str, list[str]] = rule.get("bypass_patterns", {})
        if not bypass_patterns:
            return False

        # Check each param's arg value against the bypass patterns
        for param_name, patterns in bypass_patterns.items():
            arg_value = args.get(param_name)
            if arg_value is None:
                continue
            from agent_common.core.tool_risk_cache import _glob_to_regex

            arg_str = str(arg_value)
            for pattern in patterns:
                try:
                    if _glob_to_regex(pattern).match(arg_str):
                        return True
                except Exception:
                    continue

        return False

    @staticmethod
    def _apply_bypass_rule(
        tool_name: str,
        server_slug: str,
        bypass_all: bool,
        bypass_pattern: str | None,
        context: Any,
    ) -> None:
        """Apply a bypass rule to the in-memory context.

        Updates `context.tool_bypass_rules` so subsequent calls in this
        session are automatically bypassed. The orchestrator is responsible
        for persisting the rule to the backend API after the turn completes.
        """
        bypass_rules: dict[str, BypassRule] | None = getattr(context, "tool_bypass_rules", None)
        if bypass_rules is None:
            return

        key = f"{tool_name}::{server_slug}"
        existing: BypassRule = bypass_rules.get(key, {})  # type: ignore[assignment]

        if bypass_all:
            bypass_rules[key] = {"bypass_all": True, "bypass_patterns": {}}
        elif bypass_pattern:
            # Parse param and glob from bypass_pattern.
            # Supported formats:
            #   "param_name:glob_pattern" (legacy)
            #   "param_name matches `glob_pattern`" (from risk metadata)
            param: str | None = None
            glob: str | None = None
            if " matches `" in bypass_pattern and bypass_pattern.endswith("`"):
                param, rest = bypass_pattern.split(" matches `", 1)
                glob = rest[:-1]  # strip trailing backtick
            elif ":" in bypass_pattern:
                param, glob = bypass_pattern.split(":", 1)

            if param and glob:
                patterns = existing.get("bypass_patterns", {})
                param_patterns = patterns.get(param, [])
                if glob not in param_patterns:
                    param_patterns.append(glob)
                patterns[param] = param_patterns
                bypass_rules[key] = {
                    "bypass_all": existing.get("bypass_all", False),
                    "bypass_patterns": patterns,
                }

        # Store pending bypass for persistence by the orchestrator
        if key in bypass_rules:
            pending: list[dict[str, Any]] = getattr(context, "_pending_bypass_rules", [])
            pending.append({"key": key, "rule": bypass_rules[key]})
            if not hasattr(context, "_pending_bypass_rules"):
                context._pending_bypass_rules = pending
