from __future__ import annotations

from pathlib import Path

import pytest

from finder.config import Settings
from finder.db import create_engine, init_db, session_factory


@pytest.fixture
def settings() -> Settings:
    return Settings.load()


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
