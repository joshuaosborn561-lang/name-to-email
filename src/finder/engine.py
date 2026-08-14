"""Bulk finder engine: domain-grouped, resumable, cost-capped."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finder.config import Settings
from finder.hunter import (
    HunterAPI,
    HunterBudget,
    HunterDomainContext,
    RankedCandidate,
    build_ranked_candidates,
    match_sighted,
    sighted_candidate,
)
from finder.hunter_cache import resolve_hunter_pattern
from finder.ingest import IngestedRow
from finder.models import DomainPattern, Person, Run, Verification, utcnow
from finder.normalize import NormalizedPerson
from finder.patterns import (
    catchall_is_cached,
    record_hit,
    trusted_pattern,
    upsert_catchall,
)
from finder.permute import generate_candidates
from finder.verifiers.base import Verdict, Verifier

logger = logging.getLogger(__name__)

TERMINAL = {
    "valid",
    "catchall",
    "catchall_pattern",
    "not_found",
    "insufficient_name",
    "personal_domain",
    "error",
}


class CostCeilingReached(Exception):
    pass


@dataclass
class CostTracker:
    ceiling: Decimal | None
    spent: Decimal = Decimal("0")
    stopped: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def allow(self) -> bool:
        async with self._lock:
            if self.stopped:
                return False
            if self.ceiling is not None and self.spent >= self.ceiling:
                self.stopped = True
                return False
            return True

    async def charge(self, amount: Decimal) -> None:
        async with self._lock:
            self.spent += amount or Decimal("0")
            if self.ceiling is not None and self.spent >= self.ceiling:
                self.stopped = True

    def snapshot(self) -> Decimal:
        return self.spent


def _confidence(status: str, domain_is_catchall: bool, pattern_derived: bool) -> str:
    if status == "valid" and not domain_is_catchall:
        return "high"
    if status in {"catchall", "catchall_pattern"} and pattern_derived:
        return "medium"
    return "low"


async def cached_verdict(session: AsyncSession, email: str) -> Verdict | None:
    row = await session.get(Verification, email.lower())
    if row is None:
        return None
    return Verdict(
        email=row.email,
        status=row.verdict,  # type: ignore[arg-type]
        verifier=row.verifier,
        cost_usd=Decimal("0"),
        billed=False,
        from_cache=True,
        raw=row.raw,
    )


async def store_verdict(session: AsyncSession, verdict: Verdict) -> None:
    existing = await session.get(Verification, verdict.email.lower())
    if existing is not None:
        return
    session.add(
        Verification(
            email=verdict.email.lower(),
            verdict=verdict.status,
            verifier=verdict.verifier,
            cost=verdict.cost_usd or Decimal("0"),
            checked_at=utcnow(),
            raw=verdict.raw,
        )
    )


async def verify_email(
    session: AsyncSession,
    verifier: Verifier,
    email: str,
    cost: CostTracker,
) -> Verdict:
    cached = await cached_verdict(session, email)
    if cached is not None:
        return cached
    if not await cost.allow():
        raise CostCeilingReached()
    verdict = await verifier.verify(email)
    await cost.charge(verdict.cost_usd or Decimal("0"))
    if verdict.status != "error":
        await store_verdict(session, verdict)
    return verdict


async def probe_catchall(
    session: AsyncSession,
    verifier: Verifier,
    domain: str,
    settings: Settings,
    cost: CostTracker,
    run_cache: dict[str, bool],
) -> bool:
    if domain in run_cache:
        return run_cache[domain]
    row = await session.get(DomainPattern, domain)
    if catchall_is_cached(row, settings.catchall_ttl_days):
        run_cache[domain] = bool(row and row.is_catchall)
        return run_cache[domain]

    probe = f"{settings.catchall_probe_local}@{domain}"
    verdict = await verify_email(session, verifier, probe, cost)
    is_catchall = verdict.status in {"valid", "catchall"}
    await upsert_catchall(session, domain, is_catchall)
    run_cache[domain] = is_catchall
    return is_catchall


async def _apply_result(
    session: AsyncSession,
    person: Person,
    *,
    status: str,
    email: str | None,
    pattern: str | None,
    attempts: int,
    verifier: str | None,
    domain_is_catchall: bool,
    pattern_derived: bool,
    error_message: str | None = None,
    pattern_source: str | None = None,
    sighted: bool = False,
    hunter_confidence: int | None = None,
) -> None:
    person.status = status
    person.email = email
    person.pattern_used = pattern
    person.attempts = attempts
    person.verifier = verifier
    person.domain_is_catchall = domain_is_catchall
    person.confidence = _confidence(status, domain_is_catchall, pattern_derived)
    person.error_message = error_message
    person.pattern_source = pattern_source
    person.sighted = sighted
    person.hunter_confidence = hunter_confidence


def _normalized_from_person(person: Person) -> NormalizedPerson:
    normalized = NormalizedPerson(
        original_first=person.first,
        original_last=person.last,
        original_domain=person.domain,
        first_variants=_name_variants_from_passthrough(person, "first"),
        last_variants=_name_variants_from_passthrough(person, "last"),
        domain=person.norm_domain,
    )
    if not normalized.first_variants:
        normalized.first_variants = [person.norm_first] if person.norm_first else []
    if not normalized.last_variants:
        normalized.last_variants = [person.norm_last] if person.norm_last else []
    return normalized


def _source_meta(
    ranked: RankedCandidate | None,
    hunter_ctx: HunterDomainContext | None,
) -> tuple[str, bool, int | None]:
    if ranked is None or hunter_ctx is None or hunter_ctx.row is None:
        return "inference", False, None
    if not (ranked.sighted or ranked.from_hunter_pattern):
        return "inference", False, None
    confidence = hunter_ctx.hunter_confidence
    if ranked.sighted:
        for item in hunter_ctx.sighted_emails:
            if str(item.get("email") or "").lower() == ranked.candidate.email.lower():
                if item.get("confidence") is not None:
                    confidence = item.get("confidence")
                break
    return hunter_ctx.pattern_source_label, ranked.sighted, confidence


def _rank_person_candidates(
    normalized: NormalizedPerson,
    settings: Settings,
    pattern_row: DomainPattern | None,
    hunter_ctx: HunterDomainContext | None,
) -> list[RankedCandidate]:
    known = trusted_pattern(pattern_row, settings.pattern_trust_threshold)
    preferred = pattern_row.pattern if pattern_row and pattern_row.pattern else None
    hunter_pattern = hunter_ctx.pattern if hunter_ctx else None
    sighted = None
    if hunter_ctx and hunter_ctx.sighted_emails:
        matched = match_sighted(normalized, hunter_ctx.sighted_emails)
        if matched is not None:
            sighted = sighted_candidate(normalized, matched, settings.patterns)
    return build_ranked_candidates(
        normalized,
        settings.patterns,
        hunter_pattern=hunter_pattern,
        sighted=sighted,
        known_pattern=known,
        preferred_pattern=preferred,
        max_candidates=settings.max_candidates,
    )


async def process_person(
    session: AsyncSession,
    person: Person,
    verifier: Verifier,
    settings: Settings,
    cost: CostTracker,
    domain_catchall: bool,
    pattern_row: DomainPattern | None,
    hunter_ctx: HunterDomainContext | None = None,
    hunter_client: HunterAPI | None = None,
    hunter_budget: HunterBudget | None = None,
    hunter_accept_all: bool = False,
) -> DomainPattern | None:
    if person.status in TERMINAL:
        return pattern_row

    if person.status == "pending" and not person.norm_first:
        pass

    normalized = _normalized_from_person(person)
    known = trusted_pattern(pattern_row, settings.pattern_trust_threshold)
    preferred = pattern_row.pattern if pattern_row and pattern_row.pattern else None
    emit_pattern = known or preferred
    ranked_list = _rank_person_candidates(normalized, settings, pattern_row, hunter_ctx)

    if hunter_accept_all:
        ranked = ranked_list[0] if ranked_list else None
        email = ranked.candidate.email if ranked else None
        pattern = ranked.candidate.pattern if ranked else (hunter_ctx.pattern if hunter_ctx else emit_pattern)
        source, sighted_flag, hconf = _source_meta(ranked, hunter_ctx)
        if source == "inference" and hunter_ctx and hunter_ctx.row is not None:
            source = hunter_ctx.pattern_source_label
            hconf = hunter_ctx.hunter_confidence
        await _apply_result(
            session,
            person,
            status="catchall_pattern",
            email=email,
            pattern=pattern,
            attempts=0,
            verifier=None,
            domain_is_catchall=True,
            pattern_derived=bool(pattern),
            pattern_source=source,
            sighted=sighted_flag,
            hunter_confidence=hconf,
        )
        return pattern_row

    if domain_catchall:
        candidates = generate_candidates(
            normalized,
            settings.patterns,
            known_pattern=emit_pattern or settings.patterns[0],
            max_candidates=1,
        )
        candidate = candidates[0] if candidates else None
        email = candidate.email if candidate else None
        pattern = candidate.pattern if candidate else (emit_pattern or settings.patterns[0])
        await _apply_result(
            session,
            person,
            status="catchall",
            email=email,
            pattern=pattern,
            attempts=0,
            verifier=None,
            domain_is_catchall=True,
            pattern_derived=bool(emit_pattern and emit_pattern == pattern and pattern_row and pattern_row.sample_count),
            pattern_source="inference",
            sighted=False,
            hunter_confidence=None,
        )
        return pattern_row

    attempts = 0
    for ranked in ranked_list:
        candidate = ranked.candidate
        try:
            verdict = await verify_email(session, verifier, candidate.email, cost)
        except CostCeilingReached:
            raise
        attempts += 0 if verdict.from_cache else 1
        person.attempts = attempts
        source, sighted_flag, hconf = _source_meta(ranked, hunter_ctx)

        if verdict.status == "valid":
            await _apply_result(
                session,
                person,
                status="valid",
                email=candidate.email,
                pattern=candidate.pattern,
                attempts=attempts,
                verifier=verdict.verifier,
                domain_is_catchall=False,
                pattern_derived=bool(known) or ranked.from_hunter_pattern,
                pattern_source=source,
                sighted=sighted_flag,
                hunter_confidence=hconf,
            )
            pattern_row = await record_hit(
                session,
                person.norm_domain,
                candidate.pattern,
                trust_threshold=settings.pattern_trust_threshold,
                is_catchall=False,
            )
            return pattern_row

        if verdict.status == "catchall":
            await upsert_catchall(session, person.norm_domain, True)
            await _apply_result(
                session,
                person,
                status="catchall",
                email=candidate.email,
                pattern=candidate.pattern,
                attempts=attempts,
                verifier=verdict.verifier,
                domain_is_catchall=True,
                pattern_derived=bool(known) or ranked.from_hunter_pattern,
                pattern_source=source,
                sighted=sighted_flag,
                hunter_confidence=hconf,
            )
            pattern_row = await session.get(DomainPattern, person.norm_domain)
            return pattern_row

        if verdict.status == "error":
            await _apply_result(
                session,
                person,
                status="error",
                email=None,
                pattern=None,
                attempts=attempts,
                verifier=verdict.verifier,
                domain_is_catchall=False,
                pattern_derived=False,
                error_message=str((verdict.raw or {}).get("error") or "verifier error"),
                pattern_source=source,
                sighted=False,
                hunter_confidence=None,
            )
            return pattern_row

    if await _try_email_finder(
        session,
        person,
        normalized,
        verifier,
        settings,
        cost,
        hunter_ctx,
        hunter_client,
        hunter_budget,
        attempts,
    ):
        return pattern_row

    await _apply_result(
        session,
        person,
        status="not_found",
        email=None,
        pattern=None,
        attempts=attempts,
        verifier=None,
        domain_is_catchall=False,
        pattern_derived=False,
        pattern_source="inference",
        sighted=False,
        hunter_confidence=None,
    )
    return pattern_row


async def _try_email_finder(
    session: AsyncSession,
    person: Person,
    normalized: NormalizedPerson,
    verifier: Verifier,
    settings: Settings,
    cost: CostTracker,
    hunter_ctx: HunterDomainContext | None,
    hunter_client: HunterAPI | None,
    hunter_budget: HunterBudget | None,
    attempts: int,
) -> bool:
    if hunter_ctx is not None and hunter_ctx.pattern:
        return False
    if hunter_client is None or hunter_budget is None:
        return False
    first = person.norm_first or (normalized.first_variants[0] if normalized.first_variants else "")
    last = person.norm_last or (normalized.last_variants[0] if normalized.last_variants else "")
    if not first or not last or not person.norm_domain:
        return False
    if not await hunter_budget.consume():
        return False
    try:
        hit = await hunter_client.email_finder(person.norm_domain, first, last)
    except Exception:
        logger.exception(
            "Hunter email finder failed for %s at %s, continuing with inference",
            first,
            person.norm_domain,
        )
        return False
    if hit is None or not hit.email:
        return False

    source = "hunter"
    if hit.accept_all:
        await _apply_result(
            session,
            person,
            status="catchall_pattern",
            email=hit.email,
            pattern=None,
            attempts=attempts,
            verifier=None,
            domain_is_catchall=True,
            pattern_derived=False,
            pattern_source=source,
            sighted=False,
            hunter_confidence=hit.score,
        )
        return True

    try:
        verdict = await verify_email(session, verifier, hit.email, cost)
    except CostCeilingReached:
        raise
    attempts += 0 if verdict.from_cache else 1
    if verdict.status == "valid":
        await _apply_result(
            session,
            person,
            status="valid",
            email=hit.email,
            pattern=None,
            attempts=attempts,
            verifier=verdict.verifier,
            domain_is_catchall=False,
            pattern_derived=False,
            pattern_source=source,
            sighted=False,
            hunter_confidence=hit.score,
        )
        return True
    if verdict.status == "catchall":
        await upsert_catchall(session, person.norm_domain, True)
        await _apply_result(
            session,
            person,
            status="catchall",
            email=hit.email,
            pattern=None,
            attempts=attempts,
            verifier=verdict.verifier,
            domain_is_catchall=True,
            pattern_derived=False,
            pattern_source=source,
            sighted=False,
            hunter_confidence=hit.score,
        )
        return True
    return False


def _name_variants_from_passthrough(person: Person, which: str) -> list[str]:
    extra = (person.passthrough or {}).get(f"_norm_{which}_variants")
    if isinstance(extra, list) and extra:
        return [str(v) for v in extra if v]
    return []


async def process_domain(
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    domain: str,
    person_ids: list[uuid.UUID],
    verifier: Verifier,
    settings: Settings,
    cost: CostTracker,
    run_catchall: dict[str, bool],
    hunter_client: HunterAPI | None = None,
    hunter_budget: HunterBudget | None = None,
) -> None:
    async with factory() as session:
        try:
            people_result = await session.execute(
                select(Person).where(Person.id.in_(person_ids)).order_by(Person.position, Person.id)
            )
            people = list(people_result.scalars())
            pending = [p for p in people if p.status not in TERMINAL]
            if not pending:
                await session.commit()
                return

            hunter_ctx = await resolve_hunter_pattern(
                session, domain, settings, hunter_client, hunter_budget
            )
            hunter_accept_all = bool(hunter_ctx.accept_all)
            if hunter_accept_all:
                is_catchall = True
                run_catchall[domain] = True
            else:
                is_catchall = await probe_catchall(
                    session, verifier, domain, settings, cost, run_catchall
                )
            pattern_row = await session.get(DomainPattern, domain)

            for person in pending:
                if cost.stopped:
                    break
                pattern_row = await process_person(
                    session,
                    person,
                    verifier,
                    settings,
                    cost,
                    is_catchall,
                    pattern_row,
                    hunter_ctx=hunter_ctx,
                    hunter_client=hunter_client,
                    hunter_budget=hunter_budget,
                    hunter_accept_all=hunter_accept_all,
                )
                if pattern_row and pattern_row.is_catchall:
                    is_catchall = True
                    run_catchall[domain] = True
                await session.flush()

            run = await session.get(Run, run_id)
            if run is not None:
                run.cost_usd = cost.snapshot()
                run.updated_at = utcnow()
            await session.commit()
        except CostCeilingReached:
            run = await session.get(Run, run_id)
            if run is not None:
                run.cost_usd = cost.snapshot()
                run.updated_at = utcnow()
            await session.commit()
            raise
        except Exception:
            await session.rollback()
            raise


async def compute_stats(
    session: AsyncSession,
    run_id: uuid.UUID,
    cost_usd: Decimal,
    hunter_calls: int | None = None,
) -> dict[str, Any]:
    people = list(
        (await session.execute(select(Person).where(Person.run_id == run_id))).scalars()
    )
    rows_in = len(people)
    counts: dict[str, int] = defaultdict(int)
    for p in people:
        counts[p.status] += 1
    hits = counts.get("valid", 0)
    hit_attempts = [p.attempts for p in people if p.status == "valid"]
    domains = {p.norm_domain for p in people if p.norm_domain}
    catchall_domains = {p.norm_domain for p in people if p.domain_is_catchall and p.norm_domain}
    spend = float(cost_usd or 0)
    stats = {
        "rows_in": rows_in,
        "hits": hits,
        "hit_rate": (hits / rows_in) if rows_in else 0.0,
        "avg_attempts_per_hit": (sum(hit_attempts) / len(hit_attempts)) if hit_attempts else 0.0,
        "catchall_domain_share": (len(catchall_domains) / len(domains)) if domains else 0.0,
        "catchall_domains": len(catchall_domains),
        "unique_domains": len(domains),
        "spend_usd": spend,
        "cost_per_valid": (spend / hits) if hits else None,
        "status_counts": dict(counts),
        "attempts_total": sum(p.attempts or 0 for p in people),
        "hunter_calls": int(hunter_calls or 0),
    }
    return stats


async def create_run(
    session: AsyncSession,
    ingested: Iterable[IngestedRow],
    *,
    source: str,
    cost_ceiling: Decimal | None,
) -> Run:
    run = Run(
        status="pending",
        source=source,
        cost_ceiling=cost_ceiling,
        cost_usd=Decimal("0"),
        stats={},
    )
    session.add(run)
    await session.flush()

    for index, row in enumerate(ingested):
        person = row.normalized
        status = "pending"
        if person.insufficient_name:
            status = "insufficient_name"
        elif person.personal_domain:
            status = "personal_domain"
        record = Person(
            run_id=run.id,
            first=row.first,
            last=row.last,
            domain=row.domain,
            norm_first=person.primary_first,
            norm_last=person.primary_last,
            norm_domain=person.domain,
            status=status,
            confidence="low" if status != "pending" else None,
            position=index,
            passthrough={
                **row.passthrough,
                "_norm_first_variants": person.first_variants,
                "_norm_last_variants": person.last_variants,
            },
        )
        if status == "personal_domain":
            record.confidence = "low"
        if status == "insufficient_name":
            record.confidence = "low"
        session.add(record)
    await session.commit()
    await session.refresh(run)
    return run


def _hunter_client(settings: Settings, hunter_client: HunterAPI | None) -> HunterAPI | None:
    if hunter_client is not None:
        return hunter_client
    if not settings.hunter_api_key:
        return None
    from finder.hunter import HunterClient

    return HunterClient(settings.hunter_api_key)


async def execute_run(
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    settings: Settings,
    verifier: Verifier,
    hunter_client: HunterAPI | None = None,
) -> Run:
    async with factory() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise KeyError(f"run {run_id} not found")
        if run.status == "completed":
            return run
        run.status = "running"
        run.updated_at = utcnow()
        await session.commit()
        ceiling = run.cost_ceiling
        already = run.cost_usd or Decimal("0")
        pending = list(
            (
                await session.execute(
                    select(Person.id, Person.norm_domain, Person.status).where(
                        Person.run_id == run_id
                    )
                )
            ).all()
        )

    groups: dict[str, list[uuid.UUID]] = defaultdict(list)
    for person_id, domain, status in pending:
        if status in TERMINAL:
            continue
        groups[domain or ""].append(person_id)

    cost = CostTracker(ceiling=ceiling, spent=already)
    run_catchall: dict[str, bool] = {}
    hunter_budget = HunterBudget(max_calls=settings.max_hunter_calls)
    active_hunter = _hunter_client(settings, hunter_client)
    sem = asyncio.Semaphore(max(1, settings.max_concurrency))

    async def _one(domain: str, ids: list[uuid.UUID]) -> None:
        if not domain:
            async with factory() as session:
                for pid in ids:
                    person = await session.get(Person, pid)
                    if person and person.status not in TERMINAL:
                        person.status = "error"
                        person.error_message = "missing domain"
                await session.commit()
            return
        async with sem:
            if cost.stopped:
                return
            await process_domain(
                factory,
                run_id,
                domain,
                ids,
                verifier,
                settings,
                cost,
                run_catchall,
                hunter_client=active_hunter,
                hunter_budget=hunter_budget,
            )

    results = await asyncio.gather(
        *[_one(domain, ids) for domain, ids in groups.items()],
        return_exceptions=True,
    )
    ceiling_hit = any(isinstance(r, CostCeilingReached) for r in results)
    errors = [r for r in results if isinstance(r, Exception) and not isinstance(r, CostCeilingReached)]
    if errors:
        logger.exception("domain task failed: %s", errors[0])

    async with factory() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        run.cost_usd = cost.snapshot()
        run.stats = await compute_stats(
            session, run_id, run.cost_usd, hunter_calls=hunter_budget.used
        )
        remaining = (
            await session.execute(
                select(func.count()).select_from(Person).where(
                    Person.run_id == run_id, Person.status == "pending"
                )
            )
        ).scalar_one()
        if errors and remaining:
            run.status = "failed"
            run.error = str(errors[0])
        elif ceiling_hit or cost.stopped or remaining:
            run.status = "stopped"
        else:
            run.status = "completed"
        run.updated_at = utcnow()
        await session.commit()
        await session.refresh(run)
        logger.info(
            "run %s %s rows=%s hits=%s spend=%.4f cost_per_valid=%s",
            run.id,
            run.status,
            run.stats.get("rows_in"),
            run.stats.get("hits"),
            float(run.cost_usd or 0),
            run.stats.get("cost_per_valid"),
        )
        return run
