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
    app.state.delivery_channel_repository = MagicMock(list_all_channels=AsyncMock(return_value=([], 0)))
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


class TestTheDraftIsBilledToTheCaller:
    """Console-backend's own work, billed to the requester and labelled as the console's.

    It carries no scheduled_job_id — the job it drafts does not exist yet — so without a
    declared service the usage views could only classify it as agent spend. The scope, not
    the call, states both: a second gateway call added to this handler is attributed
    without anyone remembering, and forgetting would not misclassify the spend but lose
    it, since the proxy discards a record with no subject.
    """

    def test_the_handler_runs_in_the_callers_attribution(self, draft_client, gateway, catalogue):
        from ringier_a2a_sdk.cost_tracking.attribution import current_attribution

        seen: dict = {}

        async def _snapshot(*args, **kwargs):
            seen.update(current_attribution())
            return {"job_type": "watch", "check_tool": "console_list_bug_reports"}

        gateway.side_effect = _snapshot

        assert draft_client.post(URL, json={"query": QUERY}).status_code == 200
        assert seen == {"user_sub": "sub-1", "service": "console"}

    def test_the_scope_does_not_outlive_the_request(self, draft_client, gateway, catalogue):
        from ringier_a2a_sdk.cost_tracking.attribution import current_attribution

        gateway.return_value = {"job_type": "watch"}

        draft_client.post(URL, json={"query": QUERY})

        assert current_attribution() == {}


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

        # `current` is the job being edited and `result` a sample response; neither
        # widens what the model may reference — the offer is still read server-side.
        assert set(GenerateJobDraftRequest.model_fields) == {"query", "current", "result"}

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


class TestReasoningBudget:
    """Why the bug-report request still failed after the catalogue was trimmed.

    The low tier resolved to a reasoning model; reasoning tokens count against
    `max_tokens=1024`, so it spent 979 of them thinking and was stopped 41 tokens into
    the JSON. The salvage found no object and the person was told to rephrase.
    """

    def test_thinking_is_off_for_draft_generation(self, draft_client, raw_gateway, catalogue):
        raw_gateway.return_value = '{"job_type": "watch", "check_tool": "console_list_bug_reports"}'

        resp = draft_client.post(URL, json={"query": QUERY})

        assert resp.status_code == 200
        assert raw_gateway.await_args.kwargs["reasoning_effort"] == "none"

    def test_a_reply_cut_off_by_the_budget_is_reported_as_such(self, draft_client, raw_gateway, catalogue, caplog):
        from console_backend.services.llm_gateway import GatewayText

        raw_gateway.return_value = GatewayText('{\n  "job_type": "watch",\n  "name": "New Bug Report Watcher",\n  "sch', "length")

        with caplog.at_level("WARNING"):
            resp = draft_client.post(URL, json={"query": QUERY})

        assert resp.status_code == 422
        assert "cut off" in resp.json()["detail"]
        assert "rephrase" not in resp.json()["detail"]
        message = next(r.getMessage() for r in caplog.records if "finish_reason=length" in r.getMessage())
        assert "New Bug Report Watcher" in message


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


#: The job as the detail page sends it when asking for a change to it.
CURRENT = {
    "job_type": "watch",
    "check_tool": "console_list_bug_reports",
    "check_args": {"status": "open"},
    "cel_expr": "result.reports.filter(r, r.severity == 'high')",
    "prompt": "One line per report, linking to it.",
    "destroy_after_trigger": False,
}
CHANGE = "also include medium severity"


def _edit(client, reply_to: AsyncMock, reply: dict, **body):
    reply_to.return_value = reply
    return client.post(URL, json={"query": CHANGE, "current": CURRENT, **body})


class TestEditingAnExistingJob:
    """With `current`, the query is a change to that job, and the answer is the job changed.

    It used to be read as a description of a new job, so "also include medium severity"
    produced a job about nothing in particular — and on the detail page it overwrote the
    fields of the one being edited.
    """

    def test_the_prompt_shows_the_job_and_asks_for_changes_only(self, draft_client, gateway, catalogue):
        _edit(draft_client, gateway, {"cel_expr": "result.reports"})

        prompt = _prompt(gateway)
        assert "EDITING an existing watch job" in prompt
        assert "r.severity == 'high'" in prompt
        assert f"Requested change: {CHANGE}" in prompt
        assert "User request:" not in prompt

    def test_what_the_change_does_not_touch_comes_back_as_sent(self, draft_client, gateway, catalogue):
        new_expr = "result.reports.filter(r, r.severity in ['high', 'medium'])"

        resp = _edit(draft_client, gateway, {"cel_expr": new_expr})

        assert resp.status_code == 200
        assert _filled(resp) == {**CURRENT, "cel_expr": new_expr}

    def test_an_explicit_null_removes_a_field(self, draft_client, gateway, catalogue):
        resp = _edit(draft_client, gateway, {"prompt": None, "notification_message": "New high-severity bug"})

        assert resp.status_code == 200
        body = _filled(resp)
        assert "prompt" not in body
        assert body["notification_message"] == "New high-severity bug"

    def test_the_current_tool_is_offered_even_when_the_change_does_not_name_it(
        self, draft_client, gateway, catalogue
    ):
        # "also include medium severity" ranks no tool; without the pin the job's own tool
        # was missing from the offer, and a reply naming it was discarded as an invention
        # — taking the job's arguments and condition with it.
        catalogue.return_value = MCPToolsResponse(
            tools=[_tool(f"crm_get_account_{i}", "Read one CRM account") for i in range(30)]
            + [_tool("console_list_bug_reports")]
        )

        resp = _edit(draft_client, gateway, {"check_tool": "console_list_bug_reports", "cel_expr": "result.reports"})

        assert resp.status_code == 200
        assert '"name":"console_list_bug_reports"' in _prompt(gateway)
        assert _filled(resp)["cel_expr"] == "result.reports"

    def test_choosing_an_agent_drops_the_fixed_message(self, draft_client, gateway, catalogue):
        draft_client.app.state.scheduler_service.schedulable_sub_agents = AsyncMock(
            return_value=[SimpleNamespace(id=7, name="triage", config_version=None)]
        )
        current = {**CURRENT, "notification_message": "A bug was filed"}

        gateway.return_value = {"sub_agent_id": 7, "prompt": "Triage it"}
        resp = draft_client.post(URL, json={"query": "have triage handle it", "current": current})

        body = _filled(resp)
        assert body["sub_agent_id"] == 7
        assert "notification_message" not in body

    def test_an_unreachable_agent_is_neither_applied_nor_a_removal(self, draft_client, gateway, catalogue):
        current = {**CURRENT, "sub_agent_id": 3}

        gateway.return_value = {"sub_agent_id": 99, "cel_expr": "result.reports"}
        resp = draft_client.post(URL, json={"query": CHANGE, "current": current})

        assert _filled(resp)["sub_agent_id"] == 3

    def test_fields_outside_the_definition_are_not_changed(self, draft_client, gateway, catalogue):
        # Schedule and delivery have other owners on a shared job, and the type of an
        # existing job is fixed; a change there would show and then not apply.
        resp = _edit(
            draft_client,
            gateway,
            {"job_type": "task", "cron_expr": "0 9 * * *", "delivery_channel_id": 1, "cel_expr": "result.reports"},
        )

        body = _filled(resp)
        assert body["job_type"] == "watch"
        assert "cron_expr" not in body
        assert "delivery_channel_id" not in body

    def test_a_reply_that_changes_nothing_is_a_422(self, draft_client, gateway, catalogue):
        resp = _edit(draft_client, gateway, {"cel_expr": CURRENT["cel_expr"]})

        assert resp.status_code == 422
        assert "no change" in resp.json()["detail"]

    def test_an_unrepairable_expression_is_refused_not_replaced_by_the_change_as_a_judgement(
        self, draft_client, gateway, catalogue
    ):
        # On a new job the fallback judges the request's own words; "also include medium
        # severity" is no condition, so an edit is refused instead.
        gateway.return_value = {"cel_expr": "result.reports.filter(r,"}

        resp = draft_client.post(URL, json={"query": CHANGE, "current": CURRENT})

        assert resp.status_code == 422
        assert "refine the expression" in resp.json()["detail"]

    def test_a_sample_response_is_shown_and_verifies_the_expression(self, draft_client, gateway, catalogue):
        # With a real response the expression is evaluated, not only compiled: a path
        # the response does not have is sent back for repair.
        sample = {"reports": [{"severity": "high"}]}
        gateway.side_effect = [
            {"cel_expr": "result.bugs.filter(b, b.severity == 'medium')"},
            {"cel_expr": "result.reports.filter(r, r.severity == 'medium')"},
        ]

        resp = draft_client.post(URL, json={"query": CHANGE, "current": CURRENT, "result": sample})

        assert resp.status_code == 200
        assert '{"reports":[{"severity":"high"}]}' in _prompt(gateway)
        assert gateway.await_count == 2
        assert _filled(resp)["cel_expr"] == "result.reports.filter(r, r.severity == 'medium')"


    # ── Review round 1 (PR #289) ────────────────────────────────────────────────────

    def test_a_prev_reading_expression_verifies_against_the_sample(self, draft_client, gateway, catalogue):
        # The run binds prev to the stored result; verification left it unbound, so the
        # prompt's own `result != prev` failed and a correct edit was "repaired" or refused.
        gateway.return_value = {"cel_expr": "result != prev"}

        resp = draft_client.post(URL, json={"query": CHANGE, "current": CURRENT, "result": {"reports": []}})

        assert resp.status_code == 200
        assert gateway.await_count == 1
        assert _filled(resp)["cel_expr"] == "result != prev"

    def test_moving_to_another_tool_does_not_verify_against_the_old_tools_response(
        self, draft_client, gateway, catalogue
    ):
        catalogue.return_value = MCPToolsResponse(tools=CATALOGUE + [_tool("crm_list_deals", "List CRM deals")])
        gateway.return_value = {"check_tool": "crm_list_deals", "cel_expr": "result.deals"}

        resp = draft_client.post(
            URL, json={"query": "watch crm deals instead", "current": CURRENT, "result": {"reports": []}}
        )

        assert resp.status_code == 200
        # `result.deals` is not in the old tool's response; evaluating it there failed
        # every repair round.
        assert gateway.await_count == 1
        body = _filled(resp)
        assert body["check_tool"] == "crm_list_deals"
        assert body["cel_expr"] == "result.deals"
        assert "check_args" not in body  # written for the old tool

    def test_a_judgement_offered_on_retry_does_not_replace_a_broken_expression(self, draft_client, gateway, catalogue):
        gateway.side_effect = [
            {"cel_expr": "result.reports.filter(r,"},
            {"llm_condition": "a report looks severe"},
        ]

        resp = draft_client.post(URL, json={"query": CHANGE, "current": CURRENT})

        assert resp.status_code == 422
        assert "refine the expression" in resp.json()["detail"]

    def test_removing_the_only_condition_is_refused(self, draft_client, gateway, catalogue):
        resp = _edit(draft_client, gateway, {"cel_expr": None})

        assert resp.status_code == 422
        assert "no condition" in resp.json()["detail"]

    def test_restated_expressions_that_fail_to_compile_do_not_remove_the_jobs(
        self, draft_client, gateway, catalogue
    ):
        exprs = {"since": "strftime(now - duration('168h'), '%Y-%m-%d')"}
        current = {**CURRENT, "check_args_exprs": exprs}

        gateway.return_value = {"check_args_exprs": {"since": "strftime(now -"}, "cel_expr": "result.reports"}
        resp = draft_client.post(URL, json={"query": CHANGE, "current": current})

        assert resp.status_code == 200
        assert _filled(resp)["check_args_exprs"] == exprs

    def test_an_echoed_agent_does_not_keep_the_agent_outcome(self, draft_client, gateway, catalogue):
        # Switching an agent job to a fixed message while the reply restates the agent it
        # already had: the echo is no change, so the message wins.
        draft_client.app.state.scheduler_service.schedulable_sub_agents = AsyncMock(
            return_value=[SimpleNamespace(id=3, name="triage", config_version=None)]
        )
        current = {**CURRENT, "sub_agent_id": 3, "prompt": "Triage it"}

        gateway.return_value = {"notification_message": "A severe bug was filed", "sub_agent_id": 3}
        resp = draft_client.post(URL, json={"query": "just notify me instead", "current": current})

        body = _filled(resp)
        assert body["notification_message"] == "A severe bug was filed"
        assert "sub_agent_id" not in body


class TestEditingATaskJob:
    """generate-job-draft drafts task jobs as well as watches; an edit follows the job's type."""

    TASK = {"job_type": "task", "sub_agent_id": 3, "prompt": "Summarise yesterday's bug reports"}

    @pytest.fixture(autouse=True)
    def _agents(self, draft_client):
        draft_client.app.state.scheduler_service.schedulable_sub_agents = AsyncMock(
            return_value=[
                SimpleNamespace(id=3, name="digest", config_version=None),
                SimpleNamespace(id=4, name="triage", config_version=None),
            ]
        )

    def test_the_prompt_names_a_task_job_and_its_own_fields(self, draft_client, gateway, catalogue):
        gateway.return_value = {"prompt": "Summarise yesterday's and today's bug reports"}

        draft_client.post(URL, json={"query": "include today too", "current": self.TASK})

        prompt = _prompt(gateway)
        assert "EDITING an existing task job" in prompt
        assert "Only these fields can change: prompt, sub_agent_id." in prompt
        assert "When you change cel_expr" not in prompt

    def test_the_instruction_and_agent_can_change(self, draft_client, gateway, catalogue):
        gateway.return_value = {"sub_agent_id": 4, "prompt": "Triage yesterday's bug reports"}

        resp = draft_client.post(URL, json={"query": "have triage do it", "current": self.TASK})

        assert resp.status_code == 200
        assert _filled(resp) == {"job_type": "task", "sub_agent_id": 4, "prompt": "Triage yesterday's bug reports"}

    def test_a_broken_expression_volunteered_on_a_task_does_not_refuse_the_edit(
        self, draft_client, gateway, catalogue
    ):
        # cel_expr is no field of a task; a broken one used to go through repair and
        # refuse the edit with "refine the expression", on a job with none to refine.
        gateway.return_value = {"prompt": "Summarise all bug reports", "cel_expr": "result.reports.filter(r,"}

        resp = draft_client.post(URL, json={"query": "all of them", "current": self.TASK})

        assert resp.status_code == 200
        assert gateway.await_count == 1
        assert _filled(resp)["prompt"] == "Summarise all bug reports"

    def test_watch_fields_are_not_added_to_a_task(self, draft_client, gateway, catalogue):
        gateway.return_value = {"prompt": "Summarise all bug reports", "cel_expr": "result.reports"}

        resp = draft_client.post(URL, json={"query": "all of them", "current": self.TASK})

        assert "cel_expr" not in _filled(resp)


def test_the_editable_watch_fields_are_what_the_frontend_writes_back():
    # console-frontend's lib/watchDraft.ts wires each of these fields back into the form;
    # a field added here without it would be merged server-side and silently dropped
    # client-side. Change both together.
    from console_backend.models.scheduled_job import JobType

    assert scheduler_router._EDITABLE_DRAFT_FIELDS[JobType.WATCH] == {
        "check_tool",
        "check_args",
        "check_args_exprs",
        "cel_expr",
        "llm_condition",
        "notification_message",
        "prompt",
        "sub_agent_id",
        "destroy_after_trigger",
    }


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
