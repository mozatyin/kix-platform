# KiX Platform

Gamification-as-a-Service platform for mobile games. Provides identity management, engagement modules, virtual currency, voucher campaigns, brand management, and contest matchmaking through a unified API gateway.

## Architecture

```
gateway/          Unified FastAPI app — routes, middleware, lifespan
kid/              KiX Identity — shadow/full identities, sessions, social graph
kin/              KiX Interaction Network — 15 NE modules, streaks, leagues, battle pass
kash/             KiX Asset Hub — wallets, ledger, vouchers, energy, QR verification
klub/             KiX Loyalty & User Base — brands, games, contests, promotions
kc/               KiX Contest — matchmaking, cohort scoring
shared/           Database, Redis, auth, config, event bus, common schemas
migrations/       Alembic async migrations (PostgreSQL + asyncpg)
tests/            pytest-asyncio test suite
```

## Setup

### Prerequisites

```bash
brew install postgresql@16 redis
brew services start postgresql@16
brew services start redis
```

### Database

```bash
createdb kix
```

### Python environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Run migrations

```bash
cd migrations
alembic upgrade head
```

### Start the server

```bash
uvicorn gateway.main:app --reload --host 0.0.0.0 --port 8000
```

## API Documentation

Once running, interactive docs are available at:

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- Health check: http://localhost:8000/health

## Running Tests

```bash
pytest
```

## Environment Variables

Copy `.env.example` to `.env` and adjust as needed. Key variables:

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `POSTGRES_DB` | `kix` | Database name |
| `POSTGRES_USER` | `mozat` | Database user |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `JWT_SECRET` | `kix-dev-secret-...` | JWT signing key |
| `ENV` | `development` | Environment (development/production) |
