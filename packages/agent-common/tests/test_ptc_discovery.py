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
    result = await search.arun({"query": "list commits in a repo"})
    hits = result["matches"]
    assert hits[0]["name"] == "githubListCommits"
    assert all(set(h) == {"name", "description"} for h in hits)
    assert result["total_matches"] == len(hits)
    assert result["truncated"] is False
    assert result["next_offset"] is None


async def test_search_returns_camelcase_callable_names(catalog):
    search, _ = _tools(catalog)
    hits = (await search.arun({"query": "slack message"}))["matches"]
    assert hits[0]["name"] == "slackPostMessage"


async def test_search_no_match_returns_empty_with_hint(catalog):
    search, _ = _tools(catalog)
    result = await search.arun({"query": "quantum teleportation"})
    assert result["matches"] == []
    assert result["total_matches"] == 0
    assert result["truncated"] is False
    assert "quantum" in result["hint"]


async def test_search_reports_truncation_and_pages():
    """A capped page must say so and hand the model an offset to continue with."""
    catalog = [_tool(f"github_get_issue_{i}", f"Get issue variant {i}.") for i in range(25)]
    search, _ = build_discovery_tools(catalog, top_k=10)

    page1 = await search.arun({"query": "issue"})
    assert page1["shown"] == 10
    assert page1["total_matches"] == 25
    assert page1["truncated"] is True
    assert page1["next_offset"] == 10
    assert "offset=10" in page1["hint"]

    page2 = await search.arun({"query": "issue", "offset": page1["next_offset"]})
    assert page2["offset"] == 10
    assert page2["next_offset"] == 20
    assert not {h["name"] for h in page1["matches"]} & {h["name"] for h in page2["matches"]}

    page3 = await search.arun({"query": "issue", "offset": 20})
    assert page3["shown"] == 5
    assert page3["truncated"] is False
    assert page3["next_offset"] is None
    assert "All 25" in page3["hint"]

    wide = await search.arun({"query": "issue", "limit": 50})
    assert wide["shown"] == 25 and wide["truncated"] is False


async def test_search_limit_is_clamped():
    catalog = [_tool(f"tool_{i}", "Do the thing.") for i in range(80)]
    search, _ = build_discovery_tools(catalog)
    result = await search.arun({"query": "thing", "limit": 500})
    assert result["shown"] == 50
    assert result["truncated"] is True


async def test_search_whole_word_beats_substring():
    """'repo' should rank a tool about repos above one about reports."""
    catalog = [
        _tool("gam_get_report", "Get an ad report."),
        _tool("github_get_repo", "Get a repository."),
    ]
    search, _ = build_discovery_tools(catalog)
    hits = (await search.arun({"query": "repo"}))["matches"]
    assert hits[0]["name"] == "githubGetRepo"


async def test_search_matches_words_beyond_first_description_line():
    catalog = [_tool("x_do", "Do something.\n\nSupports pagination via cursor.")]
    search, _ = build_discovery_tools(catalog)
    result = await search.arun({"query": "cursor"})
    assert result["total_matches"] == 1
    assert result["matches"][0]["description"] == "Do something."


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

    hits = asyncio.run(search2.arun({"query": "search describe"}))["matches"]
    names = {h["name"] for h in hits}
    assert PTC_SEARCH_TOOL_NAME not in names
    assert PTC_DESCRIBE_TOOL_NAME not in names
