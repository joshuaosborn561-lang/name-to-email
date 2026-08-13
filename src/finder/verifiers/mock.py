from __future__ import annotations

from decimal import Decimal

from finder.verifiers.base import Verdict, VerdictStatus, Verifier


class MockVerifier(Verifier):
    """Deterministic verifier for tests and dry runs.

    `valid` is a set of addresses that exist.
    `catchall_domains` accept every address (including the catch-all probe).
    `unknown` forces an unknown verdict for specific addresses.
    """

    name = "mock"

    def __init__(
        self,
        valid: set[str] | None = None,
        catchall_domains: set[str] | None = None,
        unknown: set[str] | None = None,
        invalid: set[str] | None = None,
        cost_usd: float = 0.00178,
        billed_statuses: list[str] | None = None,
        name: str = "mock",
    ) -> None:
        self.valid = {e.lower() for e in (valid or set())}
        self.catchall_domains = {d.lower() for d in (catchall_domains or set())}
        self.unknown = {e.lower() for e in (unknown or set())}
        self.invalid = {e.lower() for e in (invalid or set())}
        self.unit_cost = Decimal(str(cost_usd))
        self.billed_statuses = set(billed_statuses or ["valid", "invalid"])
        self.name = name
        self.calls: list[str] = []

    def _status(self, email: str) -> VerdictStatus:
        local, _, domain = email.lower().partition("@")
        if email.lower() in self.unknown:
            return "unknown"
        if email.lower() in self.invalid:
            return "invalid"
        if domain in self.catchall_domains:
            return "catchall"
        if email.lower() in self.valid:
            return "valid"
        return "invalid"

    async def verify(self, email: str) -> Verdict:
        self.calls.append(email.lower())
        status = self._status(email)
        billed = status in self.billed_statuses
        return Verdict(
            email=email,
            status=status,
            verifier=self.name,
            cost_usd=self.unit_cost if billed else Decimal("0"),
            billed=billed,
            raw={"mock": True, "status": status},
        )
