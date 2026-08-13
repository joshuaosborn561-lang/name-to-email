"""Holdout evaluation: the test that decides if permutation+verify beats a vendor."""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finder.config import Settings
from finder.engine import create_run, execute_run
from finder.ingest import IngestResult, IngestedRow, ingest_csv_path
from finder.models import Person
from finder.normalize import NormalizedPerson
from finder.patterns import person_from_seed_fields, seed_from_known
from finder.verifiers.base import Verifier
from sqlalchemy import select


@dataclass
class EvalReport:
    holdout: int
    recovered: int
    recovery_rate: float
    avg_attempts_per_hit: float
    catchall_domain_share: float
    spend_usd: float
    cost_per_valid: float | None
    vendor_cost_per_email: float
    beats_vendor: bool | None
    different_valid: int
    status_counts: dict[str, int]
    notes: list[str]
    run_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "holdout": self.holdout,
            "recovered": self.recovered,
            "recovery_rate": self.recovery_rate,
            "avg_attempts_per_hit": self.avg_attempts_per_hit,
            "catchall_domain_share": self.catchall_domain_share,
            "spend_usd": self.spend_usd,
            "cost_per_valid": self.cost_per_valid,
            "vendor_cost_per_email": self.vendor_cost_per_email,
            "beats_vendor": self.beats_vendor,
            "different_valid": self.different_valid,
            "status_counts": self.status_counts,
            "notes": self.notes,
            "run_id": self.run_id,
        }

    def render(self) -> str:
        lines = [
            "Holdout evaluation",
            "==================",
            f"Holdout size:              {self.holdout}",
            f"Recovery rate:             {self.recovery_rate:.1%}  ({self.recovered}/{self.holdout})",
            f"Average attempts per hit:  {self.avg_attempts_per_hit:.2f}",
            f"Catch-all domain share:    {self.catchall_domain_share:.1%}",
            f"Spend:                     ${self.spend_usd:.4f}",
            f"Cost per valid email:      {self._cost_str()}",
            f"Vendor benchmark:          ${self.vendor_cost_per_email:.2f}",
        ]
        if self.beats_vendor is True:
            lines.append("Verdict:                   BEATS vendor cost")
        elif self.beats_vendor is False:
            lines.append(
                "Verdict:                   DOES NOT beat vendor cost. "
                "Permutation + verify is not cheaper on this corpus."
            )
        else:
            lines.append("Verdict:                   no valid emails recovered; cannot compare cost")
        if self.different_valid:
            lines.append(
                f"Note: {self.different_valid} holdout rows resolved to a different valid address "
                "than the known seed email."
            )
        lines.append(
            "Catch-all share is the population where this approach cannot confirm "
            "anything; a paid finder still has a real edge there."
        )
        for note in self.notes:
            lines.append(f"Note: {note}")
        return "\n".join(lines)

    def _cost_str(self) -> str:
        if self.cost_per_valid is None:
            return "n/a"
        return f"${self.cost_per_valid:.4f}"


async def run_eval(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    verifier: Verifier,
    csv_path: Path,
    *,
    holdout_size: int | None = None,
    seed: int = 42,
    max_cost: float | None = None,
) -> EvalReport:
    ingested = ingest_csv_path(
        csv_path,
        aliases=settings.column_aliases,
        suffixes=settings.suffixes,
        credentials=settings.credentials,
        personal_domains=settings.personal_domains,
    )
    known: list[IngestedRow] = [r for r in ingested.rows if r.source_email]
    if not known:
        raise ValueError("eval CSV must include an email column of known-good addresses")

    holdout_n = holdout_size or settings.holdout_size
    if holdout_n > len(known):
        raise ValueError(f"holdout {holdout_n} is larger than corpus {len(known)}")

    shuffled = list(known)
    random.Random(seed).shuffle(shuffled)
    holdout = shuffled[:holdout_n]
    rest = shuffled[holdout_n:]

    seed_pairs: list[tuple[str, NormalizedPerson]] = []
    for row in rest:
        person = person_from_seed_fields(
            row.first, row.last, row.source_email or "", settings
        )
        seed_pairs.append((row.source_email or "", person))

    async with factory() as session:
        seed_stats = await seed_from_known(session, seed_pairs, settings)

    holdout_ingest = IngestResult(rows=holdout, raw_count=len(holdout))
    async with factory() as session:
        run = await create_run(
            session,
            holdout_ingest.rows,
            source=f"eval:{csv_path.name}",
            cost_ceiling=Decimal(str(max_cost if max_cost is not None else settings.default_cost_ceiling)),
        )
        run_id = run.id

    run = await execute_run(factory, run_id, settings, verifier)

    async with factory() as session:
        people = list(
            (await session.execute(select(Person).where(Person.run_id == run_id))).scalars()
        )

    expected = {
        (r.normalized.primary_first, r.normalized.primary_last, r.normalized.domain): (
            r.source_email or ""
        ).lower()
        for r in holdout
    }
    recovered = 0
    different_valid = 0
    for person in people:
        key = (person.norm_first, person.norm_last, person.norm_domain)
        want = expected.get(key, "")
        got = (person.email or "").lower()
        if got and want and got == want:
            recovered += 1
        elif person.status == "valid" and got and got != want:
            different_valid += 1

    stats = run.stats or {}
    cost_per = stats.get("cost_per_valid")
    vendor = settings.vendor_cost_per_email
    beats: bool | None
    if cost_per is None:
        beats = None
    else:
        beats = float(cost_per) < vendor

    notes = [
        f"seeded {seed_stats['derived']} patterns from {len(rest)} known emails "
        f"({seed_stats['skipped']} skipped)",
    ]
    if stats.get("catchall_domain_share", 0) > 0:
        notes.append(
            f"{stats.get('catchall_domain_share', 0):.0%} of holdout domains are catch-all; "
            "those rows cannot be confirmed by SMTP-style verification."
        )

    return EvalReport(
        holdout=holdout_n,
        recovered=recovered,
        recovery_rate=recovered / holdout_n if holdout_n else 0.0,
        avg_attempts_per_hit=float(stats.get("avg_attempts_per_hit") or 0.0),
        catchall_domain_share=float(stats.get("catchall_domain_share") or 0.0),
        spend_usd=float(stats.get("spend_usd") or 0.0),
        cost_per_valid=float(cost_per) if cost_per is not None else None,
        vendor_cost_per_email=vendor,
        beats_vendor=beats,
        different_valid=different_valid,
        status_counts=dict(stats.get("status_counts") or {}),
        notes=notes,
        run_id=str(run_id),
    )
