"""Process-wide vendor concurrency gates and jittered HTTP retry.

Domain fan-out (MAX_CONCURRENCY) can be higher than any single vendor allows.
Each vendor HTTP call takes a named semaphore so extra in-flight domains cannot
stampede Hunter, verifiers, or later vendors. 429/5xx retries use exponential
backoff with jitter; the vendor slot is held across the retry loop so a burst
of 429s does not immediately admit more callers.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

logger = logging.getLogger(__name__)

VENDOR_VERIFIER = "verifier"
VENDOR_HUNTER = "hunter"

CONCURRENCY_CAP = 20
DEFAULT_MAX_CONCURRENCY = 10
DEFAULT_VERIFIER_CONCURRENCY = 8
DEFAULT_HUNTER_CONCURRENCY = 5

_DEFAULTS: dict[str, int] = {
    VENDOR_VERIFIER: DEFAULT_VERIFIER_CONCURRENCY,
    VENDOR_HUNTER: DEFAULT_HUNTER_CONCURRENCY,
}

# loop id -> vendor name -> semaphore (Semaphore objects are loop-bound)
_semaphores: dict[int, dict[str, asyncio.Semaphore]] = {}
_limits: dict[str, int] = dict(_DEFAULTS)


def clamp_concurrency(value: object, *, default: int, cap: int = CONCURRENCY_CAP) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, cap))


def reset_vendor_limits() -> None:
    """Restore defaults and drop cached semaphores. Used by tests."""
    _limits.clear()
    _limits.update(_DEFAULTS)
    _semaphores.clear()


def configure_vendor_limits(**limits: int) -> None:
    """Set per-vendor in-flight caps. Unknown names are accepted for new vendors."""
    for name, raw in limits.items():
        _limits[name] = clamp_concurrency(raw, default=_DEFAULTS.get(name, 1))
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        _semaphores.clear()
        return
    _semaphores.pop(loop_id, None)


def configure_from_settings(settings: Any) -> None:
    configure_vendor_limits(
        verifier=int(getattr(settings, "verifier_max_concurrency", DEFAULT_VERIFIER_CONCURRENCY)),
        hunter=int(getattr(settings, "hunter_max_concurrency", DEFAULT_HUNTER_CONCURRENCY)),
    )


def vendor_limit(name: str) -> int:
    return max(1, _limits.get(name, _DEFAULTS.get(name, 1)))


def vendor_semaphore(name: str) -> asyncio.Semaphore:
    loop_id = id(asyncio.get_running_loop())
    bucket = _semaphores.setdefault(loop_id, {})
    if name not in bucket:
        bucket[name] = asyncio.Semaphore(vendor_limit(name))
    return bucket[name]


@asynccontextmanager
async def vendor_slot(name: str) -> AsyncIterator[None]:
    async with vendor_semaphore(name):
        yield


def jittered_delay(
    attempt: int,
    base_delay: float,
    max_delay: float,
    *,
    retry_after: float | None = None,
) -> float:
    """Exponential backoff with full jitter in [0.5x, 1.5x]."""
    if retry_after is not None and retry_after > 0:
        delay = retry_after
    else:
        delay = min(max_delay, base_delay * (2**attempt))
    return min(max_delay, delay * (0.5 + random.random()))


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


async def request_with_backoff(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    vendor: str = VENDOR_VERIFIER,
    **kwargs: Any,
) -> httpx.Response:
    async with vendor_slot(vendor):
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                response = await client.request(method, url, **kwargs)
                if response.status_code in {429, 500, 502, 503, 504}:
                    if attempt >= max_retries:
                        return response
                    delay = jittered_delay(
                        attempt,
                        base_delay,
                        max_delay,
                        retry_after=_retry_after_seconds(response),
                    )
                    logger.warning(
                        "%s HTTP %s on %s, retrying in %.2fs",
                        vendor,
                        response.status_code,
                        url,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                return response
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt >= max_retries:
                    raise
                delay = jittered_delay(attempt, base_delay, max_delay)
                logger.warning("%s transport error %s, retrying in %.2fs", vendor, exc, delay)
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc
