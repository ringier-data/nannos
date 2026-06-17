"""tools.search / tools.describe: runtime discovery over the exposed catalog."""

from __future__ import annotations

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from agent_common.core.ptc_discovery import (
    PTC_DESCRIBE_TOOL_NAME,
    PTC_SEARCH_TOOL_NAME,
    build_discovery_tools,
)


class _Args(BaseModel):
    owner: str = Field(description="Repo owner")
    repo: str


def _fn(**kwargs):  # noqa: ANN003, ANN202
    return None


def _tool(name: str, description: str):
    return StructuredTool.from_function(func=_fn, name=name, description=description, args_schema=_Args)


@pytest.fixture
def catalog():
    return [
        _tool("github_list_commits", "List commits of a branch in a GitHub repository."),
        _tool("github_list_issues", "List issues in a GitHub repository."),
        _tool("slack_post_message", "Post a message to a Slack channel."),
    ]


def _tools(catalog):
    search, describe = build_discovery_tools(catalog)
    assert search.name == PTC_SEARCH_TOOL_NAME
    assert describe.name == PTC_DESCRIBE_TOOL_NAME
    return search, describe


async def test_search_ranks_by_intent(catalog):
    search, _ = _tools(catalog)
    hits = await search.arun({"query": "list commits in a repo"})
    assert hits[0]["name"] == "githubListCommits"
    assert all(set(h) == {"name", "description"} for h in hits)


async def test_search_returns_camelcase_callable_names(catalog):
    search, _ = _tools(catalog)
    hits = await search.arun({"query": "slack message"})
    assert hits[0]["name"] == "slackPostMessage"


async def test_search_no_match_returns_empty(catalog):
    search, _ = _tools(catalog)
    assert await search.arun({"query": "quantum teleportation"}) == []


async def test_describe_accepts_camelcase_and_resolves_signature(catalog):
    _, describe = _tools(catalog)
    sig = await describe.arun({"name": "githubListCommits"})
    assert "async function githubListCommits" in sig
    assert "owner: string" in sig


async def test_describe_accepts_snake_case_too(catalog):
    _, describe = _tools(catalog)
    sig = await describe.arun({"name": "github_list_commits"})
    assert "async function githubListCommits" in sig


async def test_describe_unknown_tool_hints_search(catalog):
    _, describe = _tools(catalog)
    msg = await describe.arun({"name": "doesNotExist"})
    assert "No tool named" in msg
    assert PTC_SEARCH_TOOL_NAME in msg


def test_discovery_tools_excluded_from_their_own_catalog(catalog):
    """search/describe must not surface themselves as searchable entries."""
    search, describe = build_discovery_tools(catalog)
    # Rebuild including the discovery tools in the catalog; they should still be skipped.
    search2, _ = build_discovery_tools([*catalog, search, describe])
    import asyncio

    hits = asyncio.run(search2.arun({"query": "search describe"}))
    names = {h["name"] for h in hits}
    assert PTC_SEARCH_TOOL_NAME not in names
    assert PTC_DESCRIBE_TOOL_NAME not in names
