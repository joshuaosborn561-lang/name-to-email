from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from finder.limits import request_with_backoff

__all__ = [
    "Verdict",
    "VerdictStatus",
    "Verifier",
    "request_with_backoff",
]

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
