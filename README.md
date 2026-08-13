# Bulk name-to-email finder

Takes a list of people (first name, last name, company domain), generates candidate
work emails, verifies them against real mail servers, and segments results by how
much they can be trusted.

The point is to replace a per-email finder vendor (roughly $0.01–$0.05 per email)
with permutation plus verification. Whether that is actually cheaper is measured
by `finder eval`, not assumed.

**Stack:** Python 3.11+, FastAPI, Postgres, CLI, Docker, Railway.

## How it works

For each person, candidates are generated from a frequency-ordered pattern list in
`config/finder.yaml` (not hardcoded). They are verified one at a time until the
first confirmed hit. The winning pattern is stored on `domain_patterns` so the next
person at that domain is nearly free.

Rows are processed **grouped by domain**, not input order. The first hit at a
domain immediately benefits everyone else there.

### Three cost controls

1. **Catch-all detection first.** A new domain is probed with a fake address
   (`zzq-nope-9f3a2b@domain`). If it comes back valid, the domain accepts all mail
   and per-address verification proves nothing. On a catch-all domain the finder
   emits only the single highest-probability candidate, marks it `catchall`, and
   does not run the permutation loop. The verdict is cached per domain for 30 days
   and never re-probed within a run.

2. **Per-domain pattern learning.** Known-good emails (`finder seed`) and runtime
   hits populate `domain_patterns`. A pattern is trusted after 2 consistent
   confirmations; a later contradiction decrements confidence and re-derives
   rather than silently overwriting. Once trusted, only that one address is
   generated.

3. **Verification waterfall.** MillionVerifier first. No2Bounce only for
   catch-all or unknown, because MillionVerifier does not bill those outcomes.
   Providers sit behind `Verifier.verify(email) -> Verdict` and are ordered from
   config. Only MillionVerifier, No2Bounce, and `MockVerifier` ship.

**Catch-all rows are never merged into valid.** That separation is the product:
valid is safe to load into a sending tool; catch-all belongs on a warmed domain
or a separate campaign; unresolved is for phone follow-up.

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# docker compose up -d db   # or use sqlite: DATABASE_URL=sqlite+aiosqlite:///./finder.db
```

Required env (see `.env.example`):

- `DATABASE_URL`
- `MILLIONVERIFIER_API_KEY`
- `NO2BOUNCE_API_KEY`
- `API_KEY` (static header for the HTTP API)
- `MAX_CONCURRENCY` (default 5)
- `DEFAULT_COST_CEILING`

Tables are created on API/CLI startup. Alembic is available if you prefer
`alembic upgrade head`.

## CLI

```bash
finder seed --csv known_emails.csv
finder run --csv leads.csv --out ./results --max-cost 25
finder status <run_id>
finder export <run_id> --segment valid|catchall|unresolved
finder eval --csv known_emails.csv --holdout 50 --max-cost 25
finder verify --first Jane --last Doe --domain acme.com
```

`--mock` on `run` / `eval` / `verify` uses `MockVerifier` (no paid calls).

Raw scrape exports are accepted as-is: a single `name` column, `website`/`url`
instead of domain, mixed casing, stray whitespace, missing names, duplicates.
Dedupe is on `(normalized_first, normalized_last, domain)` before any
verification. Free mailbox providers (gmail, yahoo, outlook, hotmail, aol,
icloud, …) are flagged `personal_domain` and never permuted.

## HTTP

Auth: `X-API-Key` (or `Authorization: Bearer`).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | liveness |
| `POST` | `/runs` | JSON `{people, max_cost, table?}` → `{run_id}` |
| `POST` | `/runs/upload` | CSV multipart upload |
| `GET` | `/runs/{id}` | status, counts, spend |
| `GET` | `/runs/{id}/export?segment=valid` | CSV for `valid` / `catchall` / `unresolved` |
| `POST` | `/verify` | single name+domain, synchronous |

```bash
uvicorn finder.api:app --reload
```

## The test that decides if this was worth building

Seed `domain_patterns` from a verified-email corpus, hold out 50 of those
addresses, and run the finder against only their names and domains:

```bash
finder eval --csv known_emails.csv --holdout 50 --max-cost 25
```

It reports:

1. Recovery rate — share of the 50 known addresses found
2. Average attempts per hit
3. Catch-all share of the domains
4. Effective cost per valid email, compared to ~$0.01 vendor

If cost per valid does not beat $0.01, the command says so plainly. Catch-all
share is reported honestly: that is the population where this approach cannot
confirm anything, and where a paid finder still has a real edge.

Unit tests run the same holdout path against a synthetic corpus and
`MockVerifier`:

```bash
pytest -q
```

## Docker / Railway

```bash
docker compose up --build
```

`railway.toml` + `Dockerfile` deploy the API. Attach Railway Postgres and set
the env vars above. Railway injects `DATABASE_URL` and `PORT`; `postgres://`
is rewritten to `postgresql+asyncpg://` automatically.

A container restart does not re-bill: per-row state lives in `people`, and
`verifications` is a global cache keyed by email. Interrupted runs resume
pending rows. `max_cost` / `DEFAULT_COST_CEILING` stops a run cleanly and
writes partial results.

## Name cleaning

Real scrape failures this handles before generating anything:

- Generational suffixes (Jr, Sr, II, III, IV) and credentials (MBA, ASE, CPA)
- Accents → ASCII, lowercase, apostrophes and internal spaces removed
- Parenthetical nicknames preferred: `Robert (Bob) Cohen` tries `bob` first
- Hyphenated surnames: full hyphenated, concatenated, final segment
- Initial-only last names (`Oliver C.`) → `insufficient_name`, no guess
- Casing: `LIZ DELUCA` and `jose medero`

Pattern list, personal domains, suffix lists, and per-verifier costs are all in
`config/finder.yaml` so they can be retuned from observed hit rates.
