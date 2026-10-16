from sqlalchemy.pool import StaticPool

from finder.db import DB_MAX_OVERFLOW, DB_POOL_SIZE, create_engine


def test_sqlite_engine_uses_static_pool():
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    try:
        assert isinstance(engine.sync_engine.pool, StaticPool)
    finally:
        engine.sync_engine.dispose()


def test_postgres_engine_caps_pool_without_overflow():
    engine = create_engine("postgresql+asyncpg://finder:finder@localhost:5432/finder")
    try:
        pool = engine.sync_engine.pool
        assert pool.size() == DB_POOL_SIZE
        assert pool.size() == 5
        assert pool._max_overflow == DB_MAX_OVERFLOW
        assert pool._max_overflow == 0
        assert pool._pre_ping is True
    finally:
        engine.sync_engine.dispose()
