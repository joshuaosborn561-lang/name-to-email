"""Hunter domain search and email finder client.

Domain search is billed per domain. Cache that row and generate addresses
locally. Email finder is billed per lookup and is last resort only.
A missing key or a Hunter error logs and returns None so a run never fails.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

from finder.models import utcnow
from finder.normalize import NormalizedPerson
from finder.permute import Candidate, apply_pattern, generate_candidates, infer_pattern

logger = logging.getLogger(__name__)

HUNTER_BASE = "https://api.hunter.io/v2"
DOMAIN_SEARCH_PATH = "/domain-search"
EMAIL_FINDER_PATH = "/email-finder"


@dataclass
class HunterPattern:
    domain: str
    pattern: str | None
    organization: str | None
    sighted_emails: list[dict[str, Any]]
    accept_all: bool
    webmail: bool
    hunter_confidence: int | None
    fetched_at: datetime
    source: str
    from_cache: bool = False


@dataclass
class EmailFinderHit:
    email: str | None
    score: int | None
    accept_all: bool


@dataclass
class HunterDomainContext:
    row: HunterPattern | None
    origin: str

    @property
    def pattern(self) -> str | None:
        return self.row.pattern if self.row else None

    @property
    def accept_all(self) -> bool:
        return bool(self.row and self.row.accept_all)

    @property
    def sighted_emails(self) -> list[dict[str, Any]]:
        return list(self.row.sighted_emails) if self.row else []

    @property
    def hunter_confidence(self) -> int | None:
        return self.row.hunter_confidence if self.row else None

    @property
    def pattern_source_label(self) -> str:
        if self.row is None:
            return "inference"
        if self.origin == "cache":
            return "cache"
        if self.origin == "hunter":
            return "hunter"
        return "inference"


class HunterAPI(Protocol):
    async def domain_search(self, domain: str, limit: int = 50) -> HunterPattern | None: ...

    async def email_finder(
        self, domain: str, first: str, last: str
    ) -> EmailFinderHit | None: ...


class HunterBudget:
    """Per run Hunter HTTP counter. A new instance starts at zero."""

    def __init__(self, max_calls: int = 200) -> None:
        self.max_calls = int(max_calls) if max_calls is not None else 200
        self.used = 0
        self._logged = False
        self._lock = asyncio.Lock()

    async def consume(self) -> bool:
        """Claim one slot immediately before a Hunter HTTP call. Does not share state across runs."""
        async with self._lock:
            if self.used >= self.max_calls:
                if not self._logged:
                    logger.warning(
                        "Hunter call cap reached (%s), falling back to inference for the rest of this run",
                        self.max_calls,
                    )
                    self._logged = True
                return False
            self.used += 1
            return True


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_pattern(raw: Any) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def parse_sighted_emails(emails: Any) -> list[dict[str, Any]]:
    if not isinstance(emails, list):
        return []
    out: list[dict[str, Any]] = []
    for item in emails:
        if not isinstance(item, dict):
            continue
        address = str(item.get("value") or item.get("email") or "").strip().lower()
        if not address or "@" not in address:
            continue
        sources = item.get("sources")
        if isinstance(sources, list):
            sources_count = len(sources)
        else:
            sources_count = int(item.get("sources_count") or 0)
        out.append(
            {
                "email": address,
                "first_name": str(item.get("first_name") or item.get("first") or "").strip(),
                "last_name": str(item.get("last_name") or item.get("last") or "").strip(),
                "confidence": _as_int(item.get("confidence")),
                "sources_count": sources_count,
            }
        )
    return out


def pattern_from_payload(domain: str, payload: dict[str, Any], *, source: str) -> HunterPattern:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    sighted = parse_sighted_emails(data.get("emails") or payload.get("sighted_emails"))
    confidences = [s["confidence"] for s in sighted if s.get("confidence") is not None]
    stored_conf = _as_int(data.get("hunter_confidence") or payload.get("hunter_confidence"))
    if stored_conf is None and confidences:
        stored_conf = max(confidences)
    return HunterPattern(
        domain=str(data.get("domain") or payload.get("domain") or domain).lower(),
        pattern=_normalize_pattern(data.get("pattern") or payload.get("pattern")),
        organization=(data.get("organization") or payload.get("organization") or None),
        sighted_emails=sighted,
        accept_all=bool(data.get("accept_all") if "accept_all" in data else payload.get("accept_all")),
        webmail=bool(data.get("webmail") if "webmail" in data else payload.get("webmail")),
        hunter_confidence=stored_conf,
        fetched_at=utcnow(),
        source=source,
    )


def empty_pattern(domain: str) -> HunterPattern:
    return HunterPattern(
        domain=domain.lower(),
        pattern=None,
        organization=None,
        sighted_emails=[],
        accept_all=False,
        webmail=False,
        hunter_confidence=None,
        fetched_at=utcnow(),
        source="hunter_empty",
    )


def _payload_is_empty(payload: dict[str, Any]) -> bool:
    data = payload.get("data")
    if data is None:
        return True
    if not isinstance(data, dict):
        return True
    if data.get("pattern"):
        return False
    emails = data.get("emails")
    if isinstance(emails, list) and emails:
        return False
    if data.get("organization") or data.get("accept_all") or data.get("webmail"):
        return False
    return True


class HunterClient:
    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = HUNTER_BASE,
    ) -> None:
        self.api_key = api_key
        self._client = client
        self.base_url = base_url.rstrip("/")

    async def domain_search(self, domain: str, limit: int = 50) -> HunterPattern | None:
        if not self.api_key or not domain:
            return None
        url = f"{self.base_url}{DOMAIN_SEARCH_PATH}"
        try:
            http = self._client or httpx.AsyncClient(timeout=15.0)
            owns = self._client is None
            try:
                response = await http.get(
                    url,
                    params={"domain": domain, "limit": limit, "api_key": self.api_key},
                )
            finally:
                if owns:
                    await http.aclose()
        except httpx.HTTPError as exc:
            logger.info(
                "Hunter call endpoint=domain-search domain=%s http=error result=transport",
                domain,
            )
            logger.warning("Hunter domain search transport error for %s: %s", domain, exc)
            return None

        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                logger.warning("Hunter domain search returned non JSON for %s", domain)
                return empty_pattern(domain)
            if not isinstance(payload, dict) or _payload_is_empty(payload):
                logger.info(
                    "Hunter call endpoint=domain-search domain=%s http=%s result=empty",
                    domain,
                    response.status_code,
                )
                return empty_pattern(domain)
            parsed = pattern_from_payload(domain, payload, source="hunter")
            if parsed.pattern is None and not parsed.sighted_emails and not parsed.accept_all:
                parsed.source = "hunter_empty"
            result = "pattern" if parsed.pattern else ("accept_all" if parsed.accept_all else "empty")
            logger.info(
                "Hunter call endpoint=domain-search domain=%s http=%s result=%s pattern=%s accept_all=%s",
                domain,
                response.status_code,
                result,
                parsed.pattern,
                parsed.accept_all,
            )
            return parsed

        if response.status_code in {400, 404}:
            logger.info(
                "Hunter call endpoint=domain-search domain=%s http=%s result=empty",
                domain,
                response.status_code,
            )
            logger.warning(
                "Hunter domain search HTTP %s for %s, storing empty row",
                response.status_code,
                domain,
            )
            return empty_pattern(domain)

        logger.info(
            "Hunter call endpoint=domain-search domain=%s http=%s result=error",
            domain,
            response.status_code,
        )
        logger.warning(
            "Hunter domain search HTTP %s for %s, continuing with inference",
            response.status_code,
            domain,
        )
        return None

    async def email_finder(
        self, domain: str, first: str, last: str
    ) -> EmailFinderHit | None:
        if not self.api_key or not domain or not first or not last:
            return None
        url = f"{self.base_url}{EMAIL_FINDER_PATH}"
        try:
            http = self._client or httpx.AsyncClient(timeout=15.0)
            owns = self._client is None
            try:
                response = await http.get(
                    url,
                    params={
                        "domain": domain,
                        "first_name": first,
                        "last_name": last,
                        "api_key": self.api_key,
                    },
                )
            finally:
                if owns:
                    await http.aclose()
        except httpx.HTTPError as exc:
            logger.info(
                "Hunter call endpoint=email-finder domain=%s http=error result=transport",
                domain,
            )
            logger.warning("Hunter email finder transport error for %s: %s", domain, exc)
            return None

        if response.status_code != 200:
            logger.info(
                "Hunter call endpoint=email-finder domain=%s http=%s result=error",
                domain,
                response.status_code,
            )
            logger.warning(
                "Hunter email finder HTTP %s for %s, continuing with inference",
                response.status_code,
                domain,
            )
            return None
        try:
            payload = response.json()
        except ValueError:
            logger.info(
                "Hunter call endpoint=email-finder domain=%s http=200 result=empty",
                domain,
            )
            logger.warning("Hunter email finder returned non JSON for %s", domain)
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            logger.info(
                "Hunter call endpoint=email-finder domain=%s http=200 result=empty",
                domain,
            )
            return None
        email = str(data.get("email") or "").strip().lower() or None
        logger.info(
            "Hunter call endpoint=email-finder domain=%s http=200 result=%s",
            domain,
            "hit" if email else "empty",
        )
        return EmailFinderHit(
            email=email,
            score=_as_int(data.get("score")),
            accept_all=bool(data.get("accept_all")),
        )


def name_tokens(person: NormalizedPerson) -> tuple[list[str], list[str]]:
    firsts = [v for v in person.first_variants if v and len(v) >= 2]
    lasts = [v for v in person.last_variants if v and len(v) >= 2]
    if person.primary_first and len(person.primary_first) >= 2:
        firsts = [person.primary_first] + [v for v in firsts if v != person.primary_first]
    if person.primary_last and len(person.primary_last) >= 2:
        lasts = [person.primary_last] + [v for v in lasts if v != person.primary_last]
    return firsts, lasts


def localpart_matches_person(email: str, person: NormalizedPerson) -> bool:
    if "@" not in email:
        return False
    local = email.split("@", 1)[0].lower()
    firsts, lasts = name_tokens(person)
    for first in firsts:
        for last in lasts:
            if first.lower() in local and last.lower() in local:
                return True
    return False


def match_sighted(person: NormalizedPerson, sighted_emails: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in sighted_emails:
        email = str(item.get("email") or "")
        if localpart_matches_person(email, person):
            return item
    return None


def sighted_candidate(
    person: NormalizedPerson,
    item: dict[str, Any],
    patterns: list[str],
) -> Candidate:
    email = str(item["email"]).lower()
    inferred = infer_pattern(email, person, patterns)
    first = person.primary_first or (person.first_variants[0] if person.first_variants else "")
    last = person.primary_last or (person.last_variants[0] if person.last_variants else "")
    return Candidate(
        email=email,
        pattern=inferred or "sighted",
        first=first,
        last=last,
    )


def hunter_pattern_candidates(
    person: NormalizedPerson,
    template: str,
    max_candidates: int = 4,
) -> list[Candidate]:
    return generate_candidates(
        person,
        [template],
        known_pattern=template,
        max_candidates=max_candidates,
    )


def apply_hunter_pattern(template: str, person: NormalizedPerson) -> str | None:
    if not template or not person.domain:
        return None
    first = person.primary_first or (person.first_variants[0] if person.first_variants else "")
    last = person.primary_last or (person.last_variants[0] if person.last_variants else "")
    if not first:
        return None
    return apply_pattern(template, first, last, person.domain)


@dataclass(frozen=True)
class RankedCandidate:
    candidate: Candidate
    sighted: bool
    from_hunter_pattern: bool


def build_ranked_candidates(
    person: NormalizedPerson,
    patterns: list[str],
    *,
    hunter_pattern: str | None,
    sighted: Candidate | None,
    known_pattern: str | None,
    preferred_pattern: str | None = None,
    convention_patterns: list[str] | None = None,
    max_candidates: int,
) -> list[RankedCandidate]:
    out: list[RankedCandidate] = []
    seen: set[str] = set()

    def add(candidate: Candidate, sighted_flag: bool, from_hunter: bool) -> None:
        key = candidate.email.lower()
        if key in seen or not candidate.email.split("@")[0]:
            return
        seen.add(key)
        out.append(
            RankedCandidate(
                candidate=candidate,
                sighted=sighted_flag,
                from_hunter_pattern=from_hunter,
            )
        )

    # Our domain_patterns cache is the first way to find a format.
    if known_pattern:
        for candidate in generate_candidates(
            person, patterns, known_pattern=known_pattern, max_candidates=max_candidates
        ):
            add(candidate, False, False)
        return out

    lead = preferred_pattern
    if lead:
        for candidate in generate_candidates(
            person, patterns, known_pattern=lead, max_candidates=4
        ):
            add(candidate, False, False)

    for template in convention_patterns or []:
        if not template or template == lead:
            continue
        for candidate in generate_candidates(
            person, patterns, known_pattern=template, max_candidates=4
        ):
            add(candidate, False, False)

    if sighted is not None:
        add(sighted, True, False)
    if hunter_pattern:
        for candidate in hunter_pattern_candidates(person, hunter_pattern):
            add(candidate, False, True)

    for candidate in generate_candidates(
        person, patterns, known_pattern=None, max_candidates=max_candidates
    ):
        add(candidate, False, False)
        if len(out) >= max_candidates + 2:
            break
    return out
