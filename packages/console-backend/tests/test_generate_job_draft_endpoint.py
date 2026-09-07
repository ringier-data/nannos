"""Endpoint tests for POST /api/v1/scheduler/generate-job-draft.

Two defects motivated these, stacked on top of each other: the request body carried the
whole tool catalogue (hundreds of tools, every schema) into a prompt answered by a small
model, and when that model then produced nothing parseable the endpoint returned a
`200` with every field null — indistinguishable, to the UI and in the logs, from "the
request implied nothing".
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("ECS_CONTAINER_METADATA_URI", "true")

from console_backend.db.session import get_db_session
from console_backend.routers import scheduler_router
from console_backend.routers.mcp_router import MCPTool, MCPToolsResponse, rank_mcp_tools

QUERY = "notify me every time a new bug report is filed"


def _tool(name: str, description: str = "", server: str = "s") -> MCPTool:
    return MCPTool(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {"created_after": {"type": "string"}},
        },
        server=server,
    )


#: A catalogue shaped like the real one: one relevant tool among many unrelated ones,
#: more than the candidate cap in total.
CATALOGUE = [
    _tool(
        "console_list_bug_reports",
        "List bug reports filed in the console, newest first",
    )
] + [_tool(f"crm_get_account_{i}", "Read one CRM account") for i in range(30)]


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(scheduler_router.router)
    app.dependency_overrides[scheduler_router.require_auth] = lambda: SimpleNamespace(
        id="user-1", sub="sub-1"
    )
    app.dependency_overrides[get_db_session] = lambda: MagicMock()

    scheduler_service = MagicMock()
    scheduler_service.schedulable_sub_agents = AsyncMock(return_value=[])
    scheduler_service._resolve_timezone = AsyncMock(return_value="Europe/Zurich")
    app.state.scheduler_service = scheduler_service
    app.state.delivery_channel_repository = MagicMock(
        list_all_channels=AsyncMock(return_value=[])
    )
    return TestClient(app)


@pytest.fixture
def gateway():
    """The model's reply, as `gateway_chat_json` would parse it; records every prompt it saw."""
    mock = AsyncMock(return_value={})
    with (
        patch.object(scheduler_router, "gateway_chat_json", mock),
        patch.object(
            scheduler_router.ModelDefaultsRepository,
            "get_all",
            AsyncMock(return_value={"chat:low": "m"}),
        ),
    ):
        yield mock


@pytest.fixture
def catalogue():
    mock = AsyncMock(return_value=MCPToolsResponse(tools=CATALOGUE))
    with patch.object(scheduler_router, "_list_mcp_tools", mock):
        yield mock


def _prompt(gateway: AsyncMock) -> str:
    return gateway.await_args_list[0].args[0]


class TestToolsAreSelectedServerSide:
    def test_the_prompt_carries_a_ranked_handful_not_the_registry(
        self, client, gateway, catalogue
    ):
        gateway.return_value = {
            "job_type": "watch",
            "check_tool": "console_list_bug_reports",
        }

        resp = client.post(
            "/api/v1/scheduler/generate-job-draft", json={"query": QUERY}
        )

        assert resp.status_code == 200
        prompt = _prompt(gateway)
        assert "console_list_bug_reports" in prompt
        # The cap, not the catalogue size, bounds what the model sees.
        offered = [t.name for t in CATALOGUE if f'"name": "{t.name}"' in prompt]
        assert len(offered) <= scheduler_router._DRAFT_TOOL_CANDIDATES
        assert len(offered) < len(CATALOGUE)
        # The catalogue was read for this user, not taken from the body.
        catalogue.assert_awaited_once()
        assert catalogue.await_args.args[1].id == "user-1"

    def test_the_request_body_tools_are_ignored(self, client, gateway, catalogue):
        # An older UI still posts the field; a caller may also try to offer the model
        # tools this user cannot reach. Neither makes it into the prompt.
        gateway.return_value = {
            "job_type": "watch",
            "check_tool": "console_list_bug_reports",
        }
        smuggled = [
            {
                "name": "admin_delete_everything",
                "description": "bug report",
                "input_schema": {},
            }
        ]

        resp = client.post(
            "/api/v1/scheduler/generate-job-draft",
            json={"query": QUERY, "tools": smuggled},
        )

        assert resp.status_code == 200
        assert "admin_delete_everything" not in _prompt(gateway)

    def test_the_relevant_tool_is_offered_and_chosen(self, client, gateway, catalogue):
        gateway.return_value = {
            "job_type": "watch",
            "name": "New bug reports",
            "check_tool": "console_list_bug_reports",
            "destroy_after_trigger": False,
        }

        resp = client.post(
            "/api/v1/scheduler/generate-job-draft", json={"query": QUERY}
        )

        assert resp.status_code == 200
        assert resp.json()["check_tool"] == "console_list_bug_reports"
        assert resp.json()["destroy_after_trigger"] is False

    def test_a_tool_the_model_invented_is_discarded(self, client, gateway, catalogue):
        # The model only saw the candidates, so a name outside them is a hallucination
        # that would make the job fail on its first check.
        gateway.return_value = {
            "job_type": "watch",
            "name": "Bugs",
            "check_tool": "github_list_issues",
        }

        resp = client.post(
            "/api/v1/scheduler/generate-job-draft", json={"query": QUERY}
        )

        assert resp.status_code == 200
        assert resp.json()["check_tool"] is None
        assert resp.json()["name"] == "Bugs"

    def test_an_unreadable_catalogue_is_an_error_not_a_toolless_prompt(
        self, client, gateway
    ):
        with patch.object(
            scheduler_router,
            "_list_mcp_tools",
            AsyncMock(side_effect=RuntimeError("gateway down")),
        ):
            resp = client.post(
                "/api/v1/scheduler/generate-job-draft", json={"query": QUERY}
            )

        assert resp.status_code == 503
        gateway.assert_not_awaited()


class TestAnEmptyGenerationFailsLoudly:
    def test_no_json_in_the_reply_is_a_503(self, client, gateway, catalogue, caplog):
        # `gateway_chat_json` answers {} when the reply holds no object; that used to
        # flow through to a 200 with every field null.
        gateway.return_value = {}

        with caplog.at_level("WARNING"):
            resp = client.post(
                "/api/v1/scheduler/generate-job-draft", json={"query": QUERY}
            )

        assert resp.status_code == 503
        assert "no usable draft" in resp.json()["detail"]
        # Diagnosable next time: the query is in the log.
        assert any(QUERY in record.getMessage() for record in caplog.records)

    def test_a_reply_with_no_known_field_is_a_503(self, client, gateway, catalogue):
        gateway.return_value = {"answer": "I cannot help with that"}

        resp = client.post(
            "/api/v1/scheduler/generate-job-draft", json={"query": QUERY}
        )

        assert resp.status_code == 503

    def test_a_partial_draft_is_still_a_success(self, client, gateway, catalogue):
        # Inferring only some fields is the endpoint working as designed — the form
        # fills the rest — and must not be confused with inferring nothing.
        gateway.return_value = {"job_type": "task", "name": "Weekly digest"}

        resp = client.post(
            "/api/v1/scheduler/generate-job-draft",
            json={"query": "send me a weekly digest"},
        )

        assert resp.status_code == 200
        body = {k: v for k, v in resp.json().items() if v is not None}
        assert body == {"job_type": "task", "name": "Weekly digest"}


class TestRankMcpTools:
    """The scorer shared with /mcp/tools/search, used here as the candidate picker."""

    def test_the_best_match_comes_first_and_the_cap_holds(self):
        ranked = rank_mcp_tools(CATALOGUE, QUERY, 15)
        assert ranked[0].name == "console_list_bug_reports"
        assert len(ranked) <= 15

    def test_tools_that_match_nothing_are_left_out(self):
        assert (
            rank_mcp_tools(
                [_tool("unrelated_tool", "nothing in common")], "bug report", 15
            )
            == []
        )

    def test_it_does_not_mutate_or_reorder_the_input(self):
        tools = [_tool("b_bug", "bug"), _tool("a_bug_report", "bug report")]
        before = [t.name for t in tools]
        rank_mcp_tools(tools, "bug report", 15)
        assert [t.name for t in tools] == before
