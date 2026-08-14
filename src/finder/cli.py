"""Command-line interface."""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Optional
from uuid import UUID

import typer
from sqlalchemy import select

from finder.config import Settings
from finder.db import create_engine, init_db, session_factory
from finder.engine import compute_stats, create_run, execute_run
from finder.eval import run_eval
from finder.export import SEGMENTS, export_segments, write_csv
from finder.ingest import ingest_csv_path
from finder.models import Person, Run
from finder.normalize import normalize_person
from finder.patterns import person_from_seed_fields, seed_from_known
from finder.verifiers.base import Verifier
from finder.verifiers.mock import MockVerifier
from finder.verifiers.waterfall import build_verifier

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Bulk name-to-email finder")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _settings() -> Settings:
    return Settings.load()


async def _ready():
    settings = _settings()
    engine = create_engine(settings.async_database_url)
    await init_db(engine)
    factory = session_factory(engine)
    return settings, engine, factory


def _verifier(settings: Settings, mock: bool) -> Verifier:
    if mock:
        return MockVerifier()
    return build_verifier(settings)


@app.command()
def seed(
    csv: Path = typer.Option(..., "--csv", exists=True, readable=True, help="Known email + name CSV"),
) -> None:
    """Ingest known-good emails and derive domain patterns at zero verify cost."""

    async def _run() -> None:
        settings, engine, factory = await _ready()
        ingested = ingest_csv_path(
            csv,
            aliases=settings.column_aliases,
            suffixes=settings.suffixes,
            credentials=settings.credentials,
            personal_domains=settings.personal_domains,
        )
        pairs = []
        for row in ingested.rows:
            if not row.source_email:
                continue
            person = person_from_seed_fields(row.first, row.last, row.source_email, settings)
            pairs.append((row.source_email, person))
        async with factory() as session:
            stats = await seed_from_known(session, pairs, settings)
        typer.echo(json.dumps(stats, indent=2))
        await engine.dispose()

    asyncio.run(_run())


@app.command("run")
def run_cmd(
    csv: Path = typer.Option(..., "--csv", exists=True, readable=True),
    out: Path = typer.Option(Path("./results"), "--out"),
    max_cost: Optional[float] = typer.Option(None, "--max-cost"),
    mock: bool = typer.Option(False, "--mock", help="Use MockVerifier (no paid API calls)"),
) -> None:
    """Process a lead CSV, grouped by domain, and write segmented exports."""

    async def _run() -> None:
        settings, engine, factory = await _ready()
        ingested = ingest_csv_path(
            csv,
            aliases=settings.column_aliases,
            suffixes=settings.suffixes,
            credentials=settings.credentials,
            personal_domains=settings.personal_domains,
        )
        ceiling = Decimal(str(max_cost if max_cost is not None else settings.default_cost_ceiling))
        async with factory() as session:
            run = await create_run(
                session, ingested.rows, source=str(csv), cost_ceiling=ceiling
            )
            run_id = run.id
        verifier = _verifier(settings, mock)
        run = await execute_run(factory, run_id, settings, verifier)
        async with factory() as session:
            people = list(
                (await session.execute(select(Person).where(Person.run_id == run_id))).scalars()
            )
        dest = out / str(run_id)
        export_segments(people, dest)
        typer.echo(f"run_id={run_id}")
        typer.echo(f"status={run.status}")
        typer.echo(json.dumps(run.stats, indent=2, default=str))
        typer.echo(f"wrote {dest}/{{valid,catchall,unresolved}}.csv")
        await engine.dispose()

    asyncio.run(_run())


@app.command()
def status(run_id: str) -> None:
    """Show run status, counts, and spend."""

    async def _run() -> None:
        settings, engine, factory = await _ready()
        async with factory() as session:
            run = await session.get(Run, UUID(run_id))
            if run is None:
                typer.echo("run not found", err=True)
                raise typer.Exit(1)
            stats = run.stats or await compute_stats(session, run.id, run.cost_usd, hunter_calls=0)
        typer.echo(
            json.dumps(
                {
                    "id": str(run.id),
                    "status": run.status,
                    "cost_usd": str(run.cost_usd),
                    "cost_ceiling": str(run.cost_ceiling) if run.cost_ceiling is not None else None,
                    "stats": stats,
                    "hunter_calls": int((stats or {}).get("hunter_calls") or 0),
                    "error": run.error,
                },
                indent=2,
                default=str,
            )
        )
        await engine.dispose()

    asyncio.run(_run())


@app.command("export")
def export_cmd(
    run_id: str,
    segment: str = typer.Option(..., "--segment"),
    out: Optional[Path] = typer.Option(None, "--out"),
) -> None:
    """Write one segment (valid | catchall | unresolved) to stdout or a file."""
    if segment not in SEGMENTS:
        raise typer.BadParameter(f"segment must be one of {SEGMENTS}")

    async def _run() -> None:
        settings, engine, factory = await _ready()
        async with factory() as session:
            people = list(
                (await session.execute(select(Person).where(Person.run_id == UUID(run_id)))).scalars()
            )
        from finder.export import segment_for

        rows = [p for p in people if segment_for(p) == segment]
        text = write_csv(rows, out)
        if out is None:
            typer.echo(text, nl=False)
        else:
            typer.echo(str(out))
        await engine.dispose()

    asyncio.run(_run())


@app.command("eval")
def eval_cmd(
    csv: Path = typer.Option(..., "--csv", exists=True, readable=True),
    holdout: int = typer.Option(50, "--holdout"),
    max_cost: Optional[float] = typer.Option(None, "--max-cost"),
    mock: bool = typer.Option(False, "--mock"),
) -> None:
    """Hold out known emails, seed the rest, and measure recovery vs vendor cost."""

    async def _run() -> None:
        settings, engine, factory = await _ready()
        ingested = ingest_csv_path(
            csv,
            aliases=settings.column_aliases,
            suffixes=settings.suffixes,
            credentials=settings.credentials,
            personal_domains=settings.personal_domains,
        )
        if mock:
            known = {r.source_email.lower() for r in ingested.rows if r.source_email}
            verifier = MockVerifier(valid=known)
        else:
            verifier = build_verifier(settings)
        report = await run_eval(
            factory,
            settings,
            verifier,
            csv,
            holdout_size=holdout,
            max_cost=max_cost,
        )
        typer.echo(report.render())
        await engine.dispose()

    asyncio.run(_run())


@app.command()
def verify(
    first: str = typer.Option(...),
    last: str = typer.Option(...),
    domain: str = typer.Option(...),
    mock: bool = typer.Option(False, "--mock"),
) -> None:
    """Synchronous single-person lookup (same path as POST /verify)."""

    async def _run() -> None:
        from finder.engine import CostTracker, process_person, probe_catchall
        from finder.models import Person as PersonModel

        settings, engine, factory = await _ready()
        person_n = normalize_person(
            first,
            last,
            domain,
            suffixes=settings.suffixes,
            credentials=settings.credentials,
            personal_domains=settings.personal_domains,
        )
        verifier = _verifier(settings, mock)
        async with factory() as session:
            if person_n.insufficient_name:
                result = {"status": "insufficient_name", "email": None}
            elif person_n.personal_domain:
                result = {"status": "personal_domain", "email": None}
            else:
                run = await create_run(
                    session,
                    [],
                    source="cli-verify",
                    cost_ceiling=Decimal(str(settings.default_cost_ceiling)),
                )
                record = PersonModel(
                    run_id=run.id,
                    first=first,
                    last=last,
                    domain=person_n.domain,
                    norm_first=person_n.primary_first,
                    norm_last=person_n.primary_last,
                    norm_domain=person_n.domain,
                    status="pending",
                    passthrough={
                        "_norm_first_variants": person_n.first_variants,
                        "_norm_last_variants": person_n.last_variants,
                    },
                )
                session.add(record)
                await session.flush()
                cost = CostTracker(Decimal(str(settings.default_cost_ceiling)))
                cache: dict[str, bool] = {}
                from finder.hunter import HunterBudget, HunterClient
                from finder.hunter_cache import resolve_hunter_pattern
                from finder.models import DomainPattern

                hunter_budget = HunterBudget(max_calls=settings.max_hunter_calls)
                hunter_client = (
                    HunterClient(settings.hunter_api_key) if settings.hunter_api_key else None
                )
                hunter_ctx = await resolve_hunter_pattern(
                    session, person_n.domain, settings, hunter_client, hunter_budget
                )
                hunter_accept_all = bool(hunter_ctx.accept_all)
                if hunter_accept_all:
                    is_catchall = True
                else:
                    is_catchall = await probe_catchall(
                        session, verifier, person_n.domain, settings, cost, cache
                    )
                pattern_row = await session.get(DomainPattern, person_n.domain)
                await process_person(
                    session,
                    record,
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
                await session.commit()
                result = {
                    "email": record.email,
                    "status": record.status,
                    "pattern_used": record.pattern_used,
                    "attempts": record.attempts,
                    "verifier": record.verifier,
                    "domain_is_catchall": record.domain_is_catchall,
                    "confidence": record.confidence,
                    "pattern_source": record.pattern_source,
                    "sighted": bool(record.sighted),
                    "hunter_confidence": record.hunter_confidence,
                    "cost_usd": str(cost.snapshot()),
                }
        typer.echo(json.dumps(result, indent=2))
        await engine.dispose()

    asyncio.run(_run())
