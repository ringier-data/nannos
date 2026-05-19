"""Tests for ToolStatusMiddleware — descriptive status for all tool calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_common.middleware.tool_status import (
    TOOL_STATUS_EVENT,
    ToolStatusMiddleware,
    _build_status,
)

# ---------------------------------------------------------------------------
# Unit tests for _build_status()
# ---------------------------------------------------------------------------


class TestBuildStatus:
    def test_read_file_skill_path(self):
        assert _build_status("read_file", {"file_path": "/skills/weather/README.md"}) == "Loading skill weather\u2026"

    def test_read_file_skill_root(self):
        """Skill root without a subpath still shows the skill name."""
        assert _build_status("read_file", {"file_path": "/skills/summarizer/"}) == "Loading skill summarizer\u2026"

    def test_read_file_skills_dir_only(self):
        """Bare /skills/ with no skill name gives generic message."""
        assert _build_status("read_file", {"file_path": "/skills/"}) == "Loading skill\u2026"

    def test_read_file_normal_path(self):
        assert (
            _build_status("read_file", {"file_path": "/myproject/src/app.py"}) == "Reading /myproject/src/app.py\u2026"
        )

    def test_read_file_no_path(self):
        assert _build_status("read_file", {}) == "Using read_file\u2026"

    def test_read_file_empty_path(self):
        assert _build_status("read_file", {"file_path": ""}) == "Using read_file\u2026"

    def test_read_file_path_arg_fallback(self):
        """Falls back to 'path' arg when 'file_path' is absent."""
        assert _build_status("read_file", {"path": "/data/file.csv"}) == "Reading /data/file.csv\u2026"

    def test_other_tool_generic_status(self):
        assert _build_status("some_tool", {}) == "Using some_tool\u2026"

    # --- execute ---
    def test_execute_shows_command(self):
        assert _build_status("execute", {"command": "ls -la /tmp"}) == "Running `ls -la /tmp`\u2026"

    def test_execute_truncates_long_command(self):
        long_cmd = "x" * 120
        result = _build_status("execute", {"command": long_cmd})
        assert result.startswith("Running `")
        assert len(result) < 100  # 80 chars + prefix/suffix

    def test_execute_no_command(self):
        assert _build_status("execute", {}) == "Using execute\u2026"

    # --- write_file / edit_file ---
    def test_write_file_shows_path(self):
        assert _build_status("write_file", {"file_path": "/project/out.txt"}) == "Writing /project/out.txt\u2026"

    def test_edit_file_shows_path(self):
        assert _build_status("edit_file", {"file_path": "/src/app.py"}) == "Editing /src/app.py\u2026"

    # --- grep ---
    def test_grep_shows_pattern(self):
        assert _build_status("grep", {"pattern": "TODO"}) == 'Searching for "TODO"\u2026'

    # --- docstore_search ---
    def test_docstore_search_shows_query(self):
        assert (
            _build_status("docstore_search", {"query": "quarterly revenue"})
            == 'Searching documents for "quarterly revenue"\u2026'
        )


# ---------------------------------------------------------------------------
# Integration tests for awrap_tool_call
# ---------------------------------------------------------------------------


class TestToolStatusMiddleware:
    @pytest.fixture
    def middleware(self):
        return ToolStatusMiddleware()

    @pytest.fixture
    def handler(self):
        mock = AsyncMock()
        mock.return_value = MagicMock(content="ok", status="success")
        return mock

    @pytest.mark.asyncio
    async def test_emits_status_for_read_file(self, middleware, handler):
        request = MagicMock()
        request.tool_call = {
            "name": "read_file",
            "args": {"file_path": "/skills/weather/config.yaml"},
        }

        captured = []

        def fake_writer(event):
            captured.append(event)

        with patch("agent_common.middleware.tool_status.get_stream_writer", return_value=fake_writer):
            await middleware.awrap_tool_call(request, handler)

        handler.assert_awaited_once_with(request)
        assert len(captured) == 1
        event_type, payload = captured[0]
        assert event_type == TOOL_STATUS_EVENT
        assert payload["status"] == "Loading skill weather\u2026"

    @pytest.mark.asyncio
    async def test_emits_status_for_normal_file(self, middleware, handler):
        request = MagicMock()
        request.tool_call = {
            "name": "read_file",
            "args": {"file_path": "/project/main.py"},
        }

        captured = []

        def fake_writer(event):
            captured.append(event)

        with patch("agent_common.middleware.tool_status.get_stream_writer", return_value=fake_writer):
            await middleware.awrap_tool_call(request, handler)

        assert len(captured) == 1
        assert captured[0][1]["status"] == "Reading /project/main.py\u2026"

    @pytest.mark.asyncio
    async def test_emits_status_for_other_tools(self, middleware, handler):
        request = MagicMock()
        request.tool_call = {
            "name": "grep",
            "args": {"pattern": "TODO"},
        }

        captured = []

        def fake_writer(event):
            captured.append(event)

        with patch("agent_common.middleware.tool_status.get_stream_writer", return_value=fake_writer):
            await middleware.awrap_tool_call(request, handler)

        handler.assert_awaited_once_with(request)
        assert len(captured) == 1
        assert captured[0][1]["status"] == 'Searching for "TODO"\u2026'

    @pytest.mark.asyncio
    async def test_no_status_for_suppressed_tools(self, middleware, handler):
        """Response schema tools should not emit status."""
        request = MagicMock()
        request.tool_call = {
            "name": "FinalResponseSchema",
            "args": {},
        }

        captured = []

        def fake_writer(event):
            captured.append(event)

        with patch("agent_common.middleware.tool_status.get_stream_writer", return_value=fake_writer):
            await middleware.awrap_tool_call(request, handler)

        handler.assert_awaited_once_with(request)
        assert len(captured) == 0

    @pytest.mark.asyncio
    async def test_graceful_when_no_stream_writer(self, middleware, handler):
        """No crash when stream_writer is unavailable."""
        request = MagicMock()
        request.tool_call = {
            "name": "read_file",
            "args": {"file_path": "/skills/weather/README.md"},
        }

        with patch(
            "agent_common.middleware.tool_status.get_stream_writer",
            side_effect=RuntimeError("no writer"),
        ):
            result = await middleware.awrap_tool_call(request, handler)

        handler.assert_awaited_once_with(request)

    @pytest.mark.asyncio
    async def test_handles_coroutine_stream_writer(self, middleware, handler):
        """When stream_writer returns a coroutine, it is awaited."""
        request = MagicMock()
        request.tool_call = {
            "name": "read_file",
            "args": {"file_path": "/project/data.json"},
        }

        captured = []

        async def async_writer(event):
            captured.append(event)

        with patch("agent_common.middleware.tool_status.get_stream_writer", return_value=async_writer):
            await middleware.awrap_tool_call(request, handler)

        assert len(captured) == 1
        assert captured[0][1]["status"] == "Reading /project/data.json\u2026"
