"""Endpoint tests for POST /api/v1/scheduler/generate-job-draft.

Two defects motivated these, stacked on top of each other: the request body carried the
whole tool catalogue (hundreds of tools, every schema) into a prompt answered by a small
model, and when that model then produced nothing parseable the endpoint returned a
`200` with every field null — indistinguishable, to the UI and in the logs, from "the
request implied nothing".
"""

import json
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
URL = "/api/v1/scheduler/generate-job-draft"


def _tool(name: str, description: str = "", server: str = "s") -> MCPTool:
    return MCPTool(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {"created_after": {"type": "string"}}},
        server=server,
    )


#: A catalogue shaped like the real one: one relevant tool among many unrelated ones,
#: more than the candidate cap in total.
CATALOGUE = [_tool("console_list_bug_reports", "List bug reports filed in the console, newest first")] + [
    _tool(f"crm_get_account_{i}", "Read one CRM account") for i in range(30)
]


@pytest.fixture
def draft_client():
    """A sync TestClient over this router alone — named apart from conftest's async `client`."""
    app = FastAPI()
    app.include_router(scheduler_router.router)
    app.dependency_overrides[scheduler_router.require_auth] = lambda: SimpleNamespace(id="user-1", sub="sub-1")
    app.dependency_overrides[get_db_session] = lambda: MagicMock()

    scheduler_service = MagicMock()
    scheduler_service.schedulable_sub_agents = AsyncMock(return_value=[])
    scheduler_service._resolve_timezone = AsyncMock(return_value="Europe/Zurich")
    app.state.scheduler_service = scheduler_service
    app.state.delivery_channel_repository = MagicMock(list_all_channels=AsyncMock(return_value=[]))
    return TestClient(app)


@pytest.fixture
def model_default():
    with patch.object(
        scheduler_router.ModelDefaultsRepository, "get_all", AsyncMock(return_value={"chat:low": "m"})
    ):
        yield


@pytest.fixture
def gateway(model_default):
    """The model's reply, as `gateway_chat_json` would parse it; records every prompt it saw."""
    mock = AsyncMock(return_value={})
    with patch.object(scheduler_router, "gateway_chat_json", mock):
        yield mock


@pytest.fixture
def raw_gateway(model_default):
    """The model's reply as raw text — one level deeper, so the JSON salvage runs for real."""
    mock = AsyncMock(return_value="")
    with patch("console_backend.services.llm_gateway.gateway_chat", mock):
        yield mock


@pytest.fixture
def catalogue():
    mock = AsyncMock(return_value=MCPToolsResponse(tools=CATALOGUE))
    with patch.object(scheduler_router, "_list_mcp_tools", mock):
        yield mock


def _prompt(gateway: AsyncMock) -> str:
    return gateway.await_args_list[0].args[0]


def _filled(resp) -> dict:
    return {k: v for k, v in resp.json().items() if v is not None}


class TestToolsAreSelectedServerSide:
    def test_the_prompt_carries_a_ranked_handful_not_the_registry(self, draft_client, gateway, catalogue):
        gateway.return_value = {"job_type": "watch", "check_tool": "console_list_bug_reports"}

        resp = draft_client.post(URL, json={"query": QUERY})

        assert resp.status_code == 200
        prompt = _prompt(gateway)
        assert "console_list_bug_reports" in prompt
        # The cap, not the catalogue size, bounds what the model sees.
        offered = [t.name for t in CATALOGUE if f'"name":"{t.name}"' in prompt]
        assert offered, "tools are serialised compactly — the probe must match that shape"
        assert len(offered) <= scheduler_router._DRAFT_TOOL_CANDIDATES
        assert len(offered) < len(CATALOGUE)
        # The catalogue was read for this user, not taken from the body.
        catalogue.assert_awaited_once()
        assert catalogue.await_args.args[1].id == "user-1"

    def test_the_request_body_carries_no_tools(self):
        # The body used to carry the whole catalogue, and with it the chance to offer
        # the model tools this user cannot reach. The field is gone, not just ignored.
        from console_backend.models.scheduled_job import GenerateJobDraftRequest

        assert set(GenerateJobDraftRequest.model_fields) == {"query"}

    def test_the_relevant_tool_is_offered_and_chosen(self, draft_client, gateway, catalogue):
        gateway.return_value = {
            "job_type": "watch",
            "name": "New bug reports",
            "check_tool": "console_list_bug_reports",
            "destroy_after_trigger": False,
        }

        resp = draft_client.post(URL, json={"query": QUERY})

        assert resp.status_code == 200
        assert resp.json()["check_tool"] == "console_list_bug_reports"
        assert resp.json()["destroy_after_trigger"] is False

    def test_a_tool_the_model_invented_goes_with_its_arguments_and_condition(self, draft_client, gateway, catalogue):
        # The model only saw the candidates, so a name outside them is a hallucination.
        # Its arguments and condition were written against that tool: left in the draft
        # they would be applied under whatever tool the user then picks by hand.
        gateway.return_value = {
            "job_type": "watch",
            "name": "Bugs",
            "check_tool": "github_list_issues",
            "check_args": {"repo": "x"},
            "check_args_exprs": {"since": "string(now)"},
            "cel_expr": "size(result.issues) > 0",
            "llm_condition": "looks like a bug",
        }

        resp = draft_client.post(URL, json={"query": QUERY})

        assert resp.status_code == 200
        assert _filled(resp) == {"job_type": "watch", "name": "Bugs"}

    def test_a_non_string_tool_name_is_dropped_not_a_500(self, draft_client, gateway, catalogue):
        # "The single best-matching tool" comes back as a list when several match.
        gateway.return_value = {"job_type": "watch", "name": "Bugs", "check_tool": ["a", "b"]}

        resp = draft_client.post(URL, json={"query": QUERY})

        assert resp.status_code == 200
        assert resp.json()["check_tool"] is None

    def test_an_unreadable_catalogue_is_an_error_not_a_toolless_prompt(self, draft_client, gateway):
        with patch.object(scheduler_router, "_list_mcp_tools", AsyncMock(side_effect=RuntimeError("gateway down"))):
            resp = draft_client.post(URL, json={"query": QUERY})

        assert resp.status_code == 503
        gateway.assert_not_awaited()

    def test_an_empty_offer_is_logged_and_still_generates(self, draft_client, gateway, caplog):
        # Under impersonation the catalogue is empty by design, and a request can share
        # no vocabulary with any tool. A task job needs no tool, so the call still
        # happens — but the operator can see why the draft came back without one.
        gateway.return_value = {"job_type": "task", "name": "Digest"}
        with patch.object(scheduler_router, "_list_mcp_tools", AsyncMock(return_value=MCPToolsResponse(tools=[]))):
            with caplog.at_level("INFO"):
                resp = draft_client.post(URL, json={"query": "send me a digest"})

        assert resp.status_code == 200
        assert any("offers no tools" in r.getMessage() for r in caplog.records)


class TestAnEmptyGenerationFailsLoudly:
    def test_no_json_in_the_reply_is_a_422(self, draft_client, raw_gateway, catalogue, caplog):
        # A reply with no JSON object used to flow through as a 200 with every field
        # null. Patched at the raw-text level so the salvage in gateway_chat_json runs.
        raw_gateway.return_value = "I am not able to help with scheduling."

        with caplog.at_level("WARNING"):
            resp = draft_client.post(URL, json={"query": QUERY})

        assert resp.status_code == 422
        assert "no usable draft" in resp.json()["detail"]
        messages = [r.getMessage() for r in caplog.records]
        # Diagnosable next time: the reply's shape from the gateway, the query from here.
        assert any("No JSON object" in m for m in messages)
        assert any(QUERY in m for m in messages)

    def test_two_objects_in_prose_are_a_422_not_a_gateway_outage(self, draft_client, raw_gateway, catalogue, caplog):
        # The greedy salvage spans from the first `{` to the last `}`; that is not JSON,
        # and used to surface as "AI generation service unavailable".
        raw_gateway.return_value = 'Either {"job_type": "watch"} or perhaps {"job_type": "task"}.'

        with caplog.at_level("WARNING"):
            resp = draft_client.post(URL, json={"query": QUERY})

        assert resp.status_code == 422
        assert any("Unparseable JSON" in r.getMessage() for r in caplog.records)

    def test_a_reply_with_no_known_field_is_a_422(self, draft_client, gateway, catalogue):
        gateway.return_value = {"answer": "I cannot help with that"}

        resp = draft_client.post(URL, json={"query": QUERY})

        assert resp.status_code == 422

    def test_a_partial_draft_is_still_a_success(self, draft_client, gateway, catalogue):
        # Inferring only some fields is the endpoint working as designed — the form
        # fills the rest — and must not be confused with inferring nothing.
        gateway.return_value = {"job_type": "task", "name": "Weekly digest"}

        resp = draft_client.post(URL, json={"query": "send me a weekly digest"})

        assert resp.status_code == 200
        assert _filled(resp) == {"job_type": "task", "name": "Weekly digest"}

    def test_a_valid_reply_survives_the_raw_path(self, draft_client, raw_gateway, catalogue):
        raw_gateway.return_value = "```json\n" + json.dumps({"job_type": "watch", "name": "Bugs"}) + "\n```"

        resp = draft_client.post(URL, json={"query": QUERY})

        assert resp.status_code == 200
        assert _filled(resp) == {"job_type": "watch", "name": "Bugs"}


class TestRankMcpTools:
    """The scorer shared with /mcp/tools/search, used here as the candidate picker."""

    def test_the_best_match_comes_first_and_the_cap_holds(self):
        ranked = rank_mcp_tools(CATALOGUE, QUERY, 15)
        assert ranked[0].name == "console_list_bug_reports"
        assert len(ranked) <= 15

    def test_tools_that_match_nothing_are_left_out(self):
        assert rank_mcp_tools([_tool("unrelated_tool", "nothing in common")], "bug report", 15) == []

    def test_it_does_not_mutate_or_reorder_the_input(self):
        tools = [_tool("b_bug", "bug"), _tool("a_bug_report", "bug report")]
        before = [t.name for t in tools]
        rank_mcp_tools(tools, "bug report", 15)
        assert [t.name for t in tools] == before
