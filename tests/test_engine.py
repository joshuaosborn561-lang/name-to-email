from decimal import Decimal

from sqlalchemy import select

from finder.config import Settings
from finder.engine import CostTracker, create_run, execute_run
from finder.ingest import IngestedRow
from finder.models import DomainPattern, Person, Verification
from finder.normalize import normalize_person
from finder.patterns import record_hit, seed_from_known, trusted_pattern
from finder.verifiers.mock import MockVerifier
from finder.verifiers.waterfall import WaterfallVerifier


def _row(first: str, last: str, domain: str, settings: Settings, **passthrough) -> IngestedRow:
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
        passthrough=passthrough,
    )


async def test_catchall_skips_permutations(db, settings: Settings):
    factory = db
    verifier = MockVerifier(
        valid={"jane.doe@acceptall.test"},
        catchall_domains={"acceptall.test"},
    )
    async with factory() as session:
        run = await create_run(
            session,
            [_row("Jane", "Doe", "acceptall.test", settings)],
            source="test",
            cost_ceiling=Decimal("10"),
        )
        run_id = run.id
    await execute_run(factory, run_id, settings, verifier)
    async with factory() as session:
        person = (await session.execute(select(Person))).scalars().first()
        assert person.status == "catchall"
        assert person.email == "jane.doe@acceptall.test"
        assert person.attempts == 0
        assert person.confidence == "low"
    # Probe of fake address only; no permutation verifies.
    probe = f"{settings.catchall_probe_local}@acceptall.test"
    assert verifier.calls == [probe]


async def test_pattern_learning_reduces_later_attempts(db, settings: Settings):
    factory = db
    domain = "firstdotlast.test"
    people = [
        _row("Alice", "Anderson", domain, settings),
        _row("Brian", "Baker", domain, settings),
        _row("Cara", "Cole", domain, settings),
    ]
    valid = {
        "alice.anderson@firstdotlast.test",
        "brian.baker@firstdotlast.test",
        "cara.cole@firstdotlast.test",
    }
    verifier = MockVerifier(valid=valid)
    async with factory() as session:
        run = await create_run(session, people, source="test", cost_ceiling=Decimal("50"))
        run_id = run.id
    await execute_run(factory, run_id, settings, verifier)
    async with factory() as session:
        rows = list((await session.execute(select(Person).order_by(Person.first))).scalars())
        pattern = await session.get(DomainPattern, domain)
    assert [r.status for r in rows] == ["valid", "valid", "valid"]
    assert pattern.pattern == "{first}.{last}"
    assert pattern.sample_count == 3
    # First person: catchall probe (invalid, billed) + first pattern hit.
    # After 2 confirmations the third person verifies only the known pattern.
    cara = next(r for r in rows if r.first == "Cara")
    assert cara.attempts == 1
    assert cara.pattern_used == "{first}.{last}"
    assert cara.confidence == "high"


async def test_personal_and_insufficient_never_verify(db, settings: Settings):
    factory = db
    verifier = MockVerifier(valid=set())
    rows = [
        _row("Ann", "Lee", "gmail.com", settings),
        _row("Oliver", "C.", "dealer.test", settings),
    ]
    async with factory() as session:
        run = await create_run(session, rows, source="test", cost_ceiling=Decimal("10"))
        run_id = run.id
    await execute_run(factory, run_id, settings, verifier)
    async with factory() as session:
        people = list((await session.execute(select(Person))).scalars())
    statuses = {p.norm_domain: p.status for p in people}
    assert statuses["gmail.com"] == "personal_domain"
    assert any(p.status == "insufficient_name" for p in people)
    assert verifier.calls == []


async def test_cost_ceiling_stops_cleanly(db, settings: Settings):
    factory = db
    domain = "pricey.test"
    people = [_row(f"First{i}", f"Last{i}", domain, settings) for i in range(8)]
    valid = {f"first{i}.last{i}@pricey.test" for i in range(8)}
    verifier = MockVerifier(valid=valid, cost_usd=0.4)
    async with factory() as session:
        run = await create_run(session, people, source="test", cost_ceiling=Decimal("1.00"))
        run_id = run.id
    run = await execute_run(factory, run_id, settings, verifier)
    assert run.status == "stopped"
    async with factory() as session:
        pending = list(
            (await session.execute(select(Person).where(Person.status == "pending"))).scalars()
        )
        resolved = list(
            (await session.execute(select(Person).where(Person.status != "pending"))).scalars()
        )
    assert pending
    assert resolved
    assert any(p.status == "valid" for p in resolved)


async def test_verification_cache_is_idempotent(db, settings: Settings):
    factory = db
    domain = "cache.test"
    valid = {"amy.adams@cache.test"}
    verifier = MockVerifier(valid=valid)
    row = _row("Amy", "Adams", domain, settings)
    async with factory() as session:
        run = await create_run(session, [row], source="test", cost_ceiling=Decimal("10"))
        run_id = run.id
    await execute_run(factory, run_id, settings, verifier)
    first_calls = len(verifier.calls)
    async with factory() as session:
        run2 = await create_run(session, [row], source="test2", cost_ceiling=Decimal("10"))
        run2_id = run2.id
    verifier2 = MockVerifier(valid=valid)
    await execute_run(factory, run2_id, settings, verifier2)
    # Second run must not call the provider; cache on verifications table.
    assert verifier2.calls == []
    async with factory() as session:
        cached = list((await session.execute(select(Verification))).scalars())
    assert cached
    assert first_calls >= 1


async def test_contradiction_decrements_confidence(db, settings: Settings):
    factory = db
    async with factory() as session:
        row = await record_hit(
            session, "flip.test", "{first}.{last}", trust_threshold=2
        )
        row = await record_hit(
            session, "flip.test", "{first}.{last}", trust_threshold=2
        )
        assert row.confidence >= 2
        assert trusted_pattern(row, 2) == "{first}.{last}"
        row = await record_hit(session, "flip.test", "{f}{last}", trust_threshold=2)
        assert row.pattern == "{f}{last}"
        assert row.confidence == 1
        assert trusted_pattern(row, 2) is None
        await session.commit()


async def test_waterfall_uses_secondary_on_unknown():
    primary = MockVerifier(unknown={"a@x.test"}, name="millionverifier", billed_statuses=["valid", "invalid"])
    secondary = MockVerifier(valid={"a@x.test"}, name="no2bounce")
    waterfall = WaterfallVerifier(primary, secondary)
    verdict = await waterfall.verify("a@x.test")
    assert verdict.status == "valid"
    assert verdict.verifier == "no2bounce"


async def test_seed_derives_patterns(db, settings: Settings):
    factory = db
    person = normalize_person(
        "Dana",
        "Lee",
        "seeded.test",
        suffixes=settings.suffixes,
        credentials=settings.credentials,
        personal_domains=settings.personal_domains,
    )
    async with factory() as session:
        stats = await seed_from_known(
            session, [("dlee@seeded.test", person), ("dana.lee@seeded.test", person)], settings
        )
    assert stats["derived"] == 2
    async with factory() as session:
        row = await session.get(DomainPattern, "seeded.test")
    # Last write wins via contradiction/re-derive; both were valid derivations.
    assert row.pattern in {"{f}{last}", "{first}.{last}"}
    assert row.sample_count == 2
