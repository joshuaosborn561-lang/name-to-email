"""Per-domain pattern memory: seed from known-good emails and learn at runtime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finder.config import Settings
from finder.models import DomainPattern, utcnow
from finder.normalize import NormalizedPerson, normalize_person
from finder.permute import infer_pattern


def _fresh(ts: datetime | None, ttl_days: int) -> bool:
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return utcnow() - ts < timedelta(days=ttl_days)


async def get_pattern(session: AsyncSession, domain: str) -> DomainPattern | None:
    return await session.get(DomainPattern, domain)


async def upsert_catchall(
    session: AsyncSession,
    domain: str,
    is_catchall: bool,
) -> DomainPattern:
    row = await get_pattern(session, domain)
    now = utcnow()
    if row is None:
        row = DomainPattern(
            domain=domain,
            is_catchall=is_catchall,
            catchall_checked_at=now,
            last_verified_at=now,
        )
        session.add(row)
    else:
        row.is_catchall = is_catchall
        row.catchall_checked_at = now
        row.last_verified_at = now
    await session.flush()
    return row


def catchall_is_cached(row: DomainPattern | None, ttl_days: int) -> bool:
    if row is None:
        return False
    return _fresh(row.catchall_checked_at, ttl_days)


async def record_hit(
    session: AsyncSession,
    domain: str,
    pattern: str,
    *,
    trust_threshold: int,
    is_catchall: bool = False,
) -> DomainPattern:
    """Write a runtime (or seed) success back to domain_patterns.

    Matching confirmations increment confidence. A contradiction decrements
    confidence and re-derives the stored pattern instead of silently overwriting.
    """
    row = await get_pattern(session, domain)
    now = utcnow()
    if row is None:
        row = DomainPattern(
            domain=domain,
            pattern=pattern,
            confidence=1,
            sample_count=1,
            is_catchall=is_catchall,
            last_verified_at=now,
        )
        session.add(row)
        await session.flush()
        return row

    row.sample_count = (row.sample_count or 0) + 1
    row.last_verified_at = now
    if is_catchall:
        row.is_catchall = True
    if not pattern:
        await session.flush()
        return row
    if row.pattern is None or row.pattern == pattern:
        row.pattern = pattern
        row.confidence = (row.confidence or 0) + 1
    else:
        row.confidence = max(0, (row.confidence or 0) - 1)
        if row.confidence < trust_threshold:
            row.pattern = pattern
            row.confidence = 1
    await session.flush()
    return row


def trusted_pattern(row: DomainPattern | None, trust_threshold: int) -> str | None:
    if row is None or not row.pattern:
        return None
    if row.is_catchall:
        return None
    if (row.confidence or 0) >= trust_threshold:
        return row.pattern
    return None


async def seed_from_known(
    session: AsyncSession,
    rows: list[tuple[str, NormalizedPerson]],
    settings: Settings,
) -> dict[str, int]:
    """Ingest known email + name pairs and derive domain patterns at zero verify cost."""
    derived = 0
    skipped = 0
    for email, person in rows:
        if not email or "@" not in email or person.insufficient_name:
            skipped += 1
            continue
        pattern = infer_pattern(email, person, settings.patterns)
        if not pattern:
            skipped += 1
            continue
        await record_hit(
            session,
            person.domain,
            pattern,
            trust_threshold=settings.pattern_trust_threshold,
        )
        derived += 1
    await session.commit()
    return {"derived": derived, "skipped": skipped, "total": len(rows)}


def person_from_seed_fields(
    first: str,
    last: str,
    email: str,
    settings: Settings,
    full_name: str | None = None,
) -> NormalizedPerson:
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    return normalize_person(
        first,
        last,
        domain,
        suffixes=settings.suffixes,
        credentials=settings.credentials,
        personal_domains=settings.personal_domains,
        full_name=full_name,
    )


async def load_all_patterns(session: AsyncSession) -> list[DomainPattern]:
    result = await session.execute(select(DomainPattern).order_by(DomainPattern.domain))
    return list(result.scalars())
