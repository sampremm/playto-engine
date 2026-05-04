# Playto Payout Engine

A production-grade, distributed payout infrastructure built for the [Playto Founding Engineer Challenge 2026](https://www.playto.so/features/playto-pay). Features sharded PostgreSQL, two-tier idempotency, atomic concurrency control, async processing with Celery, HMAC-signed webhooks, and a glassmorphic React dashboard.

> **Live Demo:** Frontend → [playto-engine-vert.vercel.app](https://playto-engine-vert.vercel.app) · API → [playtopay.duckdns.org](http://playtopay.duckdns.org:8000)

---

## Architecture Overview

```
┌──────────────────────────┐        ┌───────────────────────────────────────┐
│   Vercel (Frontend)      │        │   AWS EC2 (Backend)                   │
│   React 19 + Vite        │  HTTP  │   Docker Compose                      │
│   Glassmorphic Dashboard │───────→│   ┌─────────────────────────────────┐ │
│   JWT Auth + Polling     │        │   │  Django API (:8000)             │ │
└──────────────────────────┘        │   │  SELECT FOR UPDATE locking      │ │
                                    │   │  Two-Tier Idempotency (L1+L2)   │ │
                                    │   └───────────┬─────────────────────┘ │
                                    │               │ Redis (broker)        │
                                    │   ┌───────────▼─────────────────────┐ │
                                    │   │  Celery Worker                  │ │
                                    │   │  Shard-aware payout processing  │ │
                                    │   │  State machine enforcement      │ │
                                    │   ├─────────────────────────────────┤ │
                                    │   │  Celery Beat (every 30s)        │ │
                                    │   │  Orphan recovery + retry logic  │ │
                                    │   └───────────┬─────────────────────┘ │
                                    │               │                       │
                                    │   ┌───────────▼─────────────────────┐ │
                                    │   │  PostgreSQL Shards (Neon Cloud) │ │
                                    │   │  shard_0 · shard_1 · idem_db   │ │
                                    │   └─────────────────────────────────┘ │
                                    └───────────────────────────────────────┘
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| **API** | Django 4.2 + Django REST Framework |
| **Auth** | JWT via `djangorestframework-simplejwt` |
| **Database** | PostgreSQL (Neon Cloud) — sharded: `shard_0`, `shard_1`, `idempotency_db` |
| **Task Queue** | Celery 5 + Redis (broker on db=0) |
| **Scheduler** | Celery Beat — orphan sweep every 30 seconds |
| **Idempotency** | Redis db=1 (L1 cache) + PostgreSQL (L2 durable store) |
| **Concurrency** | `SELECT FOR UPDATE` row-level locking inside `transaction.atomic()` |
| **Webhooks** | HMAC-SHA256 signed delivery with exponential backoff retries |
| **Frontend** | React 19 + Vite + Tailwind CSS v4 |
| **CI/CD** | GitHub Actions → Docker Hub → EC2 auto-deploy |
| **Infrastructure** | AWS EC2, Neon PostgreSQL, Redis, Vercel, DuckDNS |

---

## Core Engineering

### 1. Money Integrity — Immutable Ledger

Balance is **never stored as a field** — it is always derived:

```python
balance = LedgerEntry.objects.using(shard).filter(merchant=m).aggregate(Sum('amount_paise'))
```

| Entry Type | Amount | When | Purpose |
|---|---|---|---|
| `CREDIT` | `+10000` | Account top-up | Funds deposited |
| `DEBIT_HOLD` | `-500` | Payout requested | Reserves funds immediately |
| `DEBIT_FINAL` | `0` | Payout completed | Audit marker (hold already reduced balance) |
| `DEBIT_RELEASE` | `+500` | Payout failed | Atomic refund of held funds |

All amounts stored as `BigIntegerField` in **paise** (1 INR = 100 paise). No floats. No decimals.

### 2. Concurrency — SELECT FOR UPDATE

Two simultaneous ₹60 requests against ₹100 balance:

```
Request A: acquires row lock → reads ₹100 → deducts ₹60 → commits
Request B: waits for lock    → reads ₹40  → returns 402 Insufficient Funds ✅
```

### 3. Idempotency — Two-Tier L1/L2

```
Request with Idempotency-Key header
    │
    ▼
[L1] Redis SET NX ──── hit → Replay cached JSON (0ms)
    │ miss
    ▼
[L2] PostgreSQL SELECT ──── hit → Re-populate Redis, replay (~1ms)
    │ miss
    ▼
Process new payout → commit to both stores
```

### 4. State Machine

```
PENDING → PROCESSING → COMPLETED (85%, bank success)
                     → FAILED    (15%, bank rejection → auto-refund)
                     → [hangs]   (10%, timeout → Beat retry with backoff → force-fail after 3)
```

Illegal transitions (e.g. `FAILED → COMPLETED`) blocked by `transition_to()` in model.

### 5. Retry & Recovery

- **Celery Beat** sweeps every 30s for orphaned PENDING payouts and stuck PROCESSING payouts
- **Exponential backoff**: 30s → 60s → 120s between retries
- **Max 3 attempts** → force-fail with atomic `DEBIT_RELEASE` refund
- **`close_old_connections()`** before every DB query (critical for Neon serverless connections)
- **`acks_late=True`** — if worker crashes mid-task, message returns to Redis

---

## Quick Start

### Option A: Docker Compose (Recommended)

```bash
git clone https://github.com/sampremm/playto-engine.git
cd playto-engine
cp .env.example .env  # Edit with your credentials
docker compose up -d --build
```

The entrypoint script automatically runs migrations across all 4 database aliases and seeds demo merchants.

### Option B: Local Development

#### Prerequisites
```bash
brew services start postgresql@15
brew services start redis
```

#### Environment (`.env` in project root)
```env
SECRET_KEY=your-secret-key
DATABASE_URL=postgres://<user>@localhost:5432/postgres
SHARD_0_URL=postgres://<user>@localhost:5432/postgres
SHARD_1_URL=postgres://<user>@localhost:5432/postgres
IDEMPOTENCY_DB_URL=postgres://<user>@localhost:5432/postgres
REDIS_URL=redis://localhost:6379/0
IDEMPOTENCY_REDIS_URL=redis://localhost:6379/1
ALLOWED_HOSTS=localhost,127.0.0.1
CORS_ALLOWED_ORIGINS=http://localhost:5173
```

#### Terminal 1 — Django API
```bash
cd backend
source ../.venv/bin/activate
python manage.py migrate --database=default
python manage.py migrate --database=shard_0
python manage.py migrate --database=shard_1
python manage.py migrate --database=idempotency_db
python manage.py seed
python manage.py runserver
# → http://localhost:8000
```

#### Terminal 2 — Celery Worker + Beat
```bash
source .venv/bin/activate
export PYTHONPATH=./backend
export DJANGO_SETTINGS_MODULE=config.settings
celery -A worker.worker_app worker -l info -B
```

#### Terminal 3 — React Frontend
```bash
cd frontend
npm install && npm run dev
# → http://localhost:5173
```

---

## Test Accounts (Seeded)

| Email | Password |
|---|---|
| `arjun@demo.com` | `demo123` |
| `priya@demo.com` | `demo123` |
| `rohan@demo.com` | `demo123` |

---

## API Reference

All endpoints (except login) require: `Authorization: Bearer <token>`

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/auth/login/` | Returns `access` + `refresh` JWT tokens |
| `GET` | `/api/v1/merchants/balance/` | Available + held balance in rupees |
| `GET` | `/api/v1/merchants/ledger/` | Full append-only ledger history |
| `POST` | `/api/v1/payouts/` | Create payout (requires `Idempotency-Key: <uuid>` header) |
| `GET` | `/api/v1/payouts/list/` | All payouts for authenticated merchant |
| `POST` | `/api/v1/webhooks/endpoints/` | Register webhook URL |
| `GET` | `/api/v1/webhooks/endpoints/` | List registered endpoints |
| `DELETE` | `/api/v1/webhooks/endpoints/` | Delete endpoint |
| `GET` | `/api/v1/webhooks/deliveries/` | Delivery history with lifecycle states |

---

## Running Tests

```bash
cd backend
python manage.py test payouts.tests --verbosity=2
```

| Test | What It Proves |
|---|---|
| `test_same_key_returns_same_response` | Duplicate Idempotency-Key returns same payout ID, zero duplicate entries |
| `test_different_key_creates_new_payout` | Different keys create independent payouts |
| `test_simultaneous_payouts` | `SELECT FOR UPDATE` prevents double-spend — one 201, one 402 |

---

## CI/CD Pipeline

```
git push origin main
    │
    ▼
┌─────────────────────────────────────────────┐
│  GitHub Actions                              │
│  1. test-backend: Run Django tests (Pg+Redis)│
│  2. docker-build: Push images to Docker Hub  │
│  3. deploy-ec2:   SSH → pull → restart       │
└─────────────────────────────────────────────┘
```

Tests run against real PostgreSQL and Redis services in CI — no SQLite mocks.

---

## Production Deployment

| Component | Platform | URL |
|---|---|---|
| Frontend | Vercel | `playto-engine-vert.vercel.app` |
| Backend | AWS EC2 + Docker | `playtopay.duckdns.org:8000` |
| Database | Neon PostgreSQL | 2 shards + idempotency DB |
| DNS | DuckDNS | Dynamic DNS → EC2 |

### Environment Variables (Vercel)
```
VITE_API_BASE_URL=http://playtopay.duckdns.org:8000
```

### Deploy
```bash
git push origin main  # CI/CD handles everything
```

---

## Project Structure

```
playto-engine/
├── backend/
│   ├── config/           # Django settings, routers, URLs
│   │   ├── settings.py   # 4 DB aliases, JWT, CORS, throttling
│   │   └── routers.py    # ShardRouter (merchant_id → shard_0/shard_1)
│   ├── merchants/        # Merchant model + LedgerEntry
│   ├── payouts/          # Payout model + IdempotencyKey + views
│   │   ├── views.py      # PayoutCreateView (two-tier idem + locking)
│   │   ├── models.py     # State machine (transition_to)
│   │   └── tests/        # Concurrency + idempotency tests
│   └── webhooks/         # HMAC-signed webhook delivery engine
├── worker/
│   ├── worker_app.py     # Celery tasks (process_payout + beat sweeper)
│   └── Dockerfile        # Non-root celery user
├── frontend/
│   └── src/              # React dashboard (Login + Dashboard)
├── docker-compose.yml    # Full local orchestration (7 services)
└── .github/workflows/    # CI/CD pipeline
```
