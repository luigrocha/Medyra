"""Sesión async de SQLAlchemy."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from medintel.config import settings

engine = create_async_engine(settings.database_url, echo=False, pool_size=5, max_overflow=10)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise
