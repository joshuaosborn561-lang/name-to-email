"""Lookup and store Hunter domain patterns.

Local SQLAlchemy is always used. Supabase is an extra permanent store when
SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are set. Cache reads and writes
never raise into the run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from finder.config import Settings
from finder.hunter import (
    HunterAPI,
    HunterBudget,
    HunterDomainContext,
    HunterPattern,
    empty_pattern,
)
from finder.models import DomainEmailPattern, utcnow

logger = logging.getLogger(__name__)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def is_fresh(fetched_at: datetime | None, ttl_days: int) -> bool:
    stamped = _aware(fetched_at)
    if stamped is None:
        return False
    return utcnow() - stamped < timedelta(days=ttl_days)


def row_to_pattern(row: DomainEmailPattern) -> HunterPattern:
    return HunterPattern(
        domain=row.domain,
        pattern=row.pattern,
        organization=row.organization,
        sighted_emails=list(row.sighted_emails or []),
        accept_all=bool(row.accept_all),
        webmail=bool(row.webmail),
        hunter_confidence=row.hunter_confidence,
        fetched_at=_aware(row.fetched_at) or utcnow(),
        source=row.source or "hunter",
        from_cache=True,
    )


def dict_to_pattern(domain: str, data: dict[str, Any]) -> HunterPattern:
    fetched = data.get("fetched_at")
    if isinstance(fetched, str):
        try:
            fetched_at = datetime.fromisoformat(fetched.replace("Z", "+00:00"))
        except ValueError:
            fetched_at = utcnow()
    elif isinstance(fetched, datetime):
        fetched_at = fetched
    else:
        fetched_at = utcnow()
    return HunterPattern(
        domain=str(data.get("domain") or domain).lower(),
        pattern=data.get("pattern"),
        organization=data.get("organization"),
        sighted_emails=list(data.get("sighted_emails") or []),
        accept_all=bool(data.get("accept_all")),
        webmail=bool(data.get("webmail")),
        hunter_confidence=data.get("hunter_confidence"),
        fetched_at=_aware(fetched_at) or utcnow(),
        source=str(data.get("source") or "hunter"),
        from_cache=True,
    )


def pattern_to_payload(row: HunterPattern) -> dict[str, Any]:
    fetched = _aware(row.fetched_at) or utcnow()
    return {
        "domain": row.domain.lower(),
        "pattern": row.pattern,
        "organization": row.organization,
        "sighted_emails": row.sighted_emails,
        "accept_all": row.accept_all,
        "webmail": row.webmail,
        "hunter_confidence": row.hunter_confidence,
        "fetched_at": fetched.isoformat(),
        "source": row.source,
    }


async def get_local(session: AsyncSession, domain: str, ttl_days: int) -> HunterPattern | None:
    row = await session.get(DomainEmailPattern, domain.lower())
    if row is None or not is_fresh(row.fetched_at, ttl_days):
        return None
    return row_to_pattern(row)


async def put_local(session: AsyncSession, pattern: HunterPattern) -> None:
    domain = pattern.domain.lower()
    existing = await session.get(DomainEmailPattern, domain)
    fetched = _aware(pattern.fetched_at) or utcnow()
    if existing is None:
        session.add(
            DomainEmailPattern(
                domain=domain,
                pattern=pattern.pattern,
                organization=pattern.organization,
                sighted_emails=pattern.sighted_emails,
                accept_all=pattern.accept_all,
                webmail=pattern.webmail,
                hunter_confidence=pattern.hunter_confidence,
                fetched_at=fetched,
                source=pattern.source,
            )
        )
        return
    existing.pattern = pattern.pattern
    existing.organization = pattern.organization
    existing.sighted_emails = pattern.sighted_emails
    existing.accept_all = pattern.accept_all
    existing.webmail = pattern.webmail
    existing.hunter_confidence = pattern.hunter_confidence
    existing.fetched_at = fetched
    existing.source = pattern.source


def _supabase_headers(settings: Settings) -> dict[str, str]:
    key = settings.supabase_service_role_key
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
    }


def supabase_configured(settings: Settings) -> bool:
    return bool(settings.supabase_url and settings.supabase_service_role_key)


async def get_supabase(
    settings: Settings,
    domain: str,
    ttl_days: int,
    *,
    client: httpx.AsyncClient | None = None,
) -> HunterPattern | None:
    if not supabase_configured(settings):
        return None
    url = f"{settings.supabase_url.rstrip('/')}/rest/v1/domain_email_patterns"
    owns = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await http.get(
            url,
            params={"domain": f"eq.{domain.lower()}", "select": "*"},
            headers=_supabase_headers(settings),
        )
    except httpx.HTTPError as exc:
        logger.warning("Supabase pattern cache read failed for %s: %s", domain, exc)
        return None
    finally:
        if owns:
            await http.aclose()
    if response.status_code >= 400:
        logger.warning(
            "Supabase pattern cache read HTTP %s for %s",
            response.status_code,
            domain,
        )
        return None
    try:
        rows = response.json()
    except ValueError:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    parsed = dict_to_pattern(domain, rows[0])
    if not is_fresh(parsed.fetched_at, ttl_days):
        return None
    return parsed


async def put_supabase(
    settings: Settings,
    pattern: HunterPattern,
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    if not supabase_configured(settings):
        return
    url = f"{settings.supabase_url.rstrip('/')}/rest/v1/domain_email_patterns"
    owns = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await http.post(
            url,
            json=pattern_to_payload(pattern),
            headers=_supabase_headers(settings),
        )
        if response.status_code >= 400:
            logger.warning(
                "Supabase pattern cache write HTTP %s for %s",
                response.status_code,
                pattern.domain,
            )
    except httpx.HTTPError as exc:
        logger.warning("Supabase pattern cache write failed for %s: %s", pattern.domain, exc)
    finally:
        if owns:
            await http.aclose()


async def resolve_hunter_pattern(
    session: AsyncSession,
    domain: str,
    settings: Settings,
    client: HunterAPI | None,
    budget: HunterBudget | None,
    *,
    supabase_http: httpx.AsyncClient | None = None,
) -> HunterDomainContext:
    if not domain:
        return HunterDomainContext(row=None, origin="none")

    ttl = settings.hunter_cache_ttl_days
    local = await get_local(session, domain, ttl)
    if local is not None:
        return HunterDomainContext(row=local, origin="cache")

    remote = await get_supabase(settings, domain, ttl, client=supabase_http)
    if remote is not None:
        try:
            await put_local(session, remote)
        except Exception:
            logger.exception("Failed to copy Supabase pattern row into local cache for %s", domain)
        return HunterDomainContext(row=remote, origin="cache")

    if client is None:
        return HunterDomainContext(row=None, origin="none")
    if budget is None or not await budget.consume():
        return HunterDomainContext(row=None, origin="none")

    try:
        fetched = await client.domain_search(domain, limit=10)
    except Exception:
        logger.info(
            "Hunter call endpoint=domain-search domain=%s result=error used=%s cap=%s",
            domain,
            budget.used,
            budget.max_calls,
        )
        logger.exception("Hunter domain search failed for %s, continuing with inference", domain)
        return HunterDomainContext(row=None, origin="none")

    if fetched is None:
        logger.info(
            "Hunter call endpoint=domain-search domain=%s result=no_data used=%s cap=%s",
            domain,
            budget.used,
            budget.max_calls,
        )
        return HunterDomainContext(row=None, origin="none")

    fetched.from_cache = False
    try:
        await put_local(session, fetched)
    except Exception:
        logger.exception("Failed to store Hunter pattern locally for %s", domain)
    try:
        await put_supabase(settings, fetched, client=supabase_http)
    except Exception:
        logger.exception("Failed to store Hunter pattern in Supabase for %s", domain)
    result = "pattern" if fetched.pattern else ("accept_all" if fetched.accept_all else "empty")
    logger.info(
        "Hunter call endpoint=domain-search domain=%s result=%s pattern=%s accept_all=%s used=%s cap=%s",
        domain,
        result,
        fetched.pattern,
        fetched.accept_all,
        budget.used,
        budget.max_calls,
    )
    return HunterDomainContext(row=fetched, origin="hunter")


async def persist_empty_local(session: AsyncSession, domain: str) -> HunterPattern:
    row = empty_pattern(domain)
    await put_local(session, row)
    return row
