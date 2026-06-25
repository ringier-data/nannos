"""Router for voice-agent service principal endpoints.

Consumed exclusively by the voice-agent service via Keycloak client-credentials.
All endpoints require a Bearer token with azp == VOICE_AGENT_CLIENT_ID.

Endpoints:
  GET  /api/v1/voice/users/by-phone/{phone_number}      — resolve caller to user
  GET  /api/v1/voice/users/{user_id}/sub-agents          — list ≤5 activated sub-agents for menu
  POST /api/v1/voice/sessions                            — create inbound call session record
  GET  /api/v1/voice/sessions/latest                     — get latest resumable session
  PATCH /api/v1/voice/sessions/{session_id}/handle       — store Gemini resumption handle
  PATCH /api/v1/voice/sessions/{session_id}/complete     — mark session completed
  PATCH /api/v1/voice/sessions/{session_id}/fail         — mark session failed
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from ringier_a2a_sdk.auth import JWTValidationError, JWTValidator

from ..config import config
from ..db.session import DbSession
from ..dependencies import get_client_id_from_request
from ..models.sub_agent import SubAgentListItem, SubAgentListResponse, SubAgentStatus
from ..models.user import User, UserRole
from ..models.voice_session import (
    VoiceSession,
    VoiceSessionCreate,
    VoiceSessionHandleUpdate,
    VoiceSessionResponse,
)
from ..services.sub_agent_service import SubAgentService
from ..services.user_service import UserService
from ..services.voice_session_service import VoiceSessionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/voice", tags=["voice-agent"])

_voice_session_service = VoiceSessionService()


def _get_user_service(request: Request) -> UserService:
    return request.app.state.user_service


def _get_sub_agent_service(request: Request) -> SubAgentService:
    return request.app.state.sub_agent_service


async def require_voice_agent_service(request: Request) -> User:
    """Accept only the voice-agent service principal (azp == VOICE_AGENT_CLIENT_ID).

    Returns a synthetic User for audit purposes — not persisted to the DB.
    """
    client_id = await get_client_id_from_request(request)
    voice_client_id = config.voice_agent.client_id

    if not voice_client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Voice-agent client_id not configured on backend",
        )

    if client_id and client_id == voice_client_id:
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.split(" ", 1)[1] if auth_header.startswith("Bearer ") else ""
        sub = f"service:{client_id}"
        if token:
            try:
                validator = JWTValidator(issuer=config.oidc.issuer)
                payload = await validator.validate(token)
                sub = payload.get("sub", sub)
            except JWTValidationError:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return User(
            id=f"service:{client_id}",
            sub=sub,
            email=f"{client_id}@service.internal",
            first_name="Voice",
            last_name="Agent",
            is_administrator=False,
            role=UserRole.MEMBER,
        )

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Forbidden: voice-agent service principal required",
    )


# ── User lookup ───────────────────────────────────────────────────────────────


@router.get("/users/by-phone/{phone_number}", response_model=dict)
async def get_user_by_phone(
    phone_number: str,
    request: Request,
    db: DbSession,
    _: User = Depends(require_voice_agent_service),
) -> dict:
    """Resolve an E.164 phone number to a user record.

    Returns 404 if no user is registered with that number.
    """
    user_service = _get_user_service(request)
    user = await user_service.get_user_by_phone_number(db, phone_number)
    if user is None:
        raise HTTPException(status_code=404, detail="No user registered for that phone number")
    return {
        "id": user.id,
        "sub": user.sub,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "status": user.status,
    }


# ── Sub-agent menu list ───────────────────────────────────────────────────────


@router.get("/users/{user_id}/sub-agents", response_model=SubAgentListResponse)
async def list_sub_agents_for_menu(
    user_id: str,
    request: Request,
    db: DbSession,
    limit: int = Query(default=5, ge=1, le=5),
    _: User = Depends(require_voice_agent_service),
) -> SubAgentListResponse:
    """List up to 5 activated/approved sub-agents for the inbound call DTMF menu.

    Ordered by most-recently-used first (via voice_sessions history), then
    alphabetically for sub-agents never used on a call.
    """
    sub_agent_service = _get_sub_agent_service(request)

    try:
        sub_agents = await sub_agent_service.get_accessible_sub_agents(
            db,
            user_id,
            is_admin=False,
            status_filter=SubAgentStatus.APPROVED,
            activated_only=True,
        )
    except Exception as e:
        logger.error(f"Failed to list sub-agents for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to list sub-agents")

    # Order by most-recently-used in voice calls, then alphabetically.
    recent_ids = await _voice_session_service.get_most_recent_sub_agent_ids(db, user_id, limit=limit)
    recent_order = {sid: i for i, sid in enumerate(recent_ids)}

    def _sort_key(sa):
        idx = recent_order.get(sa.id)
        if idx is not None:
            return (0, idx, sa.name or "")
        return (1, 0, sa.name or "")

    sorted_agents = sorted(sub_agents, key=_sort_key)[:limit]
    items = [SubAgentListItem.from_sub_agent(sa) for sa in sorted_agents]
    return SubAgentListResponse(items=items, total=len(items))


# ── Voice session CRUD ────────────────────────────────────────────────────────


@router.post("/sessions", response_model=VoiceSessionResponse, status_code=201)
async def create_voice_session(
    body: VoiceSessionCreate,
    db: DbSession,
    _: User = Depends(require_voice_agent_service),
) -> VoiceSessionResponse:
    """Create a voice session record at the start of an inbound call."""
    session = await _voice_session_service.create_session(
        db,
        user_id=body.user_id,
        phone_number=body.phone_number,
        sub_agent_id=body.sub_agent_id,
        call_sid=body.call_sid,
        use_session_memory=body.use_session_memory,
    )
    if session is None:
        raise HTTPException(status_code=500, detail="Failed to create voice session")
    return VoiceSessionResponse(data=session)


@router.get("/sessions/latest", response_model=VoiceSessionResponse | None)
async def get_latest_resumable_session(
    db: DbSession,
    user_id: str = Query(...),
    sub_agent_id: int = Query(...),
    _: User = Depends(require_voice_agent_service),
) -> VoiceSessionResponse | None:
    """Return the most recent completed session with a Gemini resumption handle, or null."""
    session = await _voice_session_service.get_latest_resumable_session(db, user_id, sub_agent_id)
    if session is None:
        return None
    return VoiceSessionResponse(data=session)


@router.patch("/sessions/{session_id}/handle")
async def update_session_handle(
    session_id: str,
    body: VoiceSessionHandleUpdate,
    db: DbSession,
    _: User = Depends(require_voice_agent_service),
) -> dict:
    """Store a Gemini session resumption handle after the call ends."""
    ok = await _voice_session_service.update_handle(db, session_id, body.gemini_session_handle)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to update session handle")
    return {"ok": True}


@router.patch("/sessions/{session_id}/complete")
async def complete_voice_session(
    session_id: str,
    db: DbSession,
    _: User = Depends(require_voice_agent_service),
) -> dict:
    """Mark a voice session as completed."""
    ok = await _voice_session_service.complete_session(db, session_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to complete session")
    return {"ok": True}


@router.patch("/sessions/{session_id}/fail")
async def fail_voice_session(
    session_id: str,
    db: DbSession,
    _: User = Depends(require_voice_agent_service),
) -> dict:
    """Mark a voice session as failed."""
    ok = await _voice_session_service.fail_session(db, session_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to fail session")
    return {"ok": True}
