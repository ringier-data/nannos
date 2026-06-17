"""End-to-end: expose != render through a real ``eval`` REPL in core-only mode.

Confirms the central redesign property: when the catalog is large, MCP tools are NOT
rendered into the prompt but remain *callable* via ``tools.<name>``, and the pinned
``tools.search`` / ``tools.describe`` helpers work inside ``eval``.
"""

from __future__ import annotations

import json
import re
from collections import deque
from typing import Any, Optional

from langchain.agents.factory import create_agent
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

import agent_common.core.graph_utils as gu


class _CommitArgs(BaseModel):
    owner: str = Field(description="Repo owner")
    repo: str


class _ScriptedModel(BaseChatModel):
    responses: deque = deque()
    seen_system: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: list, **kwargs: Any) -> "_ScriptedModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen_system.append(messages[0].content if messages else "")
        return ChatResult(generations=[ChatGeneration(message=self.responses.popleft())])


def _mcp_tool(name: str) -> StructuredTool:
    async def _fn(owner: str, repo: str) -> str:
        return f"{name}:{owner}/{repo}"

    return StructuredTool.from_function(
        coroutine=_fn, name=name, description=f"{name} description.", args_schema=_CommitArgs,
        metadata={"server_name": "github"},
    )


def _last_eval_message(result: dict) -> str:
    for msg in reversed(result["messages"]):
        if getattr(msg, "name", None) == "eval":
            return str(msg.content)
    return ""


def _eval_json(result: dict) -> dict:
    """Extract the JSON payload from the bridge's ``<result>...</result>`` wrapper."""
    content = _last_eval_message(result)
    match = re.search(r"<result>(.*)</result>", content, re.DOTALL)
    return json.loads(match.group(1) if match else content)


async def test_unrendered_mcp_tool_callable_and_discovery_works(monkeypatch):
    monkeypatch.setenv("CODE_INTERPRETER_PTC", "1")
    monkeypatch.setattr(gu, "PTC_INLINE_RENDER_THRESHOLD", 2)

    catalog = [
        _mcp_tool("github_list_commits"),
        _mcp_tool("github_list_issues"),
        _mcp_tool("github_get_repo"),
        _mcp_tool("github_list_branches"),
        _mcp_tool("github_create_issue"),
    ]
    mw = gu._PTCToleranceCodeInterpreterMiddleware(
        static_ptc_tools=[],
        broaden_baseline_tools=[],
        ptc_enabled=True,
        broaden_exposure=True,
        backend_supports_execution=False,
        mode="call",
    )

    code = (
        "const direct = await tools.githubListCommits({owner: 'o', repo: 'r'});"
        "const hits = await tools.search({query: 'list issues'});"
        "const sig = await tools.describe({name: 'githubListIssues'});"
        "JSON.stringify({direct, hitNames: hits.map(h => h.name), sig})"
    )
    model = _ScriptedModel()
    model.seen_system = []
    model.responses = deque(
        [
            AIMessage(content="", id="ai-1", tool_calls=[{"id": "c1", "name": "eval", "args": {"code": code}}]),
            AIMessage(content="done", id="ai-2"),
        ]
    )
    agent = create_agent(model=model, tools=catalog, middleware=[mw])
    result = await agent.ainvoke({"messages": [HumanMessage("go")]})

    payload = _eval_json(result)
    # Expose != render: an unrendered MCP tool is still callable via tools.<name>.
    assert payload["direct"] == "github_list_commits:o/r"
    # search finds tools by intent and returns callable camelCase names.
    assert "githubListIssues" in payload["hitNames"]
    # describe returns the resolved signature for a tool not listed in the prompt.
    assert "async function githubListIssues" in payload["sig"]
    assert "owner: string" in payload["sig"]

    # The system prompt must NOT contain the MCP catalog signatures, but MUST advertise
    # discovery + the search/describe helpers. System content may be a list of blocks.
    raw_system = model.seen_system[0]
    if isinstance(raw_system, list):
        system_prompt = "\n".join(b.get("text", "") for b in raw_system if isinstance(b, dict))
    else:
        system_prompt = str(raw_system)
    assert "async function githubListCommits" not in system_prompt
    assert "async function search" in system_prompt
    assert "async function describe" in system_prompt
    assert "tools.search" in system_prompt
