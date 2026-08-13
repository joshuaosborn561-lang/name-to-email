"""HTTP API: start runs, inspect spend, export segments, single verify."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.middleware.cors import CORSMiddleware

from finder.config import Settings
from finder.db import create_engine, init_db, session_factory
from finder.engine import CostTracker, compute_stats, create_run, execute_run, process_person, probe_catchall
from finder.export import SEGMENTS, segment_for, write_csv
from finder.ingest import IngestedRow, ingest_csv_text, ingest_records
from finder.models import DomainPattern, Person, Run
from finder.normalize import normalize_person
from finder.verifiers.waterfall import build_verifier
from finder.mcp_http import router as mcp_router
from finder.oauth_open import router as oauth_router

logger = logging.getLogger(__name__)

settings: Settings
engine: AsyncEngine
factory: async_sessionmaker[AsyncSession]
_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global settings, engine, factory
    settings = Settings.load()
    engine = create_engine(settings.async_database_url)
    await init_db(engine)
    factory = session_factory(engine)
    logger.info("database ready")
    yield
    for task in list(_tasks):
        task.cancel()
    await engine.dispose()


app = FastAPI(title="name-to-email", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["mcp-session-id", "mcp-protocol-version", "WWW-Authenticate"],
)
app.include_router(oauth_router)
app.include_router(mcp_router)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "name-to-email",
        "health": "/health",
        "docs": "/docs",
        "mcp": "/mcp",
        "verify": "/verify",
    }


class VerifyRequest(BaseModel):
    first: str
    last: str
    domain: str


class RunRequest(BaseModel):
    people: list[dict[str, Any]] | None = None
    table: str | None = None
    max_cost: float | None = None
    column_map: dict[str, str] | None = None


class RunStatus(BaseModel):
    id: str
    status: str
    cost_usd: float
    cost_ceiling: float | None
    stats: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


def _status_payload(run: Run) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "status": run.status,
        "cost_usd": float(run.cost_usd or 0),
        "cost_ceiling": float(run.cost_ceiling) if run.cost_ceiling is not None else None,
        "stats": run.stats or {},
        "error": run.error,
    }


def _spawn(run_id: uuid.UUID) -> None:
    async def _job() -> None:
        verifier = build_verifier(settings)
        try:
            await execute_run(factory, run_id, settings, verifier)
        except Exception:
            logger.exception("run %s failed", run_id)

    task = asyncio.create_task(_job())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _ingest_kwargs(column_map: dict[str, str] | None = None) -> dict[str, Any]:
    return dict(
        aliases=settings.column_aliases,
        suffixes=settings.suffixes,
        credentials=settings.credentials,
        personal_domains=settings.personal_domains,
        column_map=column_map,
    )


@app.post("/runs")
async def start_run(body: RunRequest) -> dict[str, Any]:
    kwargs = _ingest_kwargs(body.column_map)
    if body.people:
        ingested = ingest_records(body.people, **kwargs)
        source = "json"
    elif body.table:
        ingested_rows = await _ingest_table(body.table, body.column_map)
        from finder.ingest import IngestResult

        ingested = IngestResult(rows=ingested_rows, raw_count=len(ingested_rows))
        source = f"table:{body.table}"
    else:
        raise HTTPException(status_code=400, detail="provide people[] or table name")

    ceiling = Decimal(
        str(body.max_cost if body.max_cost is not None else settings.default_cost_ceiling)
    )
    async with factory() as session:
        run = await create_run(session, ingested.rows, source=source, cost_ceiling=ceiling)
        payload = _status_payload(run)
    _spawn(run.id)
    payload["run_id"] = payload["id"]
    return payload


@app.post("/runs/upload")
async def start_run_upload(
    file: UploadFile = File(...),
    max_cost: float | None = Query(default=None),
) -> dict[str, Any]:
    text = (await file.read()).decode("utf-8-sig")
    ingested = ingest_csv_text(text, **_ingest_kwargs())
    ceiling = Decimal(str(max_cost if max_cost is not None else settings.default_cost_ceiling))
    async with factory() as session:
        run = await create_run(
            session, ingested.rows, source=file.filename or "upload.csv", cost_ceiling=ceiling
        )
        payload = _status_payload(run)
    _spawn(run.id)
    payload["run_id"] = payload["id"]
    return payload


@app.get("/runs/{run_id}")
async def get_run(run_id: uuid.UUID) -> dict[str, Any]:
    async with factory() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if not run.stats:
            run.stats = await compute_stats(session, run.id, run.cost_usd)
        return _status_payload(run)


@app.get("/runs/{run_id}/export")
async def export_run(
    run_id: uuid.UUID,
    segment: str = Query(..., pattern="^(valid|catchall|unresolved)$"),
) -> PlainTextResponse:
    if segment not in SEGMENTS:
        raise HTTPException(status_code=400, detail="invalid segment")
    async with factory() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        people = list(
            (await session.execute(select(Person).where(Person.run_id == run_id))).scalars()
        )
    rows = [p for p in people if segment_for(p) == segment]
    return PlainTextResponse(write_csv(rows), media_type="text/csv")


@app.post("/verify")
async def verify_one(body: VerifyRequest) -> dict[str, Any]:
    person_n = normalize_person(
        body.first,
        body.last,
        body.domain,
        suffixes=settings.suffixes,
        credentials=settings.credentials,
        personal_domains=settings.personal_domains,
    )
    if person_n.insufficient_name:
        return {"email": None, "status": "insufficient_name", "attempts": 0, "confidence": "low"}
    if person_n.personal_domain:
        return {"email": None, "status": "personal_domain", "attempts": 0, "confidence": "low"}

    verifier = build_verifier(settings)
    async with factory() as session:
        run = await create_run(
            session,
            [],
            source="verify",
            cost_ceiling=Decimal(str(settings.default_cost_ceiling)),
        )
        record = Person(
            run_id=run.id,
            first=body.first,
            last=body.last,
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
        is_catchall = await probe_catchall(
            session, verifier, person_n.domain, settings, cost, cache
        )
        pattern_row = await session.get(DomainPattern, person_n.domain)
        await process_person(session, record, verifier, settings, cost, is_catchall, pattern_row)
        await session.commit()
        return {
            "email": record.email,
            "status": record.status,
            "pattern_used": record.pattern_used,
            "attempts": record.attempts,
            "verifier": record.verifier,
            "domain_is_catchall": record.domain_is_catchall,
            "confidence": record.confidence,
            "cost_usd": float(cost.snapshot()),
        }


async def _ingest_table(table: str, column_map: dict[str, str] | None) -> list[IngestedRow]:
    if not table.replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="invalid table name")
    from sqlalchemy import text

    async with factory() as session:
        result = await session.execute(text(f'SELECT * FROM "{table}"'))
        records = [dict(row._mapping) for row in result]
    ingested = ingest_records(
        records,
        aliases=settings.column_aliases,
        suffixes=settings.suffixes,
        credentials=settings.credentials,
        personal_domains=settings.personal_domains,
        column_map=column_map,
    )
    return ingested.rows
