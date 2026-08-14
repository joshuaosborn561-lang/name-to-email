from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from finder.models import Base

logger = logging.getLogger(__name__)


def create_engine(database_url: str) -> AsyncEngine:
    kwargs: dict = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs = {
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        }
    return create_async_engine(database_url, **kwargs)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def _ensure_people_columns(sync_conn) -> None:
    dialect = sync_conn.dialect.name
    if dialect == "postgresql":
        sync_conn.execute(text("SET lock_timeout = '15s'"))
        statements = [
            "ALTER TABLE people ADD COLUMN IF NOT EXISTS pattern_source VARCHAR(32)",
            "ALTER TABLE people ADD COLUMN IF NOT EXISTS sighted BOOLEAN",
            "ALTER TABLE people ADD COLUMN IF NOT EXISTS hunter_confidence INTEGER",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS hunter_calls INTEGER DEFAULT 0",
        ]
    else:
        inspector = inspect(sync_conn)
        if "people" not in inspector.get_table_names():
            return
        existing = {col["name"] for col in inspector.get_columns("people")}
        statements = []
        if "pattern_source" not in existing:
            statements.append("ALTER TABLE people ADD COLUMN pattern_source VARCHAR(32)")
        if "sighted" not in existing:
            statements.append("ALTER TABLE people ADD COLUMN sighted BOOLEAN DEFAULT 0")
        if "hunter_confidence" not in existing:
            statements.append("ALTER TABLE people ADD COLUMN hunter_confidence INTEGER")
        if "runs" in inspector.get_table_names():
            run_cols = {col["name"] for col in inspector.get_columns("runs")}
            if "hunter_calls" not in run_cols:
                statements.append("ALTER TABLE runs ADD COLUMN hunter_calls INTEGER DEFAULT 0")
    for sql in statements:
        sync_conn.execute(text(sql))


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(_ensure_people_columns)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            logger.warning("people column migrate attempt %s failed: %s", attempt + 1, exc)
            await asyncio.sleep(2)
    if last_error is not None:
        logger.warning("people column migrate did not finish, continuing startup")


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
