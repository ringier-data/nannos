"""The client_action tool's two delivery modes.

``apply`` is a ROUND TRIP: the directive rides the interrupt value (never the
custom stream — the resume replays the handler, and a pre-interrupt emit would
fire twice), and the tool's return renders the client's actual result. The
fire-and-forget kinds (navigate/highlight) still ride the custom stream.
"""

from unittest.mock import Mock, patch

import pytest

from agent_common.core.client_action_tool import (
    _client_action_handler,
    _render_client_action_result,
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
            out = await _client_action_handler(
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
        assert "user still reviews and saves" in out

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
            out = await _client_action_handler(
                kind="apply", target_type="Campaign", target_id="7", values={"x": 1}, tool_call_id="c"
            )
        assert "REJECTED" in out
        assert "campaignType" in out
        assert "not one of the allowed values" in out

    @pytest.mark.asyncio
    async def test_apply_without_result_is_reported_honestly(self):
        with patch(f"{MODULE}.interrupt", return_value={"ok": False, "reason": "no-result"}):
            out = await _client_action_handler(
                kind="apply", target_type="Campaign", target_id="7", values={"x": 1}, tool_call_id="c"
            )
        assert "Do NOT assume the action happened" in out

    @pytest.mark.asyncio
    async def test_apply_unknown_target_tells_the_agent_to_recheck_the_page(self):
        with patch(f"{MODULE}.interrupt", return_value={"ok": False, "reason": "unknown-target"}):
            out = await _client_action_handler(
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
            out = await _client_action_handler(kind="read_current_page", tool_call_id="c2")
        fake_interrupt.assert_called_once_with(
            {"client_action_request": {"id": "c2", "directive": {"kind": "read_current_page"}}}
        )
        assert out.startswith("Current page state")
        assert '"/campaigns/7"' in out

    @pytest.mark.asyncio
    async def test_unsupported_host_is_reported_as_failure(self):
        with patch(f"{MODULE}.interrupt", return_value={"ok": False, "reason": "unsupported"}):
            out = await _client_action_handler(kind="read_current_page", tool_call_id="c2")
        assert "FAILED" in out


class TestFireAndForgetUnchanged:
    @pytest.mark.asyncio
    async def test_navigate_rides_the_custom_stream_without_interrupt(self):
        writer = Mock()
        with (
            patch(f"{MODULE}.get_stream_writer", return_value=writer),
            patch(f"{MODULE}.interrupt") as fake_interrupt,
        ):
            out = await _client_action_handler(kind="navigate", to="/campaigns/7")
        fake_interrupt.assert_not_called()
        writer.assert_called_once_with(
            ("client_action", {"directive": {"kind": "navigate", "to": "/campaigns/7"}})
        )
        assert out == "Directive sent to the client."


class TestResultRendering:
    def test_non_dict_result_never_reads_as_success(self):
        assert "do not assume" in _render_client_action_result("apply", None).lower()
        assert "do not assume" in _render_client_action_result("apply", "weird").lower()
