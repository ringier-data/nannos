"""FastAPI dependency for database session management."""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from .connection import get_async_session_factory


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides a database session.

    The session is automatically closed after the request is complete.

    Usage:
        @router.get("/items")
        async def get_items(db: Annotated[AsyncSession, Depends(get_db_session)]):
            result = await db.execute(select(Item))
            return result.scalars().all()
    """
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# Type alias for cleaner dependency injection
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
