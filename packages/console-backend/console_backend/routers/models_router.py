"""Models router — proxies available models from the orchestrator."""

import logging

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import config

logger = logging.getLogger(__name__)

router: APIRouter = APIRouter(prefix="/api/v1", tags=["models"])


class AvailableModel(BaseModel):
    """A model available on the orchestrator."""

    value: str
    label: str
    provider: str
    supports_thinking: bool = False
    thinking_levels: list[str] | None = None
    is_default: bool = False


def _orchestrator_base_url() -> str:
    """Build the orchestrator base URL from config."""
    domain = config.orchestrator.base_domain
    if not domain:
        raise HTTPException(status_code=503, detail="Orchestrator not configured")
    schema = "http" if config.orchestrator.is_local() or "localhost" in domain else "https"
    return f"{schema}://{domain}"


@router.get("/models", response_model=list[AvailableModel])
async def list_available_models():
    """Return the LLM models available on the orchestrator.

    Proxies the orchestrator's /models endpoint so the frontend
    can discover models dynamically without hardcoding.
    """
    base = _orchestrator_base_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base}/models")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        logger.error("Orchestrator /models returned %s: %s", e.response.status_code, e.response.text)
        raise HTTPException(status_code=502, detail="Failed to fetch models from orchestrator") from e
    except httpx.ConnectError:
        logger.error("Cannot reach orchestrator at %s", base)
        raise HTTPException(status_code=503, detail="Orchestrator is not reachable")
    except Exception as e:
        logger.error("Unexpected error fetching models: %s", e)
        raise HTTPException(status_code=502, detail="Failed to fetch models from orchestrator") from e
