from __future__ import annotations

from pathlib import Path

import pytest

from finder.config import Settings
from finder.db import create_engine, init_db, session_factory


@pytest.fixture(autouse=True)
def _no_paid_hunter(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "")
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "")


@pytest.fixture
def settings() -> Settings:
    loaded = Settings.load()
    loaded.hunter_api_key = ""
    loaded.supabase_url = ""
    loaded.supabase_service_role_key = ""
    loaded.max_hunter_calls = 200
    return loaded


@pytest.fixture
async def db(tmp_path: Path, settings: Settings):
    url = f"sqlite+aiosqlite:///{tmp_path}/finder.db"
    engine = create_engine(url)
    await init_db(engine)
    factory = session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()
