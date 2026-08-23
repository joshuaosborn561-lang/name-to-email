"""Per-domain pattern memory: seed from known-good emails and learn at runtime."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finder.config import Settings
from finder.models import DomainPattern, Person, utcnow
from finder.normalize import NormalizedPerson, normalize_person
from finder.permute import infer_pattern

_SOURCE_EMAIL_KEYS = ("_source_email", "email", "Email", "work_email", "Email Address")
_KNOWN_LOOKUP_LIMIT = 40


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


def source_email_from_passthrough(passthrough: dict | None) -> str | None:
    if not passthrough:
        return None
    for key in _SOURCE_EMAIL_KEYS:
        value = passthrough.get(key)
        if isinstance(value, str) and "@" in value.strip():
            return value.strip().lower()
    return None


def person_from_stored(row: Person, settings: Settings) -> NormalizedPerson:
    extras_first = (row.passthrough or {}).get("_norm_first_variants")
    extras_last = (row.passthrough or {}).get("_norm_last_variants")
    if (
        isinstance(extras_first, list)
        and extras_first
        and isinstance(extras_last, list)
        and extras_last
    ):
        return NormalizedPerson(
            original_first=row.first,
            original_last=row.last,
            original_domain=row.domain,
            first_variants=[str(v) for v in extras_first if v],
            last_variants=[str(v) for v in extras_last if v],
            domain=row.norm_domain,
        )
    return normalize_person(
        row.first,
        row.last,
        row.norm_domain or row.domain,
        suffixes=settings.suffixes,
        credentials=settings.credentials,
        personal_domains=settings.personal_domains,
    )


def extra_pairs_from_people(
    people: Iterable[Person],
    settings: Settings,
) -> list[tuple[NormalizedPerson, str]]:
    pairs: list[tuple[NormalizedPerson, str]] = []
    for row in people:
        email = source_email_from_passthrough(row.passthrough)
        if not email:
            continue
        pairs.append((person_from_stored(row, settings), email))
    return pairs


def convention_votes(
    pairs: Iterable[tuple[NormalizedPerson, str]],
    patterns: list[str],
) -> Counter[str]:
    """Count distinct people per email format."""
    votes: Counter[str] = Counter()
    seen_emails: set[str] = set()
    seen_names: set[tuple[str, str]] = set()
    for person, email in pairs:
        if not email or "@" not in email or person.insufficient_name:
            continue
        cleaned = email.strip().lower()
        name_key = (person.primary_first, person.primary_last)
        if cleaned in seen_emails:
            continue
        if name_key != ("", "") and name_key in seen_names:
            continue
        template = infer_pattern(cleaned, person, patterns)
        if not template:
            continue
        seen_emails.add(cleaned)
        if name_key != ("", ""):
            seen_names.add(name_key)
        votes[template] += 1
    return votes


def conventions_to_try(votes: Counter[str], majority: int = 3) -> list[str]:
    """Pick formats to test from people found at the domain.

    Three people on one format is enough to treat that as the convention.
    If several formats show up, try those too.
    """
    if not votes:
        return []
    ranked = votes.most_common()
    strong = [pattern for pattern, count in ranked if count >= majority]
    if len(strong) == 1:
        extras = [pattern for pattern, count in ranked if pattern != strong[0] and count >= 2]
        return strong + extras
    if strong:
        extras = [pattern for pattern, count in ranked if pattern not in strong and count >= 2]
        return strong + extras
    return [pattern for pattern, _count in ranked]


def agreeing_pattern(
    pairs: Iterable[tuple[NormalizedPerson, str]],
    patterns: list[str],
    *,
    min_agree: int = 3,
) -> str | None:
    """Return the template shared by at least min_agree distinct known people."""
    votes = convention_votes(pairs, patterns)
    if not votes:
        return None
    winner, count = votes.most_common(1)[0]
    if count < min_agree:
        return None
    if sum(1 for value in votes.values() if value == count) > 1:
        return None
    return winner


def pairs_from_sighted(
    sighted: Iterable[dict],
    domain: str,
    settings: Settings,
) -> list[tuple[NormalizedPerson, str]]:
    """Turn anyone found at the domain into name + email pairs."""
    pairs: list[tuple[NormalizedPerson, str]] = []
    for item in sighted:
        email = str(item.get("email") or "").strip().lower()
        first = str(item.get("first_name") or item.get("first") or "").strip()
        last = str(item.get("last_name") or item.get("last") or "").strip()
        if not email or "@" not in email or not first or not last:
            continue
        person = normalize_person(
            first,
            last,
            domain,
            suffixes=settings.suffixes,
            credentials=settings.credentials,
            personal_domains=settings.personal_domains,
        )
        if person.insufficient_name:
            continue
        pairs.append((person, email))
    return pairs


async def load_known_pairs_at_domain(
    session: AsyncSession,
    domain: str,
    settings: Settings,
    *,
    extra_pairs: Iterable[tuple[NormalizedPerson, str]] | None = None,
    limit: int = _KNOWN_LOOKUP_LIMIT,
) -> list[tuple[NormalizedPerson, str]]:
    """Valid people already stored for this domain, plus any extra known pairs."""
    domain = (domain or "").lower()
    pairs: list[tuple[NormalizedPerson, str]] = []
    if domain:
        result = await session.execute(
            select(Person)
            .where(
                Person.norm_domain == domain,
                Person.status == "valid",
                Person.email.is_not(None),
            )
            .order_by(Person.id)
            .limit(limit)
        )
        for row in result.scalars():
            if not row.email:
                continue
            pairs.append((person_from_stored(row, settings), row.email))
    if extra_pairs:
        pairs.extend(extra_pairs)
    return pairs


async def deduce_pattern_from_known(
    session: AsyncSession,
    domain: str,
    settings: Settings,
    *,
    extra_pairs: Iterable[tuple[NormalizedPerson, str]] | None = None,
    min_agree: int | None = None,
) -> str | None:
    """Look up known people at this domain and return their shared format."""
    pairs = await load_known_pairs_at_domain(
        session, domain, settings, extra_pairs=extra_pairs
    )
    threshold = settings.convention_majority if min_agree is None else min_agree
    return agreeing_pattern(pairs, settings.patterns, min_agree=threshold)


async def remember_deduced_pattern(
    session: AsyncSession,
    domain: str,
    pattern: str,
    *,
    trust_threshold: int,
) -> DomainPattern:
    """Store a deduced format as a preferred hint, not a trusted SMTP hit.

    Confidence stays below the trust threshold so a miss still falls through
    to the permutation ladder. A later SMTP confirmation increments as usual.
    """
    row = await get_pattern(session, domain)
    now = utcnow()
    if row is None:
        row = DomainPattern(
            domain=domain,
            pattern=pattern,
            confidence=0,
            sample_count=0,
            last_verified_at=now,
        )
        session.add(row)
        await session.flush()
        return row
    if trusted_pattern(row, trust_threshold):
        await session.flush()
        return row
    if not row.pattern or row.pattern != pattern:
        row.pattern = pattern
    row.last_verified_at = now
    await session.flush()
    return row


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
