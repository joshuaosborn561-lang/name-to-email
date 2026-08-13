from decimal import Decimal

import httpx
import pytest

from finder.verifiers.millionverifier import MillionVerifier
from finder.verifiers.no2bounce import No2BounceVerifier


@pytest.mark.asyncio
async def test_millionverifier_maps_results_and_skips_risky_billing():
    def handler(request: httpx.Request) -> httpx.Response:
        email = str(request.url.params.get("email"))
        if "nope" in email:
            return httpx.Response(200, json={"result": "catch_all", "email": email})
        if "bad" in email:
            return httpx.Response(200, json={"result": "invalid", "email": email})
        return httpx.Response(200, json={"result": "ok", "email": email})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        mv = MillionVerifier("k", client=client, cost_usd=0.00178)
        ok = await mv.verify("jane@acme.test")
        ca = await mv.verify("zzq-nope@acme.test")
        bad = await mv.verify("bad@acme.test")
    assert ok.status == "valid" and ok.billed and ok.cost_usd == Decimal("0.00178")
    assert ca.status == "catchall" and not ca.billed and ca.cost_usd == Decimal("0")
    assert bad.status == "invalid" and bad.billed


@pytest.mark.asyncio
async def test_no2bounce_maps_reachable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"is_reachable": "risky", "smtp": {"is_catch_all": True}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        n2 = No2BounceVerifier("k", client=client)
        verdict = await n2.verify("x@y.test")
    assert verdict.status == "catchall"
    assert verdict.verifier == "no2bounce"
