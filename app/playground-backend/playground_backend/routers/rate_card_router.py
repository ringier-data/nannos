"""API router for LLM rate card management."""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..db.session import DbSession
from ..dependencies import require_admin
from ..models.usage import (
    RateCardEntriesList,
    RateCardEntry,
    RateCardEntryCreate,
    RateCardModelCreate,
)
from ..models.user import User
from ..services.rate_card_service import RateCardService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/rate-cards", tags=["rate-cards", "admin"])


def get_rate_card_service(request: Request) -> RateCardService:
    """Get rate card service from app state."""
    return request.app.state.rate_card_service


@router.get("/models", response_model=list[dict])
async def list_models_with_rates(
    request: Request,
    db: DbSession,
    _: User = Depends(require_admin),
    provider: str | None = Query(None),
):
    """
    List all models that have rate cards.

    Requires admin role with admin mode enabled.
    """
    rate_card_service = get_rate_card_service(request)
    try:
        models = await rate_card_service.list_models_with_rates(
            db=db,
            provider=provider,
        )
        return models
    except Exception as e:
        logger.error(f"Failed to list models with rates: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list models: {str(e)}",
        )


@router.get("", response_model=RateCardEntriesList)
async def list_rate_card_entries(
    request: Request,
    db: DbSession,
    _: User = Depends(require_admin),
    provider: str | None = Query(None),
    model_name: str | None = Query(None),
    active_only: bool = Query(True),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
):
    """
    List rate card entries with optional filters.

    Requires admin role with admin mode enabled.
    """
    rate_card_service = get_rate_card_service(request)
    try:
        entries, total = await rate_card_service.repository.list_entries(
            db=db,
            provider=provider,
            model_name=model_name,
            active_only=active_only,
            page=page,
            limit=limit,
        )

        # Convert to response models
        entry_models = [
            RateCardEntry(
                id=entry["id"],
                provider=entry["provider"],
                model_name=entry["model_name"],
                model_name_pattern=entry.get("model_name_pattern"),
                billing_unit=entry["billing_unit"],
                flow_direction=entry["flow_direction"],
                price_per_million=Decimal(str(entry["price_per_million"])),
                effective_from=entry["effective_from"],
                effective_until=entry.get("effective_until"),
                created_at=entry["created_at"],
                updated_at=entry["updated_at"],
            )
            for entry in entries
        ]

        return RateCardEntriesList(
            entries=entry_models,
            total=total,
            page=page,
            limit=limit,
        )
    except Exception as e:
        logger.error(f"Failed to list rate card entries: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list rate cards: {str(e)}",
        )


@router.post("/entry", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_rate_card_entry(
    request: Request,
    entry_data: RateCardEntryCreate,
    db: DbSession,
    current_user: User = Depends(require_admin),
):
    """
    Create a single rate card entry.

    Requires admin role with admin mode enabled.
    """
    rate_card_service = get_rate_card_service(request)
    try:
        entry_id = await rate_card_service.repository.create_entry(
            db=db,
            actor_sub=current_user.sub,
            provider=entry_data.provider,
            model_name=entry_data.model_name,
            billing_unit=entry_data.billing_unit,
            flow_direction=entry_data.flow_direction,
            price_per_million=entry_data.price_per_million,
            effective_from=entry_data.effective_from,
        )
        await db.commit()

        # Invalidate cache
        rate_card_service._invalidate_model_cache(entry_data.provider, entry_data.model_name)

        return {"id": entry_id, "status": "created"}
    except Exception as e:
        logger.error(f"Failed to create rate card entry: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create rate card entry: {str(e)}",
        )


@router.post("/model", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_model_rate_card(
    request: Request,
    model_data: RateCardModelCreate,
    db: DbSession,
    current_user: User = Depends(require_admin),
):
    """
    Create all rate card entries for a model at once.

    This is the recommended way to add a new model as it ensures
    all token types are configured together.

    Requires admin role with admin mode enabled.
    """
    rate_card_service = get_rate_card_service(request)
    try:
        entry_ids = await rate_card_service.create_model_rate_card(
            db=db,
            actor_sub=current_user.sub,
            provider=model_data.provider,
            model_name=model_data.model_name,
            pricing=model_data.pricing,
            effective_from=model_data.effective_from,
            model_name_pattern=model_data.model_name_pattern,
        )
        await db.commit()

        return {
            "count": len(entry_ids),
            "ids": entry_ids,
            "status": "created",
            "provider": model_data.provider,
            "model_name": model_data.model_name,
        }

    except Exception as e:
        logger.error(f"Failed to create model rate card: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create model rate card: {str(e)}",
        )


@router.post("/copy", response_model=dict, status_code=status.HTTP_201_CREATED)
async def copy_model_rates(
    db: DbSession,
    current_user: User = Depends(require_admin),
    source_provider: str = Query(...),
    source_model: str = Query(...),
    target_provider: str = Query(...),
    target_model: str = Query(...),
    target_model_pattern: str | None = Query(None),
    effective_from: datetime | None = Query(None),
    request: Request = None,  # type: ignore[assignment]
):
    """
    Copy rate card from one model to another.

    Useful for creating rate cards for similar models or model variants.

    Requires admin role with admin mode enabled.
    """

    rate_card_service = get_rate_card_service(request)

    try:
        entry_ids = await rate_card_service.copy_model_rates(
            db=db,
            actor_sub=current_user.sub,
            source_provider=source_provider,
            source_model=source_model,
            target_provider=target_provider,
            target_model=target_model,
            target_model_pattern=target_model_pattern,
            effective_from=effective_from,
        )
        await db.commit()

        return {
            "count": len(entry_ids),
            "ids": entry_ids,
            "status": "copied",
            "source": f"{source_provider}/{source_model}",
            "target": f"{target_provider}/{target_model}",
        }

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Failed to copy model rates: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to copy model rates: {str(e)}",
        )


@router.post("/expire/{rate_id}", response_model=dict)
async def expire_rate_card_entry(
    rate_id: int,
    db: DbSession,
    current_user: User = Depends(require_admin),
    effective_until: datetime = Query(...),
    request: Request = None,  # type: ignore[assignment]
):
    """
    Expire a rate card entry by setting effective_until date.

    This doesn't delete the entry, allowing for accurate historical cost tracking.

    Requires admin role with admin mode enabled.
    """

    rate_card_service = get_rate_card_service(request)

    try:
        await rate_card_service.repository.expire_rate(
            db=db,
            actor_sub=current_user.sub,
            rate_id=rate_id,
            effective_until=effective_until,
        )
        await db.commit()

        return {"id": rate_id, "status": "expired", "effective_until": effective_until}

    except Exception as e:
        logger.error(f"Failed to expire rate card entry: {e}", exc_info=True)
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to expire rate card entry: {str(e)}",
        )


@router.get("/model/{provider}/{model_name}", response_model=dict)
async def get_model_rates(
    provider: str,
    model_name: str,
    db: DbSession,
    _: User = Depends(require_admin),
    as_of: datetime | None = Query(None),
    request: Request = None,  # type: ignore[assignment]
):
    """
    Get all active rates for a specific model.

    Returns a mapping of billing_unit to price_per_million.

    Requires admin role with admin mode enabled.
    """

    rate_card_service = get_rate_card_service(request)

    try:
        rates = await rate_card_service.repository.get_all_active_rates(
            db=db,
            provider=provider,
            model_name=model_name,
            as_of=as_of,
        )

        if not rates:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No rate cards found for {provider}/{model_name}",
            )

        # Convert Decimal to float for JSON serialization
        rates_serialized = {k: float(v) for k, v in rates.items()}

        return {
            "provider": provider,
            "model_name": model_name,
            "rates": rates_serialized,
            "as_of": as_of or datetime.now(timezone.utc),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get model rates: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get model rates: {str(e)}",
        )
