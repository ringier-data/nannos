import json

"""The client_action tool's two delivery modes.

``apply`` is a ROUND TRIP: the directive rides the interrupt value (never the
custom stream — the resume replays the handler, and a pre-interrupt emit would
fire twice), and the tool's return renders the client's actual result. ``navigate``
is a round trip too (the result carries the page the user landed on); ``highlight``
still rides the custom stream.
"""

from unittest.mock import Mock, patch

import pytest

from agent_common.core.client_action_tool import (
    ClientActionInput,
    _client_action_tool,
    client_action_artifact,
    render_client_action_result,
)

MODULE = "agent_common.core.client_action_tool"


class TestApplyRoundTrip:
    @pytest.mark.asyncio
    async def test_apply_interrupts_with_directive_and_call_id_and_renders_result(self):
        writer = Mock()
        with (
            patch(f"{MODULE}.get_stream_writer", return_value=writer),
            patch(
                f"{MODULE}.interrupt",
                return_value={"ok": True, "applied": ["budget", "name"], "rejected": []},
            ) as fake_interrupt,
        ):
            out, _ = await _client_action_tool(
                kind="apply",
                target_type="Campaign",
                target_id="7",
                values={"budget": 50000},
                tool_call_id="call-1",
            )

        fake_interrupt.assert_called_once_with(
            {
                "client_action_request": {
                    "id": "call-1",
                    "directive": {
                        "kind": "apply",
                        "target": {"type": "Campaign", "id": "7"},
                        "values": {"budget": 50000},
                        "confirm": True,
                    },
                }
            }
        )
        # The directive must NOT also ride the custom stream (double execution).
        writer.assert_not_called()
        assert "budget, name" in out
        assert "Nothing is saved yet" in out

    @pytest.mark.asyncio
    async def test_apply_reports_rejected_fields_to_the_model(self):
        with patch(
            f"{MODULE}.interrupt",
            return_value={
                "ok": True,
                "applied": ["budget"],
                "rejected": [{"field": "campaignType", "reason": "not one of the allowed values"}],
            },
        ):
            out, _ = await _client_action_tool(
                kind="apply", target_type="Campaign", target_id="7", values={"x": 1}, tool_call_id="c"
            )
        assert "REJECTED" in out
        assert "campaignType" in out
        assert "not one of the allowed values" in out

    @pytest.mark.asyncio
    async def test_apply_tells_the_model_what_it_overwrote_so_undo_is_an_apply(self):
        with patch(
            f"{MODULE}.interrupt",
            return_value={
                "ok": True,
                "applied": ["timezone", "name"],
                "rejected": [],
                "previous": {"timezone": "", "name": "Boss Email Alert"},
            },
        ):
            out, _ = await _client_action_tool(
                kind="apply",
                target_type="Settings",
                target_id="me",
                values={"timezone": "Europe/Zurich", "name": "qa"},
                tool_call_id="c",
            )
        assert "timezone was empty" in out
        assert 'name was "Boss Email Alert"' in out
        assert "apply these values back" in out
        assert "does NOT discard unsaved form values" in out

    @pytest.mark.asyncio
    async def test_apply_without_result_is_reported_honestly(self):
        with patch(f"{MODULE}.interrupt", return_value={"ok": False, "reason": "no-result"}):
            out, _ = await _client_action_tool(
                kind="apply", target_type="Campaign", target_id="7", values={"x": 1}, tool_call_id="c"
            )
        assert "Do NOT assume the action happened" in out

    @pytest.mark.asyncio
    async def test_apply_unknown_target_tells_the_agent_to_recheck_the_page(self):
        with patch(f"{MODULE}.interrupt", return_value={"ok": False, "reason": "unknown-target"}):
            out, _ = await _client_action_tool(
                kind="apply", target_type="Campaign", target_id="7", values={"x": 1}, tool_call_id="c"
            )
        assert "no longer on the user's screen" in out


class TestReadCurrentPageRoundTrip:
    @pytest.mark.asyncio
    async def test_read_interrupts_and_returns_the_snapshot(self):
        with patch(
            f"{MODULE}.interrupt",
            return_value={"ok": True, "content": '{"page": {"key": "/campaigns/7"}, "rows": ["a"]}'},
        ) as fake_interrupt:
            out, _ = await _client_action_tool(kind="read_current_page", tool_call_id="c2")
        fake_interrupt.assert_called_once_with(
            {"client_action_request": {"id": "c2", "directive": {"kind": "read_current_page"}}}
        )
        assert out.startswith("Current page state")
        assert '"/campaigns/7"' in out

    @pytest.mark.asyncio
    async def test_unsupported_host_is_reported_as_failure(self):
        with patch(f"{MODULE}.interrupt", return_value={"ok": False, "reason": "unsupported"}):
            out, _ = await _client_action_tool(kind="read_current_page", tool_call_id="c2")
        assert "FAILED" in out


class TestNavigateRoundTrip:
    @pytest.mark.asyncio
    async def test_navigate_waits_for_the_landed_page(self):
        writer = Mock()
        landed = '{"page": {"key": "/app"}, "objects": [{"type": "Settings", "id": "me"}]}'
        with (
            patch(f"{MODULE}.get_stream_writer", return_value=writer),
            patch(f"{MODULE}.interrupt", return_value={"ok": True, "content": landed}) as fake_interrupt,
        ):
            out, _ = await _client_action_tool(kind="navigate", to="/app", tool_call_id="nav-1")
        fake_interrupt.assert_called_once_with(
            {"client_action_request": {"id": "nav-1", "directive": {"kind": "navigate", "to": "/app"}}}
        )
        writer.assert_not_called()
        assert "do not navigate there again" in out
        assert '"type": "Settings"' in out

    @pytest.mark.asyncio
    async def test_discard_changes_rides_the_directive_only_when_set(self):
        with patch(f"{MODULE}.interrupt", return_value={"ok": True}) as fake_interrupt:
            await _client_action_tool(kind="navigate", to="/b", discard_changes=True, tool_call_id="nav-2")
        fake_interrupt.assert_called_once_with(
            {
                "client_action_request": {
                    "id": "nav-2",
                    "directive": {"kind": "navigate", "to": "/b", "discard_changes": True},
                }
            }
        )

    def test_unsaved_changes_refusal_tells_the_model_to_ask(self):
        out = render_client_action_result(
            "navigate", {"ok": False, "reason": "unsaved-changes", "detail": "Watch w1: name, condition"}
        )
        assert "NOT NAVIGATED" in out
        assert "Watch w1: name, condition" in out
        assert "Never discard on your own" in out
        assert "discard_changes=true only after the user said to discard" in out

    @pytest.mark.asyncio
    async def test_highlight_still_rides_the_custom_stream(self):
        writer = Mock()
        with (
            patch(f"{MODULE}.get_stream_writer", return_value=writer),
            patch(f"{MODULE}.interrupt") as fake_interrupt,
        ):
            out, _ = await _client_action_tool(kind="highlight", target_type="S", target_id="1", field="x")
        fake_interrupt.assert_not_called()
        writer.assert_called_once()
        assert out == "Directive sent to the client."


class TestResultRendering:
    def test_a_navigate_that_discarded_changes_says_so(self):
        out = render_client_action_result(
            "navigate", {"ok": True, "discarded": "ExistingScheduledJob:1 (max_failures)"}
        )
        assert "DISCARDED (ExistingScheduledJob:1 (max_failures))" in out
        assert "DISCARDED" not in render_client_action_result("navigate", {"ok": True})

    def test_non_dict_result_never_reads_as_success(self):
        assert "do not assume" in render_client_action_result("apply", None).lower()
        assert "do not assume" in render_client_action_result("apply", "weird").lower()


class TestSave:
    """Saving is an invoke of the form's `save` action — there is no `submit` kind."""

    @pytest.mark.asyncio
    async def test_save_is_an_invoke_round_trip_and_reports_the_save(self):
        with patch(
            "agent_common.core.client_action_tool.interrupt",
            return_value={"ok": True, "saved": True, "content": "{}"},
        ) as fake_interrupt:
            out, _ = await _client_action_tool(
                kind="invoke", target_type="Settings", target_id="me", action="save", tool_call_id="call-2"
            )
        directive = fake_interrupt.call_args.args[0]["client_action_request"]["directive"]
        assert directive["kind"] == "invoke" and directive["action"] == "save"
        assert "SAVED the change" in out

    def test_submit_is_no_longer_a_kind(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ClientActionInput(kind="submit", target_type="Settings", target_id="me")

    def test_a_save_on_a_closed_form_says_nothing_was_saved_and_how_to_redo_it(self):
        detail = (
            "It is shown read-only right now: no form is open, so nothing was saved and no unsaved values "
            "are on screen. This object offers: edit."
        )
        out = render_client_action_result("invoke", {"ok": False, "reason": "unknown-action", "detail": detail})
        assert "nothing was saved" in out and detail in out
        assert "open the form again" in out
        assert "page's own button" not in out


class TestInvokeThatSaves:
    def test_an_approved_saving_action_is_reported_done(self):
        # Told "nothing was saved" after an approved "Set as default", the agent asked again.
        out = render_client_action_result("invoke", {"ok": True, "saved": True, "content": "{}"})
        assert "SAVED the change" in out and "do not run it again" in out
        assert "nothing was saved" not in out


class TestInvoke:
    @pytest.mark.asyncio
    async def test_invoke_is_a_round_trip_with_action_and_args(self):
        writer = Mock()
        landed = '{"page": {"key": "/watches/w1"}, "objects": [{"type": "WatchForm", "id": "w1"}]}'
        with (
            patch(f"{MODULE}.get_stream_writer", return_value=writer),
            patch(
                f"{MODULE}.interrupt", return_value={"ok": True, "detail": "Edit mode on", "content": landed}
            ) as fake_interrupt,
        ):
            out, _ = await _client_action_tool(
                kind="invoke",
                target_type="Watch",
                target_id="w1",
                action="edit",
                args={"section": "condition"},
                tool_call_id="inv-1",
            )
        fake_interrupt.assert_called_once_with(
            {
                "client_action_request": {
                    "id": "inv-1",
                    "directive": {
                        "kind": "invoke",
                        "target": {"type": "Watch", "id": "w1"},
                        "action": "edit",
                        "args": {"section": "condition"},
                    },
                }
            }
        )
        writer.assert_not_called()
        assert "ran the action" in out
        assert "nothing was saved" in out
        assert "Edit mode on" in out
        assert '"type": "WatchForm"' in out

    @pytest.mark.asyncio
    async def test_invoke_without_args_omits_them(self):
        with patch(f"{MODULE}.interrupt", return_value={"ok": True}) as fake_interrupt:
            out, _ = await _client_action_tool(
                kind="invoke", target_type="Watch", target_id="w1", action="run_check", tool_call_id="inv-2"
            )
        directive = fake_interrupt.call_args.args[0]["client_action_request"]["directive"]
        assert directive == {"kind": "invoke", "target": {"type": "Watch", "id": "w1"}, "action": "run_check"}
        assert "no details about the page" in out

    @pytest.mark.asyncio
    async def test_invoke_requires_target_and_action(self):
        with patch(f"{MODULE}.interrupt") as fake_interrupt:
            assert (await _client_action_tool(kind="invoke", action="edit"))[0].startswith("Error:")
            assert (await _client_action_tool(kind="invoke", target_type="W", target_id="1"))[0].startswith("Error:")
        fake_interrupt.assert_not_called()

    def test_unknown_action_points_at_the_manifest(self):
        out = render_client_action_result(
            "invoke", {"ok": False, "reason": "unknown-action", "detail": "Watch has no action 'delete'"}
        )
        assert "FAILED" in out
        assert "`actions`" in out
        assert "Watch has no action 'delete'" in out

    def test_failed_action_is_not_success(self):
        out = render_client_action_result("invoke", {"ok": False, "reason": "failed", "detail": "dialog crashed"})
        assert out.startswith("The action FAILED (failed)")
        assert "dialog crashed" in out


class TestLogRedaction:
    """Log lines describe the action, never what the user typed into it."""

    def test_directive_logs_field_names_not_values(self):
        from agent_common.core.client_action_tool import describe_directive

        out = describe_directive(
            {
                "kind": "invoke",
                "target": {"type": "Settings", "id": "me"},
                "action": "change_phone",
                "params": {"phone": "+41795551234"},
            }
        )
        assert "+41795551234" not in out
        assert "'params_fields': ['phone']" in out
        assert "change_phone" in out
        out = describe_directive(
            {
                "kind": "apply",
                "target": {"type": "Secret", "id": "new"},
                "values": {"name": "qa", "description": "s3cret"},
            }
        )
        assert "s3cret" not in out and "'values_fields': ['description', 'name']" in out

    def test_result_logs_outcome_not_content(self):
        from agent_common.core.client_action_tool import describe_result

        out = describe_result(
            {
                "ok": True,
                "applied": ["phone"],
                "rejected": [{"field": "x", "reason": "r"}],
                "previous": {"phone": "+41796"},
                "content": "page text with +41796",
            }
        )
        assert "+41796" not in out
        assert "'applied': ['phone']" in out and "'rejected': ['x']" in out and "'previous_fields': ['phone']" in out
        assert "'content_chars': 21" in out


class TestLandedObjectsArtifact:
    """The approval layer reads the landed page's objects from the result's artifact."""

    def test_objects_of_the_landed_page(self):
        objects = [
            {"type": "ExistingScheduledJob", "id": "7", "actions": [{"name": "run_now", "requiresApproval": True}]}
        ]
        content = json.dumps({"page": {"path": "/app/scheduler/7"}, "objects": objects})
        assert client_action_artifact({"ok": True, "content": content}) == {"objects": objects}

    def test_nothing_without_a_landed_page(self):
        assert client_action_artifact({"ok": True}) is None
        assert client_action_artifact({"ok": True, "content": "not json"}) is None
        assert client_action_artifact(None) is None


class TestDescribeClientObjects:
    """The manifest in a log line: which objects, never their form values."""

    def test_shape_only(self):
        from agent_common.core.client_action_tool import describe_client_objects

        objects = [
            {
                "type": "DeliveryChannel",
                "id": "3",
                "scope": "update",
                "unsaved": True,
                "values": {"webhook_url": "https://hooks.example/secret-token"},
            },
            {"type": "Page", "id": "/app/settings", "scope": "view"},
        ]
        line = describe_client_objects(objects)
        assert line == "2 object(s): DeliveryChannel:3(update unsaved), Page:/app/settings(view)"
        assert "secret-token" not in line
