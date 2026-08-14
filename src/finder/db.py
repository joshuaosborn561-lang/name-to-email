from __future__ import annotations

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
    inspector = inspect(sync_conn)
    if "people" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("people")}
    dialect = sync_conn.dialect.name
    bool_sql = "BOOLEAN DEFAULT FALSE" if dialect != "sqlite" else "BOOLEAN DEFAULT 0"
    statements: list[str] = []
    if "pattern_source" not in existing:
        statements.append("ALTER TABLE people ADD COLUMN pattern_source VARCHAR(32)")
    if "sighted" not in existing:
        statements.append(f"ALTER TABLE people ADD COLUMN sighted {bool_sql}")
    if "hunter_confidence" not in existing:
        statements.append("ALTER TABLE people ADD COLUMN hunter_confidence INTEGER")
    for sql in statements:
        sync_conn.execute(text(sql))


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_people_columns)


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
