from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from finder.verifiers.base import Verdict, Verifier, request_with_backoff


_RESULT_MAP = {
    "ok": "valid",
    "valid": "valid",
    "catch_all": "catchall",
    "catchall": "catchall",
    "unknown": "unknown",
    "error": "error",
    "disposable": "invalid",
    "invalid": "invalid",
}


class MillionVerifier(Verifier):
    name = "millionverifier"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.millionverifier.com/api/v3/",
        timeout_seconds: int = 20,
        cost_usd: float = 0.00178,
        billed_statuses: list[str] | None = None,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 5,
        base_delay: float = 0.5,
        max_delay: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.unit_cost = Decimal(str(cost_usd))
        self.billed_statuses = set(billed_statuses or ["valid", "invalid"])
        self._client = client
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay

    async def verify(self, email: str) -> Verdict:
        if not self.api_key:
            return Verdict(email=email, status="error", verifier=self.name, raw={"error": "missing api key"})
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds + 5)
        owns = self._client is None
        try:
            response = await request_with_backoff(
                client,
                "GET",
                self.base_url,
                params={
                    "api": self.api_key,
                    "email": email,
                    "timeout": self.timeout_seconds,
                },
                max_retries=self.max_retries,
                base_delay=self.base_delay,
                max_delay=self.max_delay,
            )
        except httpx.HTTPError as exc:
            return Verdict(
                email=email,
                status="error",
                verifier=self.name,
                raw={"error": str(exc)},
            )
        finally:
            if owns:
                await client.aclose()

        try:
            payload: dict[str, Any] = response.json()
        except ValueError:
            payload = {"error": response.text, "status_code": response.status_code}

        if response.status_code >= 400:
            return Verdict(
                email=email,
                status="error",
                verifier=self.name,
                raw=payload,
            )

        result = str(payload.get("result") or payload.get("error") or "unknown").lower()
        status = _RESULT_MAP.get(result, "unknown")
        billed = status in self.billed_statuses
        return Verdict(
            email=email,
            status=status,  # type: ignore[arg-type]
            verifier=self.name,
            cost_usd=self.unit_cost if billed else Decimal("0"),
            billed=billed,
            raw=payload,
        )
