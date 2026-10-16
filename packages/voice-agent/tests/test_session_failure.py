"""A Live session that dies mid-call must end the call and fail the task.

Regression for a prod call where Gemini closed the session with 1007 (the declarations
of every gateway tool overflowed the context window): the callee heard silence until
they hung up and the A2A task still reported "Call completed".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.types import TaskState
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ringier_a2a_sdk.models import AgentStreamResponse
from starlette.websockets import WebSocketDisconnect

from voice_agent import agent as agent_mod
from voice_agent import call_bridge, twilio_transport
from voice_agent.a2a_agent import VoiceAgent, VoiceCallRequest
from voice_agent.agent import GeminiLiveAgent

_OVERFLOW = "1007 None. The input token count (212664) exceeds the maximum number of tokens allowed (131072)."


class _FakeSession:
    """Stands in for a google-genai Live session."""

    def __init__(self, receive_error: Exception | None = None) -> None:
        self._receive_error = receive_error

    async def receive(self):
        if self._receive_error is not None:
            raise self._receive_error
        await asyncio.Event().wait()  # an idle, healthy session
        yield  # pragma: no cover — makes this an async generator

    async def send_realtime_input(self, **_kwargs) -> None:
        return None

    async def send_client_content(self, **_kwargs) -> None:
        return None


def _fake_client(session: _FakeSession) -> MagicMock:
    @contextlib.asynccontextmanager
    async def _connect(**_kwargs):
        yield session

    client = MagicMock()
    client.aio.live.connect = _connect
    return client


async def _run(agent: GeminiLiveAgent, session: _FakeSession, audio_in: asyncio.Queue) -> asyncio.Queue:
    event_out: asyncio.Queue = asyncio.Queue()
    with patch.object(agent_mod, "build_gemini_client", return_value=_fake_client(session)):
        await asyncio.wait_for(agent.run(audio_in, event_out), timeout=5)
    return event_out


def _drain(queue: asyncio.Queue) -> list[dict]:
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


class TestLiveSessionEnd:
    async def test_server_error_ends_the_session_and_reports_it(self):
        """A receive error must not leave the session parked waiting for audio."""
        agent = GeminiLiveAgent(session_id="s1")
        events = _drain(await _run(agent, _FakeSession(RuntimeError(_OVERFLOW)), asyncio.Queue()))

        errors = [e for e in events if e["type"] == "error"]
        assert len(errors) == 1
        assert "131072" in errors[0]["message"]
        assert "131072" in agent.session_error

    async def test_end_of_stream_is_not_an_error(self):
        agent = GeminiLiveAgent(session_id="s1")
        audio_in: asyncio.Queue = asyncio.Queue()
        audio_in.put_nowait(None)
        events = _drain(await _run(agent, _FakeSession(), audio_in))

        assert [e for e in events if e["type"] == "error"] == []
        assert agent.session_error is None

    async def test_connect_failure_is_reported(self):
        agent = GeminiLiveAgent(session_id="s1")
        client = MagicMock()
        client.aio.live.connect = MagicMock(side_effect=RuntimeError("1008 voice not supported"))
        event_out: asyncio.Queue = asyncio.Queue()
        with patch.object(agent_mod, "build_gemini_client", return_value=client):
            await asyncio.wait_for(agent.run(asyncio.Queue(), event_out), timeout=5)

        errors = [e for e in _drain(event_out) if e["type"] == "error"]
        assert errors and "voice not supported" in errors[0]["message"]


class TestToolSelection:
    async def test_no_tool_list_means_no_gateway(self):
        """An empty list must not fall through to the user's whole tool catalogue."""
        voice = VoiceAgent()
        voice._credential_injector = MagicMock(get_headers=AsyncMock(return_value={"Authorization": "x"}))
        with patch("voice_agent.a2a_agent.GeminiLiveAgent") as live_cls:
            live_cls.return_value.run = AsyncMock()
            await voice._create_audio_session(
                session_key="k", system_prompt=None, voice_name=None, mcp_tools=[], access_token="tok"
            )

        kwargs = live_cls.call_args.kwargs
        assert kwargs["mcp_gateway_url"] is None
        assert kwargs["mcp_headers"] is None
        voice._credential_injector.get_headers.assert_not_called()

    async def test_listed_tools_reach_the_gateway(self):
        voice = VoiceAgent()
        voice._credential_injector = MagicMock(get_headers=AsyncMock(return_value={"Authorization": "x"}))
        with (
            patch("voice_agent.a2a_agent.GeminiLiveAgent") as live_cls,
            patch("voice_agent.a2a_agent._MCP_GATEWAY_URL", "https://gw/mcp"),
        ):
            live_cls.return_value.run = AsyncMock()
            await voice._create_audio_session(
                session_key="k",
                system_prompt=None,
                voice_name=None,
                mcp_tools=["gcal_list_events"],
                access_token="tok",
            )

        kwargs = live_cls.call_args.kwargs
        assert kwargs["mcp_gateway_url"] == "https://gw/mcp"
        assert kwargs["mcp_tool_filter"] == ["gcal_list_events"]

    def test_request_accepts_a_tool_list(self):
        assert VoiceCallRequest().mcp_tools == []
        assert VoiceCallRequest(mcp_tools=["gcal_list_events"]).mcp_tools == ["gcal_list_events"]


class TestCallOutcome:
    async def _drive_call(self, result: dict) -> list[AgentStreamResponse]:
        """Run _stream_phone_call with Twilio stubbed; resolve the call with `result`."""
        voice = VoiceAgent()

        async def _resolve():
            async with asyncio.timeout(5):
                while "CA1" not in call_bridge._CALL_ANSWERED:
                    await asyncio.sleep(0.01)
            call_bridge._CALL_ANSWERED.pop("CA1").set_result(True)
            call_bridge._CALL_FUTURES.pop("CA1").set_result(result)

        with (
            patch.dict("os.environ", {"PUBLIC_URL": "https://voice.example.com"}),
            patch.object(VoiceAgent, "_prewarm_audio_session", AsyncMock()),
            patch.object(VoiceAgent, "_create_outbound_voice_session", AsyncMock(return_value=None)),
            patch("voice_agent.a2a_agent.make_outbound_call", return_value="CA1"),
        ):
            resolver = asyncio.create_task(_resolve())
            responses = [
                r
                async for r in voice._stream_phone_call(
                    phone_number="+41790000000",
                    system_prompt="hi",
                    voice_name=None,
                    mcp_tools=[],
                    access_token=None,
                    context_messages=[],
                )
            ]
            await resolver
        call_bridge._PENDING_CALLS.pop("CA1", None)
        return responses

    async def test_session_error_fails_the_task(self):
        responses = await self._drive_call({"transcript": [], "call_sid": "CA1", "error": _OVERFLOW})
        final = responses[-1]
        assert final.state == TaskState.TASK_STATE_FAILED
        assert "131072" in final.content

    async def test_clean_call_completes(self):
        transcript = [{"role": "assistant", "text": "Ciao!"}]
        responses = await self._drive_call({"transcript": transcript, "call_sid": "CA1", "error": None})
        assert responses[-1].state == TaskState.TASK_STATE_COMPLETED
        assert "Ciao!" in responses[-1].content


class TestTransportHangsUp:
    def test_failed_session_closes_the_stream_and_reports_the_error(self):
        """Closing the media stream ends <Connect>; nothing follows it, so Twilio hangs up."""

        async def _failing_session(_init_config, _session_key):
            yield AgentStreamResponse(state=TaskState.TASK_STATE_FAILED, content=f"Voice processing error: {_OVERFLOW}")

        app = FastAPI()
        app.include_router(twilio_transport.twilio_router)
        loop_holder: dict = {}

        async def _register_future():
            loop_holder["future"] = asyncio.get_running_loop().create_future()
            call_bridge._CALL_FUTURES["CA9"] = loop_holder["future"]

        with (
            patch.object(twilio_transport._voice_agent, "_start_audio_session", _failing_session),
            patch.object(twilio_transport._voice_agent, "_end_session", AsyncMock()),
            patch.object(twilio_transport._voice_agent, "collect_usage_entries", return_value=[]),
            TestClient(app) as client,
        ):
            client.portal.call(_register_future)
            with client.websocket_connect("/twilio/stream") as ws:
                ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ9", "callSid": "CA9"}}))
                # The server closes the socket once the session fails.
                with pytest.raises(WebSocketDisconnect):
                    while True:
                        ws.receive_text()

            result = client.portal.call(lambda: asyncio.wait_for(loop_holder["future"], timeout=5))

        assert "131072" in result["error"]


class TestDeadBeforeDialling:
    async def test_connect_failure_is_caught_before_the_phone_rings(self):
        """A session that cannot connect must fail the task without dialling."""
        voice = VoiceAgent()
        client = MagicMock()
        client.aio.live.connect = MagicMock(side_effect=RuntimeError("1008 voice not supported"))
        dial = MagicMock(return_value="CA2")
        with (
            patch.dict("os.environ", {"PUBLIC_URL": "https://voice.example.com"}),
            patch.object(agent_mod, "build_gemini_client", return_value=client),
            patch("voice_agent.a2a_agent.make_outbound_call", dial),
        ):
            responses = [
                r
                async for r in voice._stream_phone_call(
                    phone_number="+41790000000",
                    system_prompt="hi",
                    voice_name=None,
                    mcp_tools=[],
                    access_token=None,
                    context_messages=[],
                )
            ]

        dial.assert_not_called()
        assert responses[-1].state == TaskState.TASK_STATE_FAILED
        assert "voice not supported" in responses[-1].content


class TestPersonaToolList:
    async def test_persona_list_replaces_the_callers_even_when_empty(self):
        voice = VoiceAgent()
        seen: dict = {}

        async def _fake_call(**kwargs):
            seen.update(kwargs)
            yield AgentStreamResponse(state=TaskState.TASK_STATE_COMPLETED, content="Done")

        persona = {"name": "sales-assistant", "system_prompt": "p", "mcp_tools": []}
        with (
            patch.object(VoiceAgent, "_fetch_sub_agent_config", AsyncMock(return_value=persona)),
            patch.object(VoiceAgent, "_stream_phone_call", side_effect=_fake_call),
        ):
            user_config = MagicMock(phone_number="+41790000000", access_token=None, user_sub="u")
            request = VoiceCallRequest(sub_agent_id=7, mcp_tools=["gcal_list_events"])
            _ = [r async for r in voice._handle_phone_call(request, user_config)]

        assert seen["mcp_tools"] == []


class TestUnknownToolNames:
    def test_similar_names_include_the_same_server(self):
        from voice_agent.agent import similar_tool_names

        available = ["gmail_search_threads", "gmail_get_message", "gcal_list_events", "github_list_issues"]
        assert similar_tool_names("gmail_list_messages", available) == ["gmail_get_message", "gmail_search_threads"]

    async def test_guessed_names_fail_before_dialling_with_the_real_ones(self):
        voice = VoiceAgent()
        dial = MagicMock(return_value="CA3")

        async def _fake_prewarm(self, *, session_key, **_kwargs):
            live = GeminiLiveAgent(session_id=session_key)
            live.unknown_tools = {"gmail_list_messages": ["gmail_get_message", "gmail_search_threads"]}
            live.session_ready = asyncio.get_running_loop().create_future()
            live.session_ready.set_result(True)
            task = asyncio.create_task(asyncio.Event().wait())
            self._active_sessions[session_key] = {
                "agent": live,
                "agent_task": task,
                "audio_in": asyncio.Queue(),
            }

        with (
            patch.dict("os.environ", {"PUBLIC_URL": "https://voice.example.com"}),
            patch.object(VoiceAgent, "_prewarm_audio_session", _fake_prewarm),
            patch("voice_agent.a2a_agent.make_outbound_call", dial),
        ):
            responses = [
                r
                async for r in voice._stream_phone_call(
                    phone_number="+41790000000",
                    system_prompt="hi",
                    voice_name=None,
                    mcp_tools=["gmail_list_messages", "gmail_get_message"],
                    access_token="tok",
                    context_messages=[],
                )
            ]

        dial.assert_not_called()
        final = responses[-1]
        assert final.state == TaskState.TASK_STATE_FAILED
        assert "gmail_list_messages" in final.content
        assert "gmail_search_threads" in final.content
        assert voice._active_sessions == {}


class TestPersonaUnknownTools:
    async def test_persona_tools_missing_for_this_user_do_not_block_the_call(self):
        """Only names the caller typed are refused; a persona's list runs with the subset."""
        voice = VoiceAgent()
        seen: dict = {}

        async def _fake_call(**kwargs):
            seen.update(kwargs)
            yield AgentStreamResponse(state=TaskState.TASK_STATE_COMPLETED, content="Done")

        persona = {"name": "sales-assistant", "system_prompt": "p", "mcp_tools": ["salesforce_find_accounts"]}
        with (
            patch.object(VoiceAgent, "_fetch_sub_agent_config", AsyncMock(return_value=persona)),
            patch.object(VoiceAgent, "_stream_phone_call", side_effect=_fake_call),
        ):
            user_config = MagicMock(phone_number="+41790000000", access_token=None, user_sub="u")
            _ = [r async for r in voice._handle_phone_call(VoiceCallRequest(sub_agent_id=7), user_config)]

        assert seen["refuse_unknown_tools"] is False

    async def test_failed_persona_fetch_keeps_the_callers_tools(self):
        voice = VoiceAgent()
        seen: dict = {}

        async def _fake_call(**kwargs):
            seen.update(kwargs)
            yield AgentStreamResponse(state=TaskState.TASK_STATE_COMPLETED, content="Done")

        failed = {"name": "sub-agent-7", "fetch_failed": True}
        with (
            patch.object(VoiceAgent, "_fetch_sub_agent_config", AsyncMock(return_value=failed)),
            patch.object(VoiceAgent, "_stream_phone_call", side_effect=_fake_call),
        ):
            user_config = MagicMock(phone_number="+41790000000", access_token=None, user_sub="u")
            request = VoiceCallRequest(sub_agent_id=7, mcp_tools=["gmail_get_message"])
            _ = [r async for r in voice._handle_phone_call(request, user_config)]

        assert seen["mcp_tools"] == ["gmail_get_message"]
        assert seen["refuse_unknown_tools"] is True


def test_call_trace_redacts_the_bearer_token():
    from voice_agent.a2a_agent import _redact_call_inputs

    redacted = _redact_call_inputs({"access_token": "eyJ.secret", "phone_number": "+41790000000", "mcp_tools": []})
    assert redacted["access_token"] == "<redacted>"
    assert redacted["phone_number"] == "+41790000000"
    assert _redact_call_inputs({"access_token": None})["access_token"] is None
