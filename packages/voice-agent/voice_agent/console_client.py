"""Console backend client for the voice-agent service principal.

Uses Keycloak client-credentials to obtain a service token, then calls the
console-backend voice-agent API endpoints (GET /users/by-phone, etc.).

Token is cached for its lifetime (with a 60-second safety margin).
"""

from __future__ import annotations

import logging
import os

import httpx
from pydantic import BaseModel
from ringier_a2a_sdk.oauth.client import OidcOAuth2Client

logger = logging.getLogger(__name__)

_CONSOLE_BACKEND_URL = os.getenv("CONSOLE_BACKEND_URL", "http://localhost:5001")
_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "voice-agent")

# SDK OAuth2 client — handles client-credentials caching/expiry internally
# (per-audience cache + leeway). Same client used elsewhere in this package.
_oauth_client = OidcOAuth2Client(
    client_id=_CLIENT_ID,
    client_secret=os.getenv("OIDC_CLIENT_SECRET", ""),
    issuer=os.getenv("OIDC_ISSUER", ""),
)

# Shared client — reuses connections and TLS sessions across all calls.
# Call close() at application shutdown (see server.py lifespan).
_http_client = httpx.AsyncClient(timeout=10.0)


async def _service_headers() -> dict[str, str]:
    token = await _oauth_client.get_token(_CLIENT_ID)
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
    resp = await _http_client.get(url, headers=headers)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return UserInfo(**resp.json())


async def get_user_mcp_token(user_id: str) -> str | None:
    """Fetch a Gatana (MCP gateway) token for the user, or None if unavailable.

    The backend mints it from the refresh token saved at console login. Returns None
    when the user has no stored offline token (no consent) or on error, so callers
    can fall back to a tool-less session.
    """
    headers = await _service_headers()
    url = f"{_CONSOLE_BACKEND_URL}/api/v1/voice/users/{user_id}/mcp-token"
    try:
        resp = await _http_client.get(url, headers=headers)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception as exc:
        logger.warning("Failed to fetch MCP token for user %s: %s", user_id, exc)
        return None


async def list_sub_agents_for_menu(user_id: str, limit: int = 5) -> list[SubAgentInfo]:
    """Return up to `limit` activated sub-agents for the DTMF menu."""
    headers = await _service_headers()
    url = f"{_CONSOLE_BACKEND_URL}/api/v1/voice/users/{user_id}/sub-agents"
    resp = await _http_client.get(url, headers=headers, params={"limit": limit})
    resp.raise_for_status()
    data = resp.json()

    agents: list[SubAgentInfo] = []
    for item in data.get("items", []):
        cfg = item.get("config_version") or {}
        agents.append(
            SubAgentInfo(
                id=item["id"],
                name=item.get("name", f"Agent {item['id']}"),
                system_prompt=cfg.get("system_prompt"),
                voice_name=cfg.get("voice_name"),
                mcp_tools=cfg.get("mcp_tools") or [],
            )
        )
    return agents


async def create_voice_session(
    user_id: str,
    phone_number: str,
    sub_agent_id: int | None = None,
    call_sid: str | None = None,
    use_session_memory: bool = False,
) -> VoiceSessionInfo | None:
    """Create a voice session record. Returns None on failure."""
    url = f"{_CONSOLE_BACKEND_URL}/api/v1/voice/sessions"
    payload = {
        "user_id": user_id,
        "phone_number": phone_number,
        "sub_agent_id": sub_agent_id,
        "call_sid": call_sid,
        "use_session_memory": use_session_memory,
    }
    try:
        headers = await _service_headers()
        resp = await _http_client.post(url, json=payload, headers=headers)
        if not resp.is_success:
            logger.error(
                "Failed to create voice session: HTTP %s — %s",
                resp.status_code,
                resp.text[:300],
            )
            return None
        return VoiceSessionInfo(**resp.json()["data"])
    except Exception as exc:
        logger.error("Failed to create voice session: %s", exc)
        return None


async def get_latest_resumable_session(
    user_id: str,
    sub_agent_id: int | None = None,
) -> VoiceSessionInfo | None:
    """Return the most recent completed session with a Gemini handle, or None.

    When sub_agent_id is omitted, searches across all of the user's agents.
    """
    headers = await _service_headers()
    url = f"{_CONSOLE_BACKEND_URL}/api/v1/voice/sessions/latest"
    params: dict = {"user_id": user_id}
    if sub_agent_id is not None:
        params["sub_agent_id"] = sub_agent_id
    try:
        resp = await _http_client.get(url, params=params, headers=headers)
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
        resp = await _http_client.patch(
            url, json={"gemini_session_handle": gemini_session_handle}, headers=headers
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to update session handle %s: %s", session_id, exc)


async def complete_voice_session(session_id: str) -> None:
    """Mark the voice session as completed."""
    headers = await _service_headers()
    url = f"{_CONSOLE_BACKEND_URL}/api/v1/voice/sessions/{session_id}/complete"
    try:
        resp = await _http_client.patch(url, headers=headers)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to complete voice session %s: %s", session_id, exc)


async def fail_voice_session(session_id: str) -> None:
    """Mark the voice session as failed."""
    headers = await _service_headers()
    url = f"{_CONSOLE_BACKEND_URL}/api/v1/voice/sessions/{session_id}/fail"
    try:
        resp = await _http_client.patch(url, headers=headers)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to fail voice session %s: %s", session_id, exc)
