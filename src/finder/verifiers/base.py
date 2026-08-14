from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

VerdictStatus = Literal["valid", "invalid", "catchall", "unknown", "error"]


@dataclass
class Verdict:
    email: str
    status: VerdictStatus
    verifier: str
    cost_usd: Decimal = Decimal("0")
    raw: dict[str, Any] | None = None
    billed: bool = False
    from_cache: bool = False


class Verifier(ABC):
    name: str

    @abstractmethod
    async def verify(self, email: str) -> Verdict:
        raise NotImplementedError


async def request_with_backoff(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 30.0,
    **kwargs: Any,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = await client.request(method, url, **kwargs)
            if response.status_code in {429, 500, 502, 503, 504}:
                if attempt >= max_retries:
                    return response
                delay = min(max_delay, base_delay * (2**attempt))
                delay = delay * (0.5 + random.random())
                logger.warning(
                    "verifier HTTP %s on %s, retrying in %.2fs",
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
            delay = min(max_delay, base_delay * (2**attempt))
            delay = delay * (0.5 + random.random())
            logger.warning("verifier transport error %s, retrying in %.2fs", exc, delay)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
