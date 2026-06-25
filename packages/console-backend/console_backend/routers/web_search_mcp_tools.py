"""Web-search MCP tool — grounded web search for the orchestrator and sub-agents.

Exposes ``console_web_search`` as an MCP tool (tagged "MCP" so FastApiMCP auto-discovers it),
listed in the Configure MCP Tools picker and selectable by any sub-agent. The ``console_`` prefix
is required: sub-agent MCP discovery routes ``console_*`` tools to console-backend's MCP server
(everything else goes to the Gatana gateway), so the prefix is what makes the tool reachable when
a sub-agent whitelists it.

Why here and not on the agent's own model: LiteLLM web search only happens in an isolated,
function-tool-free completion — Bedrock-Converse Claude can't search at all, and Vertex Gemini
silently drops server-side search whenever function tools are present (which a tool-using agent
always sends). This endpoint makes that dedicated tool-free call against a web-search-capable
model and returns the grounded answer + sources.
"""

import logging

from fastapi import APIRouter, Depends, Query, Request

from ..db.session import DbSession
from ..dependencies import require_auth_or_bearer_token
from ..models.user import User
from ..services.llm_gateway import gateway_web_search
from ..services.web_search import format_web_search_result, resolve_web_search_model

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/web-search")


@router.post(
    "/mcp-search",
    response_model=str,
    tags=["MCP"],
    operation_id="console_web_search",
    summary="Search the web for current, recent, or verifiable information.",
    description=(
        "Search the web and get back a grounded summary with source links. Use it whenever the "
        "answer depends on information beyond your training data — recent events, prices, "
        "releases/versions, current facts — instead of guessing, and cite the sources it returns. "
        "Pass a self-contained query (it has no conversation context)."
    ),
)
async def web_search_mcp(
    request: Request,
    query: str = Query(..., description="A self-contained natural-language search query."),
    db: DbSession = None,
    user: User = Depends(require_auth_or_bearer_token),
) -> str:
    """Run one grounded web search and return answer + sources as text.

    Returns a readable message in every case (including unavailable/failure) so the calling
    agent never sees a tool error it can't act on."""
    model = await resolve_web_search_model(request, db)
    if not model:
        return (
            "Web search is unavailable: no web-search-capable model is registered on the Model "
            "Gateway. An admin can register one (e.g. a Gemini model) in the console."
        )
    try:
        answer, citations = await gateway_web_search(query, model=model, metadata={"user_sub": user.sub})
    except Exception as e:  # network/timeout/non-2xx — surface, don't 500 the tool call
        logger.warning("console_web_search failed (model=%s): %s", model, e)
        return f"Web search failed: {e}"
    logger.info("console_web_search via %s: %d citation(s)", model, len(citations))
    return format_web_search_result(answer, citations)
