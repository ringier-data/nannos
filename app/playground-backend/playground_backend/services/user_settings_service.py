"""User settings service for managing user preferences in PostgreSQL."""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.user import UserSettings

logger = logging.getLogger(__name__)


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
            SELECT user_id, language, timezone, custom_prompt, mcp_tools, created_at, updated_at
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
                )

            return UserSettings(
                user_id=row["user_id"],
                language=row["language"],
                timezone=row["timezone"],
                custom_prompt=row["custom_prompt"],
                mcp_tools=row["mcp_tools"] if row["mcp_tools"] is not None else [],
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
        language: str | None = None,
        timezone_str: str | None = None,
        custom_prompt: str | None = None,
        mcp_tools: list[str] | None = None,
    ) -> UserSettings:
        """Create or update user settings.

        Uses INSERT ... ON CONFLICT for upsert semantics.
        Only updates fields that are provided (not None).

        Args:
            db: The database session
            user_id: The user's ID
            language: New language setting (optional)
            timezone_str: New timezone setting (optional)
            custom_prompt: New custom prompt (optional)
            mcp_tools: New MCP tools list (optional)

        Returns:
            The created or updated settings
        """
        now = datetime.now(tz=timezone.utc)

        # Get current settings to merge with updates
        current = await self.get_settings(db, user_id)

        # Use provided values or fall back to current
        new_language = language if language is not None else current.language
        new_timezone = timezone_str if timezone_str is not None else current.timezone
        new_custom_prompt = custom_prompt if custom_prompt is not None else current.custom_prompt
        new_mcp_tools = mcp_tools if mcp_tools is not None else current.mcp_tools

        query = text("""
            INSERT INTO user_settings (user_id, language, timezone, custom_prompt, mcp_tools, created_at, updated_at)
            VALUES (:user_id, :language, :timezone, :custom_prompt, CAST(:mcp_tools AS jsonb), :now, :now)
            ON CONFLICT (user_id) DO UPDATE SET
                language = EXCLUDED.language,
                timezone = EXCLUDED.timezone,
                custom_prompt = EXCLUDED.custom_prompt,
                mcp_tools = EXCLUDED.mcp_tools,
                updated_at = EXCLUDED.updated_at
            RETURNING user_id, language, timezone, custom_prompt, mcp_tools, created_at, updated_at
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
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except Exception as e:
            logger.error(f"Failed to upsert user settings: {e}")
            raise
