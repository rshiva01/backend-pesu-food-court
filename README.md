# PESU Food Court — API

FastAPI + PostgreSQL + Redis. Architecture and security plan: `../PLAN.md`.
The endpoint contract is defined by the frontend mock (`frontend/services/api.ts`, `frontend/mocks/handlers.ts`); everything is served under `/api/v1`.

## Status
Milestone 1 (skeleton): config, JSON logging with secret redaction, CSRF + security-header middleware, full data model + first migration, `GET /api/v1/health`, Docker image, compose stack, CI. Business endpoints start in milestone 2 (auth).

## Local dev
```bash
cp .env.example .env                    # dev values; never commit .env
uv sync                                  # Python 3.12 + deps
docker compose -f deploy/compose.yaml -f deploy/compose.dev.yaml --env-file .env up -d postgres redis
uv run alembic upgrade head
uv run uvicorn app.main:app --reload     # http://localhost:8000/api/v1/docs (dev only)
```
Frontend with the real API: run it with `NEXT_PUBLIC_USE_MOCKS=0`; Next rewrites `/api/*` to `localhost:8000`.

## Checks (same as CI)
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy app
uv run bandit -q -r app && uv run pip-audit
uv run pytest -q          # needs Postgres + Redis from DATABASE_URL / REDIS_URL
```

## Production
`docker compose -f deploy/compose.yaml --env-file .env up -d` (+ `--profile migrate run --rm migrate` for migrations).
Only Caddy exposes 80/443. The API refuses to start with `ENV=production` unless `SECRET_KEY` is a random 32+ char value; `/docs` and OpenAPI are off in production.

## Layout
```
app/core      config, logging, redis, middleware
app/db        engine/session, models (users, staff_accounts, sessions, vendors, menu_items,
              orders, order_items, payments, audit_log)
app/api/v1    routers (health now; auth, orders, vendor, admin, payments, events next)
alembic       migrations
deploy        compose.yaml, compose.dev.yaml, Caddyfile
```
