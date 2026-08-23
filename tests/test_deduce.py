from decimal import Decimal

from sqlalchemy import select

from finder.config import Settings
from finder.engine import create_run, execute_run
from finder.ingest import IngestedRow
from finder.hunter import HunterPattern
from finder.hunter_cache import put_local
from finder.models import DomainPattern, Person, utcnow
from finder.normalize import normalize_person
from finder.patterns import record_hit
from finder.verifiers.mock import MockVerifier


def _row(
    first: str,
    last: str,
    domain: str,
    settings: Settings,
    source_email: str | None = None,
) -> IngestedRow:
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
        source_email=source_email,
    )


class ScriptedHunter:
    def __init__(self, searches=None, finders=None):
        self.searches = searches or {}
        self.finders = finders or {}
        self.domain_calls: list[str] = []
        self.finder_calls: list[tuple[str, str, str]] = []

    async def domain_search(self, domain: str, limit: int = 10):
        self.domain_calls.append(domain)
        return self.searches.get(domain)

    async def email_finder(self, domain: str, first: str, last: str):
        self.finder_calls.append((domain, first, last))
        return self.finders.get((domain, first.lower(), last.lower()))


async def test_known_colleagues_skip_hunter_and_try_deduced_first(db, settings: Settings):
    factory = db
    domain = "colleagues.test"
    first_people = [
        _row("Alice", "Anderson", domain, settings),
        _row("Brian", "Baker", domain, settings),
        _row("Dana", "Lee", domain, settings),
    ]
    valid = {
        "alice.anderson@colleagues.test",
        "brian.baker@colleagues.test",
        "dana.lee@colleagues.test",
        "cara.cole@colleagues.test",
    }
    async with factory() as session:
        run = await create_run(
            session, first_people, source="seed", cost_ceiling=Decimal("10")
        )
        run_id = run.id
    await execute_run(factory, run_id, settings, MockVerifier(valid=valid))

    async with factory() as session:
        stored = await session.get(DomainPattern, domain)
        if stored is not None:
            await session.delete(stored)
            await session.commit()

    hunter = ScriptedHunter(searches={domain: object()})
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Cara", "Cole", domain, settings)],
            source="lookup",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    run = await execute_run(
        factory, run_id, settings, MockVerifier(valid=valid), hunter_client=hunter
    )
    assert hunter.domain_calls == []
    assert hunter.finder_calls == []
    assert run.stats["hunter_calls"] == 0
    async with factory() as session:
        cara = (
            await session.execute(select(Person).where(Person.first == "Cara"))
        ).scalars().first()
    assert cara.status == "valid"
    assert cara.email == "cara.cole@colleagues.test"
    assert cara.pattern_used == "{first}.{last}"
    assert cara.pattern_source == "known"
    assert cara.attempts == 1


async def test_one_known_colleague_still_uses_hunter(db, settings: Settings):
    factory = db
    domain = "single.test"
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Alice", "Anderson", domain, settings)],
            source="seed",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(
        factory,
        run_id,
        settings,
        MockVerifier(valid={"alice.anderson@single.test"}),
    )
    async with factory() as session:
        stored = await session.get(DomainPattern, domain)
        if stored is not None:
            await session.delete(stored)
            await session.commit()

    from finder.hunter import HunterPattern
    from finder.models import utcnow

    hunter = ScriptedHunter(
        searches={
            domain: HunterPattern(
                domain=domain,
                pattern="{f}{last}",
                organization=None,
                sighted_emails=[],
                accept_all=False,
                webmail=False,
                hunter_confidence=70,
                fetched_at=utcnow(),
                source="hunter",
            )
        }
    )
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Brian", "Baker", domain, settings)],
            source="lookup",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(
        factory,
        run_id,
        settings,
        MockVerifier(valid={"bbaker@single.test"}),
        hunter_client=hunter,
    )
    assert hunter.domain_calls == [domain]
    async with factory() as session:
        brian = (
            await session.execute(select(Person).where(Person.first == "Brian"))
        ).scalars().first()
    assert brian.status == "valid"
    assert brian.pattern_source == "hunter"


async def test_source_emails_in_same_batch_deduce_without_hunter(db, settings: Settings):
    factory = db
    domain = "batch.test"
    people = [
        _row("Alice", "Anderson", domain, settings, source_email="alice.anderson@batch.test"),
        _row("Brian", "Baker", domain, settings, source_email="brian.baker@batch.test"),
        _row("Dana", "Lee", domain, settings, source_email="dana.lee@batch.test"),
        _row("Cara", "Cole", domain, settings),
    ]
    valid = {
        "alice.anderson@batch.test",
        "brian.baker@batch.test",
        "dana.lee@batch.test",
        "cara.cole@batch.test",
    }
    hunter = ScriptedHunter(searches={domain: object()})
    async with factory() as session:
        run = await create_run(session, people, source="test", cost_ceiling=Decimal("10"))
        run_id = run.id
    run = await execute_run(
        factory, run_id, settings, MockVerifier(valid=valid), hunter_client=hunter
    )
    assert hunter.domain_calls == []
    assert run.stats["hunter_calls"] == 0
    async with factory() as session:
        cara = (
            await session.execute(select(Person).where(Person.first == "Cara"))
        ).scalars().first()
    assert cara.status == "valid"
    assert cara.pattern_used == "{first}.{last}"
    assert cara.pattern_source == "known"
    assert cara.attempts == 1


async def test_wrong_deduction_falls_through_to_permutations(db, settings: Settings):
    factory = db
    domain = "mismatch.test"
    first_people = [
        _row("Alice", "Anderson", domain, settings),
        _row("Brian", "Baker", domain, settings),
        _row("Cara", "Cole", domain, settings),
    ]
    async with factory() as session:
        run = await create_run(
            session, first_people, source="seed", cost_ceiling=Decimal("10")
        )
        run_id = run.id
    await execute_run(
        factory,
        run_id,
        settings,
        MockVerifier(
            valid={
                "alice.anderson@mismatch.test",
                "brian.baker@mismatch.test",
                "cara.cole@mismatch.test",
            }
        ),
    )
    async with factory() as session:
        stored = await session.get(DomainPattern, domain)
        if stored is not None:
            await session.delete(stored)
            await session.commit()

    hunter = ScriptedHunter(searches={domain: object()})
    verifier = MockVerifier(valid={"dlee@mismatch.test"})
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Dana", "Lee", domain, settings)],
            source="lookup",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(factory, run_id, settings, verifier, hunter_client=hunter)
    assert hunter.domain_calls == []
    async with factory() as session:
        dana = (
            await session.execute(select(Person).where(Person.first == "Dana"))
        ).scalars().first()
    assert dana.status == "valid"
    assert dana.email == "dlee@mismatch.test"
    assert dana.pattern_used == "{f}{last}"
    assert dana.attempts >= 2
    assert "dana.lee@mismatch.test" in verifier.calls
    assert "dlee@mismatch.test" in verifier.calls
    assert verifier.calls.index("dana.lee@mismatch.test") < verifier.calls.index(
        "dlee@mismatch.test"
    )


async def test_our_pattern_cache_is_first_even_when_hunter_cache_exists(
    db, settings: Settings
):
    """domain_patterns is the first lookup. Hunter cache is not consulted."""
    factory = db
    domain = "ours.first.test"
    async with factory() as session:
        await record_hit(session, domain, "{first}.{last}", trust_threshold=2)
        await put_local(
            session,
            HunterPattern(
                domain=domain,
                pattern="{f}{last}",
                organization="Acme",
                sighted_emails=[],
                accept_all=False,
                webmail=False,
                hunter_confidence=99,
                fetched_at=utcnow(),
                source="hunter",
            ),
        )
        await session.commit()

    hunter = ScriptedHunter(searches={domain: object()})
    verifier = MockVerifier(valid={"jane.doe@ours.first.test"})
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", domain, settings)],
            source="lookup",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(factory, run_id, settings, verifier, hunter_client=hunter)
    assert hunter.domain_calls == []
    assert hunter.finder_calls == []
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
    assert person.status == "valid"
    assert person.email == "jane.doe@ours.first.test"
    assert person.pattern_used == "{first}.{last}"
    assert person.pattern_source == "inference"
    assert person.attempts == 1
    probe = f"{settings.catchall_probe_local}@{domain}"
    assert verifier.calls[0] == probe
    assert verifier.calls[1] == "jane.doe@ours.first.test"
    assert "jdoe@ours.first.test" not in verifier.calls


async def test_preferred_cache_row_skips_hunter(db, settings: Settings):
    factory = db
    domain = "preferred.test"
    async with factory() as session:
        await record_hit(session, domain, "{f}{last}", trust_threshold=2)
        await session.commit()

    hunter = ScriptedHunter(searches={domain: object()})
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", domain, settings)],
            source="lookup",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(
        factory,
        run_id,
        settings,
        MockVerifier(valid={"jdoe@preferred.test"}),
        hunter_client=hunter,
    )
    assert hunter.domain_calls == []
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
    assert person.status == "valid"
    assert person.email == "jdoe@preferred.test"
    assert person.pattern_used == "{f}{last}"
    assert person.attempts == 1


async def test_trusted_local_pattern_skips_hunter(db, settings: Settings):
    factory = db
    domain = "trusted.test"
    async with factory() as session:
        await record_hit(session, domain, "{f}{last}", trust_threshold=2)
        await record_hit(session, domain, "{f}{last}", trust_threshold=2)
        await session.commit()

    hunter = ScriptedHunter(searches={domain: object()})
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", domain, settings)],
            source="lookup",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(
        factory,
        run_id,
        settings,
        MockVerifier(valid={"jdoe@trusted.test"}),
        hunter_client=hunter,
    )
    assert hunter.domain_calls == []
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
    assert person.status == "valid"
    assert person.email == "jdoe@trusted.test"
    assert person.pattern_used == "{f}{last}"
    assert person.attempts == 1
