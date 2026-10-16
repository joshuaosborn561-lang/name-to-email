from __future__ import annotations

import asyncio

import httpx
import pytest

from finder.config import Settings
from finder.engine import create_run, execute_run
from finder.ingest import IngestedRow
from finder.limits import (
    CONCURRENCY_CAP,
    DEFAULT_HUNTER_CONCURRENCY,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_VERIFIER_CONCURRENCY,
    VENDOR_HUNTER,
    VENDOR_VERIFIER,
    configure_vendor_limits,
    jittered_delay,
    request_with_backoff,
    vendor_limit,
    vendor_slot,
)
from finder.normalize import normalize_person
from finder.verifiers.mock import MockVerifier


def test_max_concurrency_default(monkeypatch):
    monkeypatch.delenv("MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("VERIFIER_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("HUNTER_MAX_CONCURRENCY", raising=False)
    settings = Settings()
    assert settings.max_concurrency == DEFAULT_MAX_CONCURRENCY
    assert settings.verifier_max_concurrency == DEFAULT_VERIFIER_CONCURRENCY
    assert settings.hunter_max_concurrency == DEFAULT_HUNTER_CONCURRENCY


def test_max_concurrency_from_env(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENCY", "7")
    monkeypatch.setenv("VERIFIER_MAX_CONCURRENCY", "3")
    monkeypatch.setenv("HUNTER_MAX_CONCURRENCY", "2")
    settings = Settings()
    assert settings.max_concurrency == 7
    assert settings.verifier_max_concurrency == 3
    assert settings.hunter_max_concurrency == 2


def test_max_concurrency_capped_at_20(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENCY", "99")
    monkeypatch.setenv("VERIFIER_MAX_CONCURRENCY", "50")
    monkeypatch.setenv("HUNTER_MAX_CONCURRENCY", "40")
    settings = Settings()
    assert settings.max_concurrency == CONCURRENCY_CAP
    assert settings.verifier_max_concurrency == CONCURRENCY_CAP
    assert settings.hunter_max_concurrency == CONCURRENCY_CAP


def test_max_concurrency_floor_is_one(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENCY", "0")
    monkeypatch.setenv("VERIFIER_MAX_CONCURRENCY", "-3")
    settings = Settings()
    assert settings.max_concurrency == 1
    assert settings.verifier_max_concurrency == 1


def test_jittered_delay_is_exponential_with_spread(monkeypatch):
    monkeypatch.setattr("finder.limits.random.random", lambda: 0.5)
    assert jittered_delay(0, 0.5, 30.0) == pytest.approx(0.5)
    assert jittered_delay(1, 0.5, 30.0) == pytest.approx(1.0)
    assert jittered_delay(2, 0.5, 30.0) == pytest.approx(2.0)


def test_jittered_delay_honors_retry_after(monkeypatch):
    monkeypatch.setattr("finder.limits.random.random", lambda: 0.5)
    assert jittered_delay(0, 0.5, 30.0, retry_after=4.0) == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_vendor_slot_caps_in_flight():
    configure_vendor_limits(verifier=2)
    current = 0
    peak = 0

    async def worker() -> None:
        nonlocal current, peak
        async with vendor_slot(VENDOR_VERIFIER):
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.02)
            current -= 1

    await asyncio.gather(*[worker() for _ in range(10)])
    assert peak == 2
    assert vendor_limit(VENDOR_VERIFIER) == 2


@pytest.mark.asyncio
async def test_hunter_and_verifier_slots_are_independent():
    configure_vendor_limits(verifier=1, hunter=1)
    holding_verifier = asyncio.Event()
    holding_hunter = asyncio.Event()
    both = asyncio.Event()

    async def take_verifier() -> None:
        async with vendor_slot(VENDOR_VERIFIER):
            holding_verifier.set()
            await holding_hunter.wait()
            both.set()

    async def take_hunter() -> None:
        async with vendor_slot(VENDOR_HUNTER):
            holding_hunter.set()
            await holding_verifier.wait()
            both.set()

    await asyncio.wait_for(
        asyncio.gather(take_verifier(), take_hunter()),
        timeout=1.0,
    )
    assert both.is_set()


@pytest.mark.asyncio
async def test_429_retries_with_jitter(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("finder.limits.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("finder.limits.random.random", lambda: 0.5)

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await request_with_backoff(
            client,
            "GET",
            "https://verifier.example/check",
            max_retries=5,
            base_delay=0.5,
            max_delay=30.0,
            vendor=VENDOR_VERIFIER,
        )
    assert response.status_code == 200
    assert attempts["n"] == 3
    assert sleeps == [pytest.approx(0.5), pytest.approx(1.0)]


@pytest.mark.asyncio
async def test_429_exhausted_returns_last_response(monkeypatch):
    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("finder.limits.asyncio.sleep", fake_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": "rate_limit"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await request_with_backoff(
            client,
            "GET",
            "https://verifier.example/check",
            max_retries=2,
            base_delay=0.01,
            vendor=VENDOR_VERIFIER,
        )
    assert response.status_code == 429
    assert calls["n"] == 3


async def test_execute_run_uses_settings_max_concurrency(db, settings: Settings, monkeypatch):
    settings.max_concurrency = 3
    seen: list[int] = []
    real = asyncio.Semaphore

    def spy(value: int) -> asyncio.Semaphore:
        seen.append(value)
        return real(value)

    monkeypatch.setattr("finder.engine.asyncio.Semaphore", spy)
    person = normalize_person(
        "Ada",
        "Lovelace",
        "example.test",
        suffixes=settings.suffixes,
        credentials=settings.credentials,
        personal_domains=settings.personal_domains,
    )
    row = IngestedRow(
        first="Ada",
        last="Lovelace",
        domain=person.domain,
        normalized=person,
        passthrough={},
    )
    async with db() as session:
        run = await create_run(session, [row], source="test", cost_ceiling=None)
        run_id = run.id
    await execute_run(db, run_id, settings, MockVerifier(valid=set()))
    assert seen == [3]
