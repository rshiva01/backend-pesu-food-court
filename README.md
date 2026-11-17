# PESU Food Court — API

FastAPI + PostgreSQL + Redis. Architecture and security plan: `../PLAN.md`.
The endpoint contract is defined by the frontend mock (`frontend/services/api.ts`, `frontend/mocks/handlers.ts`); everything is served under `/api/v1`.

## Status
- M1 skeleton: config, JSON logging with secret redaction, CSRF + security headers, data model + migrations, `GET /api/v1/health`, Docker image, compose, CI.
- M2 auth: student phone OTP (`auth/otp/request|verify`), `auth/me|refresh|logout`, staff login with password + TOTP (`auth/staff/login`), `vendor/me`, `admin/me`, staff logouts. httpOnly SameSite=Strict cookies, rotating refresh tokens with reuse detection, rate limits, lockout.
- Next: M3 catalog (vendors, menus).

## Local dev
```bash
cp .env.example .env                    # dev values; never commit .env
uv sync                                  # Python 3.12 + deps
docker compose -f deploy/compose.yaml -f deploy/compose.dev.yaml --env-file .env up -d postgres redis
uv run alembic upgrade head
uv run python -m app.cli seed-dev        # dev vendors + staff (admin/admin1234, dosa/dosa1234, …)
uv run uvicorn app.main:app --reload     # http://localhost:8000/api/v1/docs (dev only)
```
- Student OTPs print in the API log as `[dev sms] OTP for +91…: 123456` (console SMS provider, dev only).
- Staff TOTP code for seeded accounts: `uv run python -m app.cli totp dosa`.
- First real admin: `uv run python -m app.cli create-admin <username>` (prompts for password, prints the authenticator setup link once).
Frontend with the real API: run it with `NEXT_PUBLIC_USE_MOCKS=0`; Next rewrites `/api/*` to `localhost:8000`.

## Checks (same as CI)
```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy app
uv run bandit -q -r app && uv run pip-audit
# Tests WIPE their database: point them at a test DB and Redis db 1
DATABASE_URL=postgresql+asyncpg://pfc:change-me-db@localhost:5432/pfc_test \
REDIS_URL=redis://:change-me-redis@localhost:6379/1 uv run pytest -q
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
