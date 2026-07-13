"""LLM-based tool risk scorer for Dynamic HITL.

Scores tools on first encounter using a cheap LLM (gemini-3-flash-preview),
then caches the result. The scorer is injected into the middleware and called
asynchronously during `aafter_model`.

Scoring flow:
1. Check in-memory ToolRiskCache
2. On miss: fetch from console-backend API
3. On API miss: invoke LLM for structured risk assessment
4. Persist result to API + update cache
5. Return computed risk score for the tool call args
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import BaseTool
from langsmith import traceable
from pydantic import BaseModel, Field

from agent_common.core.client_action_tool import CLIENT_ACTION_TOOL_NAME
from agent_common.core.tool_risk_cache import ParamRiskProfile, ToolRiskCache, ToolRiskEntry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic models for structured LLM output
# ---------------------------------------------------------------------------


class RiskyValuesOutput(BaseModel):
    """A single parameter's risk profile as returned by the LLM."""

    risky_values: dict[str, float] = Field(
        default_factory=dict,
        description="Glob pattern -> risk score (0.0-1.0). Patterns like 'DELETE*', '*DROP*', '/etc/*'.",
    )
    default_contribution: float = Field(
        default=0.0,
        description="Risk contribution when this param has a value that matches no pattern.",
    )


class ToolRiskOutput(BaseModel):
    """Structured LLM output for tool risk scoring."""

    # NOTE: No ge/le constraints here. Pydantic would emit them as JSON-schema
    # `minimum`/`maximum`, which Bedrock's structured-output schema rejects
    # ("For 'number' type, properties maximum, minimum are not supported").
    # The valid range is documented in the description and clamped after the call.
    base_score: float = Field(
        description="Inherent risk of this tool when no risky arg patterns match. "
        "Must be between 0.0 and 1.0. "
        "0.0 = completely safe (read-only info retrieval), "
        "0.3 = low risk (data reads with filters), "
        "0.5 = moderate (writes to user's own data), "
        "0.7 = elevated (writes to shared resources), "
        "1.0 = critical (destructive, irreversible, or security-sensitive operations).",
    )
    risk_factors: dict[str, RiskyValuesOutput] = Field(
        default_factory=dict,
        description="Parameters that control the risk level of this tool. "
        "Only include params whose values meaningfully change the risk "
        "(e.g., 'action', 'method', 'file_path', 'query'). "
        "Do NOT include payload/content params (e.g., 'body', 'data', 'content').",
    )
    reasoning: str = Field(
        description="Brief explanation of why this tool has the given risk level.",
    )


# ---------------------------------------------------------------------------
# Schema hashing
# ---------------------------------------------------------------------------


def compute_schema_hash(tool: BaseTool) -> str:
    """Compute a stable SHA-256 hash of a tool's input schema."""
    schema: dict[str, Any] = tool.get_input_schema().model_json_schema()
    # Sort keys for deterministic hashing
    schema_str: str = json.dumps(schema, sort_keys=True)
    return hashlib.sha256(schema_str.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Main scorer function (injected into middleware)
# ---------------------------------------------------------------------------


@traceable(name="tool-risk-score", run_type="tool")
async def score_tool_risk(
    tool_name: str,
    args: dict[str, Any],
    *,
    tool: BaseTool | None = None,
    cache: ToolRiskCache | None = None,
    server_slug: str = "_self",
) -> tuple[float, ToolRiskEntry | None]:
    """Score a tool call's risk level.

    Returns (risk_score, entry) where entry is the full ToolRiskEntry
    (used by the middleware for allowed_actions and matched_pattern).

    Args:
        tool_name: Name of the tool being called.
        args: The tool call arguments.
        tool: The BaseTool instance (for schema hashing). If None, schema check is skipped.
        cache: The shared ToolRiskCache instance.
        server_slug: MCP server slug (e.g. 'console', 'github'). '_self' for in-process tools.

    Returns:
        Tuple of (risk_score, entry). Entry is None only if scoring completely fails.
    """
    # Embedded Nannos: the client_action tool is the SINGLE HITL path for on-screen
    # actions (the SDK no longer has its own approval card). Score it deterministically
    # by `kind` — never via the LLM/cache — so an ``apply`` (writes into the user's form)
    # always interrupts for approval while ``highlight``/``navigate`` (benign) never do.
    if tool_name == CLIENT_ACTION_TOOL_NAME:
        kind = (args or {}).get("kind")
        score = _CLIENT_ACTION_KIND_SCORES.get(kind, _CLIENT_ACTION_DEFAULT_SCORE)
        now = datetime.now(timezone.utc)
        entry = ToolRiskEntry(
            base_score=score,
            risk_factors={},
            allowed_actions=["approve", "reject"],
            schema_hash="",
            updated_at=now,
            last_accessed_at=now,
        )
        return score, entry

    if cache is None:
        # No cache available — deterministic fallback
        return _deterministic_fallback(tool_name), None

    # Compute schema hash
    current_hash = compute_schema_hash(tool) if tool is not None else ""

    # 1. Check in-memory cache
    entry = cache.get(tool_name, server_slug, current_hash)
    if entry is not None:
        score = entry.match_args(args)
        return score, entry

    # 2. Cache miss — try API (implemented by caller injecting api_client into cache)
    # The cache's refresh loop handles bulk loading. For individual misses during
    # scoring, we do an inline LLM call (step 3).

    # 3. LLM scoring
    try:
        description = ""
        input_schema: dict[str, Any] = {}
        if tool is not None:
            description = tool.description or ""
            input_schema = tool.get_input_schema().model_json_schema()

        entry = await _score_tool_via_llm(tool_name, description, input_schema)
        entry.schema_hash = current_hash

        # Safety floor: an LLM under-rating must never drop a clearly irreversible /
        # destructive operation below the HITL approval gate. Observed in practice:
        # `alloy-riad_delete_campaign_by_id` was LLM-scored 0.75 (< 0.80 threshold)
        # and deleted a campaign without asking. Floor destructive verbs so they
        # always require approval regardless of the model's estimate.
        floor = _destructive_floor(tool_name)
        if floor > entry.base_score:
            logger.info(
                "Flooring destructive tool '%s' risk %.2f -> %.2f (LLM under-rated)",
                tool_name,
                entry.base_score,
                floor,
            )
            entry.base_score = floor

        # Update cache immediately
        cache.put(tool_name, server_slug, entry)

        # Write-through to database (fire-and-forget)
        cache.persist_entry(tool_name, server_slug, entry)

        score = entry.match_args(args)
        return score, entry
    except Exception:
        logger.exception("LLM risk scoring failed for tool '%s', using deterministic fallback", tool_name)
        fallback_score = _deterministic_fallback(tool_name)
        return fallback_score, None


# ---------------------------------------------------------------------------
# LLM scoring implementation
# ---------------------------------------------------------------------------

_SCORING_SYSTEM_PROMPT = """\
You are a security analyst evaluating the risk level of AI agent tool calls.

Given a tool's name, description, and parameter schema, assess:
1. The BASE risk level (when called with typical/safe arguments)
2. Which parameters are CONTROL parameters that can change the risk level
3. For each control parameter, what VALUE PATTERNS indicate elevated risk

Risk scale:
- 0.0-0.2: Safe — read-only queries, information retrieval, status checks
- 0.2-0.4: Low — filtered reads, user's own data access
- 0.4-0.6: Moderate — writes to user's own resources, reversible changes
- 0.6-0.8: Elevated — writes to shared resources, credential access, external calls
- 0.8-1.0: Critical — destructive ops, irreversible deletes, security-sensitive, code execution

For risk_factors, use glob patterns:
- `*` matches any characters
- `?` matches a single character
- `[abc]` matches character set

Examples of control params and risky patterns:
- "method": {"DELETE*": 0.9, "PUT*": 0.6, "POST*": 0.5}
- "file_path": {"/etc/*": 0.9, "/tmp/*": 0.3, "*.exe": 0.8}
- "query": {"*DROP*": 0.95, "*DELETE*": 0.9, "*ALTER*": 0.8}

Do NOT include content/payload parameters (body, data, message) as risk factors —
only parameters whose VALUES determine HOW risky the operation is.\
"""


@traceable(name="llm-tool-risk-classification", run_type="llm")
async def _score_tool_via_llm(
    tool_name: str,
    description: str,
    input_schema: dict[str, Any],
) -> ToolRiskEntry:
    """Call LLM to classify a tool's risk profile.

    Uses the fleet's cheap/fast chat tier for speed and cost efficiency.
    Returns a ToolRiskEntry ready for caching.
    """
    from agent_common.core.model_factory import create_model, get_default_fast_model, require_default_model

    model = create_model(get_default_fast_model() or require_default_model(), streaming=False)
    structured_model = model.with_structured_output(ToolRiskOutput)

    # Build user prompt with tool details
    schema_str = json.dumps(input_schema, indent=2) if input_schema else "No schema available"
    user_prompt = (
        f"Tool name: {tool_name}\n"
        f"Description: {description or 'No description available'}\n"
        f"Input schema:\n```json\n{schema_str}\n```\n\n"
        f"Assess the risk level of this tool."
    )

    result: ToolRiskOutput = await structured_model.ainvoke(
        [
            {"role": "system", "content": _SCORING_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
    )

    # Convert LLM output to ToolRiskEntry
    risk_factors: dict[str, ParamRiskProfile] = {}
    for param_name, profile in result.risk_factors.items():
        risk_factors[param_name] = ParamRiskProfile(
            risky_values=profile.risky_values,
            default_contribution=profile.default_contribution,
        )

    now = datetime.now(timezone.utc)
    entry = ToolRiskEntry(
        base_score=max(0.0, min(1.0, result.base_score)),
        risk_factors=risk_factors,
        allowed_actions=["approve", "edit", "reject"],  # Default; overridden by DB if exists
        schema_hash="",  # Set by caller
        updated_at=now,
        last_accessed_at=now,
    )
    entry.compile_patterns()
    return entry


# ---------------------------------------------------------------------------
# Deterministic fallback (when LLM is unavailable)
# ---------------------------------------------------------------------------


# Irreversible / data-loss verbs. A tool whose name contains one of these must
# never sit below the HITL gate on the strength of an LLM estimate alone. Kept
# narrow (only clearly-destructive verbs) so read-ish names containing "run"/"exec"
# aren't over-gated.
_HARD_DESTRUCTIVE_KEYWORDS: tuple[str, ...] = ("delete", "remove", "drop", "destroy")
_DESTRUCTIVE_FLOOR_SCORE = 0.9

# Deterministic risk per client_action `kind` (Embedded Nannos). Mutating kinds
# gate for approval; benign ones never do. Unknown/new kinds default to gating
# (fail safe). ``refresh``/``invalidate`` are listed ahead of that kind landing.
_CLIENT_ACTION_KIND_SCORES: dict[str | None, float] = {
    "apply": 0.9,
    "refresh": 0.9,
    "invalidate": 0.9,
    "highlight": 0.1,
    "navigate": 0.1,
}
_CLIENT_ACTION_DEFAULT_SCORE = 0.9  # unknown kind → gate (fail safe)


def _destructive_floor(tool_name: str) -> float:
    """Minimum base_score for a clearly-destructive tool (0.0 if it isn't one)."""
    tool_lower = tool_name.lower()
    if any(kw in tool_lower for kw in _HARD_DESTRUCTIVE_KEYWORDS):
        return _DESTRUCTIVE_FLOOR_SCORE
    return 0.0


def _deterministic_fallback(tool_name: str) -> float:
    """Return a conservative risk score based on tool name heuristics.

    Used when LLM scoring fails. Always errs on the side of caution (ask user).
    """
    # Known-safe prefixes (read-only operations)
    safe_prefixes: tuple[str, ...] = ("get_", "list_", "search_", "read_", "fetch_", "describe_")
    if tool_name.startswith(safe_prefixes):
        return 0.3

    # Known-dangerous keywords
    dangerous_keywords: tuple[str, ...] = ("delete", "remove", "drop", "destroy", "kill", "exec", "execute", "run")
    tool_lower = tool_name.lower()
    if any(kw in tool_lower for kw in dangerous_keywords):
        return 0.95

    # Write-ish keywords
    write_keywords: tuple[str, ...] = ("create", "update", "put", "post", "write", "send", "modify", "set")
    if any(kw in tool_lower for kw in write_keywords):
        return 0.6

    # Unknown — conservative default
    return 0.7
