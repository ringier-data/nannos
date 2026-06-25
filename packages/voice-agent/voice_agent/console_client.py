"""Console backend client for the voice-agent service principal.

Uses Keycloak client-credentials to obtain a service token, then calls the
console-backend voice-agent API endpoints (GET /users/by-phone, etc.).

Token is cached for its lifetime (with a 60-second safety margin).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_CONSOLE_BACKEND_URL = os.getenv("CONSOLE_BACKEND_URL", "http://localhost:5001")
_OIDC_ISSUER = os.getenv("OIDC_ISSUER", "")
_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "voice-agent")
_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")

_TOKEN_MARGIN_SECONDS = 60


class _TokenCache:
    def __init__(self):
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            self._token, ttl = await _fetch_client_credentials_token()
            self._expires_at = time.monotonic() + max(0, ttl - _TOKEN_MARGIN_SECONDS)
            return self._token


_token_cache = _TokenCache()


async def _fetch_client_credentials_token() -> tuple[str, int]:
    """Obtain a client-credentials token from Keycloak."""
    if not _OIDC_ISSUER:
        raise RuntimeError("OIDC_ISSUER not set — cannot obtain service token")
    token_url = f"{_OIDC_ISSUER}/protocol/openid-connect/token"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": _CLIENT_ID,
                "client_secret": _CLIENT_SECRET,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    return data["access_token"], int(data.get("expires_in", 300))


async def _service_headers() -> dict[str, str]:
    token = await _token_cache.get()
    return {"Authorization": f"Bearer {token}"}


# ── API calls ─────────────────────────────────────────────────────────────────


class UserInfo(BaseModel):
    id: str
    sub: str
    email: str
    first_name: str
    last_name: str
    status: str


class SubAgentInfo(BaseModel):
    id: int
    name: str
    system_prompt: str | None = None
    voice_name: str | None = None
    mcp_tools: list[str] = []


class VoiceSessionInfo(BaseModel):
    id: str
    user_id: str
    sub_agent_id: int | None = None
    gemini_session_handle: str | None = None
    use_session_memory: bool = False


async def lookup_user_by_phone(phone_number: str) -> UserInfo | None:
    """Return user info for a phone number, or None if not registered."""
    headers = await _service_headers()
    url = f"{_CONSOLE_BACKEND_URL}/api/v1/voice/users/by-phone/{phone_number}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return UserInfo(**resp.json())


async def list_sub_agents_for_menu(user_id: str, limit: int = 5) -> list[SubAgentInfo]:
    """Return up to `limit` activated sub-agents for the DTMF menu."""
    headers = await _service_headers()
    url = f"{_CONSOLE_BACKEND_URL}/api/v1/voice/users/{user_id}/sub-agents"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers, params={"limit": limit})
        resp.raise_for_status()
        data = resp.json()

    agents: list[SubAgentInfo] = []
    for item in data.get("items", []):
        cfg = item.get("config_version") or {}
        agents.append(SubAgentInfo(
            id=item["id"],
            name=item.get("name", f"Agent {item['id']}"),
            system_prompt=cfg.get("system_prompt"),
            voice_name=cfg.get("voice_name"),
            mcp_tools=cfg.get("mcp_tools") or [],
        ))
    return agents


async def create_voice_session(
    user_id: str,
    phone_number: str,
    sub_agent_id: int | None = None,
    call_sid: str | None = None,
    use_session_memory: bool = False,
) -> VoiceSessionInfo | None:
    """Create a voice session record. Returns None on failure."""
    headers = await _service_headers()
    url = f"{_CONSOLE_BACKEND_URL}/api/v1/voice/sessions"
    payload = {
        "user_id": user_id,
        "phone_number": phone_number,
        "sub_agent_id": sub_agent_id,
        "call_sid": call_sid,
        "use_session_memory": use_session_memory,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return VoiceSessionInfo(**resp.json()["data"])
    except Exception as exc:
        logger.warning("Failed to create voice session: %s", exc)
        return None


async def get_latest_resumable_session(
    user_id: str,
    sub_agent_id: int,
) -> VoiceSessionInfo | None:
    """Return the most recent completed session with a Gemini handle, or None."""
    headers = await _service_headers()
    url = f"{_CONSOLE_BACKEND_URL}/api/v1/voice/sessions/latest"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url,
                params={"user_id": user_id, "sub_agent_id": sub_agent_id},
                headers=headers,
            )
            resp.raise_for_status()
            body = resp.json()
            if body is None:
                return None
            return VoiceSessionInfo(**body["data"])
    except Exception as exc:
        logger.warning("Failed to fetch latest resumable session: %s", exc)
        return None


async def update_session_handle(session_id: str, gemini_session_handle: str) -> None:
    """Persist a Gemini resumption handle to the backend."""
    headers = await _service_headers()
    url = f"{_CONSOLE_BACKEND_URL}/api/v1/voice/sessions/{session_id}/handle"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(
                url,
                json={"gemini_session_handle": gemini_session_handle},
                headers=headers,
            )
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to update session handle %s: %s", session_id, exc)


async def complete_voice_session(session_id: str) -> None:
    """Mark the voice session as completed."""
    headers = await _service_headers()
    url = f"{_CONSOLE_BACKEND_URL}/api/v1/voice/sessions/{session_id}/complete"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(url, headers=headers)
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to complete voice session %s: %s", session_id, exc)


async def fail_voice_session(session_id: str) -> None:
    """Mark the voice session as failed."""
    headers = await _service_headers()
    url = f"{_CONSOLE_BACKEND_URL}/api/v1/voice/sessions/{session_id}/fail"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.patch(url, headers=headers)
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to fail voice session %s: %s", session_id, exc)
