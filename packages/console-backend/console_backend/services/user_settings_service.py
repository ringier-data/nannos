"""User settings service for managing user preferences in PostgreSQL."""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.user import OrchestratorThinkingLevel, UserSettings

logger = logging.getLogger(__name__)

# Sentinel value to distinguish "no change" from "set to None"
_UNSET: Any = object()


class UserSettingsService:
    """Manages user settings in PostgreSQL."""

    async def get_settings(self, db: AsyncSession, user_id: str) -> UserSettings:
        """Retrieve user settings by user ID.

        Returns default settings if no record exists.

        Args:
            db: The database session
            user_id: The user's ID

        Returns:
            The user settings (defaults if not found)
        """
        query = text("""
            SELECT user_id, language, timezone, custom_prompt, mcp_tools, 
                   preferred_model, enable_thinking, thinking_level,
                   phone_number_override,
                   created_at, updated_at
            FROM user_settings
            WHERE user_id = :user_id
        """)

        try:
            result = await db.execute(query, {"user_id": user_id})
            row = result.mappings().first()

            if row is None:
                logger.debug(f"No settings found for user {user_id}, returning defaults")
                return UserSettings(
                    user_id=user_id,
                    language="en",
                    timezone="Europe/Zurich",
                    custom_prompt=None,
                    mcp_tools=[],
                    preferred_model=None,
                    enable_thinking=None,
                    thinking_level=None,
                    phone_number_override=None,
                )

            return UserSettings(
                user_id=row["user_id"],
                language=row["language"],
                timezone=row["timezone"],
                custom_prompt=row["custom_prompt"],
                mcp_tools=row["mcp_tools"] if row["mcp_tools"] is not None else [],
                preferred_model=row["preferred_model"],
                enable_thinking=row["enable_thinking"],
                thinking_level=row["thinking_level"],
                phone_number_override=row["phone_number_override"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except Exception as e:
            logger.error(f"Failed to get user settings: {e}")
            raise

    async def upsert_settings(
        self,
        db: AsyncSession,
        user_id: str,
        language: str | None = _UNSET,
        timezone_str: str | None = _UNSET,
        custom_prompt: str | None = _UNSET,
        mcp_tools: list[str] | None = _UNSET,
        preferred_model: str | None = _UNSET,
        enable_thinking: bool | None = _UNSET,
        thinking_level: OrchestratorThinkingLevel | None = _UNSET,
        phone_number_override: str | None = _UNSET,
    ) -> UserSettings:
        """Create or update user settings.

        Uses INSERT ... ON CONFLICT for upsert semantics.
        Only updates fields that are provided (not _UNSET).
        Pass None to explicitly clear a field.

        Args:
            db: The database session
            user_id: The user's ID
            language: New language setting (optional, pass None to clear)
            timezone_str: New timezone setting (optional, pass None to clear)
            custom_prompt: New custom prompt (optional, pass None to clear)
            mcp_tools: New MCP tools list (optional, pass None to clear)
            preferred_model: Preferred model (optional, pass None to use default)
            enable_thinking: Enable thinking mode (optional, pass None to clear)
            thinking_level: Thinking level (optional, pass None to clear)

        Returns:
            The created or updated settings
        """
        now = datetime.now(tz=timezone.utc)

        # Get current settings to merge with updates
        current = await self.get_settings(db, user_id)

        # Use provided values or fall back to current (_UNSET means "no change")
        new_language = language if language is not _UNSET else current.language
        new_timezone = timezone_str if timezone_str is not _UNSET else current.timezone
        new_custom_prompt = custom_prompt if custom_prompt is not _UNSET else current.custom_prompt
        new_mcp_tools = mcp_tools if mcp_tools is not _UNSET else current.mcp_tools
        new_preferred_model = preferred_model if preferred_model is not _UNSET else current.preferred_model
        new_enable_thinking = enable_thinking if enable_thinking is not _UNSET else current.enable_thinking
        new_thinking_level = thinking_level if thinking_level is not _UNSET else current.thinking_level
        new_phone_number_override = (
            phone_number_override if phone_number_override is not _UNSET else current.phone_number_override
        )

        query = text("""
            INSERT INTO user_settings (user_id, language, timezone, custom_prompt, mcp_tools, 
                                      preferred_model, enable_thinking, thinking_level,
                                      phone_number_override,
                                      created_at, updated_at)
            VALUES (:user_id, :language, :timezone, :custom_prompt, CAST(:mcp_tools AS jsonb), 
                    :preferred_model, :enable_thinking, :thinking_level,
                    :phone_number_override,
                    :now, :now)
            ON CONFLICT (user_id) DO UPDATE SET
                language = EXCLUDED.language,
                timezone = EXCLUDED.timezone,
                custom_prompt = EXCLUDED.custom_prompt,
                mcp_tools = EXCLUDED.mcp_tools,
                preferred_model = EXCLUDED.preferred_model,
                enable_thinking = EXCLUDED.enable_thinking,
                thinking_level = EXCLUDED.thinking_level,
                phone_number_override = EXCLUDED.phone_number_override,
                updated_at = EXCLUDED.updated_at
            RETURNING user_id, language, timezone, custom_prompt, mcp_tools, 
                      preferred_model, enable_thinking, thinking_level,
                      phone_number_override,
                      created_at, updated_at
        """)

        try:
            result = await db.execute(
                query,
                {
                    "user_id": user_id,
                    "language": new_language,
                    "timezone": new_timezone,
                    "custom_prompt": new_custom_prompt,
                    "mcp_tools": json.dumps(new_mcp_tools),
                    "preferred_model": new_preferred_model,
                    "enable_thinking": new_enable_thinking,
                    "thinking_level": new_thinking_level,
                    "phone_number_override": new_phone_number_override,
                    "now": now,
                },
            )
            row = result.mappings().first()
            logger.info(f"Upserted settings for user: {user_id}")

            if row is None:
                raise RuntimeError(f"upsert returned None for user settings {user_id}")

            return UserSettings(
                user_id=row["user_id"],
                language=row["language"],
                timezone=row["timezone"],
                custom_prompt=row["custom_prompt"],
                mcp_tools=row["mcp_tools"] if row["mcp_tools"] is not None else [],
                preferred_model=row["preferred_model"],
                enable_thinking=row["enable_thinking"],
                thinking_level=row["thinking_level"],
                phone_number_override=row["phone_number_override"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except Exception as e:
            logger.error(f"Failed to upsert user settings: {e}")
            raise
