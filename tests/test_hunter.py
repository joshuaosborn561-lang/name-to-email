from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select

from finder.config import Settings
from finder.engine import create_run, execute_run
from finder.export import segment_for, write_csv
from finder.hunter import (
    HunterBudget,
    HunterClient,
    HunterPattern,
    EmailFinderHit,
    build_ranked_candidates,
    empty_pattern,
    localpart_matches_person,
    match_sighted,
    sighted_candidate,
)
from finder.hunter_cache import put_local, resolve_hunter_pattern
from finder.ingest import IngestedRow
from finder.models import DomainEmailPattern, Person, utcnow
from finder.normalize import normalize_person
from finder.verifiers.mock import MockVerifier


def _row(first: str, last: str, domain: str, settings: Settings) -> IngestedRow:
    person = normalize_person(
        first,
        last,
        domain,
        suffixes=settings.suffixes,
        credentials=settings.credentials,
        personal_domains=settings.personal_domains,
    )
    return IngestedRow(
        first=first,
        last=last,
        domain=person.domain,
        normalized=person,
        passthrough={},
    )


def _pattern(
    domain: str,
    *,
    pattern: str | None = "{first}.{last}",
    accept_all: bool = False,
    sighted: list | None = None,
    source: str = "hunter",
    confidence: int | None = 88,
) -> HunterPattern:
    return HunterPattern(
        domain=domain,
        pattern=pattern,
        organization="Acme",
        sighted_emails=sighted or [],
        accept_all=accept_all,
        webmail=False,
        hunter_confidence=confidence,
        fetched_at=utcnow(),
        source=source,
    )


class ScriptedHunter:
    def __init__(self, searches=None, finders=None):
        self.searches = searches or {}
        self.finders = finders or {}
        self.domain_calls: list[str] = []
        self.finder_calls: list[tuple[str, str, str]] = []

    async def domain_search(self, domain: str, limit: int = 10) -> HunterPattern | None:
        self.domain_calls.append(domain)
        value = self.searches.get(domain)
        if callable(value):
            return value()
        return value

    async def email_finder(self, domain: str, first: str, last: str) -> EmailFinderHit | None:
        self.finder_calls.append((domain, first, last))
        key = (domain, first.lower(), last.lower())
        value = self.finders.get(key, self.finders.get(domain))
        if callable(value):
            return value()
        return value


def _hunter_http(handler):
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


async def test_client_normal_pattern():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/domain-search")
        return httpx.Response(
            200,
            json={
                "data": {
                    "domain": "acme.test",
                    "pattern": "{first}.{last}",
                    "organization": "Acme",
                    "accept_all": False,
                    "webmail": False,
                    "emails": [
                        {
                            "value": "jane.doe@acme.test",
                            "confidence": 91,
                            "sources": [{}, {}],
                        }
                    ],
                }
            },
        )

    client = HunterClient("testkey", client=_hunter_http(handler))
    row = await client.domain_search("acme.test")
    assert row is not None
    assert row.pattern == "{first}.{last}"
    assert row.accept_all is False
    assert row.sighted_emails[0]["email"] == "jane.doe@acme.test"
    assert row.sighted_emails[0]["sources_count"] == 2
    assert row.hunter_confidence == 91
    assert row.source == "hunter"


async def test_client_accept_all_domain():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "domain": "wide.test",
                    "pattern": "{f}{last}",
                    "accept_all": True,
                    "webmail": False,
                    "emails": [],
                }
            },
        )

    client = HunterClient("testkey", client=_hunter_http(handler))
    row = await client.domain_search("wide.test")
    assert row is not None
    assert row.accept_all is True
    assert row.pattern == "{f}{last}"


async def test_client_empty_result():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    client = HunterClient("testkey", client=_hunter_http(handler))
    row = await client.domain_search("empty.test")
    assert row is not None
    assert row.pattern is None
    assert row.source == "hunter_empty"


async def test_client_429_does_not_cache_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"errors": [{"id": "rate_limit"}]})

    client = HunterClient("testkey", client=_hunter_http(handler))
    row = await client.domain_search("busy.test")
    assert row is None


async def test_engine_normal_pattern_from_hunter(db, settings: Settings):
    factory = db
    hunter = ScriptedHunter(
        searches={"pattern.test": _pattern("pattern.test", pattern="{first}.{last}")}
    )
    verifier = MockVerifier(valid={"jane.doe@pattern.test"})
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", "pattern.test", settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    run = await execute_run(factory, run_id, settings, verifier, hunter_client=hunter)
    assert hunter.domain_calls == ["pattern.test"]
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
        cached = await session.get(DomainEmailPattern, "pattern.test")
    assert person.status == "valid"
    assert person.email == "jane.doe@pattern.test"
    assert person.pattern_source == "hunter"
    assert person.sighted is False
    assert person.hunter_confidence == 88
    assert cached is not None
    assert cached.pattern == "{first}.{last}"
    assert run.stats["hunter_calls"] == 1


async def test_engine_accept_all_skips_smtp(db, settings: Settings):
    factory = db
    hunter = ScriptedHunter(
        searches={
            "wide.test": _pattern("wide.test", pattern="{first}.{last}", accept_all=True, confidence=70)
        }
    )
    verifier = MockVerifier(valid=set())
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", "wide.test", settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(factory, run_id, settings, verifier, hunter_client=hunter)
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
    assert person.status == "not_found"
    assert person.email is None
    assert person.domain_is_catchall is True
    assert segment_for(person) == "unresolved"


async def test_engine_empty_result_stores_and_uses_inference(db, settings: Settings):
    factory = db
    hunter = ScriptedHunter(searches={"empty.test": empty_pattern("empty.test")})
    verifier = MockVerifier(valid={"jane.doe@empty.test"})
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", "empty.test", settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(factory, run_id, settings, verifier, hunter_client=hunter)
    assert hunter.domain_calls == ["empty.test"]
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
        cached = await session.get(DomainEmailPattern, "empty.test")
    assert person.status == "valid"
    assert person.pattern_source == "inference"
    assert cached is not None
    assert cached.pattern is None
    assert cached.source == "hunter_empty"


async def test_engine_429_falls_back_without_store(db, settings: Settings):
    factory = db
    hunter = ScriptedHunter(searches={"busy.test": None})
    verifier = MockVerifier(valid={"jane.doe@busy.test"})
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", "busy.test", settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(factory, run_id, settings, verifier, hunter_client=hunter)
    assert hunter.domain_calls == ["busy.test"]
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
        cached = await session.get(DomainEmailPattern, "busy.test")
    assert person.status == "valid"
    assert person.pattern_source == "inference"
    assert cached is None


async def test_cache_hit_makes_zero_http_calls(db, settings: Settings):
    factory = db
    hunter = ScriptedHunter(
        searches={"cache.test": _pattern("cache.test", pattern="{f}{last}")}
    )
    async with factory() as session:
        await put_local(
            session,
            _pattern(
                "cache.test",
                pattern="{first}.{last}",
                confidence=77,
                source="hunter",
            ),
        )
        await session.commit()
        run = await create_run(
            session,
            [_row("Jane", "Doe", "cache.test", settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    verifier = MockVerifier(valid={"jane.doe@cache.test"})
    run = await execute_run(factory, run_id, settings, verifier, hunter_client=hunter)
    assert hunter.domain_calls == []
    assert hunter.finder_calls == []
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
    assert person.status == "valid"
    assert person.pattern_source == "cache"
    assert person.hunter_confidence == 77
    assert run.stats["hunter_calls"] == 0


async def test_cached_null_pattern_does_not_refetch(db, settings: Settings):
    factory = db
    hunter = ScriptedHunter(
        searches={"dead.test": _pattern("dead.test", pattern="{first}.{last}")}
    )
    async with factory() as session:
        await put_local(session, empty_pattern("dead.test"))
        await session.commit()
        run = await create_run(
            session,
            [_row("Jane", "Doe", "dead.test", settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    verifier = MockVerifier(valid={"jane.doe@dead.test"})
    await execute_run(factory, run_id, settings, verifier, hunter_client=hunter)
    assert hunter.domain_calls == []
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
        cached = await session.get(DomainEmailPattern, "dead.test")
    assert person.status == "valid"
    assert person.pattern_source == "inference"
    assert cached.pattern is None
    assert cached.source == "hunter_empty"


async def test_no_hunter_path_untouched(db, settings: Settings):
    factory = db
    settings.hunter_api_key = ""
    verifier = MockVerifier(valid={"jane.doe@plain.test"})
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", "plain.test", settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    run = await execute_run(factory, run_id, settings, verifier)
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
        cached = await session.get(DomainEmailPattern, "plain.test")
    assert person.status == "valid"
    assert person.email == "jane.doe@plain.test"
    assert person.pattern_source == "inference"
    assert person.sighted is False
    assert person.hunter_confidence is None
    assert cached is None
    assert run.stats["hunter_calls"] == 0
    probe = f"{settings.catchall_probe_local}@plain.test"
    assert verifier.calls[0] == probe
    assert "jane.doe@plain.test" in verifier.calls


async def test_sighted_email_is_top_candidate(db, settings: Settings):
    factory = db
    hunter = ScriptedHunter(
        searches={
            "seen.test": _pattern(
                "seen.test",
                pattern="{f}{last}",
                sighted=[{"email": "jane.doe@seen.test", "confidence": 95, "sources_count": 3}],
                confidence=80,
            )
        }
    )
    verifier = MockVerifier(valid={"jane.doe@seen.test"})
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", "seen.test", settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(factory, run_id, settings, verifier, hunter_client=hunter)
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
    assert person.status == "valid"
    assert person.email == "jane.doe@seen.test"
    assert person.sighted is True
    assert person.pattern_source == "hunter"
    assert person.hunter_confidence == 95
    assert verifier.calls[1] == "jane.doe@seen.test"


async def test_email_finder_is_last_resort(db, settings: Settings):
    factory = db
    hunter = ScriptedHunter(
        searches={"last.test": empty_pattern("last.test")},
        finders={
            ("last.test", "jane", "doe"): EmailFinderHit(
                email="jane.special@last.test", score=64, accept_all=False
            )
        },
    )
    verifier = MockVerifier(valid={"jane.special@last.test"})
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", "last.test", settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(factory, run_id, settings, verifier, hunter_client=hunter)
    assert hunter.domain_calls == ["last.test"]
    assert hunter.finder_calls
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
    assert person.status == "valid"
    assert person.pattern_source == "hunter"
    assert person.hunter_confidence == 64


async def test_email_finder_skipped_when_pattern_exists(db, settings: Settings):
    factory = db
    hunter = ScriptedHunter(
        searches={"has.test": _pattern("has.test", pattern="{first}.{last}")},
        finders={
            ("has.test", "jane", "doe"): EmailFinderHit(
                email="other@has.test", score=10, accept_all=False
            )
        },
    )
    verifier = MockVerifier(valid=set())
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", "has.test", settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(factory, run_id, settings, verifier, hunter_client=hunter)
    assert hunter.finder_calls == []
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
    assert person.status == "not_found"


async def test_hunter_call_cap(db, settings: Settings):
    factory = db
    settings.max_hunter_calls = 1
    hunter = ScriptedHunter(
        searches={
            "one.test": _pattern("one.test"),
            "two.test": _pattern("two.test"),
        }
    )
    verifier = MockVerifier(
        valid={"jane.doe@one.test", "jane.doe@two.test"}
    )
    async with factory() as session:
        run = await create_run(
            session,
            [
                _row("Jane", "Doe", "one.test", settings),
                _row("Jane", "Doe", "two.test", settings),
            ],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    run = await execute_run(factory, run_id, settings, verifier, hunter_client=hunter)
    assert len(hunter.domain_calls) == 1
    assert run.stats["hunter_calls"] == 1
    async with factory() as session:
        people = list((await session.execute(select(Person))).scalars())
    assert {p.status for p in people} == {"valid"}
    sources = {p.norm_domain: p.pattern_source for p in people}
    assert "inference" in sources.values()
    assert "hunter" in sources.values()


async def test_export_includes_hunter_columns(db, settings: Settings):
    factory = db
    hunter = ScriptedHunter(searches={"out.test": _pattern("out.test", accept_all=True)})
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", "out.test", settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(factory, run_id, settings, MockVerifier(), hunter_client=hunter)
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
    csv_text = write_csv([person])
    assert "pattern_source" in csv_text
    assert "sighted" in csv_text
    assert "hunter_confidence" in csv_text
    assert "domain_is_catchall" in csv_text
    assert person.status == "not_found"


async def test_stale_cache_refetches(db, settings: Settings):
    factory = db
    hunter = ScriptedHunter(searches={"old.test": _pattern("old.test", pattern="{f}{last}")})
    stale = _pattern("old.test", pattern="{first}.{last}")
    stale.fetched_at = datetime.now(timezone.utc) - timedelta(days=181)
    async with factory() as session:
        await put_local(session, stale)
        await session.commit()
        ctx = await resolve_hunter_pattern(
            session, "old.test", settings, hunter, HunterBudget(max_calls=200)
        )
        await session.commit()
    assert hunter.domain_calls == ["old.test"]
    assert ctx.origin == "hunter"
    assert ctx.pattern == "{f}{last}"


async def test_hunter_budget_starts_at_zero_and_is_per_instance():
    first = HunterBudget(max_calls=300)
    second = HunterBudget(max_calls=300)
    assert first.used == 0
    assert second.used == 0
    assert await first.consume() is True
    assert first.used == 1
    assert second.used == 0
    first.used = 300
    assert await first.consume() is False
    assert await second.consume() is True
    assert second.used == 1


async def test_get_run_reports_live_hunter_calls(db, settings: Settings):
    from finder.api import _status_payload
    from finder.models import Run

    factory = db
    async with factory() as session:
        run = Run(status="running", source="test", stats={}, hunter_calls=17)
        session.add(run)
        await session.commit()
        await session.refresh(run)
        payload = _status_payload(run)
    assert payload["hunter_calls"] == 17
    assert payload["stats"]["hunter_calls"] == 17


async def test_client_logs_each_hunter_call(caplog):
    import logging

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"domain": "log.test", "pattern": "{first}.{last}", "accept_all": False, "emails": []}},
        )

    caplog.set_level(logging.INFO, logger="finder.hunter")
    client = HunterClient("testkey", client=_hunter_http(handler))
    await client.domain_search("log.test")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "Hunter call endpoint=domain-search domain=log.test" in text
    assert "result=pattern" in text


def test_sighted_match_requires_first_and_last():
    settings = Settings.load()
    person = normalize_person(
        "Jane",
        "Doe",
        "acme.test",
        suffixes=settings.suffixes,
        credentials=settings.credentials,
        personal_domains=settings.personal_domains,
    )
    assert localpart_matches_person("jane.doe@acme.test", person)
    assert match_sighted(person, [{"email": "jdoe@acme.test"}]) is None
    assert match_sighted(person, [{"email": "jane.doe@acme.test"}])["email"] == "jane.doe@acme.test"


def test_local_pattern_ranks_ahead_of_hunter():
    settings = Settings.load()
    person = normalize_person(
        "Jane",
        "Doe",
        "acme.test",
        suffixes=settings.suffixes,
        credentials=settings.credentials,
        personal_domains=settings.personal_domains,
    )
    sighted = sighted_candidate(
        person, {"email": "jdoe@acme.test"}, settings.patterns
    )
    ranked = build_ranked_candidates(
        person,
        settings.patterns,
        hunter_pattern="{f}{last}",
        sighted=sighted,
        known_pattern=None,
        preferred_pattern="{first}.{last}",
        max_candidates=10,
    )
    assert ranked[0].candidate.email == "jane.doe@acme.test"
    assert ranked[0].from_hunter_pattern is False
    assert ranked[0].sighted is False


def _found(first: str, last: str, email: str, confidence: int = 90) -> dict:
    return {
        "email": email,
        "first_name": first,
        "last_name": last,
        "confidence": confidence,
        "sources_count": 2,
    }


async def test_three_found_people_set_the_convention(db, settings: Settings):
    factory = db
    domain = "found.test"
    hunter = ScriptedHunter(
        searches={
            domain: _pattern(
                domain,
                pattern=None,
                sighted=[
                    _found("Alice", "Anderson", f"alice.anderson@{domain}"),
                    _found("Brian", "Baker", f"brian.baker@{domain}"),
                    _found("Cara", "Cole", f"cara.cole@{domain}"),
                ],
            )
        }
    )
    verifier = MockVerifier(valid={f"jane.doe@{domain}"})
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", domain, settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(factory, run_id, settings, verifier, hunter_client=hunter)
    assert hunter.domain_calls == [domain]
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
    assert person.status == "valid"
    assert person.email == f"jane.doe@{domain}"
    assert person.pattern_used == "{first}.{last}"
    probe = f"{settings.catchall_probe_local}@{domain}"
    assert verifier.calls[0] == probe
    assert verifier.calls[1] == f"jane.doe@{domain}"


async def test_mixed_conventions_are_tried_in_order(db, settings: Settings):
    factory = db
    domain = "mixed.test"
    hunter = ScriptedHunter(
        searches={
            domain: _pattern(
                domain,
                pattern=None,
                sighted=[
                    _found("Alice", "Anderson", f"alice.anderson@{domain}"),
                    _found("Brian", "Baker", f"brian.baker@{domain}"),
                    _found("Cara", "Cole", f"cara.cole@{domain}"),
                    _found("Dana", "Lee", f"dlee@{domain}"),
                    _found("Evan", "Ng", f"eng@{domain}"),
                    _found("Fay", "Ortiz", f"fortiz@{domain}"),
                ],
            )
        }
    )
    verifier = MockVerifier(valid={f"jdoe@{domain}"})
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", domain, settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(factory, run_id, settings, verifier, hunter_client=hunter)
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
    assert person.status == "valid"
    assert person.email == f"jdoe@{domain}"
    assert person.pattern_used == "{f}{last}"
    assert verifier.calls[1] == f"jane.doe@{domain}"
    assert verifier.calls[2] == f"jdoe@{domain}"


async def test_catchall_sighted_person_is_valid(db, settings: Settings):
    factory = db
    domain = "realwide.test"
    hunter = ScriptedHunter(
        searches={
            domain: _pattern(
                domain,
                accept_all=True,
                sighted=[_found("Jane", "Doe", f"jane.doe@{domain}", 95)],
            )
        }
    )
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", domain, settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(factory, run_id, settings, MockVerifier(), hunter_client=hunter)
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
    assert person.status == "valid"
    assert person.email == f"jane.doe@{domain}"
    assert person.sighted is True
    assert person.domain_is_catchall is True
    assert person.confidence == "high"
    assert segment_for(person) == "valid"


async def test_catchall_finder_confirms_mailbox(db, settings: Settings):
    factory = db
    domain = "findwide.test"
    hunter = ScriptedHunter(
        searches={domain: _pattern(domain, accept_all=True, sighted=[])},
        finders={
            (domain, "jane", "doe"): EmailFinderHit(
                email=f"jane.special@{domain}", score=82, accept_all=True
            )
        },
    )
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", domain, settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(factory, run_id, settings, MockVerifier(), hunter_client=hunter)
    assert hunter.finder_calls
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
    assert person.status == "valid"
    assert person.email == f"jane.special@{domain}"
    assert person.domain_is_catchall is True
    assert person.pattern_source == "hunter"
    assert person.hunter_confidence == 82
    assert segment_for(person) == "valid"
