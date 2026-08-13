import pytest
from httpx import ASGITransport, AsyncClient

from finder.api import app
from finder.config import Settings
from finder.db import create_engine, init_db, session_factory
from finder.verifiers.mock import MockVerifier
import finder.api as api_mod


@pytest.fixture
async def api_client(tmp_path, monkeypatch):
    settings = Settings.load()
    settings.api_key = "test-key"
    settings.database_url = f"sqlite+aiosqlite:///{tmp_path}/api.db"
    engine = create_engine(settings.async_database_url)
    await init_db(engine)
    factory = session_factory(engine)

    mock = MockVerifier(
        valid={"jane.doe@acme.test"},
        catchall_domains=set(),
    )

    api_mod.settings = settings
    api_mod.engine = engine
    api_mod.factory = factory

    def _build(s, mock=None, client=None):
        return mock or MockVerifier(valid={"jane.doe@acme.test"})

    monkeypatch.setattr(api_mod, "build_verifier", lambda s, mock=None, client=None: MockVerifier(valid={"jane.doe@acme.test"}))
    monkeypatch.setattr("finder.verifiers.waterfall.build_verifier", _build)

    try:
        transport = ASGITransport(app=app, lifespan="off")
    except TypeError:
        transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, mock
    await engine.dispose()


async def test_health_open(api_client):
    client, _ = api_client
    res = await client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


async def test_auth_required(api_client):
    client, _ = api_client
    res = await client.post("/verify", json={"first": "Jane", "last": "Doe", "domain": "acme.test"})
    assert res.status_code == 401


async def test_verify_and_run(api_client):
    client, _ = api_client
    headers = {"X-API-Key": "test-key"}
    res = await client.post(
        "/verify",
        json={"first": "Jane", "last": "Doe", "domain": "acme.test"},
        headers=headers,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "valid"
    assert body["email"] == "jane.doe@acme.test"
    assert body["confidence"] == "high"

    res = await client.post(
        "/runs",
        json={
            "people": [
                {"first": "Jane", "last": "Doe", "domain": "acme.test"},
                {"first": "Oliver", "last": "C.", "website": "acme.test"},
            ],
            "max_cost": 5,
        },
        headers=headers,
    )
    assert res.status_code == 200
    run_id = res.json()["run_id"]
    # Background task may still be running; poll briefly.
    for _ in range(50):
        status = await client.get(f"/runs/{run_id}", headers=headers)
        if status.json()["status"] in {"completed", "stopped", "failed"}:
            break
        import asyncio

        await asyncio.sleep(0.05)
    export = await client.get(f"/runs/{run_id}/export", params={"segment": "valid"}, headers=headers)
    assert export.status_code == 200
    assert "jane.doe@acme.test" in export.text
    unresolved = await client.get(
        f"/runs/{run_id}/export", params={"segment": "unresolved"}, headers=headers
    )
    assert "insufficient_name" in unresolved.text
