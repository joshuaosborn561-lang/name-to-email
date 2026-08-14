from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from finder.config import Settings
from finder.verifiers.base import Verdict, Verifier
from finder.verifiers.millionverifier import MillionVerifier
from finder.verifiers.mock import MockVerifier
from finder.verifiers.no2bounce import No2BounceVerifier


class WaterfallVerifier(Verifier):
    """MillionVerifier first; No2Bounce only for catch-all or unknown."""

    name = "waterfall"

    def __init__(self, primary: Verifier, secondary: Verifier | None = None) -> None:
        self.primary = primary
        self.secondary = secondary

    async def verify(self, email: str) -> Verdict:
        first = await self.primary.verify(email)
        if first.status not in {"catchall", "unknown"} or self.secondary is None:
            return first
        second = await self.secondary.verify(email)
        combined_cost = (first.cost_usd or Decimal("0")) + (second.cost_usd or Decimal("0"))
        combined_raw = {"primary": first.raw, "secondary": second.raw}
        if second.status in {"valid", "invalid", "catchall"}:
            return Verdict(
                email=email,
                status=second.status,
                verifier=second.verifier,
                cost_usd=combined_cost,
                billed=combined_cost > 0,
                raw=combined_raw,
            )
        return Verdict(
            email=email,
            status=first.status,
            verifier=first.verifier,
            cost_usd=combined_cost,
            billed=combined_cost > 0,
            raw=combined_raw,
        )


def _retry_kwargs(settings: Settings) -> dict[str, Any]:
    rl = settings.yaml_data.get("rate_limit", {})
    return {
        "max_retries": int(rl.get("max_retries", 5)),
        "base_delay": float(rl.get("base_delay_seconds", 0.5)),
        "max_delay": float(rl.get("max_delay_seconds", 30)),
    }


def build_verifier(
    settings: Settings,
    *,
    mock: MockVerifier | None = None,
    client: httpx.AsyncClient | None = None,
) -> Verifier:
    if mock is not None:
        return mock
    order = settings.verifier_order
    built: list[Verifier] = []
    retries = _retry_kwargs(settings)
    for name in order:
        cfg = settings.verifier_cfg(name)
        if name == "millionverifier":
            built.append(
                MillionVerifier(
                    settings.millionverifier_api_key,
                    base_url=cfg.get("base_url", "https://api.millionverifier.com/api/v3/"),
                    timeout_seconds=int(cfg.get("timeout_seconds", 20)),
                    cost_usd=float(cfg.get("cost_usd", 0.00178)),
                    billed_statuses=list(cfg.get("billed_statuses") or ["valid", "invalid"]),
                    client=client,
                    **retries,
                )
            )
        elif name == "no2bounce":
            built.append(
                No2BounceVerifier(
                    settings.no2bounce_api_key,
                    base_url=cfg.get("base_url", "https://api.reacher.email/v0/check_email"),
                    timeout_seconds=int(cfg.get("timeout_seconds", 30)),
                    cost_usd=float(cfg.get("cost_usd", 0.008)),
                    billed_statuses=list(
                        cfg.get("billed_statuses") or ["valid", "invalid", "catchall"]
                    ),
                    client=client,
                    **retries,
                )
            )
        else:
            raise ValueError(f"Unknown verifier '{name}' in config")
    if not built:
        raise ValueError("No verifiers configured")
    if len(built) == 1:
        return built[0]
    return WaterfallVerifier(built[0], built[1])
