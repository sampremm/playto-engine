# Playto Payout Engine

A production-grade, distributed payout infrastructure with idempotency, concurrency safety, async processing, and a real-time webhook delivery engine — all backed by a premium React dashboard.

> **Live Demo:** Frontend → [playto-engine-vert.vercel.app](https://playto-engine-vert.vercel.app) · API → [playtopay.duckdns.org](https://playtopay.duckdns.org)

---

## 🏗️ Architecture & Stack

### Backend (Django API)
| Layer | Technology |
|---|---|
| Framework | Django 4.2 + Django REST Framework |
| Auth | JWT via `djangorestframework-simplejwt` |
| Database | PostgreSQL — multi-shard (`default`, `shard_0`, `shard_1`) |
| Idempotency Store | PostgreSQL `idempotency_db` (L2 durable) + Redis `db=1` (L1 fast cache) |
| Concurrency | `SELECT FOR UPDATE` row lock inside `transaction.atomic()` |
| Rate Limiting | DRF `UserRateThrottle` — 300 req/min authenticated |

### Background Worker (Celery)
| Layer | Technology |
|---|---|
| Task Queue | Celery 5 + Redis `db=0` (broker) |
| Scheduler | Celery Beat — sweeps every 30s for orphaned/stuck payouts |
| Retry Logic | Exponential backoff: `30s → 120s → 600s`, max 3 attempts |
| Shard Awareness | Worker checks all shards (`default`, `shard_0`, `shard_1`) for payouts |

### Webhook Engine
| Feature | Detail |
|---|---|
| Lifecycle | `QUEUED → PROCESSING → RETRYING → SENT/FAILED` |
| Signing | `X-Playto-Signature: sha256=<hmac>` on every delivery |
| Idempotency Guard | Exits immediately if delivery is already `SENT` |
| Broker Resilience | Task enqueue failure is non-fatal — Beat recovers the payout |

### Frontend (React + Vite)
| Feature | Detail |
|---|---|
| Framework | React 19 + Vite |
| Styling | Tailwind CSS v4 (Vite plugin) |
| UI | Glassmorphic SaaS dashboard |
| Smart Polling | 60s on standard tabs, 10s when Webhooks tab is active |
| Icons | Lucide React |

---

## 💡 Core Engineering Solutions

### 1. Two-Tier Idempotency (L1 Redis + L2 PostgreSQL)

```
Request arrives with Idempotency-Key header
    │
    ▼
[L1] Redis SET NX  ──── hit (SENT) ──→ Replay cached JSON (0ms)
    │ miss
    ▼
[L2] PostgreSQL SELECT ──── hit ──────→ Replay stored response (~1ms)
    │ miss
    ▼
Process new payout → commit to both stores simultaneously
```

- **Redis `db=1`** is isolated from Celery's `db=0` — a broker flush never evicts idempotency locks.
- **PostgreSQL `idempotency_db`** is a separate alias from business data — independent migrations, routing, and crash recovery.
- Key format: `idem:<merchant_id>:<uuid>` with 24-hour TTL.

### 2. Immutable Ledger

Balance is never stored — it is computed at query time as `SUM(amount_paise)`:

| Entry Type | When | Sign |
|---|---|---|
| `CREDIT` | Funds added to account | `+` |
| `DEBIT_HOLD` | Payout requested, funds reserved | `−` |
| `DEBIT_FINAL` | Payout completed (no-op — hold already reduced balance) | `0` |
| `DEBIT_RELEASE` | Payout failed — refund written atomically | `+` |

### 3. No Double-Spend (SELECT FOR UPDATE)

Two simultaneous 60₹ requests against a 100₹ balance:

```
Request A: acquires row lock → reads 100₹ → deducts 60₹ → commits
Request B: waits for lock → reads 40₹ → returns 402 Insufficient Funds ✅
```

Proven by `test_simultaneous_payouts` — passes on every run.

### 4. Payout State Machine

```
PENDING ──→ PROCESSING ──→ COMPLETED  (70%, DEBIT_FINAL entry)
                       ──→ FAILED     (20%, DEBIT_RELEASE refund)
                       ──→ [hangs]    (10%, Beat requeues within 30s)
```

Illegal transitions (e.g. `FAILED → COMPLETED`) are blocked by `transition_to()` — the mandatory path for all status changes in production code. Django's `.update()` bypasses `save()`, so `transition_to()` is the only truly unbypassable guard.

### 5. Broker-Resilient Task Dispatch

`process_payout.delay()` is called inside `transaction.on_commit()`. If the Celery broker is temporarily unreachable:
- The **task enqueue silently fails** (non-fatal `try/except`).
- The payout is still written to the DB as `PENDING`.
- **Celery Beat** picks it up within 30 seconds via `retry_stuck_payouts`.
- Result: **zero orphaned payouts**, even during worker restarts.

---

## 🚀 How to Run

### Option A: Docker (Recommended)

```bash
# Clone and start everything
git clone https://github.com/sampremm/playto-engine.git
cd playto-engine
docker compose up -d --build
```

The backend entrypoint script automatically handles migrations, seeding, and database readiness checks. No manual setup needed.

### Option B: Local Development

#### Prerequisites
```bash
brew services start postgresql@15
brew services start redis
```

#### Environment — `.env` (project root)
```env
SECRET_KEY=your-secret-key
DATABASE_URL=postgres://<user>@localhost:5432/postgres
SHARD_0_URL=postgres://<user>@localhost:5432/postgres
SHARD_1_URL=postgres://<user>@localhost:5432/postgres
IDEMPOTENCY_DB_URL=postgres://<user>@localhost:5432/postgres
REDIS_URL=redis://localhost:6379/0
IDEMPOTENCY_REDIS_URL=redis://localhost:6379/1
ALLOWED_HOSTS=localhost,127.0.0.1
CORS_ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
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
cd /path/to/Payto-pay
source .venv/bin/activate
export PYTHONPATH=./backend
export DJANGO_SETTINGS_MODULE=config.settings
celery -A worker.worker_app worker -l info -B
```

#### Terminal 3 — React Frontend
```bash
cd frontend
npm install
npm run dev
# → http://localhost:5173
```

---

## 🔐 Test Accounts (seeded)

| Email | Password |
|---|---|
| `arjun@demo.com` | `demo123` |
| `priya@demo.com` | `demo123` |
| `rohan@demo.com` | `demo123` |

---

## 📡 API Reference

All endpoints (except login) require: `Authorization: Bearer <token>`

| Method | Endpoint | Notes |
|---|---|---|
| `POST` | `/api/v1/auth/login/` | Returns `access` + `refresh` tokens |
| `GET` | `/api/v1/merchants/balance/` | `available_rupees`, `held_rupees` |
| `GET` | `/api/v1/merchants/ledger/` | Full append-only ledger |
| `POST` | `/api/v1/payouts/` | Requires `Idempotency-Key: <uuid>` header |
| `GET` | `/api/v1/payouts/list/` | All payouts for authenticated merchant |
| `POST` | `/api/v1/webhooks/endpoints/` | Register webhook URL |
| `GET` | `/api/v1/webhooks/endpoints/` | List registered endpoints |
| `DELETE` | `/api/v1/webhooks/endpoints/` | Delete endpoint (body: `{"id": "..."}`) |
| `GET` | `/api/v1/webhooks/deliveries/` | Full delivery history + lifecycle states |

---

## 🧪 Running Tests

```bash
cd backend
source ../.venv/bin/activate
python manage.py test payouts.tests --verbosity=2
```

### Test Results (3/3 passing ✅)

| Test | What It Proves |
|---|---|
| `test_same_key_returns_same_response` | Redis L1 idempotency — duplicate request returns same payout ID, zero double-entries |
| `test_different_key_creates_new_payout` | Different keys create independent payouts |
| `test_simultaneous_payouts` | `SELECT FOR UPDATE` prevents double-spend — exactly one 201, one 402 |

```
System check identified no issues (0 silenced).
test_simultaneous_payouts (payouts.tests.test_concurrency.ConcurrencyTest.test_simultaneous_payouts)
Two 60-rupee payout requests against a 100-rupee balance. ... ok
test_different_key_creates_new_payout (payouts.tests.test_idempotency.IdempotencyTest.test_different_key_creates_new_payout)
Two requests with different Idempotency-Keys must create ... ok
test_same_key_returns_same_response (payouts.tests.test_idempotency.IdempotencyTest.test_same_key_returns_same_response)
Second call with the same Idempotency-Key must return the exact ... ok

----------------------------------------------------------------------
Ran 3 tests in 0.993s

OK ✅
```

---

## 🐳 Docker Compose Services

```bash
docker compose up -d --build
```

| Service | Image | Purpose | Exposed Port |
|---|---|---|---|
| `shard_0` | `postgres:15-alpine` | Business data shard (even merchant IDs) | Internal only |
| `shard_1` | `postgres:15-alpine` | Business data shard (odd merchant IDs) | Internal only |
| `idempotency_db` | `postgres:15-alpine` | ACID idempotency key store | Internal only |
| `redis` | `redis:7-alpine` | Celery broker (db=0) + idem cache (db=1) | Internal only |
| `backend` | Custom (Python 3.11) | Django API server | `:8000` |
| `worker` | Custom (Python 3.11) | Celery worker + Beat scheduler | None |

The backend entrypoint (`docker-entrypoint.sh`) automatically waits for PostgreSQL readiness, runs all migrations across shards, and seeds demo data.

---

## 🌐 Production Deployment

### Architecture

```
┌─────────────────────────┐        ┌──────────────────────────────────────┐
│  Vercel (Frontend)      │        │  AWS EC2 (Backend)                   │
│  playto-engine-vert     │  HTTPS │  playtopay.duckdns.org               │
│  .vercel.app            │───────→│  Nginx (TLS termination)             │
│                         │        │    ↓ proxy_pass :8000                │
│  vercel.json rewrites   │        │  Docker Compose                     │
│  SPA routing fallback   │        │    backend + worker + DBs + Redis   │
└─────────────────────────┘        └──────────────────────────────────────┘
```

### Frontend (Vercel)

- **Environment Variable:** `VITE_API_BASE_URL=https://playtopay.duckdns.org`
- **SPA Routing:** `frontend/vercel.json` rewrites all paths to `index.html`
- Redeploy after changing env vars (Vite bakes them at build time)

### Backend (EC2 + Docker)

- **Domain:** `playtopay.duckdns.org` (DuckDNS dynamic DNS)
- **TLS:** Nginx handles SSL termination, proxies to Django on `:8000`
- **Django Config:**
  - `SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')` in `settings.py`
  - `ALLOWED_HOSTS` and `CORS_ALLOWED_ORIGINS` in `.env` include both the DuckDNS domain and Vercel origin
- **Deploy:** `git pull && docker compose up -d --build`
